# Dual Setpoint Climate

Expose native `heat_cool` (a heating setpoint _and_ a cooling setpoint) on top
of any Home Assistant climate entity that has heating and cooling modes.

Many thermostats and HVAC integrations expose just one target temperature with
`heat`/`cool`/`off` modes. Home Assistant's thermostat cards, schedules, and
automations are much nicer when the entity supports `heat_cool` with
`target_temp_low`/`target_temp_high`. This integration is a _helper_: it wraps
a "source" entity and presents a complete dual-setpoint climate entity
on top of it.

## How it works

- The wrapper mirrors the source entity: availability, min/max temperature,
  target-temp step, fan modes, swing modes, preset modes, and the current
  temperature all come straight from the source.
- In `heat`/`cool`/`off` the wrapper passes through to the source like a normal
  single-setpoint climate entity.
- In `heat_cool` you set a lower and an upper bound. The wrapper compares the
  current temperature against the midpoint of the two bounds:
  - below the midpoint → the source is commanded to `heat` with the low bound;
  - above the midpoint → the source is commanded to `cool` with the high bound.
- **Compressor protection:** a flip between heating and cooling is deferred
  until `min_cycle_time` has elapsed since the last flip, then applied
  automatically.
- **Out-of-band awareness:** if the source is changed remotely (vendor app,
  cloud, another automation), the wrapper follows that change and leaves
  `heat_cool` rather than fighting it. A short settle window absorbs stale
  polls and per-mode setpoint echoes right after a mode change.

## Installation

1. Install [HACS](https://hacs.xyz/).
2. Add this repository as a custom repository (category: _Integration_)
3. Install **Dual Setpoint Climate** and restart Home Assistant.
4. Go to _Settings → Devices & Services → Add Integration → Dual Setpoint
   Climate_ and pick the source climate entity to wrap.

## Configuration options

| Option                 | Meaning                                                                                                                                                            |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Source entity**      | The single-setpoint climate entity to wrap.                                                                                                                        |
| **Minimum gap**        | Smallest allowed distance between the low and high setpoints. Ranges closer together than this are rejected to prevent energy waste. Defaults to 2.0 °C or 4.0 °F. |
| **Minimum cycle time** | How long to wait before flipping the source between heating and cooling. Defaults to 30 minutes.                                                                   |

Options can be changed later from the integration's entry (⋮ → _Configure_).

## Multi-zone coordination (shared outdoor units)

A multi-zone minisplit system — one outdoor compressor serving several indoor
heads (Mitsubishi MXZ, LG Multi-F, Fujitsu AOU multi, and similar) — can heat
_or_ cool, never both: a single reversing valve decides, and on most systems
the first indoor unit to request a mode locks the valve until every unit on
the old side goes quiet. Wrap each indoor unit and the wrappers can coordinate
so an idle unit holding the lock no longer blocks a unit that genuinely needs
the opposite mode.

Enable it per entity under **Configure → Coordinate modes with other zones**,
then give every indoor unit on the same outdoor unit the same **Coordination
group** name. Groups are independent; two outdoor units means two group names.

### How the group decides

- Each wrapper computes what its room **demands** (heat, cool, or idle) from
  its _face_ mode and setpoints versus the room reading, and publishes it as
  the `demand` attribute.
- The group runs in one mode at a time. The current mode keeps running while
  any room still demands it; otherwise the demand that has been waiting
  longest takes over (first come, first served), with **Priority** (higher
  wins) breaking exact ties. A brand-new demand must persist for two minutes
  before it can take the valve, which rides out sensor blips.
- A reversal also waits out the group's cycle time (the longest member's
  **Minimum cycle time**) and never happens while a member that would _lose_
  the flip still has its source physically heating or cooling. (The member
  being awarded the flip may already be running that way — it gets no
  commands, so nothing needs to quiet down.)
- While the group runs the other way, a split raises a
  `dual_setpoint_mode_conflict` event once per episode (plus a warning log),
  so you can automate around it.

### What a thwarted zone does

A wrapper's face **never changes mode on its own** — you set Cool, the card
keeps saying Cool, with `hvac_action: idle`. What gets parked instead is the
_underlying source entity_: it is set to `off`, or to `fan_only` at a
configurable fan speed, using separate choices for "blocked while wanting to
heat" and "blocked while wanting to cool" (**Hold when heating/cooling is
blocked**, **Fan speed while held**). If the device has no `fan_only` mode the
hold degrades to `off`. Parking only ever touches the source's mode — never
its setpoints.

When the group comes around to your side, the source is released back to
exactly what the face says it wants. A command issued through the wrapper
(card/automation) always wins: it lifts the park and the group will simply
re-park later if the room still opposes. Using the vendor remote on a parked
indoor unit also lifts the park immediately (the user in the moment wins), but
the group re-parks shortly after if the disagreement continues.

Sources that natively support dual setpoints (`heat_cool` with
target_temp_low/high) are exempt from parking — they manage their own
reversing valve — but still advertise their demand so the group can see them.

### Extra attributes

When coordination is on, each wrapper exposes: `demand` (what the room wants),
`group` (its group name), `group_mode` (the mode the group currently runs,
if any), `deferred_to` (the entity currently holding the valve for you), and
`parked` (`off`/`fan_only` while its source is held).

### Notes and limitations

- The parked source may stop reporting a fresh `current_temperature` on some
  hardware (many heads stop broadcasting when off). Demand is computed from
  the wrapper's own state, but the face's room reading is whatever the source
  last reported.
- Coordination is off by default; with the switch off nothing about the
  wrapper's behavior changes.
- A group's decision survives member restarts by re-notifying on startup;
  there is no persisted valve lock.

## FAQ

**Q: Is this dangerous?**

> All the safety features of the base Climate entity and the actual hardware are still in place. This integration isn’t doing anything that you couldn’t do yourself manually with the remote control.

**Q: Does this waste energy by flopping between heating and cooling?**

> There are two configurable settings to prevent this. First, there is an enforced minimum gap between the upper and lower setpoints (2 °C or 4 °F default). Second, there is a minimum cycle time (30 minutes default).

**Q: Does this bypass the energy saving features of my heat pump?**

> No. The mode is changed well before the room temperature crosses a setpoint, giving your heat pump time to do whatever condenser tricks it supports.

**Q: What if I make changes using the physical remote, or the underlying Climate entity?**

> The wrapper will mirror whatever the underlying Climate entity does. The ONLY case where the wrapper will fight you is if you switch the underlying entity to a “Heat/Cool” mode that has only 1 setpoint. In that case, the wrapper will supercede it with dual-setpoint behavior, kicking the underlying entity into heat mode or cool mode as appropriate.
