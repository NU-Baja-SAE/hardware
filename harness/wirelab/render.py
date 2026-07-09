"""Render a laid-out harness to SVG."""
from __future__ import annotations
from collections import defaultdict
from xml.sax.saxutils import escape
from schema import Harness, Connector, Bulkhead, Splice
from parser import parse_pin_ref


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLOR_MAP = {
    "RED": "#c62828", "BLACK": "#212121", "WHITE": "#e0e0e0",
    "BLUE": "#1565c0", "GREEN": "#2e7d32", "YELLOW": "#f9a825",
    "ORANGE": "#e65100", "BROWN": "#4e342e", "PURPLE": "#6a1b9a",
    "GRAY": "#616161", "PINK": "#c2185b",
}
WHITE_OUTLINE = "#9e9e9e"

PIN_PITCH = 22
PIN_STUB_LEN = 16
HEADER_H = 26
PIN_NUM_W = 22
PIN_NAME_PAD = 6

# Channel routing
WIRE_STEP = 12          # px between parallel channel lanes
JOG_STEP = 4           # px y-offset between wires that share an anchor point
JOG_LEN = 20           # px horizontal distance before/after the jog segment

# Wrap-around routing must clear the stub tips (which extend `PIN_STUB_LEN`
# past the body) AND leave visual breathing room — otherwise a wire's vertical
# run will graze the column of stubs sticking out of an adjacent component.
WRAP_MARGIN = PIN_STUB_LEN + JOG_LEN  # 36 px


def _color_hex(c: str | None) -> str:
    if not c:
        return "#555"
    return COLOR_MAP.get(c.upper(), c)


def _is_white(c: str | None) -> bool:
    return bool(c) and c.upper() == "WHITE"


def _label_width(text: str, font_size: int = 13) -> float:
    return len(text) * font_size * 0.62


# ---------------------------------------------------------------------------
# Component renderers
# ---------------------------------------------------------------------------

def _connector_width(comp: Connector, dual: bool = False) -> float:
    name_col = max(
        (_label_width(p.name or "", 11) for p in comp.pins),
        default=60,
    )
    # dual-stub boxes show pin name centred; single-stub boxes left-align name
    if dual:
        inner = PIN_NUM_W + PIN_NAME_PAD + name_col + 12
    else:
        inner = PIN_NUM_W + PIN_NAME_PAD + name_col + 16
    return max(_label_width(comp.label or comp.id, 13) + 20, inner, 100)


def _connector_box(comp: Connector, x: float, y: float,
                   stub_side: str = "right",
                   pin_sides: dict[str, str] | None = None):
    """Render a connector box.

    stub_side  – default side for all pins ("left" or "right").
    pin_sides  – optional per-pin override: maps pin name/number → "left"|"right"|"both".
                 When a pin is "both" it gets a stub on each side and two anchor entries.
    """
    dual = pin_sides is not None and any(v == "both" for v in pin_sides.values())
    n_pins = len(comp.pins)
    h = HEADER_H + n_pins * PIN_PITCH + 8
    w = _connector_width(comp, dual=dual)

    parts = [
        f'<g class="component" data-id="{escape(comp.id)}">',
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="4" '
        f'fill="#fff" stroke="#455a64" stroke-width="1.5"/>',
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{HEADER_H}" rx="4" fill="#37474f"/>',
        f'<rect x="{x:.1f}" y="{y + HEADER_H - 4:.1f}" width="{w:.1f}" height="4" fill="#37474f"/>',
        f'<text x="{x + w/2:.1f}" y="{y + 17:.1f}" text-anchor="middle" '
        f'fill="white" font-family="sans-serif" font-size="13" font-weight="600">'
        f'{escape(comp.label or comp.id)}</text>',
    ]

    anchors: dict[str, tuple[float, float]] = {}
    # For dual-stub boxes, we need TWO anchor entries per pin:
    # one for the left stub and one for the right stub.
    # We encode them as  "<name>:L" and "<name>:R" (and also bare name for compat).
    left_anchors: dict[str, tuple[float, float]] = {}
    right_anchors: dict[str, tuple[float, float]] = {}

    for i, pin in enumerate(comp.pins):
        py = y + HEADER_H + 8 + i * PIN_PITCH
        cx = x + PIN_NUM_W  # left edge of name column

        # Pin number
        parts.append(
            f'<text x="{x + 6:.1f}" y="{py + 4:.1f}" font-family="monospace" '
            f'font-size="11" fill="#546e7a">{pin.n}</text>'
        )
        # Divider
        parts.append(
            f'<line x1="{cx:.1f}" y1="{py - 7:.1f}" x2="{cx:.1f}" y2="{py + 8:.1f}" '
            f'stroke="#cfd8dc" stroke-width="1"/>'
        )
        # Pin name — centred in name column for dual boxes, left-aligned otherwise
        name_text = pin.name or ""
        if dual:
            name_x = x + w / 2
            anchor_attr = 'text-anchor="middle"'
        else:
            name_x = cx + PIN_NAME_PAD
            anchor_attr = ''
        parts.append(
            f'<text x="{name_x:.1f}" y="{py + 4:.1f}" font-family="sans-serif" '
            f'font-size="11" fill="#263238" {anchor_attr}>{escape(name_text)}</text>'
        )

        # Determine this pin's stub side(s)
        pin_key = pin.name or str(pin.n)
        this_side = (pin_sides or {}).get(pin_key) or (pin_sides or {}).get(str(pin.n)) or stub_side

        pin_data = (
            f'data-comp="{escape(comp.id)}" data-pin="{pin.n}" '
            f'data-pin-name="{escape(pin.name or "")}"'
        )
        if this_side in ("right", "both"):
            rx1, rx2 = x + w, x + w + PIN_STUB_LEN
            parts.append(
                f'<line x1="{rx1:.1f}" y1="{py:.1f}" x2="{rx2:.1f}" y2="{py:.1f}" '
                f'stroke="#455a64" stroke-width="1.5"/>'
            )
            parts.append(
                f'<circle class="pin-hit" {pin_data} cx="{rx2:.1f}" cy="{py:.1f}" r="6" '
                f'fill="transparent" pointer-events="all"/>'
            )
            right_anchors[str(pin.n)] = (rx2, py)
            if pin.name:
                right_anchors[pin.name] = (rx2, py)

        if this_side in ("left", "both"):
            lx1, lx2 = x, x - PIN_STUB_LEN
            parts.append(
                f'<line x1="{lx1:.1f}" y1="{py:.1f}" x2="{lx2:.1f}" y2="{py:.1f}" '
                f'stroke="#455a64" stroke-width="1.5"/>'
            )
            parts.append(
                f'<circle class="pin-hit" {pin_data} cx="{lx2:.1f}" cy="{py:.1f}" r="6" '
                f'fill="transparent" pointer-events="all"/>'
            )
            left_anchors[str(pin.n)] = (lx2, py)
            if pin.name:
                left_anchors[pin.name] = (lx2, py)

    # Build final anchors dict.
    # For single-side pins, use the plain key.
    # For dual-side pins, expose "<key>:L" and "<key>:R" for wire routing,
    # plus keep the plain key pointing to whichever side has more connections
    # (resolved by the caller via pin_sides).
    for key, pt in right_anchors.items():
        anchors[f"{key}:R"] = pt
        anchors[key] = pt          # default to right for compat
    for key, pt in left_anchors.items():
        anchors[f"{key}:L"] = pt
        if key not in anchors:    # don't overwrite right if both exist
            anchors[key] = pt

    parts.append('</g>')
    return "\n".join(parts), anchors, (w, h)


