#!/usr/bin/env python3
"""
build_baja_common_library.py

Final curation pass that turns the raw harvest of every symbol/footprint
used on the 2026 (Dingo) Baja electronics boards into a clean, deduplicated,
functional shared KiCad library.

What this does beyond the raw harvest:
  1. Skips KiCad autosave/backup snapshot folders as separate "boards".
  2. Drops verbatim copies of official KiCad stock libraries (Device,
     Connector, power, Capacitor_SMD, etc.) -- every member's KiCad install
     already has these; bundling frozen copies only adds bloat and risks
     drifting from the real thing as KiCad updates.
  3. For the remaining custom/vendor parts, merges entries that are the same
     physical part filed under different ad-hoc per-project library names
     (e.g. 'baja:917782-1' and 'baja_symbol_library:917782-1' are the same
     TE Connectivity connector, cached under two different old project
     libraries) into one canonical entry.
  4. Where a part's *content* genuinely differs across boards (a real
     revision, not just a naming duplicate), keeps the version from the
     most-recently-edited source file as canonical.
  5. Fixes broken names (stray Unicode control characters, stray spaces,
     an orphaned symbol with no library prefix at all).
  6. Repairs each symbol's Footprint association to point at the correct
     footprint in this new library where one exists, using the new
     library's nickname, instead of the old dangling/unprefixed reference.

Run: python build_baja_common_library.py

NOTE: ROOT/OUT_DIR and the SYMBOL_ALIASES / FOOTPRINT_ALIASES / DROP_SYMBOLS /
SYMBOL_FOOTPRINT_FIX tables below reflect a one-time manual review of the
2026 (Dingo) source boards specifically -- they are not a generic "rerun
blindly on next year's boards" tool. If you're harvesting a new batch of
boards later, use harvest_kicad_parts.py / harvest_kicad_footprints.py
first, review the resulting *_report_conflicts.txt by hand the way this
file's history did, and adapt the tables here (or write a fresh pass).
"""
import csv
import datetime
import re
import unicodedata
from pathlib import Path

import sexpdata
from sexpdata import Symbol

ROOT = Path(r"C:\Users\alexh\OneDrive - Northwestern University\NU Baja SAE\Solidworks\2026 - Dingo\Electronics")
OUT_DIR = Path(r"C:\Users\alexh\OneDrive\bajashit\hardware\libraries")
LIB_NICKNAME = "baja-common-parts"

STOCK_SYM_NS = {
    "Connector", "Connector_Generic", "Device", "Diode", "Interface_CAN_LIN",
    "Jumper", "Mechanical", "Memory_EEPROM", "Power_Protection", "RF_Module",
    "Regulator_Switching", "SD_Card", "Sensor_Gas", "Sensor_Magnetic",
    "Sensor_Motion", "Simulation_SPICE", "Switch", "Transistor_FET", "power",
}
STOCK_FP_NS = {
    "Capacitor_SMD", "Connector", "Connector_PinHeader_2.54mm",
    "Connector_PinSocket_2.54mm", "Converter_DCDC", "Diode_SMD", "Fuse",
    "LED_SMD", "MountingHole", "Package_LGA", "Package_SO",
    "Package_TO_SOT_SMD", "Potentiometer_THT", "Resistor_SMD",
    "Transistor_Power",
}

# canonical_name -> [old lib_ids that are the same physical part, merged into one]
SYMBOL_ALIASES = {
    "917782-1": ["baja_symbol_library:917782-1", "baja:917782-1", "Connector_1x4:917782-1"],
    "917784-1": ["baja_symbol_library:917784-1", "Connector_1x6:917784-1"],
    "917783-1": ["Connector_1x5 v2:917783-1"],
}
FOOTPRINT_ALIASES = {
    "ACT45B-101-2P-TL003": ["baja_footprint_library:ACT45B-101-2P-TL003", "baja:ACT45B-101-2P-TL003"],
    "MOLEX_15311026": ["baja_footprint_library:MOLEX_15311026", "baja:MOLEX_15311026"],
    "TE-Connectivity_917780": ["baja_footprint_library:TE-Connectivity_ 917780", "baja:TE-Connectivity_ 917780"],
    "TE-Connectivity_917782": ["baja_footprint_library:TE-Connectivity_917782", "baja:TE-Connectivity_917782"],
    "baja_logo_footprint": ["baja_footprint_library:baja_logo_footprint", "baja:baja_logo_footprint"],
    "917783-1": ["baja_footprint_library:Connector_1x5"],  # renamed to match its symbol's MPN
}

