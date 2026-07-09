"""Round-trip YAML edits for the live editor.

Why this module exists
----------------------
The harness YAML is the source of truth (see dev_notes §6). Every interactive
edit from the browser goes through `serve.py` -> here -> the file on disk -> the
file watcher -> a fresh parse and render. If we wrote the edits with a normal
YAML dumper we would lose comments, blank lines, key order, and any anchors
the user had carefully set up. `ruamel.yaml` round-trip mode preserves all of
that, so the YAML stays readable and `git diff` only shows the line you
actually changed.

What this module does NOT do
----------------------------
- **Schema validation.** Pin types, references, etc. are checked by `parser.py`
  on the next reload. We *do* enforce structural invariants that would
  otherwise silently corrupt the file (orphaning wire references on a pin
  rename, duplicate wires, deleting a referenced component) — see the
  `_validate_*` helpers and the body of `delete_component`.
- **Concurrency control.** `serve.py` holds an `edit_lock` while it calls us
  and provides revision-check semantics on the HTTP layer. We assume single-
  writer access to the file during a call.

Public surface (one function per supported edit kind)
-----------------------------------------------------
`edit_wire`, `edit_component`, `edit_pin`  - field whitelist edits
`set_position`, `clear_all_positions`      - layout overrides
`add_wire`, `delete_wire`                  - structural wire mutations
`delete_component`                         - component removal (refuses if referenced)

All mutators load the doc, mutate, and `dump_doc`. Writes are atomic
(temp-file + replace) so the file watcher never sees a half-written state.

Indent style
------------
Hard-coded to `mapping=2, sequence=4, offset=2` to match `baja_example.yaml`.
If you switch to a project with a different indent style, adjust the YAML
constructor at the top of the module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.width = 4096  # don't wrap long lines on dump
# Match the indent style of baja_example.yaml: nested mappings 2 spaces,
# block sequences indented 4 with their `-` offset 2.
_yaml.indent(mapping=2, sequence=4, offset=2)


def load_doc(path: str | Path) -> Any:
    """Load a YAML document preserving comments/formatting."""
    text = Path(path).read_text(encoding="utf-8")
    return _yaml.load(text)


def dump_doc(doc: Any, path: str | Path) -> None:
    """Atomic write: dump to a sibling temp file then replace."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        _yaml.dump(doc, f)
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Field whitelists — Phase 2 keeps the surface deliberately small.
# ---------------------------------------------------------------------------

WIRE_FIELDS = {"signal", "color", "gauge", "length"}
COMPONENT_FIELDS = {"label"}
PIN_FIELDS = {"name", "signal"}


class EditError(Exception):
    """Raised for editor-facing problems (missing target, bad field, etc.)."""


# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------

def _components(doc: Any):
    if "components" not in doc:
        raise EditError("document has no 'components' section")
    return doc["components"]


def _wires(doc: Any):
    if "wires" not in doc:
        raise EditError("document has no 'wires' section")
    return doc["wires"]


def _find_wire(doc: Any, index: int):
    wires = _wires(doc)
    if not (0 <= index < len(wires)):
        raise EditError(f"wire index {index} out of range (have {len(wires)})")
    return wires[index]


def _find_component(doc: Any, comp_id: str):
    comps = _components(doc)
    if comp_id not in comps:
        raise EditError(f"unknown component: {comp_id}")
    return comps[comp_id]


def _find_pin(doc: Any, comp_id: str, pin_ref: str):
    """Locate a pin entry on a connector/device by number or name.

    Bulkhead and splice pins have no editable record in YAML — they are
    generated from `positions:` / `pin_count:` — so this raises for them.
    """
    comp = _find_component(doc, comp_id)
    ctype = comp.get("type")
    if ctype not in ("connector", "device"):
        raise EditError(f"pins on '{ctype}' components are not directly editable")
    pins = comp.get("pins")
    if pins is None:
        raise EditError(f"component '{comp_id}' has no 'pins' list")

    # Try by number first, then by name.
    if pin_ref.isdigit():
        n = int(pin_ref)
        for p in pins:
            if p.get("n") == n:
                return p
    for p in pins:
        if p.get("name") == pin_ref:
            return p
    raise EditError(f"pin '{pin_ref}' not found on '{comp_id}'")


# ---------------------------------------------------------------------------
# Field application helpers
# ---------------------------------------------------------------------------