def _bulkhead_width(comp: Bulkhead) -> float:
    """Bulkhead box width — grows with the label.

    Pin labels (A1..An / B1..Bn) are short and fit inside fixed margins,
    so the only thing that can overflow is the header text.
    """
    title = comp.label or comp.id
    # 80 = original baseline; +20 padding around the bold 11px header text
    return max(80.0, _label_width(title, font_size=11) + 20)


def _bulkhead_box(comp: Bulkhead, x: float, y: float):
    n = comp.positions
    h = HEADER_H + n * PIN_PITCH + 8
    w = _bulkhead_width(comp)
    parts = [
        f'<g class="bulkhead" data-id="{escape(comp.id)}">',
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h:.1f}" '
        f'fill="#fff8f0" stroke="#e65100" stroke-width="2"/>',
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{HEADER_H}" fill="#e65100"/>',
        f'<text x="{x + w/2:.1f}" y="{y + 17:.1f}" text-anchor="middle" fill="white" '
        f'font-family="sans-serif" font-size="11" font-weight="600">'
        f'{escape(comp.label or comp.id)}</text>',
    ]
    anchors: dict[str, tuple[float, float]] = {}
    for i in range(n):
        py = y + HEADER_H + 8 + i * PIN_PITCH
        mid = x + w / 2
        parts.append(
            f'<line x1="{mid:.1f}" y1="{py - 3:.1f}" x2="{mid:.1f}" y2="{py + 3:.1f}" '
            f'stroke="#e65100" stroke-width="2"/>'
        )
        # A-side stub + clickable hit circle
        a_name = f"A{i+1}"
        parts.append(
            f'<line x1="{x:.1f}" y1="{py:.1f}" x2="{x - PIN_STUB_LEN:.1f}" y2="{py:.1f}" '
            f'stroke="#455a64" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{x + 4:.1f}" y="{py + 4:.1f}" font-family="monospace" '
            f'font-size="10" fill="#37474f">{a_name}</text>'
        )
        parts.append(
            f'<circle class="pin-hit" data-comp="{escape(comp.id)}" '
            f'data-pin="{a_name}" data-pin-name="{a_name}" '
            f'cx="{x - PIN_STUB_LEN:.1f}" cy="{py:.1f}" r="6" '
            f'fill="transparent" pointer-events="all"/>'
        )
        anchors[a_name] = (x - PIN_STUB_LEN, py)
        # B-side stub + clickable hit circle
        b_name = f"B{i+1}"
        parts.append(
            f'<line x1="{x + w:.1f}" y1="{py:.1f}" x2="{x + w + PIN_STUB_LEN:.1f}" y2="{py:.1f}" '
            f'stroke="#455a64" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{x + w - 24:.1f}" y="{py + 4:.1f}" font-family="monospace" '
            f'font-size="10" fill="#37474f">{b_name}</text>'
        )
        parts.append(
            f'<circle class="pin-hit" data-comp="{escape(comp.id)}" '
            f'data-pin="{b_name}" data-pin-name="{b_name}" '
            f'cx="{x + w + PIN_STUB_LEN:.1f}" cy="{py:.1f}" r="6" '
            f'fill="transparent" pointer-events="all"/>'
        )
        anchors[b_name] = (x + w + PIN_STUB_LEN, py)
    parts.append('</g>')
    return "\n".join(parts), anchors, (w, h)


def _splice_node(comp: Splice, x: float, y: float):
    r = 10
    parts = [
        f'<g class="splice" data-id="{escape(comp.id)}">',
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="#37474f" stroke="#000" stroke-width="1.5"/>',
        f'<text x="{x:.1f}" y="{y - r - 5:.1f}" text-anchor="middle" font-family="sans-serif" '
        f'font-size="10" fill="#37474f" font-weight="600">'
        f'{escape(comp.label or comp.id)}</text>',
    ]
    anchors = {str(i + 1): (x, y) for i in range(comp.pin_count)}
    # One hit-circle per pin — all share the same xy, but Connect mode just
    # needs *some* hit-circle per pin so the user can pick one.
    for i in range(comp.pin_count):
        n = i + 1
        parts.append(
            f'<circle class="pin-hit" data-comp="{escape(comp.id)}" '
            f'data-pin="{n}" data-pin-name="" '
            f'cx="{x:.1f}" cy="{y:.1f}" r="{r + 2}" '
            f'fill="transparent" pointer-events="all"/>'
        )
    parts.append('</g>')
    return "\n".join(parts), anchors, (r * 2, r * 2)


# ---------------------------------------------------------------------------
# Stub-side assignment
# ---------------------------------------------------------------------------

