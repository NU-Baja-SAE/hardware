#!/usr/bin/env python3
"""
extract_connectors.py

Generates one harness YAML per board describing that board's OFF-BOARD
connectors -- the wires that leave the board and become part of the car
wiring harness. Everything internal to a board is ignored.

How it works:
  1. For each board under boards/, run `kicad-cli sch export netlist` on the
     board's root schematic to get an authoritative pin->net table. KiCad's
     own netlister resolves connectivity (wires/labels/junctions across every
     sub-sheet), so we never re-implement that.  Symbols resolve from each
     schematic's embedded lib_symbols cache, so no library table / BAJA_LIB
     env var is required -- this runs identically on a laptop and in CI.
  2. Keep only components whose part number is in one of the harness connector
     families (TE 917xxx, Molex 15311026/15311046 -- see OFF_BOARD_FAMILIES).
     These are the physical connectors the harness mates to. Everything else
     (ESP32 socket, SD card, generic headers, ICs) is board-internal.
  3. Emit one component per physical connector, with a pin per connector pin,
     named by the net on that pin. The leaf of KiCad's hierarchical net name
     is used as the signal (e.g. "/Connector Bank/CANH" -> "CANH").

Output: harness/generated/<board>.yaml, one `connector` component per
off-board connector on that board. These files are BUILD ARTIFACTS -- never
hand-edit them; edit the board schematic and regenerate. The hand-maintained
backbone (harness/car.yaml) `includes:` these and declares the inter-board
wiring.

USAGE:
    python3 harness/scripts/extract_connectors.py            # write generated/*.yaml
    python3 harness/scripts/extract_connectors.py --check    # exit 1 if stale (CI)

Requires kicad-cli on PATH (KiCad 9). Override with --kicad-cli.
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Off-board harness connector families, matched against a component's part
# number (netlist `value` / libsource `part`). An explicit allow-list of the
# two families actually used on the boards: TE Connectivity 917xxx (signal +
# some power) and Molex 15311026/15311046 (battery / 24V power). Adding a new
# harness connector P/N is a one-line change here.
OFF_BOARD_FAMILIES = (
    re.compile(r"^917\d{3}(-\d+)?$"),   # TE Connectivity 917780-1 .. 917791-1
    re.compile(r"^15311026$|^15311046$"),  # Molex 15311026 / 15311046 (power)
)


def is_off_board_part(part: str) -> bool:
    part = (part or "").strip()
    return any(rx.match(part) for rx in OFF_BOARD_FAMILIES)


# --- Minimal KiCad S-expression parser (shared shape with bom scripts) ------

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
            yield ('"', "".join(buf))  # tagged so quoted "" isn't lost
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            yield ("t", text[i:j])
            i = j


def parse_sexpr(text: str) -> list:
    stack = [[]]
    for tok in _tokenize(text):
        if tok == "(":
            node = []
            stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced ')' in netlist")
            stack.pop()
        else:
            stack[-1].append(tok[1])
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in netlist")
    return stack[0]


def _children(node, tag):
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _child(node, tag):
    found = _children(node, tag)
    return found[0] if found else None


def _val(node, tag):
    """Value of `(tag "value")` or `(tag value)`; "" if absent."""
    c = _child(node, tag)
    return c[1] if c and len(c) > 1 else ""


# --- Netlist -> connectors --------------------------------------------------

def run_netlist(sch: Path, kicad_cli: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "net.net"
        proc = subprocess.run(
            [kicad_cli, "sch", "export", "netlist",
             "--format", "kicadsexpr", "-o", str(out), str(sch)],
            capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(
                f"kicad-cli netlist export failed for {sch}:\n{proc.stderr}")
        return out.read_text(encoding="utf-8", errors="replace")


def signal_from_net_name(net_name: str) -> str:
    """Leaf of a hierarchical net name: /Connector Bank/CANH -> CANH.

    Unnamed nets (KiCad auto-names like "Net-(J2-Pad1)") are kept verbatim so
    they're still traceable; the backbone author can rename them if needed.
    KiCad escapes '/' inside a label as '{slash}' -- restore it.
    """
    name = net_name.strip()
    leaf = name.rsplit("/", 1)[-1] if name.startswith("/") else name
    return leaf.replace("{slash}", "/")


def extract_board(netlist_text: str):
    """Returns {conn_ref: {"part": str, "pins": {pin_no: signal}}} for every
    off-board connector in one board's netlist."""
    tree = parse_sexpr(netlist_text)
    root = next((n for n in tree
                 if isinstance(n, list) and n and n[0] == "export"), None)
    if root is None:
        raise ValueError("not a kicad netlist (no `export` root)")

    # 1. Which refs are off-board connectors, and their part number.
    comps = _child(root, "components")
    off_board = {}  # ref -> part
    for comp in _children(comps or [], "comp"):
        ref = _val(comp, "ref")
        part = _val(comp, "value")
        libsource = _child(comp, "libsource")
        if not is_off_board_part(part) and libsource is not None:
            part = _val(libsource, "part") or part  # fall back to libsource
        if is_off_board_part(part):
            off_board[ref] = part

    # 2. Walk nets; record every node that lands on an off-board connector.
    connectors = {ref: {"part": part, "pins": {}}
                  for ref, part in off_board.items()}
    nets = _child(root, "nets")
    for net in _children(nets or [], "net"):
        signal = signal_from_net_name(_val(net, "name"))
        for node in _children(net, "node"):
            ref = _val(node, "ref")
            if ref in connectors:
                pin = _val(node, "pin")
                try:
                    pin_no = int(pin)
                except ValueError:
                    continue  # non-numeric pin id; skip
                connectors[ref]["pins"][pin_no] = signal
    return connectors


