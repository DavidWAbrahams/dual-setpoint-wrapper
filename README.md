# Dual Setpoint Climate

Expose native `heat_cool` (a heating setpoint *and* a cooling setpoint) on top
of any Home Assistant climate entity that has heating and cooling but only
supports a single setpoint.

Many thermostats and HVAC integrations expose just one target temperature with
`heat`/`cool`/`off` modes. Home Assistant's thermostat cards, schedules, and
automations are much nicer when the entity supports `heat_cool` with
`target_temp_low`/`target_temp_high`. This integration is a *helper*: it wraps
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
2. Add this repository as a custom repository (category: *Integration*)
3. Install **Dual Setpoint Climate** and restart Home Assistant.
4. Go to *Settings → Devices & Services → Add Integration → Dual Setpoint
   Climate* and pick the source climate entity to wrap.

## Configuration options

| Option | Meaning |
| -- | -- |
| **Source entity** | The single-setpoint climate entity to wrap. |
| **Minimum gap** | Smallest allowed distance between the low and high setpoints. Ranges closer together than this are rejected. Defaults to 2.0 °C (metric) or 4.0 °F (imperial). |
| **Minimum cycle time** | How long to wait before flipping the source between heating and cooling. Defaults to 15 minutes. |

Options can be changed later from the integration's entry (⋮ → *Configure*).
