"""Component layout.

Zone-aware strategy (preferred):
  1. Group components by their `zone:` field.
  2. Lay out each zone as a vertical column with internal Graphviz or grid spacing.
  3. Bulkheads (which bridge zones) are placed on the seam between their two
     adjacent zones; if a bulkhead has no zone it is placed in a centre column.
  4. Arrange zone columns left-to-right in a deterministic order.

Falls back to a plain Graphviz neato layout, then a simple grid, when zones are
absent or Graphviz is unavailable.
"""
from __future__ import annotations
import shutil
import subprocess
import json
from collections import defaultdict
from schema import Harness, Bulkhead, Splice, Connector, Pin
from parser import parse_pin_ref

# Pixels between zone columns — must be wide enough to route all parallel wires.
# With up to 31 wires between two columns at ~8px step each we need ~250px minimum.
# Use a per-harness adaptive value computed in layout(); this is the base fallback.
ZONE_GAP = 500
# Gap between components stacked in the same column
ROW_GAP = 30
# Starting x/y margin
MARGIN_X = 120
MARGIN_Y = 100

# Match render.py constants so we can compute real heights
_PIN_PITCH = 22
_HEADER_H = 26


def _component_height(comp) -> float:
    """Estimate rendered box height for layout spacing."""
    if isinstance(comp, Connector):
        return _HEADER_H + len(comp.pins) * _PIN_PITCH + 8
    if isinstance(comp, Bulkhead):
        return _HEADER_H + comp.positions * _PIN_PITCH + 8
    return 30  # splice


# ---------------------------------------------------------------------------
# Graphviz helpers
# ---------------------------------------------------------------------------

def _graphviz_positions(comp_ids: list[str], edges: list[tuple[str, str]]) -> dict[str, tuple[float, float]] | None:
    """Ask graphviz for positions of a subgraph. Returns None if unavailable."""
    if not shutil.which("dot"):
        return None
    nodes = [f'"{cid}" [shape=box]' for cid in comp_ids]
    seen: set[tuple[str, str]] = set()
    edge_lines = []
    for a, b in edges:
        key = tuple(sorted([a, b]))
        if key not in seen and a != b:
            seen.add(key)
            edge_lines.append(f'"{a}" -- "{b}"')
    dot = "graph G {\n  rankdir=TB;\n  " + "\n  ".join(nodes + edge_lines) + "\n}\n"
    try:
        out = subprocess.run(
            ["dot", "-Tjson", "-Kneato"],
            input=dot, capture_output=True, text=True, timeout=10, check=True,
        )
        data = json.loads(out.stdout)
        positions: dict[str, tuple[float, float]] = {}
        for obj in data.get("objects", []):
            if "name" in obj and "pos" in obj:
                x, y = map(float, obj["pos"].split(","))
                positions[obj["name"]] = (x, -y)
        return positions
    except Exception:
        return None


def _scale(positions: dict[str, tuple[float, float]], x_range: float, y_range: float,
           x_off: float, y_off: float, target_w: float, target_h: float) -> dict[str, tuple[float, float]]:
    result = {}
    for cid, (x, y) in positions.items():
        sx = (x - x_off) / x_range * target_w if x_range else 0.0
        sy = (y - y_off) / y_range * target_h if y_range else 0.0
        result[cid] = (sx, sy)
    return result


# ---------------------------------------------------------------------------
# Zone-aware layout
# ---------------------------------------------------------------------------