def _coerce(field: str, value: Any) -> Any:
    """Normalise an incoming JSON value to the YAML-friendly type for `field`."""
    if value is None or value == "":
        return None  # signals delete
    if field in ("gauge", "length"):
        try:
            f = float(value)
        except (TypeError, ValueError) as exc:
            raise EditError(f"{field} must be numeric, got {value!r}") from exc
        # Keep whole numbers as int so YAML renders `gauge: 10` not `gauge: 10.0`.
        return int(f) if f.is_integer() else f
    if field == "n":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise EditError(f"pin number must be integer, got {value!r}") from exc
    return str(value)


def _apply_fields(node: Any, edits: dict, allowed: set[str]) -> list[str]:
    """Apply whitelisted field edits to `node`. Empty value deletes the field.

    Returns the list of field names actually changed.
    """
    bad = [k for k in edits if k not in allowed]
    if bad:
        raise EditError(f"fields not editable here: {bad} (allowed: {sorted(allowed)})")

    changed: list[str] = []
    for field, raw in edits.items():
        new = _coerce(field, raw)
        old = node.get(field)
        if new is None:
            if field in node:
                del node[field]
                changed.append(field)
        else:
            if old != new:
                node[field] = new
                changed.append(field)
    return changed


# ---------------------------------------------------------------------------
# Public mutation API — one call per edit kind. Each does load → mutate → dump.
# ---------------------------------------------------------------------------

def edit_wire(path: str | Path, index: int, edits: dict) -> list[str]:
    doc = load_doc(path)
    wire = _find_wire(doc, index)
    changed = _apply_fields(wire, edits, WIRE_FIELDS)
    if changed:
        dump_doc(doc, path)
    return changed


def edit_component(path: str | Path, comp_id: str, edits: dict) -> list[str]:
    doc = load_doc(path)
    comp = _find_component(doc, comp_id)
    changed = _apply_fields(comp, edits, COMPONENT_FIELDS)
    if changed:
        dump_doc(doc, path)
    return changed


def edit_pin(path: str | Path, comp_id: str, pin_ref: str, edits: dict) -> list[str]:
    doc = load_doc(path)
    pin = _find_pin(doc, comp_id, pin_ref)

    # Pin renames need cross-checking against wire references and sibling pins.
    if "name" in edits:
        new_name = edits["name"]
        new_name = None if new_name in (None, "") else str(new_name)
        old_name = pin.get("name")
        if new_name != old_name:
            _validate_and_apply_pin_rename(doc, comp_id, pin, old_name, new_name)
            # Drop the rename from the generic apply so it isn't done twice.
            edits = {k: v for k, v in edits.items() if k != "name"}
            changed_extra = ["name"]
        else:
            changed_extra = []
    else:
        changed_extra = []

    changed = _apply_fields(pin, edits, PIN_FIELDS) + changed_extra
    if changed:
        dump_doc(doc, path)
    return changed


def _validate_and_apply_pin_rename(doc: Any, comp_id: str, pin: Any,
                                   old_name: str | None, new_name: str | None) -> None:
    """Apply a pin rename, refusing if it would orphan any wire reference.

    Also cascades the rename through the wires section so `from`/`to` strings
    that referenced the old name still resolve.
    """
    comp = _find_component(doc, comp_id)
    pins = comp.get("pins") or []

    # Collision check: another pin on this component already has this name?
    if new_name is not None:
        for other in pins:
            if other is pin:
                continue
            if other.get("name") == new_name:
                raise EditError(
                    f"pin name '{new_name}' is already used on '{comp_id}'"
                )

    # Find wires that reference this pin by the OLD name. Numeric refs are
    # unaffected (the pin number doesn't change).
    pin_n = pin.get("n")
    refs_by_name: list[tuple[int, str]] = []  # (wire_index, "from"|"to")
    for i, w in enumerate(doc.get("wires") or []):
        for side in ("from", "to"):
            ref = w.get(side)
            if not isinstance(ref, str) or "." not in ref:
                continue
            cid, pref = ref.split(".", 1)
            if cid != comp_id:
                continue
            if old_name is not None and pref == old_name:
                refs_by_name.append((i, side))

    # Clearing the name would orphan any name-based references.
    if new_name is None and refs_by_name:
        raise EditError(
            f"cannot clear name on '{comp_id}.{old_name}': "
            f"{len(refs_by_name)} wire(s) reference it by name. "
            f"Either rename instead, or change those wires to use pin number {pin_n}."
        )

    # Apply rename on the pin itself
    if new_name is None:
        if "name" in pin:
            del pin["name"]
    else:
        pin["name"] = new_name

    # Cascade to wires
    for i, side in refs_by_name:
        w = doc["wires"][i]
        w[side] = f"{comp_id}.{new_name}"


