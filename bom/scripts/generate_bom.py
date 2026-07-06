#!/usr/bin/env python3
"""
generate_bom.py

Generates bom/consolidated-bom.csv -- one bill-of-materials line item per
distinct part per board, across every board under boards/ -- straight from
the KiCad schematic source files (*.kicad_sch). No KiCad installation is
needed: the schematics are parsed directly as S-expressions, so this runs
identically on a teammate's laptop and in CI. No third-party dependencies.

What counts as a BOM component:
  - every schematic symbol with `in_bom yes` and a reference that is not a
    virtual one (power symbols, PWR_FLAG, and anything else whose reference
    starts with '#' are skipped)
  - DNP (do-not-populate) parts are excluded from quantities; they are
    listed on stderr so exclusions are visible, never silent
  - multi-unit symbols (e.g. one op-amp package drawn as several triangles)
    are counted once per reference, not once per drawn unit
  - hierarchical sheets used more than once are handled via each symbol's
    `instances` block, which carries one reference per instantiation

Grouping: parts are grouped per board by part number. If a symbol carries an
explicit part-number property (MPN / Part Number / PN and similar), that is
used; otherwise a part number is synthesized from the Value plus the package
size parsed out of the footprint (e.g. "1k 0603"), which is exactly the
granularity you order passives at. Reference designators for each group land
in the notes column so a line item can be traced back to the schematic.

USAGE:
    python3 bom/scripts/generate_bom.py            # (re)write consolidated-bom.csv
    python3 bom/scripts/generate_bom.py --check    # exit 1 if the committed CSV is stale

CI runs --check on every PR; if it fails, regenerate and commit:
    python3 bom/scripts/generate_bom.py
    git add bom/consolidated-bom.csv
"""

import argparse
import csv
import io
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

HEADER = ["part_number", "description", "manufacturer", "quantity", "board", "notes"]

# Symbol property names (case-insensitive) accepted as an explicit part number
# or manufacturer. Different libraries name these fields differently.
PART_NUMBER_PROPS = ("mpn", "manufacturer part number", "mfr part number",
                     "mfr. part number", "part number", "pn", "part_number")
MANUFACTURER_PROPS = ("manufacturer", "mfr", "mfg", "mfr.")

# Package size embedded in standard footprint names, e.g. R_0603_1608Metric_...
PACKAGE_RE = re.compile(r"_(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)_")

SKIP_DIR_MARKERS = ("backup", "-bak")  # same convention as libraries/tools lints


def is_backup_path(p: Path) -> bool:
    return any(m in part.lower() or part.startswith("_autosave-")
               for part in p.parts for m in SKIP_DIR_MARKERS)


# --- Minimal KiCad S-expression parser -------------------------------------