def _zone_layout(harness: Harness) -> dict[str, tuple[float, float]]:
    """Cluster components by zone, stack columns left-to-right."""
    # Bucket components into zones. A bulkhead with an explicit `zone:` is
    # treated as a normal zone member (placed in that column, stacked under
    # whatever's already there). A bulkhead with no zone goes into the
    # seam-placement loop below where it bridges its connected zones.
    zone_members: dict[str, list[str]] = defaultdict(list)
    bulkheads: list[str] = []

    for cid, comp in harness.components.items():
        if isinstance(comp, Bulkhead):
            if comp.zone:
                zone_members[comp.zone].append(cid)
            else:
                bulkheads.append(cid)
        elif comp.zone:
            zone_members[comp.zone].append(cid)
        else:
            zone_members["_unzoned"].append(cid)

    # Determine zone column order. Use a fixed preferred order; unknown zones
    # are appended alphabetically after known ones.
    PREFERRED_ORDER = [
        "sensors_main", "sensors_hud",  # outermost sensors
        "engine", "chassis",             # baja vehicle zones
        "power", "cabin", "dash",        # battery / power feeds
        "boxes",                         # box panel connectors (act as bulkheads)
        "boards",                        # innermost boards / ECUs
        "_unzoned",
    ]
    all_zones = list(zone_members.keys())
    ordered_zones = [z for z in PREFERRED_ORDER if z in all_zones]
    ordered_zones += sorted(z for z in all_zones if z not in PREFERRED_ORDER)

    # Build component→zone index for bulkhead seam placement
    comp_zone: dict[str, str] = {}
    for zone, members in zone_members.items():
        for cid in members:
            comp_zone[cid] = zone

    # For each bulkhead, find which zones it connects by inspecting wires
    bh_zones: dict[str, set[str]] = defaultdict(set)
    bulkhead_set = set(bulkheads)
    for w in harness.wires:
        ca, _ = parse_pin_ref(w.from_)
        cb, _ = parse_pin_ref(w.to)
        for cid, other in ((ca, cb), (cb, ca)):
            if cid in bulkhead_set and other in comp_zone:
                bh_zones[cid].add(comp_zone[other])

    # Assign each bulkhead a column index between its two zones; default to 0.5 gaps
    positions: dict[str, tuple[float, float]] = {}

    # Wire routing constants (must match render.py)
    WIRE_STEP = 12      # px per parallel wire channel
    CORRIDOR = 60       # minimum clear px between a zone's right edge and next zone's left edge
    JOG_LEN = 20       # render.py JOG_LEN — extra clearance needed at stub exit

    # For each zone, compute the maximum component width (determines right edge of zone column)
    def _comp_width(comp) -> float:
        if isinstance(comp, Connector):
            # Approximate: match render._connector_width logic
            label_w = max(len(comp.label or comp.id) * 13 * 0.62 + 20, 100)
            pin_w = max((len(p.name or "") * 11 * 0.62 + 22 + 6 + 16) for p in comp.pins) if comp.pins else 100
            return max(label_w, pin_w)
        if isinstance(comp, Bulkhead):
            # Mirror render._bulkhead_width: grows with the header label.
            title = comp.label or comp.id
            box_w = max(80.0, len(title) * 11 * 0.62 + 20)
            return box_w + 16  # box width + stub
        return 20  # splice

    zone_max_width: dict[str, float] = {}
    for zone, members in zone_members.items():
        zone_max_width[zone] = max((_comp_width(harness.components[c]) for c in members), default=100)

    # Count wires that must travel THROUGH each inter-zone gap.
    # A wire from zone A to zone C must travel through the A→B and B→C gaps.
    zone_index = {z: i for i, z in enumerate(ordered_zones)}

    # For every wire, find which inter-zone seams it must cross
    # (all seams between its source zone index and dest zone index)
    seam_wire_count: dict[int, int] = defaultdict(int)  # seam index i = gap between zone i and i+1
    for w in harness.wires:
        za = comp_zone.get(parse_pin_ref(w.from_)[0])
        zb = comp_zone.get(parse_pin_ref(w.to)[0])
        if za and zb and za != zb:
            ia, ib = zone_index.get(za, -1), zone_index.get(zb, -1)
            if ia >= 0 and ib >= 0 and ia != ib:
                lo, hi = min(ia, ib), max(ia, ib)
                for seam in range(lo, hi):
                    seam_wire_count[seam] += 1

    # Build cumulative x positions for each zone column
    zone_x: dict[str, float] = {}
    cur_x = float(MARGIN_X)
    for i, zone in enumerate(ordered_zones):
        zone_x[zone] = cur_x
        # Right edge of this zone = cur_x + max component width + stub length
        zone_right = cur_x + zone_max_width.get(zone, 100) + 16 + JOG_LEN
        # Wires crossing the seam between zone i and zone i+1
        n_crossing = seam_wire_count.get(i, 0)
        routing_width = n_crossing * WIRE_STEP
        # Next zone starts after: zone right edge + corridor + routing space
        cur_x = zone_right + CORRIDOR + routing_width

    # Layout members within each zone — height-aware stacking, no overlaps
    for zone in ordered_zones:
        members = zone_members[zone]
        if not members:
            continue
        col_x = zone_x[zone]
        cursor_y = MARGIN_Y
        for cid in members:
            positions[cid] = (col_x, cursor_y)
            cursor_y += _component_height(harness.components[cid]) + ROW_GAP

    # Place bulkheads on seams — height-aware stacking per seam x
    bh_cursor_y: dict[float, float] = defaultdict(lambda: MARGIN_Y)
    for bh_id in bulkheads:
        comp = harness.components[bh_id]
        if comp.zone and comp.zone in zone_x:
            seam_x = zone_x[comp.zone]
        else:
            zones_connected = sorted(bh_zones.get(bh_id, set()))
            if len(zones_connected) >= 2:
                x1 = zone_x.get(zones_connected[0], MARGIN_X)
                x2 = zone_x.get(zones_connected[1], MARGIN_X + ZONE_GAP)
                seam_x = (x1 + x2) / 2
            elif len(zones_connected) == 1:
                seam_x = zone_x.get(zones_connected[0], MARGIN_X) + ZONE_GAP / 2
            else:
                seam_x = MARGIN_X + len(ordered_zones) * ZONE_GAP / 2

        positions[bh_id] = (seam_x, bh_cursor_y[seam_x])
        bh_cursor_y[seam_x] += _component_height(comp) + ROW_GAP

    return positions


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------

