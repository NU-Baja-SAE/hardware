"""CLI: python cli.py build <harness.yaml> -o <output.svg>"""
import argparse
import sys
from pathlib import Path

from parser import load, validate_no_double_connections
from layout import layout
from render import render
from bom import wire_cuts, bill_of_materials, to_csv
# NOTE: `serve` (the interactive editor) is imported lazily inside cmd_serve,
# not here. It depends on ruamel.yaml, which the build/bom/check paths don't
# need -- importing it at module load would force that dependency on every
# subcommand (and breaks CI, which only installs the build deps).


def cmd_build(args):
    harness = load(args.input)
    warnings = validate_no_double_connections(harness)
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)

    positions = layout(harness)
    svg = render(harness, positions)
    Path(args.output).write_text(svg, encoding="utf-8")
    print(f"OK: wrote {args.output} ({len(harness.components)} components, "
          f"{len(harness.wires)} wires)")


def cmd_bom(args):
    harness = load(args.input)
    stem = Path(args.input).stem

    cuts_path = Path(args.output_dir) / f"{stem}_wire_cuts.csv"
    bom_path = Path(args.output_dir) / f"{stem}_bom.csv"

    cuts_path.write_text(to_csv(wire_cuts(harness)), encoding="utf-8")
    bom_path.write_text(to_csv(bill_of_materials(harness)), encoding="utf-8")
    print(f"OK: wrote {cuts_path} ({len(harness.wires)} wires)")
    print(f"OK: wrote {bom_path}")


def cmd_serve(args):
    from serve import serve as serve_preview  # lazy: needs ruamel.yaml
    serve_preview(args.input, port=args.port, host=args.host)


def cmd_check(args):
    harness = load(args.input)
    warnings = validate_no_double_connections(harness)
    for w in warnings:
        print(f"WARN: {w}")
    print(f"OK: {args.input} parsed "
          f"({len(harness.components)} components, {len(harness.wires)} wires)")


def main():
    p = argparse.ArgumentParser(prog="harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="render harness YAML to SVG")
    pb.add_argument("input")
    pb.add_argument("-o", "--output", default="harness.svg")
    pb.set_defaults(func=cmd_build)

    pb2 = sub.add_parser("bom", help="export wire cut-list and BOM to CSV")
    pb2.add_argument("input")
    pb2.add_argument("-d", "--output-dir", default=".", metavar="DIR")
    pb2.set_defaults(func=cmd_bom)

    ps = sub.add_parser("serve", help="live-preview server: re-render on YAML save")
    ps.add_argument("input")
    ps.add_argument("--port", type=int, default=8765)
    ps.add_argument("--host", default="127.0.0.1")
    ps.set_defaults(func=cmd_serve)

    pc = sub.add_parser("check", help="validate harness YAML")
    pc.add_argument("input")
    pc.set_defaults(func=cmd_check)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
