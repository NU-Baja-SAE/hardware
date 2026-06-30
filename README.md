# NU Baja SAE — Hardware

KiCad schematics, PCB layouts, wiring harness designs, and BOM tooling for all NU Baja SAE electronics boards.

## KiCad version

All board files must be authored/saved in the same KiCad version to avoid spurious diffs from format upgrades. Current standard version: **TBD** (update this line once the team confirms).

## Structure

```
hardware/
├── boards/
│   ├── ecvt-controller/   (.kicad_pro/.kicad_sch/.kicad_pcb + fab-outputs/ + README.md)
│   ├── daq-node/
│   └── hud-node/
├── harness/                # WireViz or WireLab YAML source + output/ (generated SVGs)
├── bom/
│   ├── consolidated-bom.csv
│   └── scripts/generate_bom.py
├── libraries/              # shared KiCad symbol/footprint libs
└── .github/workflows/
```

## Releases

Board releases are tagged at the point gerbers are sent to fab, e.g. `ecvt-controller-v1.2`, with a one-line changelog per board.
