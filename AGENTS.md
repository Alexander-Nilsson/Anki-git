# AGENTS.md — AnkiGit

Anki addon providing Git-based version control for Anki collections. Export collections to a human-readable Git repo.

## Architecture

```
anki_git/
├── __init__.py          # Hook registration only; calls init_addon() on import
├── addon.py             # Qt UI: menus, dialogs, hook wiring
├── config.py            # AnkiGitConfig dataclass + SyncMode enum
├── engine/              # NEVER import aqt here — must be testable without Anki
│   ├── exporter.py      # Anki→files (one-way snapshot; supports quick delta mode)
│   ├── importer.py      # files→Anki (one-way pull via conflict pipeline)
│   ├── sync.py          # Two-way sync: merge changes in both directions
│   ├── git_ops.py       # GitPython operations
│   ├── conflict.py      # Three-way merge + auto-resolution by sync_mode
│   ├── checksums.py     # meta.json hashing + quick_has_changes()
│   ├── export_helpers.py# Shared export helpers (single note export)
│   └── import_helpers.py# Shared import helpers (checksums, batch import)
├── formats/
│   ├── notes_md.py      # One file per note: decks/<Deck>/<nid>.md
│   └── notetype_yaml.py # notetypes/<Name>.yaml + <Name>.css
├── ui/
│   ├── settings.py      # Settings dialog (includes sync_mode selector)
│   ├── conflicts.py     # Conflict resolution dialog
│   ├── diff.py           # Diff preview dialog
│   └── utils.py         # Shared UI utilities (run_on_main_sync)
└── config.json          # Anki addon config manager schema
```

## Sync Modes (AnkiGitConfig.sync_mode)
- `always_ask` — show conflict dialog for true conflicts (default)
- `prefer_anki` — auto-resolve conflicts in favor of Anki side
- `prefer_repo` — auto-resolve conflicts in favor of repo side
- `accept_all` — auto-accept non-conflicting changes; for true conflicts Anki wins

## Critical Rules

- **engine/ must never import aqt** — only `anki`. Addon.py + ui/ handle Qt.
- **Collection writes only on main thread** via `mw.taskman.run_on_main()`. Never from background threads.
- **Pre-operation backups** before any import/pull.
- **Wrap imports** in `col.db.begin()` / `.commit()` / `.rollback()`.
- **Match notes by nid**, notetypes by name.
- **Auto-snapshot on close** uses `quick=True` (delta: only `mod > last_max_mod`) — fast.
- **Auto-sync on startup** uses `quick_has_changes()` first — instant skip if nothing changed. No mid-session auto-export.
- **Data pass-through between diff and apply phases** — both import and export flows compute raw parsed data during the diff preview and pass it to the apply phase, avoiding redundant collection/filesystem scans.

## Commands

```bash
uv run pytest tests/                         # all tests (needs anki/aqt installed)
uv run pytest tests/ -m "not integration"    # engine-layer tests only
uv run ruff check anki_git/ tests/           # lint
uv run pyright anki_git/                     # type check (engine layer only)
uv run python build.py all                   # clean → build → package .ankiaddon
# version bump handled by CI/CD on push to main
```

## Flows

**Startup / Import (`on_profile_open`, `import_action`)**:
1. Show menu (once per session)
2. Fire-and-forget `git fetch` in daemon thread (non-blocking)
3. `quick_has_changes()` — 2 SQL queries + git status check (~5ms)
4. No changes → return silently (instant)
5. Changes detected → `QueryOp`: `compute_import_diff_delta()`:
   - Uses `git status --porcelain` + `git diff --name-status` to find only changed repo files
   - Parses only those files, looks up only their Anki counterparts
   - Returns `ImportDiffData` (report + raw parsed notes + checksums)
6. `DiffDialog`: show changes, user accepts/rejects
7. Accepted → backup → `pull_from_repo()` with pre-computed `ImportDiffData`:
   - `import_notes()` and `import_notetypes()` skip re-scanning
   - Verification commit commits ALL staged files (no unstaging of unchecked nids)
8. Rejected → silent exit

**Export / Snapshot (`snapshot_action`)**:
1. `compute_export_diff_delta()`:
   - Delta: `SELECT id FROM notes WHERE mod > last_max_mod` on Anki side
   - `get_changed_repo_files()` for any git-side changes
   - Parses only affected `.md` files, fetches only affected Anki notes
   - Returns `ExportDiffData` (report + serialized note entries + checksums + all_nids)
   - Falls back to full scan if no `last_commit_sha` baseline exists
2. `DiffDialog`: show changes, user accepts/rejects
3. Accepted → `export_collection(export_data=...)`:
   - Skips `capture_export_data()` — builds `CapturedExport` from pre-computed data
   - Only issues 3 fast scalar queries (`all_nids`, `MAX(mod)`, `COUNT(*)`) for freshness
   - `write_export_data()` writes only changed files + stale cleanup by nid-from-filename (no file reads)

**Close (`on_profile_close`)**:
1. Guard: repo exists, collection open
2. `quick_has_changes()` → no changes → return instantly
3. `capture_export_data(quick=True)` (sync, needs collection):
   - Delta: `SELECT id FROM notes WHERE mod > last_max_mod`
   - Returns serialized `CapturedExport` — no longer needs collection access
4. Background thread: `write_export_data()`:
   - Writes files, git commit, push
   - No collection access required

## Troubleshooting

**2472-item re-detection loop at startup:**
- Root cause: user unchecked notes in DiffDialog → `pull_from_repo`'s verification commit unstaged those files (`git reset HEAD --`) → files remained as untracked/pending → `_content_has_changes()` returned True on every startup → loop persisted.
- **Fix:** Removal of unstaging loop in `importer.py` — verification commit now commits ALL staged files unconditionally. Unchecked notes skip Anki import but their git state is still baselined.
- **Secondary cause:** `write_export_data` push crash prevents meta save → stale `last_commit_sha` → re-detects all changes on next startup. **Fix:** push/commit wrapped in try/except, meta save wrapped in try/finally.

**Python 3.14 `sys.excepthook is None` RuntimeError:**
- CPython threading bug triggered when GitPython subprocess threads crash and `sys.excepthook` is None (common in embedded Python/Anki).
- `push_to_remote` catches via `except Exception` (RuntimeError is subclass), returns `(False, error_msg)`.
- `write_export_data` now wraps push in try/except to prevent crash from preventing meta save.

**DiffDialog freezes with 2000+ items:**
- `_populate_tree()` creates one QTreeWidgetItem per change on the main thread; each insertion triggers layout recalculation.
- **Fix:** wrap inserts in `setUpdatesEnabled(False/True)` in `ui/diff.py`.

## Quirks

- **Python ≥ 3.13** only.
- **Version in two places** — update both `pyproject.toml` AND `anki_git/__init__.py`.
- **`engine/importer.py`** uses `rglob("*.md")` which matches both `<nid>.md` and legacy `notes.md` — `parse_notes_file()` handles both formats.
- **Menu:** "Export to Repo" (Anki→Git), "Import from Repo" (Git→Anki), "Settings..."
- **Ruff + pyright** for static analysis. Config in `pyproject.toml`.
- **License is AGPL-3.0-only**.
- **Fixtures** in `tests/conftest.py` provide `anki_session` (headless Anki) and `mock_aqt_mw`.
- **Pre-commit hooks** available (`.pre-commit-config.yaml`).**
