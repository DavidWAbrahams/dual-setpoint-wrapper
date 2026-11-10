# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""
Mode coordination for wrappers that share one outdoor unit.

A multi-zone outdoor unit has one compressor and one reversing valve, so a
group of wrappers can only condition one direction at a time. The coordinator
is the group's single arbitration authority: members report what their rooms
actually need (their demand), the coordinator picks which direction the group
runs in, and it parks every member whose side conflicts with that choice.

The decision itself is deliberately small — first-come-first-served over the
demand queue, tie-broken by priority, with the incumbent kept while it is
still pressing — and every side effect (parking, releasing, attributes) is
pushed back through the members so this module never touches a source entity
directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    CLAIM_DELAY,
    DEFAULT_MIN_CYCLE_TIME,
    EVENT_MODE_CONFLICT,
    LOGGER,
    Demand,
    dispose_coordinator,
)

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

# How long to wait before re-checking when the only thing blocking a flip is a
# member still running. Members wake us whenever their source reports
# anything, so this is only the backstop for quiet or slow integrations.
QUIET_RETRY = timedelta(seconds=60)

# The direction that physically conflicts with a group mode.
OPPOSITE = {Demand.HEAT: Demand.COOL, Demand.COOL: Demand.HEAT}


class GroupMember(Protocol):
    """The surface the coordinator asks of one wrapper in its group."""

    @property
    def entity_id(self) -> str:
        """The wrapper's entity id."""
        ...

    @property
    def coordination_available(self) -> bool:
        """True when this member can take part in group decisions."""
        ...

    @property
    def coordination_priority(self) -> int:
        """Tie-break weight when two claims are exactly the same age."""
        ...

    @property
    def coordination_parkable(self) -> bool:
        """True when the coordinator may park this member's source."""
        ...

    @property
    def coordination_parked(self) -> bool:
        """True while the source sits in a group-imposed hold."""
        ...

    def coordination_demand(self) -> Demand:
        """Return the direction the room is pressing for, if any."""
        ...

    def coordination_intent(self) -> Demand | None:
        """Return the direction the user's face mode wants, if any."""
        ...

    def coordination_held_mode(self) -> Demand | None:
        """Return the direction the source itself is currently set to."""
        ...

    def coordination_source_running(self) -> bool:
        """Return True when this member's source is physically running."""
        ...

    def coordination_cycle_seconds(self) -> int:
        """Return the minimum seconds before the group may flip modes."""
        ...

    async def async_park_for_group(self, thwarted_from: Demand) -> None:
        """Park the source at the hold configured for this direction."""
        ...

    async def async_release_park(self) -> None:
        """Undo a group park so the member can act on its own mode again."""
        ...

    def async_group_context_changed(
        self, group_mode: Demand | None, deferred_to: str | None
    ) -> None:
        """Publish the group's mode and the member it is deferring to."""
        ...


