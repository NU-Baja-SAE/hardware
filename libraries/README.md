# libraries

Shared KiCad symbol and footprint library used across boards: `baja-common-parts`.

It was built by scanning every 2026 (Dingo) board's schematic/PCB files for
the custom, vendor-specific parts the team actually used (connectors, the
eCVT motor driver, hall sensors, DC-DC modules, the strain gauge ADC, etc.),
deduplicating them, and fixing the naming/data problems that had crept in
over three years of copy-pasting between projects. See
[CURATION_NOTES.md](CURATION_NOTES.md) for exactly what changed and why.

This library carries two kinds of part:

1. **Custom / vendor-specific parts** that aren't in a stock KiCad install
   at all (the connectors, motor driver, hall sensor, DC-DC modules, ADC,
   etc.).
2. **"Pick-proof" versions of standard parts** — a small set of common
   passives and signature ICs that *do* exist in stock KiCad, but which we
   re-package here with the **team's correct footprint already filled in**,
   so a newcomer can drop in the right part without having to know which of
   KiCad's dozens of footprint variants to choose. See "Pick-proof passives"
   and "Signature parts" below.

We deliberately do **not** copy in trivial stock parts where there's no
decision to encode (bare `power:*` rails, `Mechanical:MountingHole`, generic
`Device:LED`/`Fuse`) — those are identical in every KiCad install and
duplicating them only invites drift. The rule of thumb: a part earns a place
here if it's custom, or if putting it here saves someone from making a wrong
footprint/part choice.

## One-time setup (each member, once per computer)

