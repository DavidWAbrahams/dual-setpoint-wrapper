# Dual Setpoint Climate

Expose native `heat_cool` (a heating setpoint _and_ a cooling setpoint) on top
of any Home Assistant climate entity that has heating and cooling modes.

Many thermostats and HVAC integrations expose just one target temperature with
`heat`/`cool`/`off` modes. Home Assistant's thermostat cards, schedules, and
automations are much nicer when the entity supports `heat_cool` with
`target_temp_low`/`target_temp_high`. This integration is a _helper_: it wraps
one such "source" entity and presents a complete dual-setpoint climate entity
that drives it behind the scenes.

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

## FAQ

**Q: Is this dangerous?**  
> All the safety features of the base Climate entity and the actual hardware are still in place. This integration isn’t doing anything that you couldn’t do yourself manually with the remote control.

**Q: Does this waste energy by flopping between heating and cooling?**  
> There are two configurable settings to prevent this. First, there is an enforced minimum gap between the upper and lower setpoints (2 °C or 4 °F default). Second, there is a minimum cycle time (30 minutes default).

**Q: Does this bypass the energy saving features of my heat pump?**  
> No. The mode is changed well before the room temperature crosses a setpoint, giving your heat pump time to do whatever condenser tricks it supports.

**Q: What if I make changes using the physical remote, or the underlying Climate entity?**  
> The wrapper will mirror whatever the underlying Climate entity does. The ONLY case where the wrapper will fight you is if you switch the underlying entity to a “Heat/Cool” mode that has only 1 setpoint. In that case, the wrapper will supercede it with dual-setpoint behavior, kicking the underlying entity into heat mode or cool mode as appropriate.
