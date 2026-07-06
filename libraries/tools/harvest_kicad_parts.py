#!/usr/bin/env python3
"""
harvest_kicad_parts.py

Build a single, standardized KiCad symbol library from every symbol actually
used across a folder of existing KiCad projects (e.g. last year's boards
synced down from OneDrive).

WHY THIS WORKS WITHOUT TOUCHING YOUR ORIGINAL LIBRARIES:
Every .kicad_sch file (KiCad 6+) embeds a self-contained cache of every
symbol used in that sheet, under a top-level (lib_symbols ...) block. That
cache is a complete, standalone copy of the symbol -- no need to resolve
sym-lib-table entries or chase down whatever libraries each board originally
pointed at (which may not even be present/intact anymore). This script just
walks every .kicad_sch it finds, pulls those cached symbol definitions out,
de-duplicates them by lib_id ("OriginalLib:PartName"), and writes them into
one new .kicad_sym library.

USAGE:
    python3 harvest_kicad_parts.py /path/to/onedrive/boards/root --out ./baja-common-parts

OUTPUT:
    <out>.kicad_sym   - merged symbol library, ready to add to a sym-lib-table
    <out>_report.csv  - every part found, which board(s) used it, and any
                         naming collisions or content conflicts to review

NOTE ON FOOTPRINTS:
This script only handles symbols. For footprints, use KiCad's own built-in
exporter per board (PCB Editor -> File -> Export -> Footprints to New
Library), then merge the resulting .pretty folders by copying the
.kicad_mod files into one folder -- footprints are self-contained per-file,
so merging is just a file copy plus checking for name collisions.
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


def is_tagged(node, tag):
    return isinstance(node, list) and len(node) > 0 and isinstance(node[0], Symbol) and str(node[0]) == tag


def find_direct_children_tagged(node, tag):
    """Yield immediate children of `node` tagged with `tag` (non-recursive)."""
    if not isinstance(node, list):
        return
    for child in node:
        if is_tagged(child, tag):
            yield child


def project_label(sch_path: Path, root: Path) -> str:
    """Best-effort label for which 'board'/project a schematic belongs to:
    the nearest .kicad_pro's folder name, else the immediate parent folder."""
    for parent in [sch_path.parent, *sch_path.parents]:
        if parent == root.parent:
            break
        if list(parent.glob("*.kicad_pro")):
            return parent.name
    return sch_path.parent.name


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
    """Returns (merged_symbols: dict[lib_id -> sexpr node],
                usage: dict[lib_id -> set of board labels],
                conflicts: list[str] human-readable notes)"""
    merged = {}
    usage = {}
    conflicts = []

    all_sch = sorted(root.rglob("*.kicad_sch"))
    sch_files = [p for p in all_sch if not is_backup_path(p.relative_to(root))]
    skipped = len(all_sch) - len(sch_files)
    if skipped:
        print(f"  (skipping {skipped} schematic(s) under backup/autosave folders)")
    if not sch_files:
        sys.exit(f"No .kicad_sch files found under {root}")

    for sch_path in sch_files:
        board = project_label(sch_path, root)
        try:
            data = sexpdata.loads(sch_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            print(f"  ! skipping {sch_path} (parse error: {e})", file=sys.stderr)
            continue

        lib_symbols_blocks = list(find_direct_children_tagged(data, "lib_symbols"))
        if not lib_symbols_blocks:
            continue

        for block in lib_symbols_blocks:
            for sym_node in find_direct_children_tagged(block, "symbol"):
                # (symbol "LibName:PartName" ...)
                if len(sym_node) < 2 or not isinstance(sym_node[1], str):
                    continue
                lib_id = sym_node[1]
                usage.setdefault(lib_id, set()).add(board)

                if lib_id not in merged:
                    merged[lib_id] = sym_node
                else:
                    # Same lib_id seen before -- check whether the cached
                    # definition actually matches. If a board's cache
                    # diverged (e.g. someone hand-edited the symbol locally),
                    # flag it instead of silently picking one.
                    if sexpdata.dumps(sym_node) != sexpdata.dumps(merged[lib_id]):
                        conflicts.append(
                            f"CONFLICT: '{lib_id}' differs between boards "
                            f"(keeping first occurrence; seen again on '{board}'). "
                            f"Review manually."
                        )

        print(f"  {sch_path.relative_to(root)}  [{board}]  "
              f"({len(list(find_direct_children_tagged(lib_symbols_blocks[0], 'symbol')))} symbols)")

    return merged, usage, conflicts


def build_new_name(lib_id: str, used_names: set) -> str:
    """Strip the 'OriginalLib:' prefix for the new library's symbol name,
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


def rename_symbol_node(sym_node, new_name):
    new_node = list(sym_node)
    new_node[1] = new_name
    # Sub-units like "R_0_1" are scoped under the parent and don't need
    # renaming for the library to be valid; KiCad references them by the
    # parent symbol's lib_id at use-time, not by sub-unit name.
    return new_node


def write_library(out_path: Path, merged: dict, name_map: dict):
    body = [
        Symbol("kicad_symbol_lib"),
        [Symbol("version"), 20231120],
        [Symbol("generator"), "harvest_kicad_parts"],
    ]
    for lib_id, sym_node in merged.items():
        body.append(rename_symbol_node(sym_node, name_map[lib_id]))
    out_path.write_text(sexpdata.dumps(body), encoding="utf-8")


def write_report(report_path: Path, merged: dict, usage: dict, name_map: dict, conflicts: list):
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["original_lib_id", "new_symbol_name", "used_on_boards"])
        for lib_id in sorted(merged):
            w.writerow([lib_id, name_map[lib_id], "; ".join(sorted(usage[lib_id]))])
    if conflicts:
        with open(str(report_path).replace(".csv", "_conflicts.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(conflicts))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="Folder containing last year's KiCad projects (searched recursively)")
    ap.add_argument("--out", type=Path, default=Path("baja-common-parts"),
                     help="Output basename (writes <out>.kicad_sym and <out>_report.csv)")
    args = ap.parse_args()

    root = args.root.resolve()
    print(f"Scanning {root} for .kicad_sch files...")
    merged, usage, conflicts = harvest(root)

    used_names = set()
    name_map = {lib_id: build_new_name(lib_id, used_names) for lib_id in merged}

    sym_out = args.out.with_suffix(".kicad_sym")
    report_out = Path(str(args.out) + "_report.csv")
    write_library(sym_out, merged, name_map)
    write_report(report_out, merged, usage, name_map, conflicts)

    print(f"\nFound {len(merged)} unique symbols across {len(set(b for s in usage.values() for b in s))} board(s).")
    print(f"Wrote: {sym_out}")
    print(f"Wrote: {report_out}")
    if conflicts:
        print(f"!! {len(conflicts)} conflict(s) -- see {str(report_out).replace('.csv', '_conflicts.txt')}")


if __name__ == "__main__":
    main()