# ---------------------------------------------------------------------------
# Cross-file pin rename (cascades through every file that references the pin)
# ---------------------------------------------------------------------------

def rename_pin_across_files(
    owning_path: str | Path,
    local_comp_id: str,
    old_name: str | None,
    new_name: str | None,
    other_files: list[tuple[Path, str]],
) -> dict:
    """Rename a pin and cascade the rename through every file that references it.

    Two-phase commit: load every doc, validate every cascade target, plan
    every rewrite, only then write everything back. If validation fails for
    any file we abort with EditError and write nothing. (Disk-write failures
    mid-batch are still possible but rare; in practice we accept that.)

    Arguments
    ---------
    owning_path     Path to the file containing the pin definition.
    local_comp_id   The pin's component id within `owning_path` (no alias).
    old_name        Current pin name (None if the pin previously had no name).
    new_name        New pin name (None clears the name; refused if any wire
                    in any file references the pin by name).
    other_files     [(path, full_alias_in_root), ...] for every other file
                    that might mention the pin. The merged-id form
                    `<full_alias_in_root>.<local_comp_id>` is what root wires
                    use; the local form `<local_comp_id>` is what the owning
                    file uses internally; intermediate files (parents in a
                    nested include chain) reference the pin via the alias
                    suffix relative to their own subassembly root.

    Returns a dict {path: changed_count} for the caller's logging.
    """
    owning_path = Path(owning_path)

    # Phase 1: load owning doc, locate pin, run collision + clear-name checks.
    own_doc = load_doc(owning_path)
    own_comp = _find_component(own_doc, local_comp_id)
    own_pins = own_comp.get("pins") or []
    pin = None
    for p in own_pins:
        if p.get("name") == old_name:
            pin = p
            break
        if old_name is None and "name" not in p:
            # Ambiguous: an unnamed pin can't be uniquely identified by name.
            # The caller should have used the numeric form for unnamed pins;
            # we refuse rather than guess.
            pass
    if pin is None:
        raise EditError(
            f"pin with name {old_name!r} not found on {local_comp_id!r} "
            f"in {owning_path.name}"
        )

    if new_name is not None:
        for other in own_pins:
            if other is pin:
                continue
            if other.get("name") == new_name:
                raise EditError(
                    f"pin name {new_name!r} is already used on "
                    f"{local_comp_id!r} in {owning_path.name}"
                )

    pin_n = pin.get("n")

    # Phase 2: scan every file's wires and plan the rewrites.
    # A wire reference in a given file looks like `<some_alias>.<local>.<pin>`
    # where `<some_alias>` is the alias path from THAT file's perspective to
    # the owning subassembly. For the owning file itself, that's just
    # `<local_comp_id>.<pin>`. For files higher in the include chain, it is
    # `<alias_suffix>.<local_comp_id>.<pin>` where alias_suffix is the alias
    # this file uses to reach the owning subassembly (e.g. root sees `dash`,
    # a hypothetical grandparent might see `car.dash`).
    plans: list[tuple[Path, object, list[tuple[int, str]]]] = []

    def _scan_doc_for_refs(doc, comp_ref_prefix: str) -> list[tuple[int, str]]:
        hits: list[tuple[int, str]] = []
        for i, w in enumerate(doc.get("wires") or []):
            for side in ("from", "to"):
                ref = w.get(side)
                if not isinstance(ref, str) or "." not in ref:
                    continue
                comp_id, pin_ref = ref.rsplit(".", 1)
                if comp_id != comp_ref_prefix:
                    continue
                # Strip any :L/:R suffix for the comparison.
                bare = pin_ref[:-2] if pin_ref.endswith(":L") or pin_ref.endswith(":R") else pin_ref
                if old_name is not None and bare == old_name:
                    hits.append((i, side))
        return hits

    # Owning file's own wires reference the pin as `<local_comp_id>.<name>`.
    own_hits = _scan_doc_for_refs(own_doc, local_comp_id)
    plans.append((owning_path, own_doc, own_hits))

    # Every other file uses its own alias suffix.
    other_docs: dict[Path, object] = {}
    for path, alias_in_that_file in other_files:
        if path == owning_path:
            continue
        d = load_doc(path)
        other_docs[path] = d
        if alias_in_that_file:
            comp_ref_prefix = f"{alias_in_that_file}.{local_comp_id}"
        else:
            comp_ref_prefix = local_comp_id
        hits = _scan_doc_for_refs(d, comp_ref_prefix)
        plans.append((path, d, hits))

    # Clearing the name is only safe if no wire anywhere references the pin
    # by that name.
    total_hits = sum(len(h) for _, _, h in plans)
    if new_name is None and total_hits:
        raise EditError(
            f"cannot clear name on {local_comp_id!r}.{old_name!r}: "
            f"{total_hits} wire(s) reference it by name. Rename instead, "
            f"or change those wires to use pin number {pin_n} first."
        )

    # Phase 3: apply. Pin rename first (owning file), then cascade.
    if new_name is None:
        if "name" in pin:
            del pin["name"]
    else:
        pin["name"] = new_name

    changed_counts: dict[str, int] = {}
    for path, doc, hits in plans:
        wires = doc.get("wires") or []
        if path == owning_path:
            prefix = local_comp_id
        else:
            # Look up this file's alias to the owning subassembly from
            # `other_files` (already verified the path is there).
            alias_in_that_file = next(
                a for p, a in other_files if p == path
            )
            prefix = f"{alias_in_that_file}.{local_comp_id}" if alias_in_that_file else local_comp_id

        for i, side in hits:
            w = wires[i]
            ref = w[side]
            # Preserve any :L/:R suffix on the rewrite.
            tail = ""
            if ref.endswith(":L") or ref.endswith(":R"):
                tail = ref[-2:]
            w[side] = f"{prefix}.{new_name}{tail}"

        if hits or path == owning_path:
            dump_doc(doc, path)
            changed_counts[str(path)] = len(hits) + (1 if path == owning_path else 0)

    return changed_counts


