"""BOM and wire cut-list generation from a validated Harness."""
from __future__ import annotations
import csv
import io
from collections import defaultdict
from schema import Harness, Connector, Bulkhead, Splice


def wire_cuts(harness: Harness) -> list[dict]:
    """Return one row per wire, sorted by gauge then color."""
    rows = []
    for w in harness.wires:
        rows.append({
            "from": w.from_,
            "to": w.to,
            "signal": w.signal or "",
            "gauge_awg": w.gauge or "",
            "color": w.color or "",
            "length_mm": w.length or "",
        })
    rows.sort(key=lambda r: (r["gauge_awg"] or 99, r["color"]))
    return rows


def bill_of_materials(harness: Harness) -> list[dict]:
    """Summarise components and wire totals into BOM rows."""
    rows: list[dict] = []

    # Wire totals grouped by gauge + color
    wire_groups: dict[tuple, dict] = defaultdict(lambda: {"qty_m": 0.0, "count": 0})
    for w in harness.wires:
        key = (w.gauge or "?", (w.color or "").upper())
        wire_groups[key]["qty_m"] += (w.length or 0) / 1000.0
        wire_groups[key]["count"] += 1

    for (gauge, color), info in sorted(wire_groups.items()):
        desc = f"{gauge} AWG {color}" if color else f"{gauge} AWG"
        qty = f"{info['qty_m']:.2f} m" if info["qty_m"] else f"{info['count']} run(s)"
        rows.append({"category": "wire", "description": desc, "qty": qty, "notes": ""})

    # Connectors / devices — track category alongside description
    connector_entries: dict[str, str] = {}  # desc -> category
    connector_counts: dict[str, int] = defaultdict(int)
    for cid, comp in harness.components.items():
        if isinstance(comp, Connector):
            key = f"{comp.label or cid} ({comp.type}, {len(comp.pins)}-pin)"
            connector_counts[key] += 1
            connector_entries[key] = comp.type
    for desc, qty in sorted(connector_counts.items()):
        rows.append({"category": connector_entries[desc], "description": desc, "qty": str(qty), "notes": ""})

    # Bulkheads
    for cid, comp in harness.components.items():
        if isinstance(comp, Bulkhead):
            desc = f"{comp.label or cid} ({comp.positions}-position bulkhead)"
            rows.append({"category": "bulkhead", "description": desc, "qty": "1", "notes": ""})

    # Splices
    for cid, comp in harness.components.items():
        if isinstance(comp, Splice):
            desc = f"{comp.label or cid} ({comp.pin_count}-way splice)"
            rows.append({"category": "splice", "description": desc, "qty": "1", "notes": ""})

    return rows


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()