# --- YAML emission (hand-rolled: no PyYAML dependency, stable output) --------

def _yaml_str(s: str) -> str:
    """Quote a scalar if YAML would otherwise mis-parse it."""
    if s == "" or re.search(r'[:#\[\]{}",&*!|>%@`]', s) or s.strip() != s \
            or s.lower() in ("true", "false", "null", "yes", "no", "on", "off") \
            or re.match(r"^[-+]?[\d.]+$", s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def render_board_yaml(board: str, connectors: dict) -> str:
    lines = [
        "# GENERATED by harness/scripts/extract_connectors.py -- DO NOT EDIT.",
        f"# Off-board harness connectors extracted from board '{board}'.",
        "# Edit the board schematic and regenerate; never hand-edit this file.",
        "",
        "metadata:",
        f'  name: {_yaml_str(board + " off-board connectors")}',
        "",
        "components:",
    ]
    for ref in sorted(connectors):
        info = connectors[ref]
        label = f"{board} {ref} ({info['part']})"
        lines.append(f"  {ref}:")
        lines.append("    type: connector")
        lines.append(f"    label: {_yaml_str(label)}")
        # One zone per board so the layout clusters each board's connectors
        # into its own column instead of stacking all boards vertically.
        lines.append(f"    zone: {_yaml_str(board)}")
        lines.append("    pins:")
        for pin_no in sorted(info["pins"]):
            sig = info["pins"][pin_no]
            lines.append(f"      - {{n: {pin_no}, name: {_yaml_str(sig)}}}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- Board discovery --------------------------------------------------------

def find_root_schematic(board_dir: Path) -> Path | None:
    """A board's root sheet is the .kicad_sch that shares the .kicad_pro stem."""
    pro = next(iter(sorted(board_dir.glob("*.kicad_pro"))), None)
    if pro:
        root = pro.with_suffix(".kicad_sch")
        if root.exists():
            return root
    # fallback: a lone schematic
    schs = sorted(board_dir.glob("*.kicad_sch"))
    return schs[0] if len(schs) == 1 else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[3])
    ap.add_argument("--boards-dir", type=Path, default=REPO_ROOT / "boards")
    ap.add_argument("--out-dir", type=Path,
                    default=REPO_ROOT / "harness" / "generated")
    ap.add_argument("--kicad-cli", default=shutil.which("kicad-cli") or "kicad-cli")
    ap.add_argument("--check", action="store_true",
                    help="don't write; exit 1 if any generated file is stale")
    args = ap.parse_args(argv)

    if not args.boards_dir.is_dir():
        print(f"No such folder: {args.boards_dir}", file=sys.stderr)
        return 2

    boards = sorted(d for d in args.boards_dir.iterdir() if d.is_dir())
    stale = []
    wrote = []
    for board_dir in boards:
        board = board_dir.name
        sch = find_root_schematic(board_dir)
        if sch is None:
            print(f"skip {board}: no root schematic", file=sys.stderr)
            continue
        try:
            netlist = run_netlist(sch, args.kicad_cli)
            connectors = extract_board(netlist)
        except Exception as e:  # noqa: BLE001 -- report and keep going
            print(f"ERROR {board}: {e}", file=sys.stderr)
            return 1
        if not connectors:
            print(f"note {board}: no off-board connectors found", file=sys.stderr)
            continue
        yaml_text = render_board_yaml(board, connectors)
        out_path = args.out_dir / f"{board}.yaml"

        if args.check:
            existing = out_path.read_text(encoding="utf-8").replace("\r\n", "\n") \
                if out_path.exists() else ""
            if existing != yaml_text:
                stale.append(board)
        else:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(yaml_text, encoding="utf-8")
            n = sum(len(c["pins"]) for c in connectors.values())
            wrote.append(f"{board}: {len(connectors)} connector(s), {n} pins")

    if args.check:
        if stale:
            print("Stale generated harness files (regenerate and commit): "
                  + ", ".join(stale), file=sys.stderr)
            return 1
        print("OK: generated harness connector files are up to date.")
        return 0

    for line in wrote:
        print(f"OK: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
