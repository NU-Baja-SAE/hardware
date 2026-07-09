"""Live-preview + interactive-editor HTTP server.

Run with `python cli.py serve baja_example.yaml [--port 8765]`. Open the URL
the CLI prints. The browser shows the rendered SVG, an inspector for the
selected element, and a toolbar (Connect mode, Auto-arrange).

Architecture
============

         +-------+   POST /edit    +------------+   atomic write   +------+
browser  | UI/JS | --------------> | serve.py   | ---------------> | YAML |
         +-------+   GET /target   |            |                  +------+
            ^                      |            |                     |
            | EventSource          |            |  mtime poll         | mtime
            |   /events (SSE)      |            | <-------------------+
            |                      | rebuild()  |
            +--------- snapshots --+ render()   |
                                   +------------+

`PreviewState` owns the rendered SVG, the parsed `Harness`, the revision
counter, and the broadcast list. Every successful YAML reload bumps the
revision and pushes a snapshot to all SSE clients. Edits are dispatched to
`yaml_edit.py`, which writes the file; the file watcher then sees the mtime
change and triggers `rebuild()` -> snapshot -> SSE -> browser SVG refresh.

Why stdlib-only HTTP
--------------------
Avoiding FastAPI/Flask/Starlette keeps the install footprint to just
`pydantic + ruamel.yaml`. SSE was chosen over WebSockets because the data
flow is one-way (server -> browser) and SSE works trivially in pure Python
(`text/event-stream` + `data: ...\\n\\n`).

Key design rules
----------------
- **YAML is canonical.** The browser is a viewer + remote control. It never
  holds editor state that isn't immediately written to YAML. If the user
  edits the file in their text editor while the browser is open, the file
  wins and the inspector reloads.
- **Read endpoints are cheap. Write endpoints go through `edit_lock`.**
  Multiple in-flight POSTs are serialised so writes can't race on disk.
- **Revision check.** `POST /edit` for structural kinds (wire/component/pin)
  requires the client's last-seen `revision` to match. If it doesn't, return
  409 and let the client re-fetch. Position drags and auto-arrange skip the
  check (refusing a drag because the SVG just rerendered would be obnoxious).
- **Disconnects are not errors.** `OSError` is swallowed at the handler
  boundary (`Handler.handle`) and inside the SSE loop. Browsers routinely
  abort sockets and we never want to spam tracebacks.

HTTP routes
-----------
GET  /                   index.html (SVG host + toolbar + inspector)
GET  /svg                current rendered SVG
GET  /state              {revision, error, warnings}
GET  /events             SSE stream of state snapshots (heartbeat every 15s)
GET  /target/wire/<i>            details for the inspector
GET  /target/component/<id>
GET  /target/pin/<comp>/<pin>
POST /edit               body: {kind, ...kind-specific fields, revision?}
                         kinds: wire, component, pin, position, auto-arrange,
                                add-wire, delete-wire, delete-component

The frontend lives in `INDEX_HTML` below — it is intentionally one self-
contained file so there's nothing to bundle and nothing to install.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from parser import (
    load, validate_no_double_connections,
    included_files, component_origin, wire_origin, root_path, parse_pin_ref,
    alias_prefix, local_component_id, strip_alias_from_pin_ref,
    loaded_subassemblies,
)
from layout import layout
from render import render
from schema import Connector, Bulkhead, Splice
import yaml_edit


POLL_INTERVAL = 0.2  # seconds; mtime polling is fine at this rate for a single file


# ---------------------------------------------------------------------------
# Render state — shared across all client connections
# ---------------------------------------------------------------------------

class PreviewState:
    """Holds the current SVG + error state and broadcasts updates to SSE clients."""

    def __init__(self, yaml_path: Path):
        self.yaml_path = yaml_path
        self.lock = threading.Lock()
        self.svg: str = ""
        self.error: str | None = None
        self.warnings: list[str] = []
        self.revision: int = 0
        self.harness = None  # last successfully parsed Harness, for /target lookups
        self._listeners: list[queue.Queue] = []
        self._listeners_lock = threading.Lock()
        # Serialises edits so two PATCHes can't race on file write.
        self.edit_lock = threading.Lock()
        self.rebuild()

    def rebuild(self) -> None:
        """Reload YAML, re-render. On failure, keep last good SVG and store the error."""
        try:
            harness = load(str(self.yaml_path))
            warnings = validate_no_double_connections(harness)
            positions = layout(harness)
            svg = render(harness, positions)
            with self.lock:
                self.svg = svg
                self.error = None
                self.warnings = list(warnings)
                self.harness = harness
                self.revision += 1
        except Exception as exc:
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
                self.revision += 1
            traceback.print_exc()
        self._broadcast()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "revision": self.revision,
                "error": self.error,
                "warnings": self.warnings,
            }

    def add_listener(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def remove_listener(self, q: queue.Queue) -> None:
        with self._listeners_lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def _broadcast(self) -> None:
        snap = self.snapshot()
        with self._listeners_lock:
            for q in list(self._listeners):
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    pass  # slow client; skip


# ---------------------------------------------------------------------------
# File watcher
# ---------------------------------------------------------------------------

def watch_file(state: PreviewState, stop: threading.Event) -> None:
    """Poll mtimes of the root YAML AND every currently-included subassembly.

    The list of watched files comes from `included_files()`, which is
    refreshed by `parser.load()` on every successful parse. A change to any
    of them triggers a rebuild; if the root file itself adds or removes
    `includes:` entries, the next rebuild updates the watch list naturally.
    """
    last_mtimes: dict = {}

    def current_watch_paths():
        # Always watch the root; on top of that, watch whatever the last
        # parse reported. After a failed parse `included_files()` returns
        # whatever the last successful parse left there, so we don't lose
        # subassembly watches on a transient root-file syntax error.
        paths = {state.yaml_path}
        for p in included_files():
            paths.add(p)
        return paths

    def snapshot_mtimes(paths):
        out = {}
        for p in paths:
            try:
                out[p] = p.stat().st_mtime
            except OSError:
                out[p] = None
        return out

    last_mtimes = snapshot_mtimes(current_watch_paths())
    while not stop.wait(POLL_INTERVAL):
        paths = current_watch_paths()
        mtimes = snapshot_mtimes(paths)
        if mtimes != last_mtimes:
            last_mtimes = mtimes
            # Editors often write in two steps; tiny debounce avoids reading mid-write.
            time.sleep(0.05)
            state.rebuild()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Wirelab live preview</title>
<style>
  html, body { margin: 0; height: 100%; font-family: -apple-system, system-ui, sans-serif;
               background: #eceff1; color: #263238; }
  #app { display: grid; grid-template-columns: 1fr 320px;
         grid-template-rows: 36px 1fr; height: 100%;
         grid-template-areas: "toolbar inspector" "canvas inspector"; }
  #toolbar { grid-area: toolbar; background: #37474f; color: #eceff1;
             display: flex; align-items: center; padding: 0 10px; gap: 8px;
             font-size: 12px; }
  #toolbar button { background: #455a64; color: white; border: none;
                    padding: 5px 12px; font-size: 12px; border-radius: 3px;
                    cursor: pointer; font-family: inherit; }
  #toolbar button:hover { background: #546e7a; }
  #toolbar button.active { background: #1565c0; }
  #toolbar .spacer { flex: 1; }
  #toolbar .hint { color: #b0bec5; font-style: italic; }
  #svg-host { grid-area: canvas; }
  #inspector { grid-area: inspector; }
  #status { position: fixed; top: 8px; right: 336px; padding: 4px 10px; border-radius: 4px;
            font-size: 12px; background: #37474f; color: white; opacity: 0.85; z-index: 10; }
  #status.error { background: #c62828; }
  #status.ok { background: #2e7d32; }
  #banner { position: fixed; top: 0; left: 0; right: 320px; padding: 10px 16px;
            background: #c62828; color: white; font-family: monospace; font-size: 12px;
            white-space: pre-wrap; display: none; z-index: 9; }
  #warnings { position: fixed; bottom: 8px; left: 12px; max-width: 50%;
              background: #fff8e1; color: #6d4c00; border-left: 4px solid #f9a825;
              padding: 6px 28px 6px 10px; font-size: 12px; font-family: monospace;
              white-space: pre-wrap; display: none; z-index: 9; border-radius: 2px; }
  #warnings-close { position: absolute; top: 2px; right: 4px; cursor: pointer;
                    background: transparent; border: none; color: #6d4c00;
                    font-size: 16px; line-height: 1; padding: 2px 6px; }
  #warnings-close:hover { color: #000; }
  #svg-host { overflow: hidden; background: #eceff1; position: relative; }
  #svg-host svg { display: block; width: 100%; height: 100%;
                  user-select: none; -webkit-user-select: none; }
  body.pan-mode #svg-host { cursor: grab; }
  body.panning #svg-host { cursor: grabbing; }
  #inspector { background: #fafafa; border-left: 1px solid #cfd8dc; padding: 14px;
               overflow-y: auto; font-size: 13px; }
  #inspector h2 { margin: 0 0 4px 0; font-size: 14px; color: #455a64; }
  #inspector .sub { color: #78909c; font-size: 11px; margin-bottom: 14px;
                    font-family: monospace; }
  #inspector .empty { color: #90a4ae; font-style: italic; padding-top: 20px; text-align: center; }
  #inspector label { display: block; margin: 8px 0 2px; font-size: 11px;
                     color: #546e7a; text-transform: uppercase; letter-spacing: 0.05em; }
  #inspector input { width: 100%; box-sizing: border-box; padding: 5px 7px;
                     font-size: 13px; border: 1px solid #cfd8dc; border-radius: 3px;
                     font-family: inherit; }
  #inspector input:focus { outline: none; border-color: #1565c0; }
  #inspector .btn-row { margin-top: 14px; display: flex; gap: 8px; }
  #inspector button { padding: 6px 14px; font-size: 13px; border: none;
                      border-radius: 3px; cursor: pointer; }
  #inspector .save { background: #1565c0; color: white; }
  #inspector .save:hover { background: #0d47a1; }
  #inspector .cancel { background: #eceff1; color: #455a64; }
  #inspector .cancel:hover { background: #cfd8dc; }
  #inspector .toast { margin-top: 10px; padding: 6px 8px; border-radius: 3px;
                      font-size: 12px; display: none; }
  #inspector .toast.ok { background: #c8e6c9; color: #1b5e20; display: block; }
  #inspector .toast.err { background: #ffcdd2; color: #b71c1c; display: block; }
  #inspector .pin-list { font-family: monospace; font-size: 11px;
                         border: 1px solid #eceff1; border-radius: 3px;
                         max-height: 180px; overflow-y: auto; margin-top: 4px; }
  #inspector .pin-list .row { padding: 3px 6px; cursor: pointer; }
  #inspector .pin-list .row:hover { background: #e3f2fd; }
  #inspector .delete { background: #c62828; color: white; }
  #inspector .delete:hover { background: #b71c1c; }
  /* Selection highlight in SVG */
  .wirelab-sel { outline: 2px solid #ff6f00; outline-offset: 2px; }
  path.wirelab-sel { stroke: #ff6f00 !important; stroke-width: 5 !important; opacity: 1 !important; }
  circle.wirelab-pin-sel { fill: #ff6f00 !important; opacity: 0.5; }
  /* Make selectable elements look clickable */
  .wire-hit, .pin-hit { cursor: pointer; }
  g.component, g.bulkhead, g.splice { cursor: pointer; }
  /* Connect-mode visuals */
  body.connect-mode #svg-host { cursor: crosshair; }
  body.connect-mode .pin-hit { stroke: #1565c0; stroke-width: 1.5; fill: #bbdefb; opacity: 0.7; }
  circle.wirelab-pin-pending { fill: #1565c0 !important; opacity: 0.9 !important; }
  /* Drag visuals */
  g.component.wirelab-dragging, g.bulkhead.wirelab-dragging,
  g.splice.wirelab-dragging { opacity: 0.7; }
  /* Modal */
  #modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.4);
                    display: none; align-items: center; justify-content: center;
                    z-index: 100; }
  #modal-backdrop.show { display: flex; }
  #modal { background: #fafafa; border-radius: 4px; padding: 18px 20px;
           min-width: 360px; max-width: 480px; box-shadow: 0 6px 24px rgba(0,0,0,0.3); }
  #modal h2 { margin: 0 0 4px 0; font-size: 15px; color: #455a64; }
  #modal .sub { color: #78909c; font-size: 11px; margin-bottom: 14px; }
  #modal label { display: block; margin: 8px 0 2px; font-size: 11px;
                 color: #546e7a; text-transform: uppercase; letter-spacing: 0.05em; }
  #modal input, #modal textarea { width: 100%; box-sizing: border-box; padding: 5px 7px;
                                  font-size: 13px; border: 1px solid #cfd8dc; border-radius: 3px;
                                  font-family: inherit; }
  #modal textarea { font-family: monospace; min-height: 140px; resize: vertical; }
  #modal input:focus, #modal textarea:focus { outline: none; border-color: #1565c0; }
  #modal .btn-row { margin-top: 14px; display: flex; gap: 8px; justify-content: flex-end; }
  #modal button { padding: 6px 14px; font-size: 13px; border: none;
                  border-radius: 3px; cursor: pointer; }
  #modal .save { background: #1565c0; color: white; }
  #modal .save:hover { background: #0d47a1; }
  #modal .cancel { background: #eceff1; color: #455a64; }
  #modal .cancel:hover { background: #cfd8dc; }
  #modal .err { color: #c62828; font-size: 12px; margin-top: 8px; min-height: 14px; }
</style>
</head>
<body>
<div id="app">
  <div id="toolbar">
    <button id="btn-connect" title="Click two pins to wire them together (Esc to cancel)">Connect mode</button>
    <button id="btn-new-component" title="Create a new component (connector, bulkhead, splice…)">New component</button>
    <button id="btn-arrange" title="Clear all manual positions and re-layout">Auto-arrange</button>
    <button id="btn-reset-view" title="Reset pan/zoom">Reset view</button>
    <span class="spacer"></span>
    <span class="hint" id="toolbar-hint"></span>
  </div>
  <div id="svg-host">loading…</div>
  <aside id="inspector">
    <div class="empty">Click a wire, pin, or component<br>to inspect.</div>
  </aside>
</div>
<div id="status">connecting…</div>
<div id="banner"></div>
<div id="warnings"><button id="warnings-close" title="Dismiss">&times;</button><span id="warnings-text"></span></div>
<div id="modal-backdrop">
  <div id="modal">
    <h2>New component</h2>
    <div class="sub">Adds a new entry to the <code>components:</code> section.</div>
    <form id="new-comp-form">
      <label>Type</label>
      <select name="type" id="new-comp-type" style="width:100%;padding:5px 7px;font-size:13px;border:1px solid #cfd8dc;border-radius:3px;font-family:inherit">
        <option value="connector">connector</option>
        <option value="device">device</option>
        <option value="bulkhead">bulkhead</option>
        <option value="splice">splice</option>
      </select>
      <label>ID</label>
      <input name="id" autocomplete="off" placeholder="e.g. MAIN_BOX_IN" required>
      <label>Label (optional)</label>
      <input name="label" autocomplete="off" placeholder="e.g. Main Box Entry">
      <label>Zone (optional)</label>
      <input name="zone" list="zone-suggestions" autocomplete="off" placeholder="e.g. boxes, engine, chassis…">
      <datalist id="zone-suggestions">
        <option value="sensors_main"><option value="sensors_hud">
        <option value="engine"><option value="chassis">
        <option value="power"><option value="cabin"><option value="dash">
        <option value="boxes"><option value="boards">
      </datalist>

      <div id="field-pins">
        <label>Pins (one name per line; auto-numbered)</label>
        <textarea name="pins" placeholder="PWR&#10;GND&#10;CANH&#10;CANL"></textarea>
      </div>
      <div id="field-positions" style="display:none">
        <label>Positions (pin pairs, generates A1..An / B1..Bn)</label>
        <input name="positions" type="number" min="1" value="4">
      </div>
      <div id="field-pin-count" style="display:none">
        <label>Pin count (>= 2)</label>
        <input name="pin_count" type="number" min="2" value="2">
      </div>

      <div class="err" id="new-comp-err"></div>
      <div class="btn-row">
        <button type="button" class="cancel" id="new-comp-cancel">Cancel</button>
        <button type="submit" class="save">Create</button>
      </div>
    </form>
  </div>
</div>
<script>
const statusEl = document.getElementById('status');
const banner = document.getElementById('banner');
const warnings = document.getElementById('warnings');
const warningsText = document.getElementById('warnings-text');
const host = document.getElementById('svg-host');
const inspector = document.getElementById('inspector');

// Track whether the user has dismissed the current warnings, so we don't
// keep popping them back open on every SSE snapshot. Reset when the warnings
// payload actually changes.
let warningsDismissedKey = null;
document.getElementById('warnings-close').addEventListener('click', () => {
  warnings.style.display = 'none';
  warningsDismissedKey = warnings.dataset.key || '';
});

// ---------- Pan / zoom (manipulates the SVG viewBox) ----------
// View state is persisted across SVG re-renders so an edit doesn't snap
// the user back to the default view.
let view = null;  // {x, y, w, h}  current viewBox
let baseView = null;  // initial viewBox parsed from the SVG, for reset
let pan = null;  // {startX, startY, vx, vy}

function applyView() {
  const svg = host.querySelector('svg');
  if (!svg || !view) return;
  svg.setAttribute('viewBox', view.x + ' ' + view.y + ' ' + view.w + ' ' + view.h);
}

function resetView() {
  if (baseView) view = {...baseView};
  applyView();
}

function initView() {
  const svg = host.querySelector('svg');
  if (!svg) return;
  const vb = svg.getAttribute('viewBox');
  if (vb) {
    const [x, y, w, h] = vb.split(/[\\s,]+/).map(parseFloat);
    baseView = {x, y, w, h};
  } else {
    const w = parseFloat(svg.getAttribute('width')) || 1000;
    const h = parseFloat(svg.getAttribute('height')) || 600;
    baseView = {x: 0, y: 0, w, h};
  }
  if (!view) view = {...baseView};
  applyView();
}

function svgViewPoint(evt) {
  // Convert client pixel coords to current viewBox coords (account for the
  // host's CSS box, not the screen CTM, so wheel zoom is intuitive).
  const rect = host.getBoundingClientRect();
  const fx = (evt.clientX - rect.left) / rect.width;
  const fy = (evt.clientY - rect.top) / rect.height;
  return {x: view.x + fx * view.w, y: view.y + fy * view.h};
}

host.addEventListener('wheel', (e) => {
  if (!view) return;
  e.preventDefault();
  const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
  const p = svgViewPoint(e);
  view.x = p.x - (p.x - view.x) * factor;
  view.y = p.y - (p.y - view.y) * factor;
  view.w *= factor;
  view.h *= factor;
  applyView();
}, {passive: false});

// Pan with middle-mouse, or with left-mouse on empty SVG background
// (i.e. when the click isn't on a component / wire / pin).
host.addEventListener('mousedown', (e) => {
  if (!view) return;
  const isMiddle = e.button === 1;
  const isBackground = e.button === 0 && e.target.tagName === 'svg';
  if (!isMiddle && !isBackground) return;
  e.preventDefault();
  pan = {startX: e.clientX, startY: e.clientY, vx: view.x, vy: view.y};
  document.body.classList.add('panning');
});

window.addEventListener('mousemove', (e) => {
  if (!pan) return;
  const rect = host.getBoundingClientRect();
  const dx = (e.clientX - pan.startX) / rect.width * view.w;
  const dy = (e.clientY - pan.startY) / rect.height * view.h;
  view.x = pan.vx - dx;
  view.y = pan.vy - dy;
  applyView();
});

window.addEventListener('mouseup', () => {
  if (pan) {
    pan = null;
    document.body.classList.remove('panning');
  }
});

document.getElementById('btn-reset-view').addEventListener('click', resetView);

let currentRev = 0;
// Currently selected target so we can re-select after a re-render.
// Shape: {kind: 'wire'|'component'|'pin', ref: string}  where ref is the URL tail.
let selection = null;

// Connect-mode state. When `connectMode` is true the next pin click becomes
// the source; the click after that completes the wire. Esc cancels.
let connectMode = false;
let connectFirst = null;  // {ref: 'comp.pin', el: SVGElement}

function setConnectMode(on) {
  connectMode = on;
  document.body.classList.toggle('connect-mode', on);
  document.getElementById('btn-connect').classList.toggle('active', on);
  if (!on) clearConnectPending();
  toolbarHint(on ? 'click two pins to wire them (Esc to cancel)' : '');
}

function clearConnectPending() {
  if (connectFirst) {
    connectFirst.el.classList.remove('wirelab-pin-pending');
    connectFirst = null;
  }
}

async function handleConnectClick(ref, el) {
  if (!connectFirst) {
    connectFirst = {ref, el};
    el.classList.add('wirelab-pin-pending');
    toolbarHint('first pin: ' + ref + ' — click second pin');
    return;
  }
  if (connectFirst.ref === ref) {
    toolbarHint('cannot wire a pin to itself', true);
    return;
  }
  const fromRef = connectFirst.ref;
  const toRef = ref;
  clearConnectPending();
  try {
    const r = await fetch('/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: 'add-wire', from: fromRef, to: toRef,
                            extra: {gauge: 18}}),
    });
    const j = await r.json();
    if (!r.ok) {
      toolbarHint(j.error || ('HTTP ' + r.status), true);
      return;
    }
    toolbarHint('wired ' + fromRef + ' to ' + toRef + '; auto-selecting');
    // Select the new wire so the user can immediately set color/signal.
    selection = {kind: 'wire', ref: String(j.index)};
    // Stay in connect mode so they can wire several in a row.
  } catch (err) {
    toolbarHint('add-wire failed: ' + err, true);
  }
}

function toolbarHint(msg, isError) {
  const el = document.getElementById('toolbar-hint');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = isError ? '#ef9a9a' : '#b0bec5';
}

// Esc cancels connect mode.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (connectMode) setConnectMode(false);
  }
});

// ---------- SVG load + selection wiring ----------

async function loadSvg() {
  try {
    const r = await fetch('/svg?t=' + Date.now());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    host.innerHTML = await r.text();
    initView();
    wireSvgEvents();
    if (selection) refreshInspector();  // re-fetch in case data changed
    if (selection) reapplyHighlight();
  } catch (e) {
    host.innerHTML = '<p style="padding:20px;color:#c62828">SVG load failed: ' + e + '</p>';
  }
}

function wireSvgEvents() {
  const svg = host.querySelector('svg');
  if (!svg) return;

  // ---- Drag state for component repositioning ----
  let drag = null;  // {comp, group, startMouse, startSvg, moved}

  function svgPoint(evt) {
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse());
  }

  // Mouse down on a draggable component group starts a potential drag.
  svg.addEventListener('mousedown', (e) => {
    if (connectMode) return;        // pins handle their own clicks in connect mode
    if (e.button !== 0) return;
    const group = findComponentGroup(e.target);
    if (!group) return;
    e.preventDefault();
    const p = svgPoint(e);
    drag = {
      comp: group.dataset.id,
      group,
      startMouse: {x: p.x, y: p.y},
      // Read current translate from a wirelab-tx wrapper, or assume 0.
      tx: 0, ty: 0,
      moved: false,
    };
  });

  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    const p = svgPoint(e);
    const dx = p.x - drag.startMouse.x;
    const dy = p.y - drag.startMouse.y;
    if (!drag.moved && Math.hypot(dx, dy) > 4) {
      drag.moved = true;
      drag.group.classList.add('wirelab-dragging');
    }
    if (drag.moved) {
      // Live preview: just translate the group visually. The real position
      // is committed to YAML on mouseup; until then wires stay anchored to
      // the old position (they'll snap on the rerender).
      drag.group.setAttribute('transform', `translate(${dx} ${dy})`);
    }
  });

  window.addEventListener('mouseup', async (e) => {
    if (!drag) return;
    const wasDrag = drag.moved;
    const cur = drag;
    drag = null;
    if (!wasDrag) {
      cur.group.classList.remove('wirelab-dragging');
      return;  // treat as click; the click handler below will select it
    }
    // Compute the new position in SVG coords. We need the component's
    // *original* x,y to add the delta to. Easiest: read from the box;
    // simpler: ask the server to recompute. We instead post the delta in
    // SVG units and let the server snap; but the server expects absolute
    // coords. Use original_pos + delta where original_pos comes from the
    // bounding box of the inner geometry (we compensate for our transform).
    const p = svgPoint(e);
    const dx = p.x - cur.startMouse.x;
    const dy = p.y - cur.startMouse.y;
    // Read the component's current absolute position from one of its child
    // shape attributes — the renderer uses the same x for the outer rect/circle.
    const child = cur.group.querySelector('rect, circle');
    let baseX = 0, baseY = 0;
    if (child) {
      if (child.tagName === 'rect') {
        baseX = parseFloat(child.getAttribute('x')) || 0;
        baseY = parseFloat(child.getAttribute('y')) || 0;
      } else if (child.tagName === 'circle') {
        baseX = parseFloat(child.getAttribute('cx')) || 0;
        baseY = parseFloat(child.getAttribute('cy')) || 0;
      }
    }
    const newX = baseX + dx;
    const newY = baseY + dy;
    cur.group.removeAttribute('transform');
    try {
      const r = await fetch('/edit', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({kind: 'position', id: cur.comp, x: newX, y: newY}),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        toolbarHint('drag failed: ' + (j.error || r.status), true);
      }
    } catch (err) {
      toolbarHint('drag failed: ' + err, true);
    } finally {
      cur.group.classList.remove('wirelab-dragging');
    }
  });

  // ---- Click handler (selection / connect-mode pin pick) ----
  svg.addEventListener('click', (e) => {
    let el = e.target;
    while (el && el !== svg) {
      if (el.classList && el.classList.contains('pin-hit')) {
        const c = el.dataset.comp, p = el.dataset.pin;
        const ref = c + '.' + (el.dataset.pinName || p);
        if (connectMode) {
          handleConnectClick(ref, el);
          return;
        }
        select({kind: 'pin', ref: c + '/' + (el.dataset.pinName || p)});
        return;
      }
      if (connectMode) { el = el.parentNode; continue; }  // ignore non-pin clicks in connect mode
      if (el.classList && (el.classList.contains('wire-hit') || el.classList.contains('wire'))) {
        const idx = el.dataset.wireIndex;
        select({kind: 'wire', ref: idx});
        return;
      }
      if (el.tagName === 'g' && el.dataset && el.dataset.id) {
        select({kind: 'component', ref: el.dataset.id});
        return;
      }
      el = el.parentNode;
    }
    if (connectMode) return;
    selection = null;
    clearHighlight();
    renderEmpty();
  });
}

function findComponentGroup(target) {
  // Walk up to a g[data-id] but only those that are top-level component groups.
  let el = target;
  while (el && el.tagName !== 'svg') {
    if (el.tagName === 'g' && el.dataset && el.dataset.id &&
        (el.classList.contains('component') ||
         el.classList.contains('bulkhead') ||
         el.classList.contains('splice'))) {
      return el;
    }
    el = el.parentNode;
  }
  return null;
}

function clearHighlight() {
  host.querySelectorAll('.wirelab-sel').forEach(el => el.classList.remove('wirelab-sel'));
  host.querySelectorAll('.wirelab-pin-sel').forEach(el => el.classList.remove('wirelab-pin-sel'));
}

function reapplyHighlight() {
  clearHighlight();
  if (!selection) return;
  if (selection.kind === 'wire') {
    const p = host.querySelector('path.wire[data-wire-index="' + selection.ref + '"]');
    if (p) p.classList.add('wirelab-sel');
  } else if (selection.kind === 'component') {
    const g = host.querySelector('g[data-id="' + cssEscape(selection.ref) + '"]');
    if (g) g.classList.add('wirelab-sel');
  } else if (selection.kind === 'pin') {
    const [c, p] = selection.ref.split('/');
    // Match by comp + (n or name)
    host.querySelectorAll('circle.pin-hit[data-comp="' + cssEscape(c) + '"]').forEach(el => {
      if (el.dataset.pin === p || el.dataset.pinName === p) {
        el.classList.add('wirelab-pin-sel');
      }
    });
  }
}

function cssEscape(s) {
  return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/"/g, '\\\\"');
}

// ---------- Inspector ----------

function renderEmpty() {
  inspector.innerHTML = '<div class="empty">Click a wire, pin, or component<br>to inspect.</div>';
}

async function select(sel) {
  selection = sel;
  reapplyHighlight();
  await refreshInspector();
}

async function refreshInspector() {
  if (!selection) { renderEmpty(); return; }
  try {
    const r = await fetch('/target/' + selection.kind + '/' + selection.ref);
    if (!r.ok) {
      const err = await r.json().catch(() => ({error: 'HTTP ' + r.status}));
      inspector.innerHTML = '<div class="empty">Not found: ' + escapeHtml(err.error) + '</div>';
      return;
    }
    const data = await r.json();
    renderInspector(data);
  } catch (e) {
    inspector.innerHTML = '<div class="empty">Load failed: ' + escapeHtml(String(e)) + '</div>';
  }
}

function renderInspector(d) {
  const lines = [];
  let title, sub;
  if (d.kind === 'wire') {
    title = 'Wire #' + d.index;
    sub = d.from + ' → ' + d.to;
  } else if (d.kind === 'component') {
    title = d.id;
    sub = d.type;
  } else {
    title = d.comp + '.' + d.pin;
    sub = 'pin (n=' + d.fields.n + ')';
  }
  lines.push('<h2>' + escapeHtml(title) + '</h2>');
  lines.push('<div class="sub">' + escapeHtml(sub) + '</div>');

  if (d.editable.length === 0) {
    lines.push('<div class="empty">No editable fields here.</div>');
    // Even non-editable components (bulkhead, splice) get a Delete button
    // because deletion is a structural action, not a field edit.
    if (d.kind === 'component') {
      lines.push('<div class="btn-row">');
      lines.push('<button type="button" class="delete" id="btn-delete">Delete</button>');
      lines.push('</div>');
      lines.push('<div class="toast" id="toast"></div>');
    }
  } else {
    lines.push('<form id="edit-form">');
    for (const f of d.editable) {
      const v = d.fields[f];
      lines.push('<label>' + f + '</label>');
      lines.push('<input name="' + f + '" value="' + escapeHtml(v == null ? '' : String(v)) + '" autocomplete="off">');
    }
    lines.push('<div class="btn-row">');
    lines.push('<button type="submit" class="save">Save</button>');
    lines.push('<button type="button" class="cancel" id="btn-cancel">Cancel</button>');
    if (d.kind === 'wire' || d.kind === 'component') {
      lines.push('<span style="flex:1"></span>');
      lines.push('<button type="button" class="delete" id="btn-delete">Delete</button>');
    }
    lines.push('</div>');
    lines.push('<div class="toast" id="toast"></div>');
    lines.push('</form>');
  }

  // Pin list for connectors — clicking jumps to pin inspection
  if (d.kind === 'component' && d.pins) {
    lines.push('<label style="margin-top:18px">Pins</label>');
    lines.push('<div class="pin-list">');
    for (const p of d.pins) {
      const ref = p.name || String(p.n);
      lines.push('<div class="row" data-pin-ref="' + escapeHtml(ref) + '">');
      lines.push(p.n + '. ' + escapeHtml(p.name || '') +
                 (p.signal ? ' <span style="color:#78909c">[' + escapeHtml(p.signal) + ']</span>' : ''));
      lines.push('</div>');
    }
    lines.push('</div>');
  }

  inspector.innerHTML = lines.join('');

  const form = document.getElementById('edit-form');
  if (form) {
    form.addEventListener('submit', (e) => { e.preventDefault(); submitEdit(d, form); });
    document.getElementById('btn-cancel').addEventListener('click', () => refreshInspector());
  }
  const delBtn = document.getElementById('btn-delete');
  if (delBtn) {
    delBtn.addEventListener('click', () => submitDelete(d));
  }
  inspector.querySelectorAll('.pin-list .row').forEach(row => {
    row.addEventListener('click', () => {
      select({kind: 'pin', ref: d.id + '/' + row.dataset.pinRef});
    });
  });
}

async function submitEdit(d, form) {
  const toast = document.getElementById('toast');
  toast.className = 'toast';
  const edits = {};
  for (const f of d.editable) {
    edits[f] = form.elements[f].value;
  }
  const body = {kind: d.kind, revision: currentRev, edits};
  if (d.kind === 'wire') body.index = d.index;
  if (d.kind === 'component') body.id = d.id;
  if (d.kind === 'pin') { body.comp = d.comp; body.pin = d.pin; }

  try {
    const r = await fetch('/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (r.ok) {
      toast.className = 'toast ok';
      toast.textContent = j.changed && j.changed.length
        ? 'Saved: ' + j.changed.join(', ')
        : 'No changes.';
    } else if (r.status === 409) {
      toast.className = 'toast err';
      toast.textContent = 'YAML changed on disk; reloading inspector.';
      setTimeout(refreshInspector, 600);
    } else {
      toast.className = 'toast err';
      toast.textContent = j.error || ('HTTP ' + r.status);
    }
  } catch (e) {
    toast.className = 'toast err';
    toast.textContent = String(e);
  }
}

async function submitDelete(d) {
  const what = d.kind === 'wire' ? `wire #${d.index} (${d.from} -> ${d.to})`
                                  : `component '${d.id}'`;
  if (!confirm('Delete ' + what + '?')) return;
  const body = d.kind === 'wire'
    ? {kind: 'delete-wire', index: d.index}
    : {kind: 'delete-component', id: d.id};
  // Find the toast element if the form rendered one; otherwise create alerts via toolbar.
  const toast = document.getElementById('toast');
  function fail(msg) {
    if (toast) { toast.className = 'toast err'; toast.textContent = msg; }
    else toolbarHint(msg, true);
  }
  function ok(msg) {
    if (toast) { toast.className = 'toast ok'; toast.textContent = msg; }
    else toolbarHint(msg);
  }
  try {
    const r = await fetch('/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) { fail(j.error || ('HTTP ' + r.status)); return; }
    ok('deleted');
    selection = null;
    setTimeout(() => { renderEmpty(); clearHighlight(); }, 250);
  } catch (e) {
    fail(String(e));
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

// ---------- SSE / status ----------

function applySnapshot(snap) {
  currentRev = snap.revision;
  if (snap.error) {
    banner.style.display = 'block';
    banner.textContent = 'Error: ' + snap.error;
    statusEl.className = 'error';
    statusEl.textContent = 'error';
  } else {
    banner.style.display = 'none';
    statusEl.className = 'ok';
    statusEl.textContent = 'rev ' + snap.revision;
    loadSvg();
  }
  if (snap.warnings && snap.warnings.length) {
    const key = snap.warnings.join('\\u0001');
    warnings.dataset.key = key;
    warningsText.textContent = 'Warnings:\\n  ' + snap.warnings.join('\\n  ');
    if (warningsDismissedKey !== key) {
      warnings.style.display = 'block';
    }
  } else {
    warnings.style.display = 'none';
    warnings.dataset.key = '';
    warningsDismissedKey = null;
  }
}

const es = new EventSource('/events');
es.onmessage = (e) => {
  try { applySnapshot(JSON.parse(e.data)); } catch (_) {}
};
es.onerror = () => {
  statusEl.className = 'error';
  statusEl.textContent = 'disconnected';
};

// ---------- Toolbar wiring ----------
document.getElementById('btn-connect').addEventListener('click', () => {
  setConnectMode(!connectMode);
});

// ---------- New component modal ----------
const modalBackdrop = document.getElementById('modal-backdrop');
const newCompForm = document.getElementById('new-comp-form');
const newCompErr = document.getElementById('new-comp-err');
const newCompType = document.getElementById('new-comp-type');

function syncTypeFields() {
  const t = newCompType.value;
  document.getElementById('field-pins').style.display =
    (t === 'connector' || t === 'device') ? '' : 'none';
  document.getElementById('field-positions').style.display =
    (t === 'bulkhead') ? '' : 'none';
  document.getElementById('field-pin-count').style.display =
    (t === 'splice') ? '' : 'none';
}
newCompType.addEventListener('change', syncTypeFields);

function openNewCompModal() {
  if (connectMode) setConnectMode(false);
  newCompForm.reset();
  newCompErr.textContent = '';
  syncTypeFields();
  modalBackdrop.classList.add('show');
  setTimeout(() => newCompForm.elements['id'].focus(), 0);
}
function closeNewCompModal() {
  modalBackdrop.classList.remove('show');
}

document.getElementById('btn-new-component').addEventListener('click', openNewCompModal);
document.getElementById('new-comp-cancel').addEventListener('click', closeNewCompModal);
modalBackdrop.addEventListener('click', (e) => {
  if (e.target === modalBackdrop) closeNewCompModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && modalBackdrop.classList.contains('show')) {
    closeNewCompModal();
  }
});

newCompForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  newCompErr.textContent = '';
  const f = newCompForm.elements;
  const type = f['type'].value;
  const id = f['id'].value.trim();
  const label = f['label'].value.trim();
  const zone = f['zone'].value.trim();
  if (!id) { newCompErr.textContent = 'ID is required.'; return; }

  const body = {kind: 'add-component', id, comp_type: type,
                label: label || null, zone: zone || null,
                revision: currentRev};

  if (type === 'connector' || type === 'device') {
    const pins = f['pins'].value.split(/\\r?\\n/).map(s => s.trim()).filter(Boolean);
    if (pins.length === 0) { newCompErr.textContent = 'At least one pin is required.'; return; }
    body.pins = pins;
  } else if (type === 'bulkhead') {
    const n = parseInt(f['positions'].value, 10);
    if (!Number.isFinite(n) || n < 1) {
      newCompErr.textContent = 'Positions must be an integer >= 1.';
      return;
    }
    body.positions = n;
  } else if (type === 'splice') {
    const n = parseInt(f['pin_count'].value, 10);
    if (!Number.isFinite(n) || n < 2) {
      newCompErr.textContent = 'Pin count must be an integer >= 2.';
      return;
    }
    body.pin_count = n;
  }

  try {
    const r = await fetch('/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) {
      newCompErr.textContent = j.error || ('HTTP ' + r.status);
      return;
    }
    closeNewCompModal();
    toolbarHint('created ' + type + ' ' + j.id);
    // Auto-select the new component once the rerender lands.
    selection = {kind: 'component', ref: j.id};
  } catch (err) {
    newCompErr.textContent = String(err);
  }
});
document.getElementById('btn-arrange').addEventListener('click', async () => {
  if (!confirm('Clear all manual positions and re-layout? This cannot be undone (except via git).')) return;
  try {
    const r = await fetch('/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: 'auto-arrange'}),
    });
    const j = await r.json();
    if (!r.ok) toolbarHint(j.error || ('HTTP ' + r.status), true);
    else toolbarHint('auto-arranged (' + j.cleared + ' position overrides cleared)');
  } catch (err) {
    toolbarHint('auto-arrange failed: ' + err, true);
  }
});

loadSvg();
</script>
</body>
</html>
"""


