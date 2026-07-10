"""Parse YAML harness files and validate that all pin references resolve.

Subassembly / include support
-----------------------------
A file may declare `includes:` to pull in other harness files:

    includes:
      engine: engine.yaml
      dash: dash.yaml

Each included file is loaded recursively. Its components are merged into the
top-level harness with their IDs prefixed by the include alias (and any
parent aliases): `engine.ECU`, `dash.switches.HORN`, etc. Wires inside an
included file may only reference the included file's own components (or its
own nested includes); cross-subassembly wires live in the root file and use
the dotted form, e.g. `from: engine.ECU.CANH`.

Cycle detection works on resolved file paths. Re-importing the same file
under two different aliases is allowed; importing a file that (transitively)
imports itself is an error.
"""
from pathlib import Path
import yaml
from schema import Harness, Connector, Bulkhead, Splice, Wire


class HarnessError(Exception):
    pass


def parse_pin_ref(ref: str) -> tuple[str, str]:
    """Parse '<component>.<pin>' or '<component>.<pin>:side'.

    The component portion may itself contain dots when the component comes
    from an included subassembly (e.g. `engine.ECU.VBAT` -> component
    `engine.ECU`, pin `VBAT`). The split happens on the LAST dot so subassembly
    prefixes are kept with the component id.

    Examples:
      'ecu.VBAT'                  -> ('ecu', 'VBAT')
      'engine.ECU.VBAT'           -> ('engine.ECU', 'VBAT')
      'dash.HUD_BOX_IN.CANH:L'    -> ('dash.HUD_BOX_IN', 'CANH:L')
    """
    if "." not in ref:
        raise HarnessError(f"Pin reference must be 'component.pin', got: {ref}")
    comp_id, pin_ref = ref.rsplit(".", 1)
    return comp_id, pin_ref


def _strip_side_suffix(pin_ref: str) -> str:
    """Drop a trailing ':L' or ':R' from a pin reference (for pin-existence checks)."""
    if pin_ref.endswith(":L") or pin_ref.endswith(":R"):
        return pin_ref[:-2]
    return pin_ref


def validate_pin_exists(harness: Harness, ref: str) -> None:
    comp_id, pin_ref = parse_pin_ref(ref)
    if comp_id not in harness.components:
        raise HarnessError(f"Unknown component: {comp_id} (in '{ref}')")

    comp = harness.components[comp_id]
    pin_ref = _strip_side_suffix(pin_ref)

    if isinstance(comp, (Connector,)):
        if comp.find_pin(pin_ref) is None:
            raise HarnessError(f"Pin '{pin_ref}' not found on connector '{comp_id}'")
    elif isinstance(comp, Bulkhead):
        if not (len(pin_ref) >= 2 and pin_ref[0] in "AB" and pin_ref[1:].isdigit()):
            raise HarnessError(f"Bulkhead pin must be A<n> or B<n>, got: {pin_ref}")
        n = int(pin_ref[1:])
        if not (1 <= n <= comp.positions):
            raise HarnessError(f"Bulkhead '{comp_id}' has only {comp.positions} positions")
    elif isinstance(comp, Splice):
        if not pin_ref.isdigit() or not (1 <= int(pin_ref) <= comp.pin_count):
            raise HarnessError(f"Splice '{comp_id}' pin must be 1..{comp.pin_count}")


def validate_no_double_connections(harness: Harness) -> list[str]:
    """A pin should appear at most once across wires (splices are exempt)."""
    warnings = []
    pin_uses: dict[str, int] = {}
    for w in harness.wires:
        for ref in (w.from_, w.to):
            comp_id, _ = parse_pin_ref(ref)
            comp = harness.components[comp_id]
            if isinstance(comp, Splice):
                continue
            pin_uses[ref] = pin_uses.get(ref, 0) + 1
    for ref, count in pin_uses.items():
        if count > 1:
            warnings.append(f"Pin {ref} used by {count} wires")
    return warnings


# Tag the YAML with type discriminator for Pydantic union resolution
def _check_type_tags(raw: dict) -> dict:
    """Pydantic needs the 'type' field present; this is just a passthrough check."""
    for cid, c in raw.get("components", {}).items():
        if "type" not in c:
            raise HarnessError(f"Component '{cid}' missing 'type' field")
    return raw


# ---------------------------------------------------------------------------
# Subassembly / include resolution
# ---------------------------------------------------------------------------

# When True, included_files() returns every file touched by the most recent
# load() call. The live editor uses this for mtime polling.
_last_loaded_paths: list[Path] = []


def included_files() -> list[Path]:
    """Resolved paths of every file touched by the last successful load(), in
    depth-first order with the root first. Used by serve.py's file watcher."""
    return list(_last_loaded_paths)


def _prefix_pin_ref(ref: str, prefix: str) -> str:
    """Rewrite `comp.pin` -> `prefix.comp.pin` (preserves any `:side` suffix)."""
    if not prefix:
        return ref
    return f"{prefix}.{ref}"


