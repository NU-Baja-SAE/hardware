#!/usr/bin/env python3
"""
check_passive_footprints.py

Lint check: flags SMD resistor/capacitor footprints that don't match the
team's standard sizes (see "Passive footprint sizes" in libraries/README.md).

Standard chip sizes: 0603 by default, 0805 where a part needs more pad area
(higher power/voltage, or easier hand-rework), and 1206 for the higher-power
cases; always the "HandSolder" pad variant, since boards are hand-soldered
rather than reflowed. Chosen by scanning what's actually used on the boards,
not arbitrarily.

Bulk/polarized capacitors (electrolytic, tantalum -- the `CP_*` families) are
not chip-size parts and are exempt: you can't shrink a bulk energy-storage cap
to an 0805, so the chip-size rule doesn't apply to them.

Scans every .kicad_pcb (placed footprints -- what actually gets fabricated)
and .kicad_sch (a symbol's default Footprint field -- catches problems
before layout even exists) under the given root for
`Resistor_SMD:*` / `Capacitor_SMD:*` references and checks each against the
allowed pattern.

USAGE:
    python3 check_passive_footprints.py [root]   # root defaults to boards/

Exits non-zero (and prints one line per offender) if any disallowed
footprint is found. No third-party dependencies -- safe to run in CI
without a pip install step.
"""

import re
import sys
from pathlib import Path

# Standard chip sizes, HandSolder pad variant.
ALLOWED = re.compile(r"^[RC]_(0603|0805|1206)_\d+Metric_Pad[\d.]+x[\d.]+mm_HandSolder$")

# Bulk/polarized caps (electrolytic `CP_Elec_*`, tantalum `CP_Tantalum_*`, and
# the generic polarized `CP_*`) aren't chip-size passives -- they can't be
# shrunk to a chip footprint, so they're exempt from the size rule entirely.
BULK_CAP_EXEMPT = re.compile(r"^CP_")

# Matches both the standalone PCB footprint reference, e.g.
#   (footprint "Resistor_SMD:R_0603_..._HandSolder" ...
# and a schematic symbol's Footprint property, e.g.
#   (property "Footprint" "Capacitor_SMD:C_0402_..." ...
REFERENCE = re.compile(r'"(Resistor_SMD|Capacitor_SMD):([^"]+)"')

SKIP_DIR_MARKERS = ("backup", "-bak")


def is_backup_path(p: Path) -> bool:
    return any(m in part.lower() or part.startswith("_autosave-") for part in p.parts for m in SKIP_DIR_MARKERS)


def check_file(path: Path):
    """Yields (footprint_name,) for every disallowed Resistor_SMD/Capacitor_SMD reference in path."""
    text = path.read_text(encoding="utf-8", errors="replace")
    for lib, name in REFERENCE.findall(text):
        if ALLOWED.match(name) or BULK_CAP_EXEMPT.match(name):
            continue
        yield f"{lib}:{name}"


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("boards")
    if not root.exists():
        sys.exit(f"No such folder: {root}")

    files = sorted(
        p for p in list(root.rglob("*.kicad_pcb")) + list(root.rglob("*.kicad_sch"))
        if not is_backup_path(p.relative_to(root))
    )

    violations = []
    for path in files:
        for footprint in check_file(path):
            violations.append((path, footprint))

    if not violations:
        print(f"OK: all resistor/capacitor footprints in {root} match the standard sizes.")
        return

    print("Disallowed resistor/capacitor footprint(s) found "
          "(standard chip sizes: 0603/0805/1206, HandSolder pad variant; "
          "bulk CP_* caps exempt):\n")
    for path, footprint in violations:
        print(f"  {path}: {footprint}")
    sys.exit(1)


if __name__ == "__main__":
    main()