def _describe_target(harness, parts: list[str]) -> dict:
    """Build the JSON payload the inspector needs to populate its form."""
    if not parts:
        raise KeyError("empty target")
    kind = parts[0]
    if kind == "wire":
        if len(parts) < 2:
            raise KeyError("missing wire index")
        idx = int(parts[1])
        if not (0 <= idx < len(harness.wires)):
            raise KeyError(f"wire index {idx} out of range")
        w = harness.wires[idx]
        return {
            "kind": "wire", "index": idx,
            "from": w.from_, "to": w.to,
            "fields": {
                "signal": w.signal, "color": w.color,
                "gauge": w.gauge, "length": w.length,
            },
            "editable": ["signal", "color", "gauge", "length"],
        }
    if kind == "component":
        if len(parts) < 2:
            raise KeyError("missing component id")
        cid = parts[1]
        if cid not in harness.components:
            raise KeyError(f"unknown component: {cid}")
        c = harness.components[cid]
        ctype = c.type
        editable = ["label"] if isinstance(c, Connector) else []
        info = {
            "kind": "component", "id": cid, "type": ctype,
            "fields": {"label": c.label},
            "editable": editable,
        }
        if isinstance(c, Connector):
            info["pins"] = [
                {"n": p.n, "name": p.name, "signal": p.signal} for p in c.pins
            ]
        elif isinstance(c, Bulkhead):
            info["positions"] = c.positions
        elif isinstance(c, Splice):
            info["pin_count"] = c.pin_count
        return info
    if kind == "pin":
        if len(parts) < 3:
            raise KeyError("missing component or pin")
        cid, pref = parts[1], parts[2]
        if cid not in harness.components:
            raise KeyError(f"unknown component: {cid}")
        c = harness.components[cid]
        if not isinstance(c, Connector):
            raise KeyError(f"pins on '{c.type}' are not editable")
        pin = c.find_pin(pref)
        if pin is None:
            raise KeyError(f"pin '{pref}' not found on '{cid}'")
        return {
            "kind": "pin", "comp": cid, "pin": pref,
            "fields": {"name": pin.name, "signal": pin.signal, "n": pin.n},
            "editable": ["name", "signal"],
        }
    raise KeyError(f"unknown target kind: {kind}")