# ---------------------------------------------------------------------------
# Position / layout mutations
# ---------------------------------------------------------------------------

def set_position(path: str | Path, comp_id: str,
                 x: float, y: float, snap: int = 10) -> tuple[int, int]:
    """Write a manual position override for a component, snapped to `snap` px."""
    sx = int(round(x / snap) * snap)
    sy = int(round(y / snap) * snap)
    doc = load_doc(path)
    comp = _find_component(doc, comp_id)
    pos = CommentedSeq([sx, sy])
    pos.fa.set_flow_style()
    if "position" in comp:
        comp["position"] = pos
    else:
        # Insert right after `type` so the field has a predictable home and
        # we don't append a stray blank-line gap at the end of the mapping.
        keys = list(comp.keys())
        idx = keys.index("type") + 1 if "type" in keys else len(keys)
        comp.insert(idx, "position", pos)
    dump_doc(doc, path)
    return sx, sy


def clear_all_positions(path: str | Path) -> int:
    """Remove every `position:` override. Returns count cleared."""
    doc = load_doc(path)
    n = 0
    for _cid, comp in _components(doc).items():
        if "position" in comp:
            del comp["position"]
            n += 1
    if n:
        dump_doc(doc, path)
    return n


# ---------------------------------------------------------------------------
# Wire add / delete
# ---------------------------------------------------------------------------

def add_wire(path: str | Path, from_ref: str, to_ref: str,
             extra: dict | None = None) -> int:
    """Append a new wire. Returns the new wire's index in the wires list."""
    if not from_ref or not to_ref:
        raise EditError("from and to are required")
    if from_ref == to_ref:
        raise EditError("a wire cannot loop a pin to itself")
    doc = load_doc(path)
    wires = doc.get("wires")
    if wires is None:
        # Document had no wires section; create one.
        wires = CommentedSeq()
        doc["wires"] = wires

    # Reject exact duplicates (same from+to in either direction).
    pair = {from_ref, to_ref}
    for w in wires:
        if {w.get("from"), w.get("to")} == pair:
            raise EditError(f"wire {from_ref} <-> {to_ref} already exists")

    new = CommentedMap()
    new["from"] = from_ref
    new["to"] = to_ref
    if extra:
        for k, v in extra.items():
            if k in WIRE_FIELDS:
                coerced = _coerce(k, v)
                if coerced is not None:
                    new[k] = coerced
    new.fa.set_flow_style()
    wires.append(new)
    dump_doc(doc, path)
    return len(wires) - 1


