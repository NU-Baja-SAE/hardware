#!/usr/bin/env python3
"""
harvest_kicad_footprints.py

Companion to harvest_kicad_parts.py: builds a single, standardized KiCad
footprint library (.pretty folder) from every footprint actually used
across a folder of existing KiCad projects.

WHY THIS WORKS WITHOUT TOUCHING YOUR ORIGINAL LIBRARIES:
Every .kicad_pcb file embeds a fully self-contained copy of each footprint
placed on it, under a top-level (footprint "LibName:FootprintName" ...)
block -- complete pads, graphics, and 3D model refs, no need to resolve
fp-lib-table entries. This script walks every .kicad_pcb it finds, strips
the board-specific instance data (position, uuid, net assignments, sheet
path), de-duplicates by lib_id, and writes each surviving footprint out as
its own standalone .kicad_mod file in a new .pretty library folder.

USAGE:
    python3 harvest_kicad_footprints.py /path/to/onedrive/boards/root --out ./baja-common-footprints

OUTPUT:
    <out>.pretty/        - folder of .kicad_mod files, ready to add as a
                            fp-lib-table entry
    <out>_report.csv      - every footprint found, which board(s) used it,
                            and any naming collisions or content conflicts
"""

import argparse
import csv
import sys
from pathlib import Path

try:
    import sexpdata
    from sexpdata import Symbol
except ImportError:
    sys.exit("Missing dependency. Run: pip install sexpdata --break-system-packages")

STRIP_TOP_LEVEL = {"uuid", "at", "path", "sheetname", "sheetfile", "tstamp"}


def is_tagged(node, tag):
    return isinstance(node, list) and len(node) > 0 and isinstance(node[0], Symbol) and str(node[0]) == tag


def find_direct_children_tagged(node, tag):
    """Yield immediate children of `node` tagged with `tag` (non-recursive)."""
    if not isinstance(node, list):
        return
    for child in node:
        if is_tagged(child, tag):
            yield child


def project_label(pcb_path: Path, root: Path) -> str:
    """Best-effort label for which 'board'/project a layout belongs to:
    the nearest .kicad_pro's folder name, else the immediate parent folder."""
    for parent in [pcb_path.parent, *pcb_path.parents]:
        if parent == root.parent:
            break
        if list(parent.glob("*.kicad_pro")):
            return parent.name
    return pcb_path.parent.name


def strip_pad_nets(node):
    """Recursively drop (net ...) clauses found inside (pad ...) blocks --
    those are board-specific connections, meaningless in a library footprint."""
    if not isinstance(node, list):
        return node
    if is_tagged(node, "pad"):
        return [child for child in (strip_pad_nets(c) for c in node) if not is_tagged(child, "net")]
    return [strip_pad_nets(c) for c in node]


def normalize_footprint(fp_node, header_fields):
    """Strip board-instance-specific data and inject standalone .kicad_mod
    header fields (version/generator/generator_version), producing a node
    suitable both for de-dup comparison and for writing to disk."""
    name = fp_node[1]
    rest = [c for c in fp_node[2:] if not (is_tagged(c, "uuid") or (isinstance(c, list) and len(c) > 0
            and isinstance(c[0], Symbol) and str(c[0]) in STRIP_TOP_LEVEL))]
    rest = strip_pad_nets(rest)
    return [Symbol("footprint"), name, *header_fields, *rest]


def is_backup_path(p: Path) -> bool:
    """True if any path component looks like a KiCad autosave/backup snapshot
    folder (e.g. 'foo-backups/foo-2026-03-06_141242') rather than a distinct
    design -- these are old/mid-edit copies of a board that was already
    harvested under its real name, and only add noise/false conflicts."""
    for part in p.parts:
        low = part.lower()
        if "backup" in low or part.startswith("_autosave-") or low.endswith("-bak"):
            return True
    return False