def make_handler(state: PreviewState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter default log
            return

        def handle(self):
            # The default `handle()` calls `handle_one_request()` in a loop
            # and lets OSErrors propagate up to socketserver, which prints a
            # full traceback. Browsers routinely abort SSE/keep-alive sockets
            # (tab close, navigation, network blip) so those exceptions are
            # entirely expected. Swallow them here — anything genuinely wrong
            # will still surface from inside our handler methods.
            try:
                super().handle()
            except OSError:
                pass

        def _send(self, status: int, content_type: str, body: bytes,
                  extra_headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/" or path == "/index.html":
                self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
                return
            if path == "/svg":
                with state.lock:
                    svg = state.svg
                self._send(200, "image/svg+xml; charset=utf-8", svg.encode("utf-8"))
                return
            if path == "/state":
                body = json.dumps(state.snapshot()).encode("utf-8")
                self._send(200, "application/json", body)
                return
            if path == "/events":
                self._serve_events()
                return
            if path.startswith("/target/"):
                self._serve_target(path[len("/target/"):])
                return
            self._send(404, "text/plain", b"not found")

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/edit":
                self._serve_edit()
                return
            self._send(404, "text/plain", b"not found")

        # ----- target introspection -----

        def _serve_target(self, rest: str) -> None:
            """GET /target/wire/3   /target/component/ecu   /target/pin/ecu/VBAT"""
            with state.lock:
                harness = state.harness
                rev = state.revision
            if harness is None:
                self._send(503, "application/json",
                           json.dumps({"error": "no valid harness loaded"}).encode())
                return
            parts = rest.split("/")
            try:
                payload = _describe_target(harness, parts)
            except KeyError as exc:
                self._send(404, "application/json",
                           json.dumps({"error": str(exc)}).encode())
                return
            payload["revision"] = rev
            self._send(200, "application/json", json.dumps(payload).encode())

        # ----- edit -----

        def _resolve_component_target(self, merged_comp_id: str) -> tuple[Path, str]:
            """Map a merged-harness component id to (owning_file, local_id).

            `dash.HUD` -> (dash.yaml, 'HUD'). `BATTERY` -> (root.yaml, 'BATTERY').
            Raises EditError if the id is unknown.
            """
            harness = state.harness
            if harness is None:
                raise yaml_edit.EditError("harness not yet loaded")
            origin = component_origin(harness, merged_comp_id)
            if origin is None:
                raise yaml_edit.EditError(f"unknown component: {merged_comp_id}")
            return origin, local_component_id(merged_comp_id)

        def _resolve_wire_target(self, merged_index: int) -> tuple[Path, int, str]:
            """Map a merged-harness wire index to (owning_file, local_index, alias).

            The local_index is the wire's position within its owning file's
            `wires:` list. `alias` is the alias prefix that the owning file's
            wires are written under in the merged view; we use it to strip
            the prefix when rewriting refs back to local form.

            We recompute the wire→file mapping by re-loading the YAML rather
            than caching, because indices in the merged list don't directly
            correspond to indices in any single file — but the order within
            each file is preserved by the parser (subassemblies appended
            depth-first, then the root file's own wires).
            """
            harness = state.harness
            if harness is None:
                raise yaml_edit.EditError("harness not yet loaded")
            origins = harness.__dict__.get("_wire_origins") or []
            if not (0 <= merged_index < len(origins)):
                raise yaml_edit.EditError(f"wire index {merged_index} out of range")
            owning = origins[merged_index]
            # Count how many earlier merged wires share the same owning file —
            # that's the local index within that file's `wires:` list.
            local_index = sum(
                1 for o in origins[:merged_index] if o == owning
            )
            # Find the alias under which this file appears in the merged view
            # (by inspecting one of its components — any of them carries the
            # same prefix). Empty string for the root file.
            alias = ""
            for cid, o in (harness.__dict__.get("_origins") or {}).items():
                if o == owning:
                    alias = alias_prefix(harness, cid)
                    break
            return owning, local_index, alias

        def _strip_alias_in_wire_refs(self, fr: str, to: str, alias: str) -> tuple[str, str]:
            """Strip the owning-file alias from both endpoints of an add-wire
            request. If only one endpoint is inside the subassembly the wire
            doesn't belong there at all, so the caller should have routed it
            to the root instead — but if it gets here, leave the cross-file
            endpoint untouched (it'll fail validation downstream, with a
            clear pin-not-found error)."""
            return (
                strip_alias_from_pin_ref(fr, alias),
                strip_alias_from_pin_ref(to, alias),
            )

        def _dispatch_pin_edit(self, body: dict, edits: dict) -> dict:
            """Edit a pin on a component, possibly cascading a rename across
            every loaded file that references the pin."""
            merged_comp = body["comp"]
            pin_ref = body["pin"]
            owning, local_id = self._resolve_component_target(merged_comp)
            harness = state.harness

            # If this is a rename, fan out across files. Otherwise it's a
            # signal-only edit and the local edit_pin call is enough.
            if "name" in edits:
                # Look up the current name. The pin_ref from the UI may be a
                # number (so old_name comes from the schema) or a name
                # (old_name = pin_ref itself).
                comp = harness.components.get(merged_comp) if harness else None
                old_name = None
                if comp is not None and hasattr(comp, "find_pin"):
                    p = comp.find_pin(pin_ref)
                    if p is not None:
                        old_name = p.name

                new_raw = edits["name"]
                new_name = None if new_raw in (None, "") else str(new_raw)

                # Build the cascade target list: every (file, alias_in_that_file)
                # the rename must touch besides the owning file. Phase 2 only
                # cascades across one level of nesting in either direction
                # (root <-> direct subassembly) — enough for the demo and
                # consistent with how loaded_subassemblies() reports state.
                other_files: list[tuple[Path, str]] = []
                root = root_path(harness) if harness else state.yaml_path
                if owning != root and root is not None:
                    # Root file's view of the pin is `<alias>.<local_id>.<pin>`.
                    alias_in_root = alias_prefix(harness, merged_comp) if harness else ""
                    other_files.append((root, alias_in_root))
                if owning == root and harness is not None:
                    # Pin lives in the root; the rename may also be referenced
                    # by no subassembly (subassembly wires can only reference
                    # within their own subtree). Nothing to cascade.
                    pass

                changed_counts = yaml_edit.rename_pin_across_files(
                    owning, local_id, old_name, new_name, other_files
                )

                # If there are also non-rename fields (signal), apply them
                # locally on the owning file.
                rest = {k: v for k, v in edits.items() if k != "name"}
                if rest:
                    yaml_edit.edit_pin(owning, local_id,
                                       new_name if new_name is not None else pin_ref,
                                       rest)
                return {"changed": ["name"] + list(rest.keys()),
                        "files": changed_counts}

            # Non-rename edits go straight to the owning file.
            changed = yaml_edit.edit_pin(owning, local_id, pin_ref, edits)
            return {"changed": changed}

        def _dispatch_add_wire(self, body: dict) -> dict:
            """Add a wire. If both endpoints live in the same subassembly,
            write it into that subassembly's file (with alias stripped from
            the refs). Otherwise write it into the root file with merged
            refs intact."""
            fr = body["from"]
            to = body["to"]
            extra = body.get("extra")

            harness = state.harness
            root = root_path(harness) if harness else state.yaml_path

            # Determine each endpoint's owning file.
            fc, _ = parse_pin_ref(fr)
            tc, _ = parse_pin_ref(to)
            fc_origin = component_origin(harness, fc) if harness else None
            tc_origin = component_origin(harness, tc) if harness else None

            if (fc_origin is not None and tc_origin is not None
                    and fc_origin == tc_origin and fc_origin != root):
                # Intra-subassembly wire: strip the alias and write into the
                # subassembly file.
                alias = alias_prefix(harness, fc) if harness else ""
                local_fr = strip_alias_from_pin_ref(fr, alias)
                local_to = strip_alias_from_pin_ref(to, alias)
                idx = yaml_edit.add_wire(fc_origin, local_fr, local_to, extra)
                return {"index": idx, "file": str(fc_origin)}

            # Cross-file (or fully root) wire goes to the root.
            idx = yaml_edit.add_wire(state.yaml_path, fr, to, extra)
            return {"index": idx, "file": str(state.yaml_path)}

        def _serve_edit(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                self._send(400, "application/json",
                           json.dumps({"error": f"bad json: {exc}"}).encode())
                return

            kind = body.get("kind")
            client_rev = body.get("revision")
            edits = body.get("edits") or {}

            # Kinds that don't require a revision check: position drags and
            # auto-arrange should never be refused mid-drag just because the
            # SVG re-rendered between mousedown and mouseup.
            skip_rev_check = kind in ("position", "auto-arrange")

            with state.edit_lock:
                with state.lock:
                    server_rev = state.revision
                if (not skip_rev_check
                        and client_rev is not None
                        and client_rev != server_rev):
                    self._send(409, "application/json", json.dumps({
                        "error": "stale revision",
                        "client_revision": client_rev,
                        "server_revision": server_rev,
                    }).encode())
                    return
                # All edits resolve to (owning_file, local_id) and write to
                # the correct file. The root yaml is just one possible owner.
                try:
                    if kind == "wire":
                        owning, local_idx, _alias = self._resolve_wire_target(
                            int(body["index"]))
                        result = {"changed": yaml_edit.edit_wire(
                            owning, local_idx, edits)}
                    elif kind == "component":
                        owning, local_id = self._resolve_component_target(body["id"])
                        result = {"changed": yaml_edit.edit_component(
                            owning, local_id, edits)}
                    elif kind == "pin":
                        result = self._dispatch_pin_edit(body, edits)
                    elif kind == "position":
                        owning, local_id = self._resolve_component_target(body["id"])
                        sx, sy = yaml_edit.set_position(
                            owning, local_id,
                            float(body["x"]), float(body["y"]))
                        result = {"snapped": [sx, sy]}
                    elif kind == "auto-arrange":
                        # Clear positions in every loaded file (root + each
                        # subassembly) so the rearrange affects the whole
                        # merged harness.
                        cleared = 0
                        for p in included_files():
                            cleared += yaml_edit.clear_all_positions(p)
                        result = {"cleared": cleared}
                    elif kind == "add-wire":
                        result = self._dispatch_add_wire(body)
                    elif kind == "delete-wire":
                        owning, local_idx, _alias = self._resolve_wire_target(
                            int(body["index"]))
                        yaml_edit.delete_wire(owning, local_idx)
                        result = {"deleted": True}
                    elif kind == "delete-component":
                        merged_id = body["id"]
                        owning, local_id = self._resolve_component_target(merged_id)
                        # Any wire in the merged harness that mentions the
                        # component blocks the delete, regardless of which
                        # file the wire lives in. yaml_edit.delete_component
                        # only sees its own file, so we pre-check across all.
                        harness = state.harness
                        if harness is not None:
                            offenders = []
                            for i, w in enumerate(harness.wires):
                                fc, _ = parse_pin_ref(w.from_)
                                tc, _ = parse_pin_ref(w.to)
                                if fc == merged_id or tc == merged_id:
                                    offenders.append(f"wire #{i} ({w.from_} -> {w.to})")
                            if offenders:
                                raise yaml_edit.EditError(
                                    f"cannot delete '{merged_id}': still "
                                    f"referenced by {len(offenders)} wire(s):\n  "
                                    + "\n  ".join(offenders[:10])
                                    + ("\n  ..." if len(offenders) > 10 else "")
                                )
                        yaml_edit.delete_component(owning, local_id)
                        result = {"deleted": True}
                    elif kind == "add-component":
                        yaml_edit.add_component(
                            state.yaml_path,
                            body["id"],
                            body.get("comp_type") or body.get("type"),
                            label=body.get("label"),
                            zone=body.get("zone"),
                            pin_names=body.get("pins"),
                            positions=body.get("positions"),
                            pin_count=body.get("pin_count"),
                        )
                        result = {"id": body["id"]}
                    else:
                        raise yaml_edit.EditError(f"unknown kind: {kind!r}")
                except yaml_edit.EditError as exc:
                    self._send(400, "application/json",
                               json.dumps({"error": str(exc)}).encode())
                    return
                except (KeyError, ValueError, TypeError) as exc:
                    self._send(400, "application/json",
                               json.dumps({"error": f"bad request: {exc}"}).encode())
                    return

                # File watcher will pick up the mtime change and rebuild + broadcast.
                self._send(200, "application/json",
                           json.dumps(result).encode())

        def _serve_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            q = state.add_listener()
            try:
                # Send current state immediately so the client renders without waiting.
                self._sse_send(state.snapshot())
                while True:
                    try:
                        snap = q.get(timeout=15)
                        self._sse_send(snap)
                    except queue.Empty:
                        # Heartbeat keeps proxies and the browser from closing the stream.
                        try:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                        except OSError:
                            break
            except OSError:
                # Browsers routinely drop SSE streams (tab closed, navigation,
                # network blip). Quiet exit; no traceback to spam the console.
                pass
            finally:
                state.remove_listener(q)

        def _sse_send(self, payload: dict) -> None:
            data = json.dumps(payload)
            try:
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except OSError:
                raise

    return Handler


# ---------------------------------------------------------------------------
# Entry point used by cli.py
# ---------------------------------------------------------------------------

def serve(yaml_path: str, port: int = 8765, host: str = "127.0.0.1") -> None:
    path = Path(yaml_path)
    if not path.exists():
        raise SystemExit(f"file not found: {yaml_path}")

    state = PreviewState(path)

    stop = threading.Event()
    watcher = threading.Thread(target=watch_file, args=(state, stop), daemon=True)
    watcher.start()

    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"Wirelab live preview: http://{host}:{port}/  (watching {path})")
    print("Edit and save the YAML; the browser updates automatically. Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        stop.set()
        httpd.server_close()