def _resolve_includes(raw: dict, base_path: Path, chain: list[Path],
                      alias_prefix: str, seen_paths: list[Path]) -> tuple[dict, list]:
    """Recursively expand `includes:` in `raw`. Returns (components, wires).

    - `base_path`: directory of the file `raw` was loaded from (for resolving
      relative include paths).
    - `chain`: stack of resolved file paths from root to current; used for
      cycle detection.
    - `alias_prefix`: dotted alias prefix to prepend to every component id and
      wire ref defined in this file. Empty at the root.
    - `seen_paths`: accumulator for `included_files()`.
    """
    out_components: dict = {}
    out_wires: list = []

    includes = raw.get("includes") or {}
    if not isinstance(includes, dict):
        raise HarnessError(
            "`includes:` must be a mapping of alias -> path, "
            f"got {type(includes).__name__}"
        )

    for alias, sub_path in includes.items():
        if not isinstance(alias, str) or not alias or "." in alias:
            raise HarnessError(
                f"include alias {alias!r} must be a simple identifier "
                "(no dots, no empty string)"
            )
        if not isinstance(sub_path, str):
            raise HarnessError(
                f"include path for alias {alias!r} must be a string, "
                f"got {type(sub_path).__name__}"
            )
        resolved = (base_path / sub_path).resolve()
        if not resolved.exists():
            raise HarnessError(
                f"included file not found: {sub_path} "
                f"(resolved to {resolved}, referenced from {chain[-1]})"
            )
        if resolved in chain:
            cycle = " -> ".join(str(p) for p in chain + [resolved])
            raise HarnessError(f"include cycle detected: {cycle}")

        sub_raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        if not isinstance(sub_raw, dict):
            raise HarnessError(f"included file {resolved} is not a YAML mapping")
        seen_paths.append(resolved)

        sub_alias = f"{alias_prefix}.{alias}" if alias_prefix else alias
        sub_components, sub_wires = _resolve_includes(
            sub_raw, resolved.parent, chain + [resolved], sub_alias, seen_paths
        )
        out_components.update(sub_components)
        out_wires.extend(sub_wires)

        # Now merge the included file's own components/wires.
        _check_type_tags(sub_raw)
        for cid, c in (sub_raw.get("components") or {}).items():
            prefixed = f"{sub_alias}.{cid}"
            if prefixed in out_components:
                raise HarnessError(
                    f"duplicate component id after include: {prefixed} "
                    f"(check that two includes don't share a nested alias)"
                )
            # Namespace the layout zone by the include alias so that the SAME
            # generated file imported under several aliases (e.g. one wheel-hall
            # board instanced as wheel_fl/fr/rl/rr) lands each instance in its
            # own zone column instead of collapsing them into one. The zone
            # baked into the generated file (the board-folder name) is only a
            # sensible default for a singly-imported board; the alias is the
            # instance identity, so it wins. Copy the dict so we don't mutate
            # the shared spec when the same file is imported twice.
            c = dict(c)
            c["zone"] = sub_alias
            out_components[prefixed] = (c, resolved)

        for w in (sub_raw.get("wires") or []):
            fr = w.get("from")
            to = w.get("to")
            if not isinstance(fr, str) or not isinstance(to, str):
                raise HarnessError(
                    f"wire in {resolved} is missing from/to (got {w!r})"
                )
            new_w = dict(w)
            new_w["from"] = _prefix_pin_ref(fr, sub_alias)
            new_w["to"] = _prefix_pin_ref(to, sub_alias)
            # Restrict subassembly-internal wires to refs within their own
            # subtree. The check is "the prefixed comp_id is in out_components
            # at the end" — deferred until we've collected everything from
            # this file. Tag the wire with its origin for the deferred check.
            out_wires.append((new_w, resolved, sub_alias))

    return out_components, out_wires