def harvest(root: Path):
    """Returns (merged: dict[lib_id -> node], usage: dict[lib_id -> set of boards],
                conflicts: list[str])"""
    merged = {}
    usage = {}
    conflicts = []

    all_pcb = sorted(root.rglob("*.kicad_pcb"))
    pcb_files = [p for p in all_pcb if not is_backup_path(p.relative_to(root))]
    skipped = len(all_pcb) - len(pcb_files)
    if skipped:
        print(f"  (skipping {skipped} board(s) under backup/autosave folders)")
    if not pcb_files:
        sys.exit(f"No .kicad_pcb files found under {root}")

    for pcb_path in pcb_files:
        board = project_label(pcb_path, root)
        try:
            data = sexpdata.loads(pcb_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            print(f"  ! skipping {pcb_path} (parse error: {e})", file=sys.stderr)
            continue

        version = next(find_direct_children_tagged(data, "version"), [None, 20241229])[1]
        generator = next(find_direct_children_tagged(data, "generator"), [None, "pcbnew"])[1]
        generator_version = next(find_direct_children_tagged(data, "generator_version"), [None, "9.0"])[1]
        header_fields = [
            [Symbol("version"), version],
            [Symbol("generator"), generator],
            [Symbol("generator_version"), generator_version],
        ]

        fp_nodes = list(find_direct_children_tagged(data, "footprint"))
        if not fp_nodes:
            continue

        for fp_node in fp_nodes:
            if len(fp_node) < 2 or not isinstance(fp_node[1], str):
                continue
            lib_id = fp_node[1]
            usage.setdefault(lib_id, set()).add(board)
            normalized = normalize_footprint(fp_node, header_fields)

            if lib_id not in merged:
                merged[lib_id] = normalized
            elif sexpdata.dumps(normalized) != sexpdata.dumps(merged[lib_id]):
                conflicts.append(
                    f"CONFLICT: '{lib_id}' differs between boards "
                    f"(keeping first occurrence; seen again on '{board}'). "
                    f"Review manually."
                )

        print(f"  {pcb_path.relative_to(root)}  [{board}]  ({len(fp_nodes)} footprints)")

    return merged, usage, conflicts


def build_new_name(lib_id: str, used_names: set) -> str:
    """Strip the 'OriginalLib:' prefix for the new library's footprint name,
    disambiguating if two different source libs had a same-named part."""
    if ":" in lib_id:
        lib, name = lib_id.split(":", 1)
    else:
        lib, name = "Unknown", lib_id

    candidate = name
    if candidate in used_names:
        candidate = f"{name}_{lib}"
    n = 1
    base = candidate
    while candidate in used_names:
        n += 1
        candidate = f"{base}_{n}"
    used_names.add(candidate)
    return candidate


def write_library(out_dir: Path, merged: dict, name_map: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    for lib_id, node in merged.items():
        new_name = name_map[lib_id]
        node = list(node)
        node[1] = new_name
        (out_dir / f"{new_name}.kicad_mod").write_text(sexpdata.dumps(node), encoding="utf-8")


def write_report(report_path: Path, merged: dict, usage: dict, name_map: dict, conflicts: list):
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["original_lib_id", "new_footprint_name", "used_on_boards"])
        for lib_id in sorted(merged):
            w.writerow([lib_id, name_map[lib_id], "; ".join(sorted(usage[lib_id]))])
    if conflicts:
        with open(str(report_path).replace(".csv", "_conflicts.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(conflicts))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="Folder containing last year's KiCad projects (searched recursively)")
    ap.add_argument("--out", type=Path, default=Path("baja-common-footprints"),
                     help="Output basename (writes <out>.pretty/ and <out>_report.csv)")
    args = ap.parse_args()

    root = args.root.resolve()
    print(f"Scanning {root} for .kicad_pcb files...")
    merged, usage, conflicts = harvest(root)

    used_names = set()
    name_map = {lib_id: build_new_name(lib_id, used_names) for lib_id in merged}

    pretty_out = args.out.with_suffix(".pretty")
    report_out = Path(str(args.out) + "_report.csv")
    write_library(pretty_out, merged, name_map)
    write_report(report_out, merged, usage, name_map, conflicts)

    print(f"\nFound {len(merged)} unique footprints across {len(set(b for s in usage.values() for b in s))} board(s).")
    print(f"Wrote: {pretty_out}/")
    print(f"Wrote: {report_out}")
    if conflicts:
        print(f"!! {len(conflicts)} conflict(s) -- see {str(report_out).replace('.csv', '_conflicts.txt')}")


if __name__ == "__main__":
    main()