# symbols to drop outright: no valid "Library:Name" lib_id (orphaned by a bad save).
# Compared against the *cleaned* name (see clean_name below), so no stray
# Unicode control chars here.
DROP_SYMBOLS = {"BL99232CH_1"}

# symbol canonical name -> new Footprint field value (name only, gets our nickname prefix).
# Built from cross-referencing each symbol's MPN against the footprints actually
# harvested from real PCBs -- fixes Footprint fields that were previously blank
# or pointed at a bare, unresolvable name.
SYMBOL_FOOTPRINT_FIX = {
    "917780-1": "CON2_1X2_P100",
    "917781-1": "CON3_1X3_P100",
    "917782-1": "CON4_1X4_P100",
    "917783-1": "917783-1",
    "917784-1": "TE-Connectivity_1x6",
    "917786-1": "TE-Connectivity_917786",
    "917791-1": "TE-Connectivity_917791",
    "ACT45B-101-2P-TL003": "ACT45B-101-2P-TL003",
    "TEA_1-0505": "CONV_TEA_1-0505",
    "TSR_1-24120": "TSR1-SINGLE_TRP",
    "NAU7802SGI": "SOI16_NAU7802SGI_NUV",
}

AD_HOC_FOOTPRINTS = {  # hand-drawn generic footprints, likely redundant with stock PinHeader/Socket libs
    "CON2_1X2_P100", "CON3_1X3_P100", "CON3_1X3_P100_KiCADv6", "CON4_1X4_P100",
}


def clean_name(name: str) -> str:
    """Strip Unicode control/format chars (e.g. stray U+200E) and surrounding
    whitespace that crept into names via bad copy/paste in the original libs."""
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    return name.strip()


def is_backup_path(p: Path) -> bool:
    for part in p.parts:
        low = part.lower()
        if "backup" in low or part.startswith("_autosave-") or low.endswith("-bak"):
            return True
    return False


def is_tagged(node, tag):
    return isinstance(node, list) and len(node) > 0 and isinstance(node[0], Symbol) and str(node[0]) == tag


def find_direct_children_tagged(node, tag):
    if not isinstance(node, list):
        return
    for child in node:
        if is_tagged(child, tag):
            yield child


def project_label(path: Path, root: Path) -> str:
    for parent in [path.parent, *path.parents]:
        if parent == root.parent:
            break
        if list(parent.glob("*.kicad_pro")):
            return parent.name
    return path.parent.name


def strip_pad_nets(node):
    if not isinstance(node, list):
        return node
    if is_tagged(node, "pad"):
        return [ch for ch in (strip_pad_nets(c) for c in node) if not is_tagged(ch, "net")]
    return [strip_pad_nets(c) for c in node]


# ---------------------------------------------------------------- symbols --

