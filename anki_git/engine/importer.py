"""Import engine: apply changes from Git repo back into Anki collection.

This module reads the Markdown notes and YAML notetypes from the repo
and writes them into the Anki collection. All collection writes happen on
the main thread via mw.taskman.run_on_main().

Matching strategy:
  - Notes: matched by nid. If nid exists in collection -> update.
           If nid absent -> create new note.
  - Notetypes: matched by name. If name exists -> update preserving ID.
               If name absent -> create new.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anki.collection import Collection

    from anki_git.formats.notes_md import Note
    from anki_git.formats.notetype_yaml import Notetype
from dataclasses import dataclass, field

from anki_git.engine import import_helpers

_logger = logging.getLogger("anki_git")


def _nid_from_path(path: Path) -> int | None:
    """Extract nid from a decks/<deck>/<nid>.md path."""
    if path.suffix != ".md":
        return None
    try:
        return int(path.stem)
    except ValueError:
        return None


@dataclass
class ImportResult:
    notes_updated: int = 0
    notes_created: int = 0
    notetypes_updated: int = 0
    notetypes_created: int = 0
    notes_deleted_from_anki: int = 0
    notes_deleted_from_git: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflict_report: object = None


def preview_import(repo_path: Path, col: "Collection | None" = None) -> ImportResult:
    """Analyze what would change without applying anything (dry-run).

    If col is provided, performs real diffing via compute_import_diff.
    Otherwise falls back to simple file counting.
    """
    if col is not None:
        from anki_git.engine.diff import compute_import_diff_delta

        data = compute_import_diff_delta(col, repo_path)
        report = data.report
        created = sum(1 for d in report.note_diffs if d.change_type == "added")
        modified = sum(1 for d in report.note_diffs if d.change_type == "modified")
        deleted = sum(1 for d in report.note_diffs if d.change_type == "deleted")
        nt_created = sum(1 for d in report.notetype_diffs if d.change_type == "added")
        nt_modified = sum(1 for d in report.notetype_diffs if d.change_type == "modified")
        nt_deleted = sum(1 for d in report.notetype_diffs if d.change_type == "deleted")
        return ImportResult(
            notes_created=created,
            notes_updated=modified,
            notetypes_created=nt_created,
            notetypes_updated=nt_modified,
            warnings=[]
            if not (deleted or nt_deleted)
            else [
                f"{deleted} notes would be deleted",
                f"{nt_deleted} notetypes would be deleted",
            ],
        )

    # Fallback: simple file counting without collection access
    from anki_git.formats.notes_md import parse_notes_file
    from anki_git.formats.notetype_yaml import read_all_notetypes

    notetypes_dir = repo_path / "notetypes"
    decks_dir = repo_path / "decks"

    repo_notetypes = read_all_notetypes(notetypes_dir)
    note_count = 0
    for notes_file in sorted(decks_dir.rglob("*.md")):
        notes = parse_notes_file(notes_file)
        note_count += len(notes)

    return ImportResult(
        notetypes_created=len(repo_notetypes),
        notes_created=note_count,
    )


def pull_from_repo(
    col: "Collection",
    repo_path: Path,
    conflict_callback=None,
    sync_mode: str = "accept_all",
    anki_checksums: dict[str, str] | None = None,
    git_checksums: dict[str, str] | None = None,
    git_notes_lookup: dict[int, "Note"] | None = None,
    repo_notetypes: dict[str, "Notetype"] | None = None,
) -> ImportResult:
    """Import repo state into Anki with conflict detection and optional resolution.

    Steps:
    1. Compute checksums for current Anki notes, Git notes, and base (meta.json)
    2. Run conflict detection + auto-resolution
    3. If conflicts exist, invoke conflict_callback(report) for user resolution
    4. Apply only git-winning changes to Anki
    5. Delete from Anki notes that were deleted in repo (if resolution says so)
    6. Delete from repo notes that were deleted in Anki (if resolution says so)
    7. Persist tracking metadata (last_note_count, last_max_mod, last_commit_sha)
       to prevent unnecessary re-export on close
    8. Create verification commit recording the import

    conflict_callback receives a ConflictReport and must return a resolved ConflictReport.

    If anki_checksums / git_checksums / git_notes_lookup / repo_notetypes
    are provided, skips the corresponding filesystem scans.
    """
    from anki_git.engine.checksums import content_hash, load_meta, save_meta
    from anki_git.engine.conflict import ConflictType, process_conflicts

    if anki_checksums is None:
        anki_checksums = import_helpers.compute_anki_checksums(col)
    if git_checksums is None and git_notes_lookup is None:
        git_checksums, git_notes_lookup = import_helpers.compute_git_checksums(repo_path)

    assert anki_checksums is not None
    assert git_checksums is not None and git_notes_lookup is not None

    _logger.info(
        "DEBUG pull_from_repo: anki_checksums=%d entries, git_checksums=%d entries",
        len(anki_checksums),
        len(git_checksums),
    )
    anki_keys = set(anki_checksums)
    git_keys = set(git_checksums)
    _logger.info(
        "DEBUG pull_from_repo: key_overlap=%d anki_only=%d git_only=%d first_5_match=%s",
        len(anki_keys & git_keys),
        len(anki_keys - git_keys),
        len(git_keys - anki_keys),
        {
            k: (anki_checksums.get(k) == git_checksums.get(k))
            for k in sorted(anki_keys & git_keys)[:5]
        },
    )

    meta = load_meta(repo_path)
    base_checksums = meta.get("note_checksums", {})
    _logger.info("DEBUG pull_from_repo: base_checksums=%d entries", len(base_checksums))

    report = process_conflicts(
        base_checksums,
        anki_checksums,
        git_checksums,
        sync_mode,
        col,
        repo_path,
        notes_lookup=git_notes_lookup,
    )
    _logger.info(
        "DEBUG pull_from_repo: conflicts=%d (unresolved=%d)",
        len(report.conflicts),
        sum(1 for c in report.conflicts if not c.resolved),
    )
    for c in report.conflicts[:5]:
        _logger.info(
            "DEBUG conflict: nid=%s type=%s resolved=%s resolution=%s",
            c.nid,
            c.conflict_type,
            c.resolved,
            c.resolution,
        )

    if conflict_callback and report.has_conflicts:
        report = conflict_callback(report)
        _logger.info(
            "DEBUG pull_from_repo: after callback conflicts=%d unresolved=%d",
            len(report.conflicts),
            sum(1 for c in report.conflicts if not c.resolved),
        )

    resolved_nids: set[int] = set()
    delete_from_anki_nids: set[int] = set()
    delete_from_git_nids: set[int] = set()
    anki_wins_nids: set[int] = set()

    for c in report.conflicts:
        if not c.resolved:
            continue
        if c.resolution == "git":
            resolved_nids.add(c.nid)
        elif c.resolution == "anki":
            pass  # Keep Anki version, nothing to import
        if c.conflict_type == ConflictType.DELETE_FROM_ANKI:
            delete_from_anki_nids.add(c.nid)
        if c.conflict_type == ConflictType.DELETE_FROM_GIT:
            delete_from_git_nids.add(c.nid)
        if c.resolution == "anki" and c.conflict_type not in (
            ConflictType.DELETE_FROM_GIT,
            ConflictType.ALREADY_GONE,
        ):
            anki_wins_nids.add(c.nid)

    _logger.info(
        "DEBUG pull_from_repo: resolved_nids=%d delete_from_git_nids=%d anki_wins_nids=%d",
        len(resolved_nids),
        len(delete_from_git_nids),
        len(anki_wins_nids),
    )

    # Write Anki-winning content to git files so the repo converges.
    # This prevents the same conflict from being detected on every import.
    if anki_wins_nids:
        from anki_git.engine.constants import DECKS_DIR
        from anki_git.engine.export_helpers import capture_single_note
        from anki_git.formats.notes_md import write_note_file

        for nid in anki_wins_nids:
            captured = capture_single_note(col, nid)
            if captured is not None:
                serialized, note = captured
                deck_parts = note.deck.split("::")
                note_dir = repo_path / DECKS_DIR / Path(*deck_parts)
                write_note_file(note_dir, note, content=serialized)

    result = import_from_repo(
        col,
        repo_path,
        nid_filter=resolved_nids,
        notes_lookup=git_notes_lookup,
        repo_notetypes=repo_notetypes,
    )
    result.conflict_report = report

    for nid in delete_from_anki_nids:
        if import_helpers.delete_note_from_anki(col, nid):
            result.notes_deleted_from_anki += 1

    for nid in delete_from_git_nids:
        if import_helpers.delete_note_from_repo(repo_path, nid):
            result.notes_deleted_from_git += 1

    assert col.db is not None
    meta["last_note_count"] = col.db.scalar("SELECT COUNT(*) FROM notes WHERE id > 0") or 0
    meta["last_max_mod"] = col.db.scalar("SELECT MAX(mod) FROM notes WHERE id > 0") or 0

    from anki_git.engine.git_ops import open_repo

    repo = open_repo(repo_path)

    total_imported = result.notes_updated + result.notes_created
    total_notetypes = result.notetypes_updated + result.notetypes_created
    _logger.info(
        "DEBUG pull_from_repo: total_imported=%d total_notetypes=%d",
        total_imported,
        total_notetypes,
    )

    # Determine which nids to keep staged in the verification commit.
    needs_commit_nids: set[int] = resolved_nids | delete_from_git_nids | anki_wins_nids
    if needs_commit_nids:
        all_imported_nids = needs_commit_nids
        needs_commit = True
        _logger.info(
            "DEBUG pull_from_repo: branch=import resolved=%d deleted=%d anki_wins=%d",
            len(resolved_nids),
            len(delete_from_git_nids),
            len(anki_wins_nids),
        )
    elif anki_checksums and git_checksums:
        matching = {
            int(nid) for nid, a_cs in anki_checksums.items() if a_cs == git_checksums.get(nid)
        }
        non_matching = {
            int(nid) for nid, a_cs in anki_checksums.items() if a_cs != git_checksums.get(nid)
        }
        _logger.info(
            "DEBUG pull_from_repo: branch=matching matching=%d non_matching=%d (first10=%s)",
            len(matching),
            len(non_matching),
            sorted(non_matching)[:10] if non_matching else [],
        )
        all_imported_nids = matching
        needs_commit = bool(all_imported_nids)
    else:
        all_imported_nids = set()
        needs_commit = False
        _logger.info(
            "DEBUG pull_from_repo: branch=none anki_cs=%s git_cs=%s",
            anki_checksums is not None,
            git_checksums is not None,
        )

    # If no notes were resolved but the repo still has dirty content files,
    # force a verification commit to baseline them.  This prevents a loop
    # where dirty files (e.g. unchecked notes or uncommitted notetypes)
    # are re-detected on every startup because no commit updates HEAD.
    if not needs_commit and repo:
        has_dirty_content = any(repo.is_dirty(path=p) for p in ("decks", "notetypes")) or any(
            f.startswith("decks/") or f.startswith("notetypes/") for f in repo.untracked_files
        )
        if has_dirty_content:
            all_imported_nids = set()
            needs_commit = True
            _logger.info("Forcing verification commit for dirty repo content")

    committed_checksums: dict[str, str] = {}
    if repo and needs_commit:
        parts = []
        if total_imported:
            parts.append(f"{total_imported} notes")
        if total_notetypes:
            parts.append(f"{total_notetypes} notetypes")
        if not parts:
            parts.append("cleanup")
        msg = f"Import {', '.join(parts)} from repo"
        repo.git.add(all=True)
        staged_count = len(repo.git.diff("--cached", "--name-status").splitlines())
        _logger.info("Verification commit: %d files staged", staged_count)

        committed = False
        try:
            repo.index.commit(msg)
            _logger.info("Verification commit created: %s", msg)
            committed = True
        except Exception:
            _logger.info("Verification commit skipped — nothing to commit (already up to date)")

        # Record git checksums for committed files so future delta
        # operations see them as baselined — regardless of how many
        # notes were actually imported into Anki.
        if committed:
            for nid in all_imported_nids:
                sn = str(nid)
                if sn in git_checksums:
                    committed_checksums[sn] = git_checksums[sn]
            _logger.info(
                "pull_from_repo: committed_checksums=%d entries (total_imported=%d)",
                len(committed_checksums),
                total_imported,
            )

    # Persist tracking metadata
    existing_checksums = meta.get("note_checksums", {})
    before_len = len(existing_checksums)
    existing_checksums.update(committed_checksums)

    # Fill in checksums for all remaining collection notes so that
    # future delta operations have a complete baseline for conflict detection.
    if col.db is not None:
        all_nids = col.db.list("SELECT id FROM notes WHERE id > 0")
        missing_nids = [nid for nid in all_nids if str(nid) not in existing_checksums]
        if missing_nids:
            from anki_git.engine.export_helpers import capture_single_note

            for i, nid in enumerate(missing_nids):
                if i % 1000 == 0 and i > 0:
                    _logger.info("Filling missing checksums: %d/%d", i, len(missing_nids))
                captured = capture_single_note(col, nid)
                if captured is not None:
                    serialized, _ = captured
                    existing_checksums[str(nid)] = content_hash(serialized)
            _logger.info(
                "Filled %d missing checksums (total=%d, added in import=%d)",
                len(missing_nids),
                len(existing_checksums),
                len(committed_checksums),
            )

    # Prune stale base checksums: remove entries for nids that no longer
    # exist in either Anki or git. This prevents unbounded bloat in meta.json
    # from deleted notes accumulating over time.
    live_nids = set(anki_checksums) | set(git_checksums) | {str(nid) for nid in resolved_nids}
    if live_nids:
        stale = len(existing_checksums) - sum(1 for k in existing_checksums if k in live_nids)
        if stale > 0:
            existing_checksums = {k: v for k, v in existing_checksums.items() if k in live_nids}
            _logger.info("Pruned %d stale checksums (remaining=%d)", stale, len(existing_checksums))

    meta["note_checksums"] = existing_checksums
    _logger.info(
        "pull_from_repo: note_checksums %d->%d (added %d)",
        before_len,
        len(existing_checksums),
        len(committed_checksums),
    )

    if repo:
        from contextlib import suppress

        with suppress(ValueError, Exception):
            meta["last_commit_sha"] = repo.head.commit.hexsha
    save_meta(repo_path, meta)

    return result


def import_from_repo(
    col: "Collection",
    repo_path: Path,
    nid_filter: set[int] | None = None,
    notes_lookup: dict[int, "Note"] | None = None,
    repo_notetypes: dict[str, "Notetype"] | None = None,
) -> ImportResult:
    """Apply repo state to an Anki collection.

    Must be called on Anki's main thread.
    If nid_filter is provided, only import notes whose nid is in the set.
    If notes_lookup / repo_notetypes are provided, skips filesystem scans.
    """
    result = ImportResult()

    try:
        col.db.execute("begin")
        import_helpers.import_notetypes(col, repo_path, result, repo_notetypes=repo_notetypes)
        import_helpers.import_notes(
            col, repo_path, result, nid_filter=nid_filter, notes_lookup=notes_lookup
        )
        col.db.execute("commit")
        _logger.info(
            "Import complete: %d notes, %d notetypes",
            result.notes_updated + result.notes_created,
            result.notetypes_updated + result.notetypes_created,
        )
    except Exception as e:
        col.db.execute("rollback")
        _logger.exception("Import failed")
        result.errors.append(str(e))

    return result
