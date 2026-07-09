# harness/scripts

## `extract_connectors.py`

Generates `harness/generated/<board>.yaml` — one file per board describing
that board's **off-board** connectors (the pins whose wires leave the board
and become part of the car harness). Board-internal nets are ignored.

It runs `kicad-cli sch export netlist` on each board's root schematic, keeps
only the harness connector families (TE 917xxx, Molex 15311026/15311046), and
writes a pin-per-connector-pin YAML with each pin named by the net on it.

```
python3 harness/scripts/extract_connectors.py          # regenerate generated/*.yaml
python3 harness/scripts/extract_connectors.py --check   # exit 1 if any file is stale (CI)
```

Requires `kicad-cli` (KiCad 9) on PATH. Symbols resolve from each schematic's
embedded cache, so no library table / `BAJA_LIB` env var is needed — it runs
the same on a laptop and in the CI `kicad/kicad:9.0` container.

### Files under `harness/generated/` are BUILD ARTIFACTS

Never hand-edit them. To change a connector, edit the board schematic and
regenerate. CI runs `--check` on every PR; if it fails, regenerate and commit.

### The pipeline

```
boards/<board>/*.kicad_sch
        │  extract_connectors.py (kicad-cli netlist)
        ▼
harness/generated/<board>.yaml        (one connector component per J-ref)
        │  included by
        ▼
harness/car.yaml   (hand-maintained backbone: joins boards, junction boxes,
        │           inter-board wires)
        ▼  wireviz_alternative build
harness/output/car.svg                (the whole-car off-board wiring diagram)
```