def _compute_stub_sides(harness: Harness,
                        positions: dict[str, tuple[float, float]]) -> dict[str, str]:
    """Overall stub side for each connector (used as default for single-side pins)."""
    score: dict[str, float] = defaultdict(float)
    for w in harness.wires:
        ca, _ = parse_pin_ref(w.from_)
        cb, _ = parse_pin_ref(w.to)
        xa = positions.get(ca, (0, 0))[0]
        xb = positions.get(cb, (0, 0))[0]
        if ca != cb:
            score[ca] += xb - xa
            score[cb] += xa - xb
    return {
        cid: ("right" if score.get(cid, 1) >= 0 else "left")
        for cid, comp in harness.components.items()
        if isinstance(comp, Connector)
    }


def _compute_pin_sides(harness: Harness,
                       positions: dict[str, tuple[float, float]],
                       stub_sides: dict[str, str]) -> dict[str, dict[str, str]]:
    """Per-pin stub side for every connector.

    Returns  { comp_id: { pin_key: "left"|"right"|"both" } }

    A pin gets "both" when it has wires arriving from opposite sides.
    The plain stub_sides value is used as fallback for unconnected pins.
    """
    # For each (comp_id, pin_key), track which sides wires arrive from
    pin_directions: dict[tuple[str, str], set[str]] = defaultdict(set)

    def _strip_side(p: str) -> str:
        return p[:-2] if p.endswith(":L") or p.endswith(":R") else p

    for w in harness.wires:
        ca, pa = parse_pin_ref(w.from_)
        cb, pb = parse_pin_ref(w.to)
        pa = _strip_side(pa)
        pb = _strip_side(pb)
        xa = positions.get(ca, (0, 0))[0]
        xb = positions.get(cb, (0, 0))[0]
        if xa == xb:
            continue
        # Wire goes from ca→cb; ca sees its neighbour to the right/left
        direction_from_ca = "right" if xb > xa else "left"
        direction_from_cb = "right" if xa > xb else "left"
        pin_directions[(ca, pa)].add(direction_from_ca)
        pin_directions[(cb, pb)].add(direction_from_cb)

    result: dict[str, dict[str, str]] = {}
    for cid, comp in harness.components.items():
        if not isinstance(comp, Connector):
            continue
        # Box-entry connectors (zone == "boxes") always render dual stubs on
        # every pin, so wires can attach from either side regardless of where
        # the neighbour component sits. This lets you, e.g., connect HUD_BOX_IN
        # to MAIN_BOX_IN even though the natural wire side would otherwise be
        # the same edge as the board-side stubs.
        force_dual = (comp.zone == "boxes")
        default = stub_sides.get(cid, "right")
        pin_map: dict[str, str] = {}
        has_dual = force_dual
        for pin in comp.pins:
            keys = [str(pin.n)] + ([pin.name] if pin.name else [])
            dirs: set[str] = set()
            for k in keys:
                dirs |= pin_directions.get((cid, k), set())
            if force_dual:
                side = "both"
            elif not dirs:
                side = default
            elif len(dirs) == 2:
                side = "both"
                has_dual = True
            else:
                side = next(iter(dirs))
            for k in keys:
                pin_map[k] = side
        result[cid] = pin_map if has_dual else {}  # only return map when needed

    return result


# ---------------------------------------------------------------------------
# Wire routing
# ---------------------------------------------------------------------------

