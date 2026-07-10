# harness

The car's off-board wiring diagram — the wires that run **between** boards,
not the wiring internal to any one board. It is generated from the KiCad
board schematics on every push, so it can't drift out of sync with the boards.

## What's here

| Path | What it is |
|---|---|
| `car.yaml` | **Hand-maintained backbone.** The only file you edit by hand: it declares the battery/master-switch spine, the inter-board wires (CAN bus, power), and any junction boxes. It `includes:` the generated per-board connectors. |
| `generated/<board>.yaml` | **Build artifacts — never hand-edit.** One file per board listing that board's off-board connectors, extracted from its schematic. |
| `output/car.svg` | The rendered whole-car diagram. Regenerated + committed by CI. |
| `scripts/extract_connectors.py` | Extracts off-board connectors from each board's KiCad schematic (see `scripts/README.md`). |
| `scripts/test_pipeline.py` | Smoke test: extractor → build → idempotency. |
| `wirelab/` | Vendored copy of the YAML→SVG harness renderer (Wirelab). Build path needs `pydantic` + `PyYAML` (see `wirelab/requirements.txt`). |

## The pipeline

```
boards/<board>/*.kicad_sch
   │  extract_connectors.py  (kicad-cli netlist; keeps TE 917xxx + Molex 153110xx connectors)
   ▼
harness/generated/<board>.yaml
   │  included by
   ▼
harness/car.yaml  (hand-maintained backbone: battery, switch, inter-board wires)
   │  wirelab/cli.py build
   ▼
harness/output/car.svg
```

## Regenerate locally

Needs `kicad-cli` (KiCad 9) on PATH and the renderer's deps installed:

```
pip install -r harness/wirelab/requirements.txt
python3 harness/scripts/extract_connectors.py
python3 harness/wirelab/cli.py build harness/car.yaml -o harness/output/car.svg
```

Or just run the smoke test, which does the extract + build:

```
python3 harness/scripts/test_pipeline.py
```

## CI

The `harness-render` job (`.github/workflows/ci.yml`) runs in the
`kicad/kicad:9.0` container. On a **PR** it fails if the committed
`generated/*.yaml` or `output/car.svg` are stale (`extract_connectors.py
--check` + an SVG diff). On a **push to `main`** it regenerates and
auto-commits them (`[skip ci]`), the same safety-net pattern the consolidated
BOM job uses. So: change a board → push → the diagram updates itself.

## Adding an inter-board wire / junction box

Edit `car.yaml`. Reference a board connector by its include alias and refdes,
e.g. `ecvt.J6.CANH`. Cross-board wires and the battery/switch spine live only
here; per-board connector pins come from `generated/` and update when the
board changes.

## Auxiliary boards & repeated (instanced) boards

Small aux boards (wheel/eCVT hall sensors, fuel sensor) follow the same
pattern as the big three: **one file per unique design**, instanced in
`car.yaml`.

- **A board that appears more than once on the car** (e.g. four identical
  wheel hall sensors) is still just ONE `generated/*.yaml`. Import it under
  several aliases in `car.yaml`'s `includes:` — `wheel_fl:`, `wheel_fr:`, … all
  pointing at `generated/wheel-hall.yaml`. Each alias becomes a separate copy
  of the connector in the diagram. The renderer namespaces each component's
  layout **zone by its include alias**, so every instance gets its own column
  (see `_resolve_includes` in `wirelab/parser.py`). Wire each instance
  independently, e.g. `daq.J7.LEFT_HALL -> wheel_fl.J1.HALL_OUT`.

- **A real KiCad board** goes in `boards/<name>/` and its `generated/<name>.yaml`
  is produced by `extract_connectors.py` like any other board.

- **A placeholder board** (design not drawn yet) can be a hand-written
  `generated/<name>.yaml` with a `# HAND-WRITTEN PLACEHOLDER` header. Because
  the extractor only writes/`--check`s files for folders that exist under
  `boards/`, a placeholder with no matching board folder is left untouched by
  CI. When you later add `boards/<name>/`, the extractor regenerates that file
  and it becomes a normal build artifact — the hand file is transparently
  replaced. (Keep the placeholder's connectors/pins matching the intended
  design so the diagram stays truthful until the real board lands.)
