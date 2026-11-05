# Dual Setpoint Climate

Expose native `heat_cool` (a heating setpoint *and* a cooling setpoint) on top
of any Home Assistant climate entity that only supports a single setpoint.

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
- The wrapper's own state (mode, bounds, last flip time) survives restarts via
  Home Assistant's restore mechanism.

## Installation

1. Install [HACS](https://hacs.xyz/).
2. Add this repository as a custom repository (category: *Integration*) or use
   the default repo listing once published.
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

### Gap guidance

`min_gap` only rejects *requests* that are too tight — it cannot change your
hardware. If your low/high bounds are so close that normal thermostat
overshoot carries the temperature past the midpoint, the wrapper will flip back
and forth and the unit will fight itself. Set the bounds, and `min_gap`,
comfortably farther apart than your equipment's overshoot so the temperature
has room to settle before the other side becomes active.

## Development

This repository started as the
[`integration_blueprint`](https://github.com/ludeeus/integration_blueprint)
template.

- `custom_components/dual_setpoint/` — the integration itself.
- `tests/` — pytest suite (`pytest-homeassistant-custom-component`); run with
  `python -m pytest tests/ -q`.
- `scripts/develop` — start Home Assistant with this integration loaded
  (the dev config enables `demo:` so there are climate entities to wrap).
- Linting uses [ruff](https://docs.astral.sh/ruff/): `python -m ruff check .`
  and `python -m ruff format .`.
- `scripts/generate_brand_images.py` regenerates the brand images
  (dial with two setpoints) written to
  `custom_components/dual_setpoint/brand/`.

### Brand images

Since Home Assistant 2026.3 an integration can ship its own icon and logo in
`custom_components/dual_setpoint/brand/`; Home Assistant serves them
automatically and they take priority over the
[brands](https://github.com/home-assistant/brands) repository, so no extra
configuration is needed. The images here are generated with
`python scripts/generate_brand_images.py` (requires Pillow, already a Home
Assistant dependency).

`ignore: brands` stays in `.github/workflows/validate.yml` only because the
pinned HACS validation action predates local brand images; a companion PR to
the brands repository remains optional.