def _build_routing(wires, all_anchors, box_extents=None, positions=None,
                   nudge_obstacles=None):
    """Compute per-wire routing waypoints that avoid passing through boxes.

    Routing model
    -------------
    A wire is described by an ordered list of `(x, y)` corner points between
    its source and destination anchors. The renderer turns those into an
    orthogonal SVG path. This is a switch from the old "channel_x + jog"
    representation, which couldn't express wrap-around routes.

    Algorithm (in order)
    --------------------
    1. Resolve the source and destination anchor for each wire (handling
       the dual-stub :L/:R case).
    2. Group wires by their (lx, rx) column pair. Each group will share a
       common channel-x corridor and y-bundle ordering.
    3. For each group, find a clear x-corridor in `[lx, rx]` that avoids
       every box. If a clear gap exists, build a standard "exit stub ->
       vertical channel -> approach" path.
    4. If no clear gap exists between lx and rx (boxes fully obstruct),
       route over the top or under the bottom of the obstructing boxes,
       picking whichever requires less detour.
    5. Within an anchor point shared by N wires, assign small y-jogs so
       leaving horizontal segments visibly separate.

    Returns dict: `wire_index -> {a1, a2, waypoints}` where `waypoints`
    is the list of internal corner points (anchors are added by the path
    builder).
    """
    def _pick_anchor(comp_anchors: dict, pin_key: str,
                     other_x: float, my_x: float) -> tuple[float, float] | None:
        """Pick :L or :R anchor when both exist.

        If `pin_key` already carries an explicit ':L' or ':R' suffix (from
        the YAML), honour it directly. Otherwise pick based on which side
        the neighbour component sits.
        """
        if pin_key.endswith(":L") or pin_key.endswith(":R"):
            return comp_anchors.get(pin_key) or comp_anchors.get(pin_key[:-2])
        if f"{pin_key}:L" in comp_anchors and f"{pin_key}:R" in comp_anchors:
            return comp_anchors[f"{pin_key}:L"] if other_x < my_x else comp_anchors[f"{pin_key}:R"]
        return comp_anchors.get(pin_key)

    boxes = list(box_extents or [])

    # ----- 1. Resolve anchors --------------------------------------------
    resolved: list[tuple[int, float, float, float, float]] = []
    for i, w in enumerate(wires):
        c1, p1 = parse_pin_ref(w.from_)
        c2, p2 = parse_pin_ref(w.to)
        anch1 = all_anchors.get(c1, {})
        anch2 = all_anchors.get(c2, {})
        x1_pos = (positions or {}).get(c1, (0, 0))[0]
        x2_pos = (positions or {}).get(c2, (0, 0))[0]
        a1 = _pick_anchor(anch1, p1, other_x=x2_pos, my_x=x1_pos)
        a2 = _pick_anchor(anch2, p2, other_x=x1_pos, my_x=x2_pos)
        if a1 and a2:
            resolved.append((i, a1[0], a1[1], a2[0], a2[1]))

    # ----- 2. Group by column pair and pick channel-x for each group -----
    col_groups: dict[tuple[float, float], list] = defaultdict(list)
    for entry in resolved:
        _i, x1, _y1, x2, _y2 = entry
        col_groups[(min(x1, x2), max(x1, x2))].append(entry)

    # For each group decide:
    #   route_kind = "channel"  -> normal routing through a clear x-corridor
    #   route_kind = "over"     -> wrap above all blocking boxes
    #   route_kind = "under"    -> wrap below all blocking boxes
    # Plus the relevant detour y for over/under.
    group_route: dict[tuple[float, float], dict] = {}
    for (lx, rx), entries in col_groups.items():
        n = len(entries)
        gap = _largest_clear_gap(lx, rx, boxes, padding=JOG_LEN + 20)
        if gap is not None:
            gap_lo, gap_hi = gap
            # Centre the bundle in the clear gap; pull back if too narrow.
            bundle_w = (n - 1) * WIRE_STEP
            mid = (gap_lo + gap_hi) / 2
            bundle_w = min(bundle_w, max(0.0, gap_hi - gap_lo - 4))
            start = mid - bundle_w / 2
            channels = [start + (slot * (bundle_w / max(1, n - 1)) if n > 1 else 0)
                        for slot in range(n)]
            group_route[(lx, rx)] = {"kind": "channel", "channels": channels}
        else:
            # No clear corridor between lx and rx. Wrap over or under, picking
            # whichever side requires the smaller detour relative to the
            # bundle's average y. Fall back to ALL boxes if `_largest_clear_gap`
            # returned None just because (rx - lx) was too small for a corridor
            # rather than because of actual obstruction.
            blocking = [b for b in boxes if not (b[2] <= lx or b[0] >= rx)]
            if not blocking:
                blocking = list(boxes) or [(0, 0, 0, 0)]
            top_y = min(b[1] for b in blocking) - WRAP_MARGIN
            bot_y = max(b[3] for b in blocking) + WRAP_MARGIN
            avg_y = sum((e[2] + e[4]) / 2 for e in entries) / n
            if (avg_y - top_y) <= (bot_y - avg_y):
                detour_y = top_y
                kind = "over"
            else:
                detour_y = bot_y
                kind = "under"
            # Stagger the detour y per wire so they don't overlap perfectly.
            offsets = [(-(n - 1) / 2 + i) * WIRE_STEP for i in range(n)]
            sign = -1 if kind == "over" else 1
            ys = [detour_y + sign * abs(off) for off in offsets]  # spread away from boxes
            # Per-wire vertical-run x offsets so the climb/drop lines also separate.
            # Positive only: each wire gets pushed FURTHER from the box edge by
            # a different amount, which guarantees different jog x's without
            # ever pushing back toward an obstacle.
            x_offsets = [i * WIRE_STEP for i in range(n)]
            group_route[(lx, rx)] = {"kind": kind, "detour_ys": ys,
                                     "x_offsets": x_offsets,
                                     "blocking": blocking}

    # ----- 3. Per-wire channel/detour y assignment -----------------------
    wire_meta: dict[int, dict] = {}
    for (lx, rx), entries in col_groups.items():
        info = group_route[(lx, rx)]
        # sort by source y for stable bundle ordering
        entries.sort(key=lambda e: e[2])
        for slot, (i, x1, y1, x2, y2) in enumerate(entries):
            if info["kind"] == "channel":
                wire_meta[i] = {"kind": "channel",
                                "channel_x": info["channels"][slot]}
            else:
                wire_meta[i] = {"kind": info["kind"],
                                "detour_y": info["detour_ys"][slot],
                                "x_offset": info["x_offsets"][slot]}

    # ----- 4. Anchor-shared jog assignment -------------------------------
    # For each anchor point shared by N wires, spread their leaving y so
    # the short horizontal stubs visibly separate.
    anchor_wires: dict[tuple[float, float], list[tuple[int, str]]] = defaultdict(list)
    for i, w in enumerate(wires):
        c1, p1 = parse_pin_ref(w.from_)
        c2, p2 = parse_pin_ref(w.to)
        a1 = all_anchors.get(c1, {}).get(p1)
        a2 = all_anchors.get(c2, {}).get(p2)
        if a1:
            anchor_wires[a1].append((i, "src"))
        if a2:
            anchor_wires[a2].append((i, "dst"))

    src_jog: dict[int, float] = {}
    dst_jog: dict[int, float] = {}
    for _pt, wire_list in anchor_wires.items():
        n = len(wire_list)
        if n <= 1:
            continue
        # Order by routing kind/x for stable visual ordering.
        def _order_key(t):
            wi, _role = t
            m = wire_meta.get(wi, {})
            return (m.get("channel_x") or m.get("detour_y") or 0)
        wire_list.sort(key=_order_key)
        total = (n - 1) * JOG_STEP
        start_jog = -total / 2
        for slot, (wi, role) in enumerate(wire_list):
            jog = start_jog + slot * JOG_STEP
            (src_jog if role == "src" else dst_jog)[wi] = jog

    # ----- 5. Build per-wire waypoint list -------------------------------
    # Any channel-routed wire whose horizontal exit/approach segments would
    # cross a box gets promoted to a wrap-around route (over or under).
    # We compute a fallback detour_y for that case from the global box bounds.
    if boxes:
        global_top = min(b[1] for b in boxes) - JOG_LEN
        global_bot = max(b[3] for b in boxes) + JOG_LEN
    else:
        global_top, global_bot = -JOG_LEN, JOG_LEN

    def _channel_path_crosses_box(a1, a2, ch_x, sj, dj):
        """Return True if any segment of the channel route would cross a box."""
        wpts = _channel_waypoints(a1, a2, ch_x, sj, dj)
        pts = [a1] + wpts + [a2]
        for p, q in zip(pts, pts[1:]):
            for b in boxes:
                if _segment_crosses_box(p, q, b):
                    return True
        return False

    routing: dict[int, dict] = {}
    for i, x1, y1, x2, y2 in resolved:
        meta = wire_meta.get(i)
        sj = src_jog.get(i, 0.0)
        dj = dst_jog.get(i, 0.0)
        a1 = (x1, y1)
        a2 = (x2, y2)
        kind = meta["kind"] if meta else "channel"
        if kind == "channel":
            ch_x = meta["channel_x"] if meta else (x1 + x2) / 2
            # If the candidate channel path would intersect a box, promote
            # to a wrap-around route. Promoted wires don't carry a group
            # `x_offset`, so they get 0 — fine for one-off promotions.
            if _channel_path_crosses_box(a1, a2, ch_x, sj, dj):
                avg_y = (y1 + y2) / 2
                if avg_y - global_top <= global_bot - avg_y:
                    detour_y = global_top
                    kind = "over"
                else:
                    detour_y = global_bot
                    kind = "under"
                waypoints = _wrap_waypoints(a1, a2, detour_y, kind, sj, dj,
                                            boxes, x_offset=0.0)
            else:
                waypoints = _channel_waypoints(a1, a2, ch_x, sj, dj)
        else:
            x_off = meta.get("x_offset", 0.0)
            waypoints = _wrap_waypoints(a1, a2, meta["detour_y"], kind, sj, dj,
                                        boxes, x_offset=x_off)
        routing[i] = {"a1": a1, "a2": a2, "waypoints": waypoints, "kind": kind}

    # Post-process: clean degenerate waypoints, then separate overlapping segments.
    # Nudging uses a stricter obstacle list (`nudge_obstacles`) when supplied
    # so it doesn't push verticals into stub columns of dual-stub connectors.
    for info in routing.values():
        info["waypoints"] = _strip_zero_length(info["a1"], info["waypoints"], info["a2"])
    _separate_overlapping_segments(routing, list(nudge_obstacles or boxes))

    return routing


