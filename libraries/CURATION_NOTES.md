# Curation notes: baja-common-parts

How `baja-common-parts.kicad_sym` / `.pretty` were built from three years'
worth of copy-pasted symbols and footprints on the 2026 (Dingo) boards, and
what got fixed along the way. Source boards: everything under
`NU Baja SAE/Solidworks/2026 - Dingo/Electronics` in the team OneDrive.
Full per-part detail is in `curation_report_symbols.csv` and
`curation_report_footprints.csv` in this folder.

## 1. Excluded KiCad autosave/backup folders

`ecvt_main_board_v3-backups/ecvt_main_board_v3-2026-03-06_141242/` and a
second `-backups(1)/...-2026-04-01_121653/` folder are old autosave
snapshots of `ecvt_main_board_v3`, not separate designs. The first pass at
this library treated them as independent boards, which made an
already-edited part look like it was "in conflict" with its own later
revision. Excluding anything under a folder with "backup"/"autosave" in the
name in the path.

## 2. Dropped stock KiCad libraries

Every schematic/PCB embeds a self-contained cache of the symbols/footprints
it uses, including ones that come from KiCad's own official libraries
(`Device`, `Connector`, `power`, `Capacitor_SMD`, `Resistor_SMD`,
`MountingHole`, `Package_SO`, `Diode_SMD`, `Switch`, `Transistor_FET`,
`Interface_CAN_LIN`, `RF_Module`, `SD_Card`, and others — full list in
`tools/build_baja_common_library.py`). Those aren't unique to Baja; every
member's KiCad install already ships them. Bundling frozen copies would:

- bloat the library for no reason,
- go stale the moment KiCad updates its stock libraries, and
- generate false "conflicts" — most of the ~300 flagged conflicts in the
  original raw harvest turned out to be stock parts whose cached copies
  differed only by trivial KiCad-version formatting noise between boards
  saved in different KiCad versions, not real design differences.

Excluding these cut the flagged conflicts from ~315 down to 12 real ones
on custom parts, and cut the merged library from 73 symbols / 65 footprints
(11 MB combined) down to 21 symbols / 20 footprints (~300 KB combined).

## 3. One 5 MB embedded PDF

`DRV8452DDWR` (used on one adapter board, `tssop-44-dip-48_adapters`) had a
full datasheet PDF embedded directly in the symbol via KiCad's "embedded
files" feature — 5 MB by itself, more than the rest of the entire library
combined. Stripped from every symbol/footprint; the `Datasheet` URL property
already links out to the real datasheet.

## 4. Merged same-part duplicates filed under different old library names

A handful of parts were cached under two unrelated library nicknames
because different people/projects pointed their local KiCad library table
at different folders over the years, even though the underlying part is
identical (or nearly so). Merged into one canonical entry, named after the
manufacturer part number:

| Canonical name | Old lib_ids merged |
|---|---|
| `917782-1` (symbol) | `baja_symbol_library:917782-1`, `baja:917782-1`, `Connector_1x4:917782-1` |
| `917784-1` (symbol) | `baja_symbol_library:917784-1`, `Connector_1x6:917784-1` |
| `917783-1` (symbol, footprint) | `Connector_1x5 v2:917783-1` (symbol); `baja_footprint_library:Connector_1x5` (footprint, renamed to match) |
| `ACT45B-101-2P-TL003` (footprint) | `baja_footprint_library:...`, `baja:...` |
| `MOLEX_15311026` (footprint) | `baja_footprint_library:...`, `baja:...` |
| `TE-Connectivity_917780` (footprint) | `baja_footprint_library:TE-Connectivity_ 917780` (note stray space — fixed), `baja:TE-Connectivity_ 917780` |
| `TE-Connectivity_917782` (footprint) | `baja_footprint_library:...`, `baja:...` |
| `baja_logo_footprint` (footprint) | `baja_footprint_library:...`, `baja:...` |

Where the merged sources' content genuinely differed (not just a naming
duplicate — see §5), the version from the most recently edited source file
was kept as canonical.

## 5. Real content conflicts — resolved by picking the latest edit