1. Clone this `hardware` repo (see the top-level [README](../README.md) if you haven't already).
2. Open KiCad → **Preferences → Configure Paths**. Add an environment variable:
   - Name: `BAJA_LIB`
   - Path: the full path to this `libraries` folder, e.g. `C:\Users\you\Documents\NU-Baja-SAE\hardware\libraries` (Windows) or `/Users/you/NU-Baja-SAE/hardware/libraries` (Mac/Linux).
3. Open KiCad → **Preferences → Manage Symbol Libraries**. Under the **Global Libraries** tab, click the folder icon and add `baja-common-parts.kicad_sym`, or just append this line to your global `sym-lib-table` (Preferences will tell you where that file lives):
   ```
   (lib (name "baja-common-parts")(type "KiCad")(uri "${BAJA_LIB}/baja-common-parts.kicad_sym")(options "")(descr "NU Baja SAE shared parts"))
   ```
4. Do the same under **Preferences → Manage Footprint Libraries**, or append this line to your global `fp-lib-table`:
   ```
   (lib (name "baja-common-parts")(type "KiCad")(uri "${BAJA_LIB}/baja-common-parts.pretty")(options "")(descr "NU Baja SAE shared footprints"))
   ```
5. Restart KiCad. `baja-common-parts` now shows up as a library in the symbol/footprint pickers in every project, right alongside the stock KiCad libraries.

(Ready-to-use copies of both table snippets are in [`sym-lib-table`](sym-lib-table) and [`fp-lib-table`](fp-lib-table) in this folder if you'd rather copy the whole file than hand-edit yours — back up your existing global table first if you already have other libraries configured.)

## What's in it

**Custom / vendor-specific parts:**
TE Connectivity board connectors (917780/781/782/783/784/786/791-1), Molex
15311026/15311046, the ACT45B CAN connector, the eCVT motor driver
(DRV8452DDWR / DRV8462DDVR), the MLX90316 hall sensor, the NAU7802 load-cell
ADC, TRACO TSR/TEA DC-DC modules, an ESD protection diode, brake-light ICs,
a thermistor pad, the ESP32-DevKitC-V4 module (the MCU board used on the
daq/hud/eCVT nodes — its footprint is two 1x19 2.54mm female pin sockets at
22.86mm/0.9" row spacing, matching how those boards socket the module), and
the Baja logo footprint.

**Pick-proof passives** (see "Passive footprint sizes" below) — stock
`Device:R`/`Device:C` bodies with the team-standard HandSolder footprint
pre-filled so you never leave the footprint field blank or pick a wrong size:
`R_0603`, `R_0805`, `R_1206`, `C_0603`, `C_0805`. Just place one and type the
value.

**Signature parts** — parts that exist in stock KiCad but that the team uses
on essentially every board, re-packaged here with the footprint we actually
use so they're one-stop: `TJA1051T-3` (CAN transceiver, SOIC-8), `TSR_1-2450`
(TRACO switching regulator, THT), `TPD2EUSB30` (USB/ESD protection),
`SMAJ26A` (400 W TVS, D_SMA), `Q_PMOS_DGS` (P-channel MOSFET, SOT-23-3),
`Conn_01x19_Socket` (the 1x19 female socket the ESP32 plugs into), and
`DM3CS-SF` (microSD socket — its footprint was already here; now the symbol
is too).

A few footprints (`CON2_1X2_P100`, `CON3_1X3_P100`, `CON3_1X3_P100_KiCADv6`,
`CON4_1X4_P100`) are hand-drawn generic pin-header footprints kept only for
backward reference — prefer KiCad's stock `Connector_PinHeader_2.54mm` /
`Connector_PinSocket_2.54mm` libraries for new designs instead of these.

## Passive footprint sizes

Standard KiCad stock libraries cover resistors/capacitors (see above), but
KiCad ships dozens of footprint sizes for each, and picking a different one
per part makes boards inconsistent to hand-assemble and rework. Based on
what's actually used on last year's boards (e.g. `boards/daq-node`'s
`daq_board.kicad_pcb`), the team standard is:

- **0603** by default (`R_0603_1608Metric_..._HandSolder` /
  `C_0603_1608Metric_..._HandSolder`).
- **0805** where a part needs more pad area (higher power/voltage dissipation,
  or easier hand-rework of a part you expect to swap).
- **1206** for the higher-power resistor/cap cases.
- Always the **`HandSolder`** pad variant (larger pads/toe fillets than the
  reflow-only default) — boards are hand-soldered, not reflowed.
- Nothing smaller than 0603 (e.g. 0402/0201) — too fiddly to hand-solder
  reliably.
- **Bulk/polarized caps** (electrolytic / tantalum — the `CP_*` footprint
  families) are exempt from the chip-size rule: a bulk energy-storage cap
  can't be shrunk to a chip footprint, so its own package footprint is fine.

**The easy way to get this right:** place one of the pick-proof passive
symbols from this library — `R_0603` / `R_0805` / `R_1206` / `C_0603` /
`C_0805` — instead of stock `Device:R` / `Device:C`. Each already has the
correct `HandSolder` footprint filled in, so you just set the value and move
on. (The `Value` defaults to the size name as a reminder — change it to the
actual value, e.g. `10k`.) This is what a newcomer should reach for.

If you do place a bare stock `Device:R`/`Device:C`, pick the matching
`Resistor_SMD:R_0603_1608Metric_Pad0.98x0.95mm_HandSolder` /
`Capacitor_SMD:C_0603_1608Metric_Pad1.08x0.95mm_HandSolder` (or the 0805/1206
equivalent) footprint from KiCad's stock library — don't leave the footprint
field blank or pick an arbitrary size. `tools/check_passive_footprints.py`
(run in CI) will flag any that don't match the standard.

`tools/check_passive_footprints.py` scans `boards/**/*.kicad_pcb` and
`*.kicad_sch` for `Resistor_SMD`/`Capacitor_SMD` references that don't match
this standard and is run in CI on every PR that touches `boards/**` (see
`.github/workflows/ci.yml`).

## Trace width / netclass convention

Standard across boards (matches the netclasses already set up on
`daq_board.kicad_pro`):

- **`Default` netclass** (general signal wiring): track width >= **0.3mm**.
- **`Power` netclass**: track width >= **0.6mm**. Add a higher-current
  class (e.g. a project-specific `24V` class) with its own wider default
  if a board has a rail that needs more than 0.6mm.
- **Absolute manufacturer floor, regardless of class**: **0.1mm** trace
  width, **0.09mm** clearance (JLCPCB's stated capability for the 4-6
  layer/preferred tier — see comments in `baja-common.kicad_dru`).

[`baja-common.kicad_dru`](baja-common.kicad_dru) is the shared DRC rule
template encoding all of the above (plus the JLCPCB hole/annular
ring/silkscreen rules already used on `daq_board.kicad_dru`). Copy it into
a new board's project folder as `<project>.kicad_dru` so KiCad's DRC
enforces it locally.

Enforcement split, since a text-based lint can't evaluate real board
geometry the way KiCad's DRC engine can:

- **Trace width** — `tools/check_trace_widths.py` parses each
  `boards/**/*.kicad_pcb` (+ sibling `.kicad_pro` for netclass info) and
  checks every routed segment/arc against its netclass's standard width.
  Two severities: **error** (narrower than the 0.1mm absolute
  manufacturer floor — fails the CI check, since that's not
  manufacturable) and **warning** (at/above 0.1mm but not exactly the
  class standard — e.g. a 0.2mm impedance-controlled diff pair, or a
  deliberately-beefed-up 1.0mm power trace — printed for visibility but
  doesn't fail the check, so the PR can still merge). Runs in CI on every
  PR touching `boards/**`.
- **Clearance** — not checked by CI yet; enforced only by the `.kicad_dru`
  rules above when you run KiCad's DRC (Inspect → Design Rules Checker)
  locally before committing. Wiring `kicad-cli pcb drc` into the (currently
  placeholder) `erc-drc` CI job would close this gap.

## Updating the library

Don't hand-edit `baja-common-parts.kicad_sym` / `.pretty` piecemeal if you
can avoid it — future maintainers won't know why a symbol looks the way it
does. If a part needs fixing, fix it in KiCad's Symbol/Footprint Editor with
this library open directly, save, and commit with a clear message. If
you're harvesting a fresh batch of parts from a new season's boards, see
`tools/` for the scripts used to build this one.
