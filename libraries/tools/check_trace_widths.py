#!/usr/bin/env python3
"""
check_trace_widths.py

Lint check: flags routed copper traces narrower than the team's netclass
minimums (see "Trace width / netclass convention" in libraries/README.md):

  - "Default" netclass (general signal wiring): >= 0.3mm
  - "Power" netclass:                            >= 0.6mm
  - any other netclass (e.g. a project-specific "24V" class for a higher
    current rail): >= that class's own configured track_width in the
    project's .kicad_pro (so a manually-drawn trace can't quietly end up
    thinner than the class default it's supposed to follow)
  - absolute manufacturer floor, regardless of class: 0.1mm

This only checks trace WIDTH, which is explicit in each segment/arc. It does
NOT check clearance (0.09mm floor) -- clearance is a function of real board
geometry (nearby copper on other nets, pours, etc.) that a text scan can't
evaluate reliably. Clearance is enforced by the DRC rules in each board's
<project>.kicad_dru (see libraries/baja-common.kicad_dru for the shared
template) -- run KiCad's DRC (Inspect > Design Rules Checker, or
`kicad-cli pcb drc` once that's wired into CI) to check it.

For each `boards/**/*.kicad_pcb` found, this looks for a sibling
`<same-stem>.kicad_pro` to read netclass definitions and net-to-class
assignments from. If none exists, only the absolute 0.1mm floor is checked
(there's no netclass info to resolve a per-class minimum against).

Net-to-class resolution mirrors KiCad's own project file: exact matches in
"netclass_assignments" first, then glob-style "netclass_patterns", else the
"Default" class -- same rule KiCad itself uses when you don't explicitly
assign a net class.

Two severities:
  - ERROR: trace is narrower than the 0.1mm absolute manufacturer floor.
    Fails the check (non-zero exit) -- this is a real manufacturability
    violation, not just a style deviation.
  - WARNING: trace is at or above the 0.1mm floor but doesn't exactly
    match its netclass's standard width (e.g. 0.2mm or 1.0mm in a class
    whose standard is 0.3mm). Printed for visibility but does NOT fail the
    check -- there are legitimate reasons to deviate (an impedance-
    controlled differential pair routed thinner, extra current margin
    routed wider, matching an existing pour), so this is a "heads up,
    double-check this was intentional," not a blocker.

USAGE:
    python3 check_trace_widths.py [root]   # root defaults to boards/

Exits non-zero only if at least one ERROR-level trace is found; warnings
alone exit 0 so a PR can still merge. No third-party dependencies -- safe
to run in CI without a pip install step.
"""

import fnmatch
import json
import re
import sys
from pathlib import Path

ABS_MIN_WIDTH_MM = 0.1
POLICY_MIN_MM = {
    "Default": 0.3,
    "Power": 0.6,
}

NET_DEF_RE = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
TRACK_RE = re.compile(
    r'\((segment|arc)\s'
    r'(?:(?!\)\s*\(segment|\)\s*\(arc).)*?'
    r'\(width\s+([\d.]+)\)'
    r'(?:(?!\)\s*\(segment|\)\s*\(arc).)*?'
    r'\(net\s+(\d+)\)',
    re.DOTALL,
)

SKIP_DIR_MARKERS = ("backup", "-bak")


def is_backup_path(p: Path) -> bool:
    return any(m in part.lower() or part.startswith("_autosave-") for part in p.parts for m in SKIP_DIR_MARKERS)


def load_netclasses(pro_path: Path):
    """Returns (class_min_width: dict[name -> mm], net_to_class: dict[net_name -> class_name], patterns: list[(pattern, class_name)])."""
    try:
        data = json.loads(pro_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        print(f"  ! could not parse {pro_path} as JSON ({e}); skipping netclass resolution", file=sys.stderr)
        return {}, {}, []

    net_settings = data.get("net_settings", {})
    class_min_width = {c["name"]: c.get("track_width", ABS_MIN_WIDTH_MM) for c in net_settings.get("classes", [])}
    net_to_class = {name: classes[0] for name, classes in net_settings.get("netclass_assignments", {}).items() if classes}
    patterns = [(p["pattern"], p["netclass"]) for p in net_settings.get("netclass_patterns", [])]
    return class_min_width, net_to_class, patterns


def resolve_class(net_name: str, net_to_class: dict, patterns: list) -> str:
    if net_name in net_to_class:
        return net_to_class[net_name]
    for pattern, cls in patterns:
        if fnmatch.fnmatchcase(net_name, pattern):
            return cls
    return "Default"


def standard_mm(cls: str, class_min_width: dict) -> float:
    """The expected width for `cls` -- team policy if set, else whatever the
    project's own .kicad_pro configures for that class, else the absolute
    floor (no better info available)."""
    return POLICY_MIN_MM.get(cls, class_min_width.get(cls, ABS_MIN_WIDTH_MM))


def check_board(pcb_path: Path):
    """Yields (severity, net_name, netclass, width_mm, expected_mm) for every
    trace that's either below the absolute floor ('error') or doesn't match
    its class's standard width ('warning')."""
    pro_path = pcb_path.with_suffix(".kicad_pro")
    if pro_path.exists():
        class_min_width, net_to_class, patterns = load_netclasses(pro_path)
    else:
        print(f"  ! no sibling {pro_path.name}; only checking the {ABS_MIN_WIDTH_MM}mm absolute floor", file=sys.stderr)
        class_min_width, net_to_class, patterns = {}, {}, []

    text = pcb_path.read_text(encoding="utf-8", errors="replace")
    net_names = {int(num): name for num, name in NET_DEF_RE.findall(text)}

    for _kind, width_str, net_num in TRACK_RE.findall(text):
        width = float(width_str)
        net_name = net_names.get(int(net_num), "")
        cls = resolve_class(net_name, net_to_class, patterns)
        label = net_name or f"(net {net_num})"
        if width + 1e-9 < ABS_MIN_WIDTH_MM:
            yield "error", label, cls, width, ABS_MIN_WIDTH_MM
            continue
        expected = standard_mm(cls, class_min_width)
        if abs(width - expected) > 1e-9:
            yield "warning", label, cls, width, expected


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("boards")
    if not root.exists():
        sys.exit(f"No such folder: {root}")

    pcb_files = sorted(p for p in root.rglob("*.kicad_pcb") if not is_backup_path(p.relative_to(root)))

    errors, warnings = [], []
    for pcb_path in pcb_files:
        for severity, net_name, cls, width, req in check_board(pcb_path):
            (errors if severity == "error" else warnings).append((pcb_path, net_name, cls, width, req))

    if warnings:
        print("Non-standard trace width(s) (allowed, but double-check these were intentional):\n")
        for pcb_path, net_name, cls, width, expected in warnings:
            print(f"  {pcb_path}: net '{net_name}' (class {cls}) is {width}mm, standard is {expected}mm")
        print()

    if not errors:
        print(f"OK: no traces in {root} are under the {ABS_MIN_WIDTH_MM}mm absolute manufacturer floor.")
        return

    print(f"Trace(s) below the {ABS_MIN_WIDTH_MM}mm manufacturer floor found:\n")
    for pcb_path, net_name, cls, width, floor in errors:
        print(f"  {pcb_path}: net '{net_name}' (class {cls}) is {width}mm, needs >= {floor}mm")
    sys.exit(1)


if __name__ == "__main__":
    main()