def _tokenize(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "()":
            yield c
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            yield "".join(buf)
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            yield text[i:j]
            i = j


def parse_sexpr(text: str) -> list:
    """Parses S-expression text into nested lists of strings."""
    stack = [[]]
    for tok in _tokenize(text):
        if tok == "(":
            node = []
            stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced ')' in schematic")
            stack.pop()
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in schematic")
    return stack[0]


def _children(node, tag):
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _child(node, tag):
    found = _children(node, tag)
    return found[0] if found else None


def _flag(node, tag, default="no"):
    c = _child(node, tag)
    return (c[1] if c and len(c) > 1 else default) == "yes"


# --- Schematic -> components ------------------------------------------------

def _properties(sym) -> dict:
    return {p[1]: p[2] for p in _children(sym, "property") if len(p) >= 3}


def _references(sym, props) -> list:
    """All reference designators this symbol node is instantiated as."""
    refs = []
    inst = _child(sym, "instances")
    if inst:
        for project in _children(inst, "project"):
            for path in _children(project, "path"):
                ref = _child(path, "reference")
                if ref and len(ref) > 1:
                    refs.append(ref[1])
    if not refs and props.get("Reference"):
        refs.append(props["Reference"])
    return refs


def _lookup(props: dict, names) -> str:
    lowered = {k.lower(): v for k, v in props.items()}
    for name in names:
        if lowered.get(name):
            return lowered[name].strip()
    return ""


def extract_components(sch_path: Path):
    """Yields (ref, dnp, part_number, description, manufacturer) per reference."""
    tree = parse_sexpr(sch_path.read_text(encoding="utf-8", errors="replace"))
    root = next((n for n in tree if isinstance(n, list) and n and n[0] == "kicad_sch"), None)
    if root is None:
        raise ValueError(f"{sch_path}: not a kicad_sch file")

    for sym in _children(root, "symbol"):
        lib_id_node = _child(sym, "lib_id")
        if lib_id_node is None:  # lib_symbols definitions have no lib_id at this level
            continue
        if not _flag(sym, "in_bom", default="yes"):
            continue
        props = _properties(sym)
        dnp = _flag(sym, "dnp")

        value = props.get("Value", "").strip()
        footprint = props.get("Footprint", "").strip()

        part_number = _lookup(props, PART_NUMBER_PROPS)
        if not part_number:
            pkg = PACKAGE_RE.search(footprint)
            part_number = f"{value} {pkg.group(1)}" if pkg else value

        description = props.get("Description", "").strip()
        manufacturer = _lookup(props, MANUFACTURER_PROPS)

        for ref in _references(sym, props):
            if ref.startswith("#"):  # power symbols, PWR_FLAG, etc.
                continue
            yield ref, dnp, part_number, description, manufacturer


def _ref_sort_key(ref: str):
    m = re.match(r"([A-Za-z_]*)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0), ref)


def collect_rows(boards_dir: Path):
    """Returns consolidated BOM rows (list of dicts keyed by HEADER)."""
    rows = []
    excluded_dnp = []

    board_dirs = sorted(d for d in boards_dir.iterdir() if d.is_dir())
    for board_dir in board_dirs:
        board = board_dir.name
        # ref -> (part_number, description, manufacturer); dict dedupes
        # multi-unit symbols, which repeat the same reference
        components = {}
        sch_files = sorted(p for p in board_dir.rglob("*.kicad_sch")
                           if not is_backup_path(p.relative_to(board_dir)))
        for sch in sch_files:
            for ref, dnp, part_number, description, manufacturer in extract_components(sch):
                if dnp:
                    excluded_dnp.append((board, ref))
                    continue
                components.setdefault(ref, (part_number, description, manufacturer))

        groups = {}
        for ref, key in components.items():
            groups.setdefault(key, []).append(ref)

        for (part_number, description, manufacturer), refs in sorted(groups.items()):
            refs = sorted(refs, key=_ref_sort_key)
            rows.append({
                "part_number": part_number,
                "description": description,
                "manufacturer": manufacturer,
                "quantity": len(refs),
                "board": board,
                "notes": " ".join(refs),
            })

    if excluded_dnp:
        print("Excluded DNP (do-not-populate) parts:", file=sys.stderr)
        for board, ref in excluded_dnp:
            print(f"  {board}: {ref}", file=sys.stderr)
    return rows


def render_csv(rows) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[3])
    parser.add_argument("--boards-dir", type=Path, default=REPO_ROOT / "boards",
                        help="folder containing one subfolder per board (default: boards/)")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "bom" / "consolidated-bom.csv",
                        help="CSV to write (default: bom/consolidated-bom.csv)")
    parser.add_argument("--check", action="store_true",
                        help="don't write; exit 1 if the existing CSV doesn't match what "
                             "would be generated (used by CI)")
    args = parser.parse_args(argv)

    if not args.boards_dir.is_dir():
        print(f"No such folder: {args.boards_dir}", file=sys.stderr)
        return 2

    generated = render_csv(collect_rows(args.boards_dir))

    if args.check:
        existing = args.output.read_text(encoding="utf-8").replace("\r\n", "\n") \
            if args.output.exists() else ""
        if existing == generated:
            print(f"OK: {args.output} is up to date.")
            return 0
        import difflib
        sys.stdout.writelines(difflib.unified_diff(
            existing.splitlines(keepends=True), generated.splitlines(keepends=True),
            fromfile=f"{args.output} (committed)", tofile=f"{args.output} (regenerated)"))
        print(f"\n{args.output} is stale. Regenerate it and commit the result:\n"
              f"    python3 bom/scripts/generate_bom.py", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(generated)
    n_items = generated.count("\n") - 1
    print(f"Wrote {args.output} ({n_items} line item{'s' if n_items != 1 else ''}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