def scan_symbols():
    occ = {}  # lib_id -> list of (mtime, board, path, node)
    for sch in sorted(ROOT.rglob("*.kicad_sch")):
        if is_backup_path(sch.relative_to(ROOT)):
            continue
        try:
            data = sexpdata.loads(sch.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        board = project_label(sch, ROOT)
        mtime = sch.stat().st_mtime
        for block in find_direct_children_tagged(data, "lib_symbols"):
            for sym_node in find_direct_children_tagged(block, "symbol"):
                if len(sym_node) < 2 or not isinstance(sym_node[1], str):
                    continue
                lib_id = sym_node[1]
                ns = lib_id.split(":", 1)[0] if ":" in lib_id else None
                if ns in STOCK_SYM_NS:
                    continue
                occ.setdefault(lib_id, []).append((mtime, board, str(sch), sym_node))
    return occ


def strip_embedded_files(node):
    """Drop (embedded_files ...) blocks -- KiCad lets you attach a PDF/3D file
    directly inside a symbol, and at least one harvested part (DRV8452DDWR)
    has a whole datasheet PDF embedded this way, ballooning it from a few KB
    to 5+ MB. A shared parts library shouldn't carry that; the Datasheet URL
    property already covers documentation, and every member paying a 5 MB
    tax on library sync for one adapter-board part isn't worth it."""
    return [c for c in node if not is_tagged(c, "embedded_files")]


def set_property(node, prop_name, new_value):
    node = list(node)
    for i, c in enumerate(node):
        if isinstance(c, list) and len(c) > 2 and is_tagged(c, "property") and c[1] == prop_name:
            c = list(c)
            c[2] = new_value
            node[i] = c
            break
    return node


def build_symbol_library(occ):
    alias_of = {}  # raw lib_id -> canonical name
    for canon, raws in SYMBOL_ALIASES.items():
        for r in raws:
            alias_of[r] = canon

    groups = {}  # canonical name -> list of (mtime, board, path, node, raw_lib_id)
    for lib_id, insts in occ.items():
        name = lib_id.split(":", 1)[1] if ":" in lib_id else lib_id
        name = clean_name(name)
        if name in DROP_SYMBOLS or clean_name(lib_id.split(":", 1)[-1]) in DROP_SYMBOLS:
            continue
        canon = alias_of.get(lib_id, name)
        canon = clean_name(canon)
        for mtime, board, path, node in insts:
            groups.setdefault(canon, []).append((mtime, board, path, node, lib_id))

    report_rows = []
    lib_body = [Symbol("kicad_symbol_lib"), [Symbol("version"), 20231120],
                [Symbol("generator"), "build_baja_common_library"]]

    for canon in sorted(groups):
        insts = groups[canon]
        insts.sort(key=lambda t: -t[0])
        winner_mtime, winner_board, winner_path, winner_node, winner_libid = insts[0]
        distinct_hashes = {sexpdata.dumps(n) for _, _, _, n, _ in insts}
        raw_libids = sorted({r for *_, r in insts})
        boards = sorted({b for _, b, *_ in insts})

        node = strip_embedded_files(winner_node)
        node[1] = canon
        if canon in SYMBOL_FOOTPRINT_FIX:
            node = set_property(node, "Footprint", f"{LIB_NICKNAME}:{SYMBOL_FOOTPRINT_FIX[canon]}")
        lib_body.append(node)

        report_rows.append({
            "symbol": canon,
            "source_lib_ids": "; ".join(raw_libids),
            "used_on_boards": "; ".join(boards),
            "content_variants": len(distinct_hashes),
            "canonical_from": f"{winner_board} ({datetime.datetime.fromtimestamp(winner_mtime):%Y-%m-%d})",
            "merged_aliases": len(raw_libids) > 1,
        })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{LIB_NICKNAME}.kicad_sym").write_text(sexpdata.dumps(lib_body), encoding="utf-8")
    return report_rows


# -------------------------------------------------------------- footprints --

def scan_footprints():
    occ = {}
    for pcb in sorted(ROOT.rglob("*.kicad_pcb")):
        if is_backup_path(pcb.relative_to(ROOT)):
            continue
        try:
            data = sexpdata.loads(pcb.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        board = project_label(pcb, ROOT)
        mtime = pcb.stat().st_mtime
        version = next(find_direct_children_tagged(data, "version"), [None, 20241229])[1]
        generator = next(find_direct_children_tagged(data, "generator"), [None, "pcbnew"])[1]
        generator_version = next(find_direct_children_tagged(data, "generator_version"), [None, "9.0"])[1]
        header_fields = [[Symbol("version"), version], [Symbol("generator"), generator],
                          [Symbol("generator_version"), generator_version]]
        for fp_node in find_direct_children_tagged(data, "footprint"):
            if len(fp_node) < 2 or not isinstance(fp_node[1], str):
                continue
            lib_id = fp_node[1]
            ns = lib_id.split(":", 1)[0] if ":" in lib_id else None
            if ns in STOCK_FP_NS:
                continue
            rest = [c for c in fp_node[2:] if not (is_tagged(c, "uuid") or
                    (isinstance(c, list) and len(c) > 0 and isinstance(c[0], Symbol)
                     and str(c[0]) in {"uuid", "at", "path", "sheetname", "sheetfile", "tstamp"}))]
            rest = strip_pad_nets(rest)
            normalized = [Symbol("footprint"), lib_id, *header_fields, *rest]
            occ.setdefault(lib_id, []).append((mtime, board, str(pcb), normalized))
    return occ


def build_footprint_library(occ):
    alias_of = {}
    for canon, raws in FOOTPRINT_ALIASES.items():
        for r in raws:
            alias_of[r] = canon

    groups = {}
    for lib_id, insts in occ.items():
        name = lib_id.split(":", 1)[1] if ":" in lib_id else lib_id
        name = clean_name(name)
        canon = clean_name(alias_of.get(lib_id, name))
        for mtime, board, path, node in insts:
            groups.setdefault(canon, []).append((mtime, board, path, node, lib_id))

    pretty_dir = OUT_DIR / f"{LIB_NICKNAME}.pretty"
    pretty_dir.mkdir(parents=True, exist_ok=True)

    report_rows = []
    for canon in sorted(groups):
        insts = groups[canon]
        insts.sort(key=lambda t: -t[0])
        winner_mtime, winner_board, winner_path, winner_node, winner_libid = insts[0]
        distinct_hashes = {sexpdata.dumps(n) for _, _, _, n, _ in insts}
        raw_libids = sorted({r for *_, r in insts})
        boards = sorted({b for _, b, *_ in insts})

        node = strip_embedded_files(winner_node)
        node[1] = canon
        (pretty_dir / f"{canon}.kicad_mod").write_text(sexpdata.dumps(node), encoding="utf-8")

        report_rows.append({
            "footprint": canon,
            "source_lib_ids": "; ".join(raw_libids),
            "used_on_boards": "; ".join(boards),
            "content_variants": len(distinct_hashes),
            "canonical_from": f"{winner_board} ({datetime.datetime.fromtimestamp(winner_mtime):%Y-%m-%d})",
            "merged_aliases": len(raw_libids) > 1,
            "ad_hoc_generic": canon in AD_HOC_FOOTPRINTS,
        })
    return report_rows


def main():
    sym_occ = scan_symbols()
    fp_occ = scan_footprints()
    sym_rows = build_symbol_library(sym_occ)
    fp_rows = build_footprint_library(fp_occ)

    with open(OUT_DIR / "curation_report_symbols.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(sym_rows[0].keys()))
        w.writeheader()
        w.writerows(sym_rows)
    with open(OUT_DIR / "curation_report_footprints.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(fp_rows[0].keys()))
        w.writeheader()
        w.writerows(fp_rows)

    print(f"Symbols:    {len(sym_rows)} final ({sum(r['merged_aliases'] for r in sym_rows)} merged from aliases, "
          f"{sum(1 for r in sym_rows if r['content_variants'] > 1)} had real content conflicts)")
    print(f"Footprints: {len(fp_rows)} final ({sum(r['merged_aliases'] for r in fp_rows)} merged from aliases, "
          f"{sum(1 for r in fp_rows if r['content_variants'] > 1)} had real content conflicts)")
    print(f"Wrote {OUT_DIR / (LIB_NICKNAME + '.kicad_sym')}")
    print(f"Wrote {OUT_DIR / (LIB_NICKNAME + '.pretty')}")


if __name__ == "__main__":
    main()