def delete_wire(path: str | Path, index: int) -> None:
    doc = load_doc(path)
    wires = _wires(doc)
    if not (0 <= index < len(wires)):
        raise EditError(f"wire index {index} out of range")
    del wires[index]
    dump_doc(doc, path)


# ---------------------------------------------------------------------------
# Component delete
# ---------------------------------------------------------------------------

def delete_component(path: str | Path, comp_id: str) -> None:
    """Refuse if any wire still references this component."""
    doc = load_doc(path)
    if comp_id not in _components(doc):
        raise EditError(f"unknown component: {comp_id}")
    refs = []
    for i, w in enumerate(doc.get("wires") or []):
        for side in ("from", "to"):
            ref = w.get(side)
            if isinstance(ref, str) and ref.split(".", 1)[0] == comp_id:
                refs.append(f"wire #{i} ({w.get('from')} → {w.get('to')})")
    if refs:
        raise EditError(
            f"cannot delete '{comp_id}': still referenced by "
            f"{len(refs)} wire(s):\n  " + "\n  ".join(refs[:10]) +
            ("\n  …" if len(refs) > 10 else "")
        )
    del _components(doc)[comp_id]
    dump_doc(doc, path)


# ---------------------------------------------------------------------------
# Component add — supports connector / device / bulkhead / splice.
# ---------------------------------------------------------------------------

import re as _re

_ID_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALID_TYPES = ("connector", "device", "bulkhead", "splice")


def add_component(path: str | Path, comp_id: str, comp_type: str,
                  label: str | None = None, zone: str | None = None,
                  pin_names: list[str] | None = None,
                  positions: int | None = None,
                  pin_count: int | None = None) -> None:
    """Append a component of any supported type.

    Required per type:
      - connector / device: `pin_names` (list of strings; auto-numbered 1..N)
      - bulkhead:           `positions` (int, generates A1..An / B1..Bn)
      - splice:             `pin_count` (int >= 2)

    `zone` is optional for any type. `label` is always optional.
    """
    if not comp_id or not _ID_RE.match(comp_id):
        raise EditError(
            f"invalid id {comp_id!r}: must start with a letter/underscore "
            f"and contain only letters, digits, and underscores"
        )
    if comp_type not in _VALID_TYPES:
        raise EditError(
            f"invalid type {comp_type!r}: must be one of {list(_VALID_TYPES)}"
        )

    doc = load_doc(path)
    comps = _components(doc)
    if comp_id in comps:
        raise EditError(f"component {comp_id!r} already exists")

    new = CommentedMap()
    new["type"] = comp_type
    if label:
        new["label"] = str(label)
    if zone:
        z = str(zone).strip()
        if z and not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", z):
            raise EditError(
                f"invalid zone {z!r}: must be a simple identifier"
            )
        if z:
            new["zone"] = z

    if comp_type in ("connector", "device"):
        cleaned = [p.strip() for p in (pin_names or []) if p and p.strip()]
        if not cleaned:
            raise EditError("at least one pin is required")
        seen: set[str] = set()
        for name in cleaned:
            if name in seen:
                raise EditError(f"duplicate pin name: {name!r}")
            seen.add(name)
        pins = CommentedSeq()
        for i, name in enumerate(cleaned, start=1):
            p = CommentedMap()
            p["n"] = i
            p["name"] = name
            p.fa.set_flow_style()
            pins.append(p)
        new["pins"] = pins
    elif comp_type == "bulkhead":
        if positions is None:
            raise EditError("bulkhead requires 'positions'")
        try:
            n = int(positions)
        except (TypeError, ValueError) as exc:
            raise EditError(f"positions must be an integer, got {positions!r}") from exc
        if n < 1:
            raise EditError("positions must be >= 1")
        new["positions"] = n
    elif comp_type == "splice":
        if pin_count is None:
            raise EditError("splice requires 'pin_count'")
        try:
            n = int(pin_count)
        except (TypeError, ValueError) as exc:
            raise EditError(f"pin_count must be an integer, got {pin_count!r}") from exc
        if n < 2:
            raise EditError("pin_count must be >= 2")
        new["pin_count"] = n

    comps[comp_id] = new
    dump_doc(doc, path)