def load(path: str | Path) -> Harness:
    """Load a harness from disk, expanding any `includes:` recursively.

    Components from included files are merged into a single flat namespace
    with their ids prefixed by their include alias. Downstream stages
    (layout, render, bom, yaml_edit) operate on this flat view and need no
    awareness that the harness came from multiple files.
    """
    root = Path(path).resolve()
    if not root.exists():
        raise HarnessError(f"harness file not found: {path}")
    raw = yaml.safe_load(root.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise HarnessError(f"{path} is not a YAML mapping")
    _check_type_tags(raw)

    seen_paths: list[Path] = [root]

    # Components/wires pulled in from `includes:` (already prefixed).
    sub_components, sub_wires = _resolve_includes(
        raw, root.parent, [root], "", seen_paths
    )

    # The root file's own components/wires (no prefix).
    root_component_specs: dict = {}
    for cid, c in (raw.get("components") or {}).items():
        if cid in sub_components:
            raise HarnessError(
                f"component id {cid!r} in {root} collides with an included "
                f"alias; rename one of them"
            )
        root_component_specs[cid] = (c, root)

    all_specs: dict = {**sub_components, **root_component_specs}

    # Build the schema objects in one pass.
    components: dict = {}
    origins: dict[str, Path] = {}
    alias_prefixes: dict[str, str] = {}
    for cid, (c, origin) in all_specs.items():
        t = c.get("type")
        if t in ("connector", "device"):
            components[cid] = Connector(**c)
        elif t == "bulkhead":
            components[cid] = Bulkhead(**c)
        elif t == "splice":
            components[cid] = Splice(**c)
        elif t is None:
            raise HarnessError(f"Component '{cid}' missing 'type' field")
        else:
            raise HarnessError(f"Unknown component type: {t} (on '{cid}')")
        origins[cid] = origin
        # The alias prefix is everything before the last `.` of the merged id.
        # `dash.HUD` -> alias `dash`, local id `HUD`. `HUD` (root) -> alias `""`.
        if "." in cid:
            alias_prefixes[cid] = cid.rsplit(".", 1)[0]
        else:
            alias_prefixes[cid] = ""

    # Wires: subassembly wires already prefixed; the root file's wires go in
    # as-is (no prefix; cross-subassembly refs use dotted form by hand).
    wires: list[Wire] = []
    wire_origins: list[Path] = []

    for new_w, origin, sub_alias in sub_wires:
        wires.append(Wire(**new_w))
        wire_origins.append(origin)
        # An included file's wires may only reference its own subtree. Any
        # ref that escapes the subtree will fail the existence check below
        # ("Unknown component: <prefixed>"), which is a clearer error for
        # the user than a custom message that has to second-guess intent.

    for w in (raw.get("wires") or []):
        wires.append(Wire(**w))
        wire_origins.append(root)

    harness = Harness(
        metadata=raw.get("metadata", {}),
        components=components,
        wires=wires,
    )

    # Attach origin info as plain attributes (not part of the Pydantic schema —
    # downstream code that doesn't need them won't notice). The editor reads
    # these to decide whether an edit targets the root or a subassembly.
    harness.__dict__["_origins"] = origins
    harness.__dict__["_alias_prefixes"] = alias_prefixes
    harness.__dict__["_wire_origins"] = wire_origins
    harness.__dict__["_root_path"] = root

    # Pin-reference validation across the merged harness.
    for w in harness.wires:
        validate_pin_exists(harness, w.from_)
        validate_pin_exists(harness, w.to)

    # Publish the file list for the watcher (root first, then includes in
    # depth-first order).
    global _last_loaded_paths
    _last_loaded_paths = list(seen_paths)

    return harness


def component_origin(harness: Harness, comp_id: str) -> Path | None:
    """Return the file a component was declared in, or None if unknown."""
    origins = harness.__dict__.get("_origins") or {}
    return origins.get(comp_id)


def wire_origin(harness: Harness, index: int) -> Path | None:
    """Return the file a wire was declared in, or None if unknown."""
    origins = harness.__dict__.get("_wire_origins") or []
    if 0 <= index < len(origins):
        return origins[index]
    return None


def root_path(harness: Harness) -> Path | None:
    return harness.__dict__.get("_root_path")


def alias_prefix(harness: Harness, comp_id: str) -> str:
    """Alias chain that prefixes a component id in the merged harness.

    Returns "" for root-owned components, "dash" for `dash.HUD`, etc.
    """
    return (harness.__dict__.get("_alias_prefixes") or {}).get(comp_id, "")


def local_component_id(comp_id: str) -> str:
    """Strip any alias prefix from a merged-id component reference.

    `dash.HUD` -> `HUD`. `HUD` -> `HUD`. `dash.switches.HORN` -> `HORN`.
    """
    return comp_id.rsplit(".", 1)[-1]


def strip_alias_from_pin_ref(ref: str, prefix: str) -> str:
    """Remove `<prefix>.` from the front of a pin ref, if present.

    `dash.HUD.PWR` with prefix `dash` -> `HUD.PWR`.
    `HUD.PWR` with prefix `""` -> `HUD.PWR`.
    """
    if not prefix:
        return ref
    if ref.startswith(prefix + "."):
        return ref[len(prefix) + 1:]
    return ref


def loaded_subassemblies(harness: Harness) -> list[tuple[str, Path]]:
    """Distinct (alias, file) pairs for every non-root subassembly currently
    contributing components. Used by the editor's cross-file pin-rename pass.
    """
    origins = harness.__dict__.get("_origins") or {}
    prefixes = harness.__dict__.get("_alias_prefixes") or {}
    root = harness.__dict__.get("_root_path")
    seen: dict[tuple[str, Path], None] = {}
    for cid, origin in origins.items():
        if origin == root:
            continue
        alias = prefixes.get(cid, "")
        if not alias:
            continue
        seen[(alias, origin)] = None
    return list(seen.keys())