A few parts weren't just duplicate names — the actual graphics/pads
differed between boards, meaning someone genuinely revised the part between
board revisions:

- **`DRV8462DDVR`** (eCVT motor driver symbol): 3 distinct versions, one per
  `ecvt_main_board_v1/v2/v3`. Kept the `v3` version (2026-02-27), since it's
  the most recent and `ecvt_main_board_v3` is the most-recently-touched
  eCVT revision overall.
- The TE Connectivity/Molex footprints in §4's table, plus `917784-1` and
  `917782-1` symbols: each had multiple distinct graphical variants across
  boards (likely independent hand-redraws over three years rather than one
  shared source). Resolution rule applied uniformly: **keep the variant from
  whichever source file has the latest modification time.** In practice this
  converged on `ecvt_main_board_v3` (last touched 2026-04-19, the most
  recently maintained board in the dataset) for nearly every connector.

If a part you rely on doesn't match what you remember, check the CSV
reports for its `source_lib_ids` and `content_variants` columns and go look
at the original board file if you need the historical version.

## 6. Fixed broken names

- `baja_symbol_library:‎BL99232CH` had an invisible Unicode
  left-to-right-mark character baked into the name (likely a bad
  copy/paste from a datasheet or web page). Stripped to `BL99232CH`.
- A second, unprefixed symbol literally named `‎BL99232CH_1` (no
  `Library:` prefix at all — not a valid KiCad library reference) was cached
  alongside it in the same schematic. Dropped; it wasn't usable as a library
  part in the first place and its content wasn't distinguishable from a
  broken duplicate of the fix above.
- `TE-Connectivity_ 917780` (stray space after the underscore) → renamed to
  `TE-Connectivity_917780` to match the naming of its siblings
  (`TE-Connectivity_917781`, `_917782`, etc).

## 7. Repaired Footprint associations on symbols

Most custom symbols' `Footprint` property was either blank or pointed at a
bare name with no library prefix (e.g. `CON4_1X4_P100` with no
`LibraryName:` in front) — in KiCad that's not a valid, auto-resolving
reference, so it never actually worked as an assignment. Cross-referenced
each symbol's manufacturer part number against the footprints that were
actually placed on a real PCB, and re-pointed the `Footprint` field at
`baja-common-parts:<name>` wherever a real match existed:

| Symbol | Footprint now assigned |
|---|---|
| `917780-1` | `CON2_1X2_P100` |
| `917781-1` | `CON3_1X3_P100` |
| `917782-1` | `CON4_1X4_P100` |
| `917783-1` | `917783-1` |
| `917784-1` | `TE-Connectivity_1x6` |
| `917786-1` | `TE-Connectivity_917786` |
| `917791-1` | `TE-Connectivity_917791` |
| `ACT45B-101-2P-TL003` | `ACT45B-101-2P-TL003` |
| `TEA_1-0505` | `CONV_TEA_1-0505` |
| `TSR_1-24120` | `TSR1-SINGLE_TRP` |
| `NAU7802SGI` | `SOI16_NAU7802SGI_NUV` |

**Left unresolved, needs a human:** `15311026` and `15311046` (Molex
symbols) had `Footprint` fields pointing at `CONN_SD-5219-02AX-X_MOL` /
`CONN_SD-5219-04A_MOL`, which don't match any footprint actually harvested
from a real PCB layout — these parts may never have made it to layout.
`ESD3V3AP-TP` and `‎BL99232CH`→`BL99232CH` have no footprint assigned
at all and none was found on any board. Assign these manually before using
the part on a new board.

## 8. Kept but flagged: ad hoc generic pin-header footprints

`CON2_1X2_P100`, `CON3_1X3_P100`, `CON3_1X3_P100_KiCADv6`, and
`CON4_1X4_P100` are simple hand-drawn 1xN pin header footprints (not
tied to a specific vendor part), most likely redundant with KiCad's stock
`Connector_PinHeader_2.54mm` / `Connector_PinSocket_2.54mm` libraries.
Kept for backward reference since several symbols' `Footprint` fields point
at them (see §7), but new designs should prefer the stock libraries instead.