class ModeCoordinator:
    """Arbitrate one named group of wrappers sharing an outdoor unit."""

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        """Initialize an empty group."""
        self._hass = hass
        self._name = name
        self._members: dict[str, GroupMember] = {}
        # entity_id -> (demand, when this demand first appeared). Keeping the
        # appearance time is what makes the queue fair: a waiting member does
        # not lose its place every time somebody else re-reports.
        self._demands: dict[str, tuple[Demand, datetime]] = {}
        self._group_mode: Demand | None = None
        self._last_change: datetime | None = None
        self._retry_unsub: CALLBACK_TYPE | None = None
        self._applying = False
        self._dirty = False
        self._conflict_fired = False

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------
    def register(self, member: GroupMember) -> None:
        """Add a wrapper to the group."""
        self._members[member.entity_id] = member

    def unregister(self, member: GroupMember) -> None:
        """Remove a wrapper; dispose of the group once it stands empty."""
        self._members.pop(member.entity_id, None)
        self._demands.pop(member.entity_id, None)
        if not self._members:
            self._cancel_retry()
            dispose_coordinator(self._hass, self._name)

    # ------------------------------------------------------------------
    # Arbitration
    # ------------------------------------------------------------------
    async def async_member_updated(self, member: GroupMember) -> None:
        """Re-run the group decision because one member changed."""
        if member.entity_id not in self._members:
            return
        self._note_demand(member, dt_util.utcnow())
        await self.async_arbitrate()

    def _note_demand(self, member: GroupMember, now: datetime) -> None:
        """Record a member's demand, keeping its queue position."""
        demand = member.coordination_demand()
        previous = self._demands.get(member.entity_id)
        if (
            previous is not None
            and previous[0] is demand
            and demand in (Demand.HEAT, Demand.COOL)
        ):
            # Still pressing the same direction: keep the original timestamp.
            self._demands[member.entity_id] = (demand, previous[1])
        else:
            self._demands[member.entity_id] = (demand, now)

    async def async_arbitrate(self) -> None:
        """Decide the group mode and reconcile every member against it."""
        if self._applying:
            # A member echoed our own park/release commands back at us; one
            # follow-up pass after we finish applying is enough.
            self._dirty = True
            return

        self._cancel_retry()
        now = dt_util.utcnow()
        active = [m for m in self._members.values() if m.coordination_available]
        heat = [m for m in active if self._demand_of(m) is Demand.HEAT]
        cool = [m for m in active if self._demand_of(m) is Demand.COOL]
        incumbent = self._group_mode

        if incumbent is Demand.HEAT and heat:
            # Rule: the incumbent is never preempted while still pressing.
            target = Demand.HEAT
        elif incumbent is Demand.COOL and cool:
            target = Demand.COOL
        else:
            ordered = sorted(heat + cool, key=self._rank)
            if not ordered:
                target = None
            else:
                eligible = [m for m in ordered if self._started(m) + CLAIM_DELAY <= now]
                if not eligible:
                    # Somebody wants the valve but nobody has persisted long
                    # enough to claim it; keep the peace and wake up when the
                    # first claim matures.
                    target = incumbent
                    self._schedule(self._deadline(ordered[0]) - now)
                else:
                    target = self._demand_of(eligible[0])

        if target is not incumbent:
            # The member that would run the group in target's direction is
            # exempt from the quiet gate: awarding it its own direction sends
            # it no commands, so its running compressor protects nothing.
            awarded = self._lead(active, target)
            blocked_until = self._blocked_until(active, now, awarded)
            if blocked_until is not None:
                self._schedule(max(timedelta(seconds=1), blocked_until - now))
                target = incumbent

        await self._reconcile(active, target, now)
        self._report_conflict(heat, cool)

        if self._dirty:
            self._dirty = False
            self._hass.async_create_task(self.async_arbitrate())

    async def _reconcile(
        self, active: list[GroupMember], target: Demand | None, now: datetime
    ) -> None:
        """Park the conflicting members, release the rest, publish context."""
        applied_change = target is not self._group_mode

        deferred_to: str | None = None
        if target is not None:
            winners = [m for m in active if self._demand_of(m) is target]
            if winners:
                deferred_to = min(winners, key=self._rank).entity_id

        self._applying = True
        try:
            if applied_change:
                self._group_mode = target
                self._last_change = now

            for member in active:
                demand = self._demand_of(member)
                # Never park the member whose own demand is the group's
                # target: it may still physically hold the old side, but its
                # own engine flips the valve, and parking here would fight it.
                # A member merely *wanting* the opposite direction counts too:
                # its engine would flip the shared valve the moment the room
                # crosses the midpoint.
                conflicting = (
                    target is not None
                    and demand is not target
                    and member.coordination_parkable
                    and (
                        demand is OPPOSITE[target]
                        or member.coordination_intent() is OPPOSITE[target]
                        or member.coordination_held_mode() is OPPOSITE[target]
                    )
                )
                if conflicting:
                    # Parking before the winner is re-commanded is what
                    # physically frees the reversing valve.
                    thwarted_from = next(
                        (
                            side
                            for side in (
                                self._demand_of(member),
                                member.coordination_intent(),
                                member.coordination_held_mode(),
                            )
                            if side in (Demand.HEAT, Demand.COOL)
                        ),
                        OPPOSITE[target],
                    )
                    await member.async_park_for_group(thwarted_from)
                elif member.coordination_parked:
                    await member.async_release_park()

            for member in active:
                member.async_group_context_changed(target, deferred_to)
        finally:
            self._applying = False

    def _blocked_until(
        self,
        active: list[GroupMember],
        now: datetime,
        awarded: GroupMember | None = None,
    ) -> datetime | None:
        """Return when the gates open for a group-mode change, or None."""
        if self._last_change is not None:
            cycle = max(
                (m.coordination_cycle_seconds() for m in active),
                default=int(DEFAULT_MIN_CYCLE_TIME.total_seconds()),
            )
            until = self._last_change + timedelta(seconds=cycle)
            if now < until:
                return until
        if any(m is not awarded and m.coordination_source_running() for m in active):
            # Nobody may reverse the valve while a *losing* member is still
            # physically running; the awarded member already runs that way and
            # receives no commands. There is no clock to point at; the backstop
            # retry plus the members' state-change notifications will wake us.
            return now + QUIET_RETRY
        return None

    def _report_conflict(
        self, heat: list[GroupMember], cool: list[GroupMember]
    ) -> None:
        """Warn once per conflict episode; two rooms genuinely disagreeing."""
        conflicted = bool(heat) and bool(cool)
        if conflicted and not self._conflict_fired:
            LOGGER.warning(
                "Group %s is split: %s want heat (%s) while %s want cool (%s); "
                "the group runs %s",
                self._name,
                len(heat),
                ", ".join(m.entity_id for m in heat),
                len(cool),
                ", ".join(m.entity_id for m in cool),
                self._group_mode,
            )
            self._hass.bus.async_fire(
                EVENT_MODE_CONFLICT,
                {
                    "group": self._name,
                    "group_mode": (
                        self._group_mode.value if self._group_mode is not None else None
                    ),
                    "heat": [m.entity_id for m in heat],
                    "cool": [m.entity_id for m in cool],
                },
            )
        self._conflict_fired = conflicted

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------
    def _schedule(self, delay: timedelta) -> None:
        """Retry arbitration once the gates can plausibly have opened."""
        if self._retry_unsub is not None:
            return
        delay = max(delay, timedelta(seconds=1))

        @callback
        def _retry(_now: datetime) -> None:
            self._retry_unsub = None
            self._hass.async_create_task(self.async_arbitrate())

        self._retry_unsub = async_call_later(self._hass, delay, _retry)

    def _cancel_retry(self) -> None:
        """Drop any pending retry; a live member event already woke us."""
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None

    # ------------------------------------------------------------------
    # Bookkeeping helpers
    # ------------------------------------------------------------------
    def _demand_of(self, member: GroupMember) -> Demand:
        entry = self._demands.get(member.entity_id)
        return entry[0] if entry is not None else Demand.NONE

    def _rank(self, member: GroupMember) -> tuple[datetime, int, str]:
        """Return the FCFS sort key: claim age, then priority, then id."""
        return (self._started(member), -member.coordination_priority, member.entity_id)

    def _lead(
        self, active: list[GroupMember], target: Demand | None
    ) -> GroupMember | None:
        """Return the member that would run the group in target's direction."""
        if target is None:
            return None
        leads = [m for m in active if self._demand_of(m) is target]
        if not leads:
            return None
        return min(leads, key=self._rank)

    def _started(self, member: GroupMember) -> datetime:
        entry = self._demands.get(member.entity_id)
        return entry[1] if entry is not None else dt_util.utcnow()

    def _deadline(self, member: GroupMember) -> datetime:
        return self._started(member) + CLAIM_DELAY

    def snapshot(self) -> dict[str, Any]:
        """Return the group's live state for attributes or diagnostics."""
        return {
            "group": self._name,
            "group_mode": (
                self._group_mode.value if self._group_mode is not None else None
            ),
            "last_change": (
                self._last_change.isoformat() if self._last_change else None
            ),
            "members": {
                entity_id: {
                    "demand": self._demand_of(member).value,
                    "priority": member.coordination_priority,
                    "parked": member.coordination_parked,
                }
                for entity_id, member in self._members.items()
            },
        }