def _try_graphviz_layout(harness: Harness) -> dict[str, tuple[float, float]] | None:
    comp_ids = list(harness.components.keys())
    edges = [(parse_pin_ref(w.from_)[0], parse_pin_ref(w.to)[0]) for w in harness.wires]
    raw = _graphviz_positions(comp_ids, edges)
    if raw is None:
        return None
    xs = [p[0] for p in raw.values()]
    ys = [p[1] for p in raw.values()]
    if not xs:
        return None
    scaled = _scale(raw, max(max(xs) - min(xs), 1), max(max(ys) - min(ys), 1),
                    min(xs), min(ys), target_w=1000, target_h=600)
    return {cid: (100 + sx, 100 + sy) for cid, (sx, sy) in scaled.items()}


def _grid_layout(harness: Harness) -> dict[str, tuple[float, float]]:
    cols = {"connector": 0, "device": 0, "bulkhead": 1, "splice": 2}
    cursor_y: dict[int, float] = {0: 100.0, 1: 100.0, 2: 100.0}
    col_x = [100, 500, 900]
    positions: dict[str, tuple[float, float]] = {}
    for cid, comp in harness.components.items():
        col = cols.get(comp.type, 0)
        positions[cid] = (col_x[col], cursor_y[col])
        cursor_y[col] += _component_height(comp) + ROW_GAP
    return positions


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def layout(harness: Harness) -> dict[str, tuple[float, float]]:
    manual = {cid: c.position for cid, c in harness.components.items() if c.position}

    has_zones = any(c.zone for c in harness.components.values())
    if has_zones:
        auto = _zone_layout(harness)
    else:
        auto = _try_graphviz_layout(harness) or _grid_layout(harness)

    return {**auto, **manual}