def _strip_zero_length(a1, waypoints, a2):
    """Drop degenerate waypoints. Two cases:

    - Consecutive duplicates (zero-length segment between them).
    - Collinear triples (mid-point sits on the straight line through its
      neighbours), which are silent corner-faking traps for the nudge pass:
      moving the mid-point breaks orthogonality of the adjacent segments.
    """
    pts = [a1] + list(waypoints) + [a2]
    # Pass 1: consecutive duplicates.
    cleaned = [pts[0]]
    for p in pts[1:]:
        if p != cleaned[-1]:
            cleaned.append(p)
    # Pass 2: collinear interior points. We only consider axis-aligned
    # collinearity since the routing produces orthogonal paths.
    while True:
        changed = False
        for i in range(1, len(cleaned) - 1):
            prev_p, mid, next_p = cleaned[i - 1], cleaned[i], cleaned[i + 1]
            same_x = (prev_p[0] == mid[0] == next_p[0])
            same_y = (prev_p[1] == mid[1] == next_p[1])
            if same_x or same_y:
                del cleaned[i]
                changed = True
                break
        if not changed:
            break
    # Strip the bookending anchors back off; only return the inner waypoints.
    return cleaned[1:-1]


def _separate_overlapping_segments(routing: dict, boxes: list,
                                   max_passes: int = 4,
                                   min_overlap: float = 4.0) -> None:
    """Detect collinear segments from different wires whose projections
    overlap (not just identical) and nudge the later owners perpendicular
    to the segment direction so they visibly separate.

    Two horizontal segments overlap if they share a y AND their x-ranges
    intersect by more than `min_overlap`. Vertical likewise.

    Crossings between wires are fine; collinear overlap is what hides one
    wire under another, which is what we care about. Iterates because a
    nudge can create a new overlap with a third wire.
    """
    for _pass in range(max_passes):
        # Group segments by (axis, fixed_coord). For verticals: ('V', x).
        # For horizontals: ('H', y). Within a group we look for range overlap.
        by_line: dict[tuple, list[tuple[int, int, float, float]]] = defaultdict(list)
        for wi, info in routing.items():
            pts = [info["a1"]] + info["waypoints"] + [info["a2"]]
            for si, (p, q) in enumerate(zip(pts, pts[1:])):
                if p == q:
                    continue
                if p[0] == q[0]:  # vertical
                    by_line[("V", p[0])].append((wi, si, min(p[1], q[1]), max(p[1], q[1])))
                elif p[1] == q[1]:  # horizontal
                    by_line[("H", p[1])].append((wi, si, min(p[0], q[0]), max(p[0], q[0])))

        # For each line, find pairs whose ranges overlap.
        nudges: list[tuple[int, int, bool, float]] = []  # (wi, si, is_vertical, step)
        for (axis, _coord), segs in by_line.items():
            if len(segs) < 2:
                continue
            is_vert = (axis == "V")
            # Sort by left/lo and walk: keep an "owner" per overlap chain.
            segs.sort(key=lambda s: (s[2], s[0]))
            # For every pair (i, j > i) with overlap > min_overlap, plan to
            # nudge j (the second owner). Nudge direction alternates.
            for i in range(len(segs)):
                wi_i, si_i, lo_i, hi_i = segs[i]
                for j in range(i + 1, len(segs)):
                    wi_j, si_j, lo_j, hi_j = segs[j]
                    if lo_j >= hi_i:
                        break  # sorted by lo, no further overlap on this line
                    if wi_i == wi_j:
                        continue  # same wire, ignore self-overlap (shouldn't happen)
                    overlap = min(hi_i, hi_j) - max(lo_i, lo_j)
                    if overlap < min_overlap:
                        continue
                    # Nudge j; step alternates per j-rank within this line so
                    # we get -1, +1, -2, +2, ... spread instead of stacking.
                    rank = j  # later owner among collinear segs
                    sign = 1 if rank % 2 else -1
                    mag = WIRE_STEP * ((rank + 1) // 2 + 1)
                    nudges.append((wi_j, si_j, is_vert, sign * mag))

        if not nudges:
            return

        # Apply nudges. We dedupe by (wi, si) keeping the largest |step| so a
        # single segment isn't moved multiple times in one pass.
        best: dict[tuple[int, int], tuple[bool, float]] = {}
        for wi, si, is_vert, step in nudges:
            key = (wi, si)
            if key not in best or abs(best[key][1]) < abs(step):
                best[key] = (is_vert, step)
        for (wi, si), (is_vert, step) in best.items():
            _nudge_segment(routing[wi], si, is_vert, step, boxes)
        # Re-strip zero-length segments that nudges may have produced.
        for info in routing.values():
            info["waypoints"] = _strip_zero_length(info["a1"], info["waypoints"], info["a2"])


def _nudge_segment(info: dict, seg_idx: int, is_vertical: bool,
                   step: float, boxes: list) -> None:
    """Move segment `seg_idx` of a wire's path by `step` perpendicular to its
    direction. The two adjacent waypoints are also moved so the path stays
    connected. Refuses the nudge if it would push the segment through a box.
    If the requested direction is blocked, automatically tries the opposite
    direction at the same magnitude.

    Segment indexing: `pts = [a1] + waypoints + [a2]`, segment `i` connects
    `pts[i]` to `pts[i+1]`. Waypoints in `info["waypoints"]` are at indices
    1..len(waypoints), so segment `i`'s endpoints map to waypoint indices
    `i - 1` and `i` (with `-1` and `len` referring to anchors which we don't move).
    """
    pts = [info["a1"]] + info["waypoints"] + [info["a2"]]
    n_wp = len(info["waypoints"])
    if seg_idx >= len(pts) - 1:
        return
    # Can only move segments whose BOTH endpoints are real waypoints (not anchors).
    # Waypoint indices: seg_idx-1 maps to waypoints[seg_idx-1]; seg_idx maps to
    # waypoints[seg_idx]. Anchor a1 is at pts[0] (waypoint index -1), a2 is at
    # pts[n_wp+1] (waypoint index n_wp). If either endpoint is an anchor, we
    # can't move the segment without breaking orthogonality, so refuse.
    wp_lo = seg_idx - 1
    wp_hi = seg_idx
    if wp_lo < 0 or wp_hi >= n_wp:
        return  # Touches an anchor; nudging would create a diagonal.
    p, q = pts[seg_idx], pts[seg_idx + 1]
    # Try the requested step first, then the opposite sign if blocked.
    for trial in (step, -step):
        if is_vertical:
            new_x = p[0] + trial
            new_p = (new_x, p[1]); new_q = (new_x, q[1])
            if any(_segment_crosses_box(new_p, new_q, b) for b in boxes):
                continue
            _shift_waypoint_x(info, wp_lo, new_x)
            _shift_waypoint_x(info, wp_hi, new_x)
            return
        else:
            new_y = p[1] + trial
            new_p = (p[0], new_y); new_q = (q[0], new_y)
            if any(_segment_crosses_box(new_p, new_q, b) for b in boxes):
                continue
            _shift_waypoint_y(info, wp_lo, new_y)
            _shift_waypoint_y(info, wp_hi, new_y)
            return
    # Both directions blocked; leave the segment alone (overlap persists).


def _shift_waypoint_x(info: dict, wp_idx: int, new_x: float) -> None:
    """Set the x-coordinate of waypoint `wp_idx`. wp_idx out of range = anchor;
    anchors are never moved (their coords are pinned by the components)."""
    wp = info["waypoints"]
    if 0 <= wp_idx < len(wp):
        x, y = wp[wp_idx]
        wp[wp_idx] = (new_x, y)


def _shift_waypoint_y(info: dict, wp_idx: int, new_y: float) -> None:
    wp = info["waypoints"]
    if 0 <= wp_idx < len(wp):
        x, y = wp[wp_idx]
        wp[wp_idx] = (x, new_y)


def _segment_crosses_box(p, q, box, slop: float = 2.0) -> bool:
    """Check if an axis-aligned segment p->q passes through the interior of `box`.

    `slop` shrinks the box slightly so that a wire hugging a box edge doesn't
    register as crossing through it.
    """
    bx1, by1, bx2, by2 = box
    bx1 += slop; by1 += slop; bx2 -= slop; by2 -= slop
    if bx2 <= bx1 or by2 <= by1:
        return False
    px, py = p; qx, qy = q
    if py == qy:  # horizontal
        if not (by1 < py < by2):
            return False
        return min(px, qx) < bx2 and max(px, qx) > bx1
    if px == qx:  # vertical
        if not (bx1 < px < bx2):
            return False
        return min(py, qy) < by2 and max(py, qy) > by1
    return False  # diagonals don't appear in our routing


def _largest_clear_gap(lx: float, rx: float,
                       boxes: list[tuple[float, float, float, float]],
                       padding: float) -> tuple[float, float] | None:
    """Within the open interval (lx, rx), find the largest x-range that no box
    covers. Returns (gap_lo, gap_hi) with `padding` margin pulled in from any
    box edge. Returns None if no usable gap exists.

    A box "covers" an x-range [b.x1, b.x2] for any y; we conservatively assume
    the box obstructs every wire in the column pair, since the alternative
    (per-wire y-aware corridor selection) is much more code and rarely needed
    once the over/under fallback exists.
    """
    if rx - lx < 4:
        return None
    # Collect box x-intervals that overlap (lx, rx). A box that *contains* the
    # whole interval blocks completely and yields no gap.
    intervals = []
    for (bx1, _by1, bx2, _by2) in boxes:
        if bx2 <= lx or bx1 >= rx:
            continue  # outside the search range
        intervals.append((max(bx1, lx), min(bx2, rx)))
    if not intervals:
        # No blockers; the whole range is one big gap.
        return (lx + padding, rx - padding) if rx - lx > 2 * padding else None

    # Merge overlapping/adjacent intervals.
    intervals.sort()
    merged = [intervals[0]]
    for a, b in intervals[1:]:
        if a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    # Build the list of free gaps between lx, the intervals, and rx.
    free: list[tuple[float, float]] = []
    cursor = lx
    for a, b in merged:
        if a - cursor > 2 * padding + 4:
            free.append((cursor + padding, a - padding))
        cursor = b
    if rx - cursor > 2 * padding + 4:
        free.append((cursor + padding, rx - padding))

    if not free:
        return None
    # Prefer the rightmost reasonably-sized gap (closer to destination -> less
    # crossing). Among gaps with usable width, pick the largest; ties go right.
    free.sort(key=lambda g: (g[1] - g[0], g[0]))
    return free[-1]


def _channel_waypoints(a1, a2, ch_x, src_jog, dst_jog):
    """Standard channel routing path corner points (between the two anchors)."""
    x1, y1 = a1
    x2, y2 = a2
    jy1 = y1 + src_jog
    jy2 = y2 + dst_jog
    jog_x1 = x1 + JOG_LEN if ch_x > x1 else x1 - JOG_LEN
    jog_x2 = x2 - JOG_LEN if ch_x < x2 else x2 + JOG_LEN
    return [
        (jog_x1, y1),    # exit stub horizontally
        (jog_x1, jy1),   # small y jog
        (ch_x,   jy1),   # horizontal to channel
        (ch_x,   jy2),   # vertical run down channel
        (jog_x2, jy2),   # horizontal toward destination
        (jog_x2, y2),    # y jog back to anchor row
    ]


def _escape_x(start_x: float, anchor_y: float, direction: int,
              boxes: list[tuple[float, float, float, float]],
              margin: float = JOG_LEN,
              y_range: tuple[float, float] | None = None) -> float:
    """Walk horizontally from `start_x` in `direction` until clear of every box.

    Two y-checks are performed:

    - The horizontal segment at `anchor_y` (the wire leaves the stub at this y).
    - The vertical segment that will then run from `anchor_y` to `y_range[1]`
      (the climb/drop to the detour line). Pass `y_range=(anchor_y, detour_y)`
      to enable. If omitted only the horizontal check runs (legacy behaviour).

    Returns an x at which both checks are satisfied. Strict inequalities are
    used at the boundary so we don't loop forever at `bx1 - margin`.
    """
    if y_range is None:
        y_lo = y_hi = anchor_y
    else:
        y_lo, y_hi = min(y_range), max(y_range)
    x = start_x
    for _ in range(16):
        blocking = []
        for b in boxes:
            bx1, by1, bx2, by2 = b
            # A box is blocking if its y-range overlaps the wire's y-range AND
            # its x-range (with margin) currently contains x.
            if by2 < y_lo - 1 or by1 > y_hi + 1:
                continue
            if bx1 - margin < x < bx2 + margin:
                blocking.append(b)
        if not blocking:
            return x
        if direction > 0:
            x = max(b[2] for b in blocking) + margin
        else:
            x = min(b[0] for b in blocking) - margin
    return x


def _wrap_waypoints(a1, a2, detour_y, kind, src_jog, dst_jog,
                    boxes: list[tuple[float, float, float, float]],
                    x_offset: float = 0.0):
    """Wrap-around routing: leave each stub horizontally, climb (over) or drop
    (under) past `detour_y`, traverse, then descend/climb back.

    The horizontal jog at each end must escape every box that sits at the
    anchor's y row AND clear the stub-tip column of any neighbouring component
    — otherwise the long vertical run will graze adjacent stubs (`WRAP_MARGIN`
    handles this; it's larger than `JOG_LEN` by `PIN_STUB_LEN`).

    `x_offset` shifts both jog x's by the same signed amount so wires within
    the same wrap group don't share an exact vertical column. The caller is
    expected to give each wire in a group a different offset.

    We try all four (src_dir, dst_dir) combinations of {+1,-1} and pick the
    one with the fewest box intersections (ties broken by total path length).
    """
    x1, y1 = a1
    x2, y2 = a2

    candidates = []
    for src_dir in (1, -1):
        for dst_dir in (1, -1):
            base_x1 = _escape_x(x1, y1, src_dir, boxes,
                                margin=WRAP_MARGIN, y_range=(y1, detour_y))
            base_x2 = _escape_x(x2, y2, dst_dir, boxes,
                                margin=WRAP_MARGIN, y_range=(y2, detour_y))
            # Apply the per-wire offset further outward from the anchor in
            # whichever direction we just escaped. `x_offset` is non-negative
            # so we never push back toward an obstacle.
            jog_x1 = base_x1 + (src_dir * x_offset)
            jog_x2 = base_x2 + (dst_dir * x_offset)
            wp = [(jog_x1, y1), (jog_x1, detour_y),
                  (jog_x2, detour_y), (jog_x2, y2)]
            # Count box intersections across the full path.
            pts = [a1] + wp + [a2]
            crosses = 0
            length = 0.0
            for p, q in zip(pts, pts[1:]):
                length += abs(q[0] - p[0]) + abs(q[1] - p[1])
                for b in boxes:
                    if _segment_crosses_box(p, q, b):
                        crosses += 1
            candidates.append(((crosses, length), wp))

    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def _routed_path(a1: tuple[float, float], a2: tuple[float, float],
                 waypoints: list[tuple[float, float]]) -> str:
    """Build an orthogonal SVG path string from the anchors and waypoints."""
    parts = [f"M {a1[0]:.1f} {a1[1]:.1f}"]
    for x, y in waypoints:
        parts.append(f"L {x:.1f} {y:.1f}")
    parts.append(f"L {a2[0]:.1f} {a2[1]:.1f}")
    return " ".join(parts)


def _wire_label(w) -> str:
    if w.signal:
        return w.signal
    _, pin = parse_pin_ref(w.to)
    return pin


# ---------------------------------------------------------------------------
# Main render entry point
# ---------------------------------------------------------------------------

def render(harness: Harness, positions: dict[str, tuple[float, float]]) -> str:
    stub_sides = _compute_stub_sides(harness, positions)
    pin_sides_map = _compute_pin_sides(harness, positions, stub_sides)

    body = []
    all_anchors: dict[str, dict[str, tuple[float, float]]] = {}

    for cid, comp in harness.components.items():
        x, y = positions.get(cid, (50, 50))
        if isinstance(comp, Connector):
            svg, anchors, _ = _connector_box(
                comp, x, y,
                stub_side=stub_sides.get(cid, "right"),
                pin_sides=pin_sides_map.get(cid) or None,
            )
        elif isinstance(comp, Bulkhead):
            svg, anchors, _ = _bulkhead_box(comp, x, y)
        elif isinstance(comp, Splice):
            svg, anchors, _ = _splice_node(comp, x, y)
        else:
            continue
        body.append(svg)
        all_anchors[cid] = anchors

    # Collect bounding boxes of all components.
    #   `body_extents`  - the component's solid body only (no stub extensions).
    #                     Used by routing to know what to avoid.
    #   `box_extents`   - body PLUS stubs and jog padding. Used for viewBox
    #                     computation and the layout/anchor sizing.
    body_extents: list[tuple[float, float, float, float]] = []
    box_extents: list[tuple[float, float, float, float]] = []
    # Stricter obstacle list used by nudge-pass: includes dual-stub regions so
    # collinear-segment separation can't push verticals into a stub column.
    nudge_extents: list[tuple[float, float, float, float]] = []
    for cid, comp in harness.components.items():
        x, y = positions.get(cid, (50, 50))
        if isinstance(comp, Connector):
            has_dual = bool(pin_sides_map.get(cid))
            w = _connector_width(comp, dual=has_dual)
            h = HEADER_H + len(comp.pins) * PIN_PITCH + 8
            stub_ext = PIN_STUB_LEN + JOG_LEN
            # For dual-stub connectors, the stubs themselves are obstacles —
            # other wires must not run vertically through the stub column on
            # either side. Single-side stubs already have the existing
            # body-edge-as-channel behaviour because the stub side is chosen
            # to face the neighbour, so the off-side has no stubs.
            if has_dual:
                body_extents.append((x, y, x + w, y + h))
                # Block stub-tip column with extra px past the segment-crossing
                # slop so nudges can't hug the column.
                stub_block = PIN_STUB_LEN + 4
                nudge_extents.append((x - stub_block, y, x + w + stub_block, y + h))
                box_extents.append((x - stub_ext, y, x + w + stub_ext, y + h))
            else:
                body_extents.append((x, y, x + w, y + h))
                nudge_extents.append((x, y, x + w, y + h))
                if stub_sides.get(cid, "right") == "right":
                    box_extents.append((x, y, x + w + stub_ext, y + h))
                else:
                    box_extents.append((x - stub_ext, y, x + w, y + h))
        elif isinstance(comp, Bulkhead):
            bh = HEADER_H + comp.positions * PIN_PITCH + 8
            bw = _bulkhead_width(comp)
            body_extents.append((x, y, x + bw, y + bh))
            nudge_extents.append((x, y, x + bw, y + bh))
            box_extents.append((x - PIN_STUB_LEN, y, x + bw + PIN_STUB_LEN, y + bh))

    routing = _build_routing(harness.wires, all_anchors, body_extents, positions,
                             nudge_obstacles=nudge_extents)

    wire_layer = []
    label_layer = []

    for i, w in enumerate(harness.wires):
        if i not in routing:
            continue
        info = routing[i]
        a1, a2 = info["a1"], info["a2"]
        waypoints = info["waypoints"]
        d = _routed_path(a1, a2, waypoints)

        color = _color_hex(w.color)
        gauge = w.gauge or 18
        stroke_w = max(1.5, 5 - (gauge / 4))

        tooltip = f"{w.from_} to {w.to}"
        if w.gauge:
            tooltip += f" | {w.gauge} AWG"
        if w.signal:
            tooltip += f" | {w.signal}"

        if _is_white(w.color):
            wire_layer.append(
                f'<path d="{d}" stroke="{WHITE_OUTLINE}" stroke-width="{stroke_w + 2.0:.1f}" '
                f'fill="none" opacity="0.45"/>'
            )
        wire_layer.append(
            f'<path class="wire" data-wire-index="{i}" d="{d}" stroke="{color}" '
            f'stroke-width="{stroke_w:.1f}" fill="none" opacity="0.85">'
            f'<title>{escape(tooltip)}</title></path>'
        )
        # Invisible wider hit target so thin wires are easy to click.
        wire_layer.append(
            f'<path class="wire-hit" data-wire-index="{i}" d="{d}" stroke="transparent" '
            f'stroke-width="{max(stroke_w + 8, 12):.1f}" fill="none" pointer-events="stroke"/>'
        )

        # Label placement: pick the midpoint of the longest segment.
        # This works for both channel and wrap-around routes.
        pts = [a1] + list(waypoints) + [a2]
        best = (0, 0, 0, 0, 0.0)  # x, y, dx, dy, length
        for (px, py), (qx, qy) in zip(pts, pts[1:]):
            length = abs(qx - px) + abs(qy - py)
            if length > best[4]:
                best = (px, py, qx, qy, length)
        lx, ly, qx, qy, _ = best
        mid_x = (lx + qx) / 2
        mid_y = (ly + qy) / 2
        # Vertical-ish segment -> rotate label; horizontal -> upright.
        is_vertical = abs(qy - ly) > abs(qx - lx)
        rot = f' transform="rotate(-90,{mid_x:.1f},{mid_y:.1f})"' if is_vertical else ""
        label = escape(_wire_label(w))
        label_layer.append(
            f'<text x="{mid_x:.1f}" y="{mid_y:.1f}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-family="monospace" font-size="8" fill="#263238"{rot} '
            f'paint-order="stroke" stroke="#eceff1" stroke-width="3" stroke-linejoin="round">'
            f'{label}</text>'
        )

    # Compute viewBox from box extents (not just anchor points — boxes extend left of their stubs)
    all_x = [bx1 for (bx1, _, bx2, _) in box_extents] + [bx2 for (_, _, bx2, _) in box_extents]
    all_y = [by1 for (_, by1, _, by2) in box_extents] + [by2 for (_, _, _, by2) in box_extents]
    # Also include every waypoint coord so wrap-around routes aren't clipped
    for info in routing.values():
        for (px, py) in info["waypoints"]:
            all_x.append(px); all_y.append(py)
    if not all_x:
        all_x, all_y = [0, 800], [0, 600]
    minx = min(all_x) - 30
    maxx = max(all_x) + 30
    miny = min(all_y) - 60
    maxy = max(all_y) + 40
    vb = f"{minx:.1f} {miny:.1f} {maxx - minx:.1f} {maxy - miny:.1f}"

    title_text = escape(harness.metadata.name)

    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" font-family="sans-serif">\n'
        f'  <rect x="{minx:.1f}" y="{miny:.1f}" width="{maxx-minx:.1f}" height="{maxy-miny:.1f}" fill="#eceff1"/>\n'
        f'  <text x="{minx + 20:.1f}" y="{miny + 35:.1f}" font-size="18" font-weight="700" fill="#263238">'
        f'{title_text}</text>\n'
        f'  <g class="wires">\n    {"".join(wire_layer)}\n  </g>\n'
        f'  <g class="wire-labels">\n    {"".join(label_layer)}\n  </g>\n'
        f'  <g class="components">\n    {"".join(body)}\n  </g>\n'
        f'</svg>'
    )
