# NU Baja SAE — Hardware

KiCad schematics, PCB layouts, wiring harness designs, and BOM tooling for all NU Baja SAE electronics boards: the eCVT controller, the DAQ node, and the HUD node.

This guide assumes you have never used Git, GitHub, or KiCad before. Follow it top to bottom the first time; after that, skip to [Day-to-day workflow](#day-to-day-workflow).

## Table of contents

- [Glossary](#glossary)
- [One-time setup](#one-time-setup)
- [Getting the files onto your computer](#getting-the-files-onto-your-computer)
- [Opening a board in KiCad](#opening-a-board-in-kicad)
- [Day-to-day workflow](#day-to-day-workflow)
- [Structure](#structure)
- [Releases](#releases)
- [KiCad version](#kicad-version)

## Glossary

A few words you will see everywhere in this guide and on GitHub itself:

| Term | Meaning |
|---|---|
| Repository ("repo") | A folder of files (this one) tracked by Git, with full history of every change. |
| Git | The tool that tracks changes to files over time. Runs on your computer. |
| GitHub | The website that hosts a copy of the repo online so the team can share it. |
| Clone | Downloading a full copy of the repo (with its history) onto your computer. |
| Commit | A saved snapshot of your changes, with a short message describing what changed. |
| Push | Uploading your commits from your computer to GitHub. |
| Pull | Downloading other people's commits from GitHub to your computer. |
| Branch | An independent line of work, so you can make changes without touching `main` until they are ready. |
| Pull Request (PR) | A request to merge your branch into `main`, where others can review your changes before they land. |
| `main` | The default branch — the official, current state of the repo. |

## One-time setup

You only need to do this once per computer.

1. Create a GitHub account at [github.com](https://github.com) if you do not have one, and ask a team lead to add you to the `NU-Baja-SAE` organization so you have push access.
2. Install Git:
   - Windows: download and run the installer from [git-scm.com](https://git-scm.com/downloads). Default options are fine.
   - Mac: open Terminal and run `git --version` — macOS will offer to install it for you if it is missing.
3. Install GitHub Desktop (recommended if you are new to Git) from [desktop.github.com](https://desktop.github.com). It gives you a visual interface for the commit/push/pull steps below instead of typing commands. Everything in this guide can be done either with GitHub Desktop's buttons or with the `git` command line — both are described below.
4. Install KiCad — see [KiCad version](#kicad-version) below for which version to install. Download from [kicad.org/download](https://www.kicad.org/download/).

## Getting the files onto your computer

This is called cloning the repo — it only needs to be done once per computer, after which you just "pull" updates.

Using GitHub Desktop:
1. Open GitHub Desktop and sign in with your GitHub account (File → Options → Accounts).
2. Click File → Clone Repository.
3. Choose the URL tab and paste: `https://github.com/NU-Baja-SAE/hardware`
4. Pick a local folder (e.g. `Documents/NU-Baja-SAE/hardware`) and click Clone.

Using the command line (Git Bash on Windows, Terminal on Mac):
```
git clone https://github.com/NU-Baja-SAE/hardware.git
cd hardware
```

Either way, you now have a folder called `hardware` on your computer containing everything in this repo.

## Opening a board in KiCad

Each board lives in its own folder under `boards/`, e.g. `boards/ecvt-controller/`. Inside that folder is a `.kicad_pro` file — this is the project file.

1. Open KiCad.
2. File → Open Project, then navigate to e.g. `hardware/boards/ecvt-controller/` and select the `.kicad_pro` file.
3. KiCad's project window shows the schematic (`.kicad_sch`) and PCB layout (`.kicad_pcb`) for that board — double-click either to open it in the schematic editor or PCB editor.
4. Make your edits and save (Ctrl+S / Cmd+S) as you normally would in KiCad. Saving only writes to your local copy of the files — it does not upload anything to GitHub. See the next section for how to share your changes.

If you are new to KiCad itself (not just this repo), the official [Getting Started guide](https://docs.kicad.org/) walks through schematic capture and PCB layout basics — worth a read before your first board edit.

## Day-to-day workflow

The general loop, every time you sit down to work on a board:

### 1. Pull the latest changes

Before you start editing, make sure you have everyone else's latest work, so you are not editing an outdated copy.

- GitHub Desktop: click Fetch origin, then Pull origin if it appears.
- Command line: `git pull`

### 2. Create a branch for your change

Do not edit `main` directly — work on a branch so your in-progress changes do not affect anyone else until they are reviewed.

- GitHub Desktop: click the branch dropdown (top middle) → New Branch. Name it something descriptive, e.g. `ecvt-add-current-sense`.
- Command line: `git checkout -b ecvt-add-current-sense`

### 3. Make your changes

Edit the KiCad files (schematic, PCB, etc.), or BOM/harness files, and save them as usual.

### 4. Commit your changes

A commit is a saved checkpoint with a message describing what you did. Commit often, in small logical chunks (e.g. "Add current sense resistor to eCVT controller" rather than one giant commit at the end).

- GitHub Desktop: the left panel lists every changed file. Check the ones you want to include, write a summary in the box at bottom-left, and click Commit to `<branch-name>`.
- Command line:
  ```
  git add boards/ecvt-controller/
  git commit -m "Add current sense resistor to eCVT controller"
  ```

### 5. Push your branch to GitHub

This uploads your commits so others (and CI, see below) can see them.

- GitHub Desktop: click Push origin.
- Command line: `git push -u origin ecvt-add-current-sense`

### 6. Open a Pull Request (PR)

A PR is how you propose merging your branch into `main`.

1. Go to [github.com/NU-Baja-SAE/hardware](https://github.com/NU-Baja-SAE/hardware) — GitHub usually shows a yellow banner offering to Compare & pull request for the branch you just pushed. Click it (or use GitHub Desktop's Create Pull Request button).
2. Write a short description of what changed and why.
3. Click Create pull request.
4. Automated checks (CI) will run against your PR — see `.github/workflows/ci.yml` for what gets checked (ERC/DRC, BOM diff, rendered schematic/PCB images, etc.). Wait for these to finish and fix anything they flag.
5. Ask a teammate to review. Once approved, click Merge pull request on GitHub.
6. Delete the branch (GitHub will prompt you) — its changes now live permanently in `main`.

### 7. Switch back to `main` and pull

- GitHub Desktop: switch the branch dropdown back to `main`, then Pull origin.
- Command line: `git checkout main && git pull`

You are back to a clean, up-to-date `main`, ready to start the loop again for your next change.

## Structure

```
hardware/
├── boards/
│   ├── ecvt-controller/   (.kicad_pro/.kicad_sch/.kicad_pcb + fab-outputs/ + README.md)
│   ├── daq-node/
│   └── hud-node/
├── harness/                # WireViz or WireLab YAML source + output/ (generated SVGs)
├── bom/
│   ├── consolidated-bom.csv
│   └── scripts/generate_bom.py
├── libraries/              # shared KiCad symbol/footprint libs
└── .github/workflows/
```

- `boards/<name>/` — one folder per physical board. Each contains its own KiCad project (`.kicad_pro`), schematic (`.kicad_sch`), PCB layout (`.kicad_pcb`), a `fab-outputs/` folder for generated gerbers/drill files/STEP models (not committed by hand — see below), and a short `README.md` describing the board.
- `harness/` — wiring harness definitions (WireViz/WireLab YAML), with rendered diagrams in `harness/output/`.
- `bom/` — the consolidated bill of materials across all boards (`consolidated-bom.csv`) and the script that generates it from each board's KiCad BOM export.
- `libraries/` — shared KiCad symbol and footprint libraries used by more than one board, so parts stay consistent across designs.
- `.github/workflows/` — CI configuration; this is what automatically checks your PRs (see step 6 above).

Do not manually commit fab outputs (gerbers, drill files, `.step` files) — these are generated from the KiCad source files and are gitignored. CI regenerates them automatically on each PR.

## Releases

Board releases are tagged at the point gerbers are sent to fab, e.g. `ecvt-controller-v1.2`, with a one-line changelog per board.

## KiCad version

All board files must be authored/saved in the same KiCad version to avoid spurious diffs from format upgrades. Current standard version: TBD (update this line once the team confirms).
