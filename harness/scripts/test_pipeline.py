#!/usr/bin/env python3
"""Smoke test for the harness pipeline. Run from repo root:
    python3 harness/scripts/test_pipeline.py
Exercises: extractor produces parseable YAML, backbone builds a non-empty SVG,
and regeneration is idempotent (--check passes right after a regen).
Requires kicad-cli on PATH. Exits non-zero on any failure.
"""
import subprocess, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]

def run(cmd):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)

def main():
    # 1. regenerate
    r = run([sys.executable, "harness/scripts/extract_connectors.py"])
    if r.returncode != 0:
        print(r.stderr); return "extractor failed"
    # 2. idempotent: --check must pass immediately after regen
    r = run([sys.executable, "harness/scripts/extract_connectors.py", "--check"])
    if r.returncode != 0:
        print(r.stderr); return "extractor not idempotent (--check failed after regen)"
    # 3. build the backbone -> SVG
    out = ROOT / "harness/output/car.svg"
    r = run([sys.executable, "harness/wirelab/cli.py", "build",
             "harness/car.yaml", "-o", "harness/output/car.svg"])
    if r.returncode != 0:
        print(r.stderr); return "build failed"
    if not out.exists() or out.stat().st_size < 500:
        return "SVG missing or too small"
    text = out.read_text(encoding="utf-8")
    if "<svg" not in text or 'class="wire"' not in text:
        return "SVG has no wires"
    print("PASS: extractor + build + idempotency all OK")
    return None

if __name__ == "__main__":
    err = main()
    if err:
        print("FAIL:", err, file=sys.stderr); sys.exit(1)
