# dupfinder

Find **byte-identical duplicate files** and review them in a local web UI before
deleting anything. Pure Python standard library — no `pip install`, no build step.

Deleting never calls `rm`. Files move to a quarantine folder on the same drive
and every move is undoable.

[ภาษาไทย →](README.th.md)

![Reviewing duplicates](docs/01-review.png)

> The web interface is currently **in Thai**. Everything else — code, comments,
> CLI, this document — is in English. If you want an English UI, open an issue.

---

## Quick start

```bash
python3 dupfinder.py
```

Opens `http://127.0.0.1:8777` in your browser. Pick a folder, scan, review, delete.

```
--port 9000        use a different port
--root ~/Pictures  pre-fill the scan folder (default: your home directory)
--no-open          don't launch a browser (headless / SSH)
```

Requires Python 3.7+. No dependencies.

---

## Screenshots

Duplicate groups, with the outcome of each group labelled before you commit to it —
selected copies are marked **"will be moved"**, the survivor is marked **"keeping"**:

![Duplicate groups](docs/02-groups.png)

Quarantine: everything you removed is still here, previewable, and restorable —
per file or per batch:

![Quarantine](docs/04-quarantine.png)

---

## Why not just compare filenames?

Because copies get renamed. `IMG_0421.JPG`, `IMG_0421 copy.JPG`, and
`photo-final-v2.jpg` can be the same 4 MB of bytes. dupfinder compares content.

## How the scan works

Hashing every file on a large drive would be unbearably slow, so the scan runs
in three passes and most files never get read at all:

| Pass | What it does | What it eliminates |
|------|--------------|--------------------|
| 1 | Group by **file size** (metadata only) | Any size that appears once cannot have a duplicate — typically 90%+ of files |
| 2 | Hash the **first 64 KB** of remaining candidates | Files that merely happen to share a size |
| 3 | Hash the **whole file** (BLAKE2b) for survivors | Confirms a true byte-for-byte match |

Results are cached in SQLite keyed by `path + size + mtime`, so a re-scan is
nearly instant and an interrupted scan can be resumed.

**Real-world example:** 4,370 files (~30 GB) on a USB hard drive finished in
about 15 seconds, because only 227 files needed a full hash.

## What it skips automatically

- `._*` (AppleDouble), `.DS_Store`, `Thumbs.db`, `desktop.ini`
- `$RECYCLE.BIN`, `System Volume Information`, `.Spotlight-V100`, `.Trashes`,
  `.fseventsd`, `lost+found`, `@eaDir`
- Zero-byte files, and anything below the size threshold you pick
- Symlinks
- **Hard links** — two names pointing at the same inode are one physical file,
  not a duplicate, and deleting one frees nothing

---

## Safety model

This is a tool that deletes things, so the defaults are deliberately timid.

- **Nothing is ever unlinked by the delete button.** Files are *moved* to
  `<scan folder>/_DUPFINDER_QUARANTINE/<timestamp>/`, keeping their original
  directory structure. On the same volume this is a metadata rename — instant,
  even for gigabytes.
- **Undo restores a whole batch or a single file** to exactly where it came from.
- **Permanent deletion is a separate, explicit action** in the Quarantine tab.
- **The server refuses any request that would leave a duplicate group with zero
  files.** You cannot accidentally delete every copy of something.
- **Each batch is bound to the folder it came from**, not to whatever you happen
  to be scanning now. Switching drives and hitting delete won't target the wrong
  path.
- **If the quarantine folder can't be found** (drive unplugged), permanent
  deletion is refused and the ledger is left intact, so files can't be orphaned.
- The server binds to `127.0.0.1` only, and will only read file contents for
  paths it has indexed or quarantined.

### If the bookkeeping is lost

The record of what was moved where lives in `.state/ledger.db`. Every quarantine
batch *also* carries a `manifest.json` inside its own folder, so the files can
describe themselves even if that database is deleted, or the drive is moved to
another machine.

The Quarantine tab has a **"find orphaned quarantines"** search that scans for
`_DUPFINDER_QUARANTINE` folders and adopts them back into the ledger. It works
with or without a manifest — the folder layout inside a batch already mirrors
the original paths.

---

## Choosing what to keep

Reviewing thousands of groups by hand is the real work, so there are rules that
select everything *except* the copy you want to keep:

- **oldest / newest copy**
- **shortest path** — usually the original, before it got buried in backups
- **the copy inside a specific folder** — the most useful one in practice. Point
  it at the folder you trust and everything outside gets selected.

Groups that are ambiguous (every copy is inside the target folder, or none are)
are skipped rather than guessed at, and auto-select only touches the groups
visible on the current page — you always see what you're about to delete.

Selected rows are tagged **"will be moved"**; their surviving siblings are tagged
**"keeping"**, so the outcome of each group is legible at a glance.

---

## Storage layout

```
.state/index.db     scan index      — disposable, rebuilt by re-scanning
.state/ledger.db    quarantine log  — keep this; it maps files back to origins
.state/thumbs/      thumbnail cache — disposable
```

They are deliberately separate files so clearing the index can never take the
quarantine records with it.

---

## Platform support

| | Status |
|---|---|
| **macOS** | Developed and tested here. Thumbnails come from `qlmanage` (QuickLook), which handles HEIC, MOV, RAW and PDF with no extra dependencies. |
| **Linux** | Expected to work. Thumbnails need `Pillow` (`pip install pillow`) and cover common image formats only; without it, files show a file-type badge instead. "Reveal in file manager" uses `xdg-open`. |
| **Windows** | Untested. Paths are handled portably and drive letters are detected, but nobody has run it. Reports welcome. |

Thumbnails are strictly optional everywhere — the tool is fully usable without them.

---

## Limitations

- **Exact duplicates only.** A photo re-encoded, resized, or converted from HEIC
  to JPEG is a different file and will not be matched. Perceptual/near-duplicate
  matching is not implemented.
- **The UI is in Thai.**
- Auto-select applies per page, by design.
- Not intended to be exposed to a network. It is a local single-user tool and
  has no authentication.

## Standalone app

No Python on the machine? Build a self-contained bundle (~10 MB):

```bash
pip install pyinstaller
pyinstaller --noconfirm --windowed --name dupfinder --icon icon.icns \
  --add-data "index.html:." dupfinder.py
```

`dist/dupfinder.app` then runs on its own. When frozen, state moves to
`~/Library/Application Support/dupfinder` (`~/.dupfinder` elsewhere) since the
bundle is read-only. Unsigned, so the first launch needs right-click → Open.

## License

MIT — see [LICENSE](LICENSE).
