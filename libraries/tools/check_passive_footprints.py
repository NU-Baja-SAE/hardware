#!/usr/bin/env python3
"""
check_passive_footprints.py

Lint check: flags SMD resistor/capacitor footprints that don't match the
team's standard sizes (see "Passive footprint sizes" in libraries/README.md).

Standard: 0603 by default, 0805 where a part needs more pad area (higher
power/voltage, or easier hand-rework); always the "HandSolder" pad variant,
since boards are hand-soldered rather than reflowed. Chosen by scanning
what's actually used on last year's boards, not arbitrarily.

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

ALLOWED = re.compile(r"^[RC]_(0603|0805)_\d+Metric_Pad[\d.]+x[\d.]+mm_HandSolder$")

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
        if not ALLOWED.match(name):
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
          "(standard: 0603 default / 0805 alternate, HandSolder pad variant):\n")
    for path, footprint in violations:
        print(f"  {path}: {footprint}")
    sys.exit(1)


if __name__ == "__main__":
    main()
