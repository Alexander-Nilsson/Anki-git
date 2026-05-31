"""Two-way sync engine: merge changes between Anki collection and Git repo.

Flow:
  1. Compute checksums for Anki notes, Git notes, and base (meta.json)
  2. Run three-way conflict detection
  3. Auto-resolve based on sync_mode or prompt user for true conflicts
  4. Apply resolved changes in both directions
  5. Update meta.json and commit
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from anki.collection import Collection
from anki.notes import NoteId

from anki_git.config import SyncMode
from anki_git.engine import export_helpers, import_helpers
from anki_git.engine.checksums import content_hash, load_meta, save_meta
from anki_git.engine.conflict import (
    ConflictType,
    merge_notetypes,
    process_conflicts,
)
from anki_git.engine.constants import DECKS_DIR, NOTETYPES_DIR
from anki_git.engine.git_ops import (
    create_snapshot_commit,
    ensure_gitignore,
    get_commit_count,
    get_or_init_repo,
    push_to_remote,
    stage_files,
)
from anki_git.formats.notes_md import Note
from anki_git.formats.notetype_yaml import (
    Notetype,
    notetype_paths,
    read_all_notetypes,
    write_notetype,
)

_logger = logging.getLogger("anki_git")


@dataclass
class SyncPreview:
    notes_to_export: int = 0
    notes_to_import: int = 0
    notes_to_delete_from_git: int = 0
    notes_to_delete_from_anki: int = 0
    conflicts_unresolved: int = 0
    notetypes_to_sync: int = 0

    @property
    def has_changes(self) -> bool:
        return (
            self.notes_to_export > 0
            or self.notes_to_import > 0
            or self.notes_to_delete_from_git > 0
            or self.notes_to_delete_from_anki > 0
            or self.notetypes_to_sync > 0
        )


@dataclass
class SyncResult:
    notes_exported: int = 0
    notes_imported: int = 0
    notetypes_exported: int = 0
    notetypes_imported: int = 0
    notes_deleted_from_git: int = 0
    notes_deleted_from_anki: int = 0
    conflicts_resolved: int = 0
    conflicts_unresolved: int = 0
    error: str = ""
    duration_seconds: float = 0.0
    commit_count: int = 0



def sync_collection(
    col: Collection,
    repo_path: Path,
    sync_mode: str = SyncMode.ALWAYS_ASK,
    conflict_callback: Callable | None = None,
    remote_url: str = "",
    progress_callback: Callable[[str], None] | None = None,
    preview_callback: Callable | None = None,
) -> SyncResult:
    _start = time.perf_counter()
    result = SyncResult()

    if progress_callback:
        progress_callback("Initializing repository...")
    repo = get_or_init_repo(repo_path)
    ensure_gitignore(repo_path)

    if progress_callback:
        progress_callback("Loading metadata...")
    meta = load_meta(repo_path)
    base_checksums = meta.get("note_checksums", {})

    if progress_callback:
        progress_callback("Computing Anki checksums...")
    anki_checksums = import_helpers.compute_anki_checksums(col)

    if progress_callback:
        progress_callback("Computing repo checksums...")
    git_checksums, git_notes_lookup = import_helpers.compute_git_checksums(repo_path)

    if progress_callback:
        progress_callback("Detecting conflicts...")
    report = process_conflicts(
        base_checksums, anki_checksums, git_checksums,
        sync_mode, col, repo_path, notes_lookup=git_notes_lookup,
    )

    if report.has_conflicts and conflict_callback and sync_mode == SyncMode.ALWAYS_ASK:
        unresolved = [c for c in report.conflicts if not c.resolved]
        if unresolved:
            if progress_callback:
                progress_callback("Waiting for conflict resolution...")
            report = conflict_callback(report)

    changed_files: set[str] = set()
    notes_to_export: set[int] = set()
    notes_to_import_nids: set[int] = set()
    delete_from_anki_nids: set[int] = set()
    delete_from_git_nids: set[int] = set()

    if progress_callback:
        progress_callback("Processing changes...")

    notetypes_dir = repo_path / NOTETYPES_DIR

    for c in report.conflicts:
        if not c.resolved:
            result.conflicts_unresolved += 1
            continue
        result.conflicts_resolved += 1

        if c.resolution == "anki" and c.conflict_type != ConflictType.DELETE_FROM_GIT:
            notes_to_export.add(c.nid)
        elif c.resolution == "git" and c.conflict_type != ConflictType.DELETE_FROM_ANKI:
            notes_to_import_nids.add(c.nid)

        if c.conflict_type == ConflictType.DELETE_FROM_ANKI:
            delete_from_anki_nids.add(c.nid)
        if c.conflict_type == ConflictType.DELETE_FROM_GIT:
            delete_from_git_nids.add(c.nid)

    # Show preview before applying anything
    if preview_callback:
        anki_notetypes_preview: dict[str, Notetype] = {}
        for nt_dict in col.models.all():
            nt = Notetype.from_anki_dict(nt_dict)
            anki_notetypes_preview[nt.name] = nt
        repo_notetypes_preview = read_all_notetypes(repo_path / NOTETYPES_DIR)
        nt_to_sync = 0
        for name in set(anki_notetypes_preview) | set(repo_notetypes_preview):
            anki_nt = anki_notetypes_preview.get(name)
            git_nt = repo_notetypes_preview.get(name)
            if anki_nt and git_nt:
                if anki_nt != git_nt:
                    nt_to_sync += 1
            elif anki_nt or git_nt:
                nt_to_sync += 1

        preview = SyncPreview(
            notes_to_export=len(notes_to_export),
            notes_to_import=len(notes_to_import_nids),
            notes_to_delete_from_git=len(delete_from_git_nids),
            notes_to_delete_from_anki=len(delete_from_anki_nids),
            conflicts_unresolved=result.conflicts_unresolved,
            notetypes_to_sync=nt_to_sync,
        )
        if preview.has_changes and not preview_callback(preview):
            _logger.info("Sync cancelled by user preview")
            result.duration_seconds = time.perf_counter() - _start
            return result

    # Use notes lookup from checksum phase for O(n) import
    notes_lookup = git_notes_lookup

    # Apply changes: import (repo -> anki)
    if progress_callback:
        progress_callback("Importing changes from repo...")

    def _do_import():
        for nid in notes_to_import_nids:
            if import_helpers.import_single_note(
                col, repo_path, nid, notes_lookup=notes_lookup,
            ):
                result.notes_imported += 1

        for nid in delete_from_anki_nids:
            if import_helpers.delete_note_from_anki(col, nid):
                result.notes_deleted_from_anki += 1

    db = col.db
    assert db is not None
    try:
        db.transact(_do_import)
    except Exception as e:
        _logger.exception("Failed to import notes from repo")
        result.error = str(e)
        return result

    # Apply changes: export (anki -> repo)
    if progress_callback:
        progress_callback("Exporting changes to repo...")

    for nid in notes_to_export:
        exported = export_helpers.export_single_note(col, repo_path, nid)
        if exported:
            file_path = exported[0]
            result.notes_exported += 1
            changed_files.add(str(file_path.relative_to(repo_path)))

    for nid in delete_from_git_nids:
        if import_helpers.delete_note_from_repo(repo_path, nid):
            result.notes_deleted_from_git += 1
            md_path = repo_path / DECKS_DIR / f"{nid}.md"
            if md_path.exists():
                changed_files.add(str(md_path.relative_to(repo_path)))

    # Incrementally update Anki checksums instead of full recompute
    new_anki_checksums = anki_checksums.copy()
    for nid in delete_from_anki_nids:
        new_anki_checksums.pop(str(nid), None)
    for nid in notes_to_import_nids:
        try:
            note_obj = col.get_note(NoteId(nid))
            nt_dict = note_obj.note_type()
            if nt_dict is None:
                continue
            nt_name = nt_dict["name"]
            cards = note_obj.cards()
            deck_name = col.decks.name(cards[0].did) if cards else "Default"
            note = Note(
                nid=nid, notetype=nt_name, tags=list(note_obj.tags),
                deck=deck_name, fields=dict(note_obj.items()),
            )
            new_anki_checksums[str(nid)] = content_hash(note.serialize())
        except Exception:
            new_anki_checksums.pop(str(nid), None)

    # Detect notetype changes between Anki and repo
    anki_notetypes: dict[str, Notetype] = {}
    for nt_dict in col.models.all():
        nt = Notetype.from_anki_dict(nt_dict)
        anki_notetypes[nt.name] = nt

    if progress_callback:
        progress_callback("Syncing notetypes...")

    repo_notetypes = read_all_notetypes(notetypes_dir)
    all_nt_names = set(anki_notetypes) | set(repo_notetypes)

    for name in sorted(all_nt_names):
        anki_nt = anki_notetypes.get(name)
        git_nt = repo_notetypes.get(name)

        if anki_nt and git_nt:
            if anki_nt == git_nt:
                continue
            merged, nt_conflicts = merge_notetypes(anki_nt, git_nt, sync_mode)
            write_notetype(notetypes_dir, merged)
            for p in notetype_paths(notetypes_dir, name):
                changed_files.add(str(p.relative_to(repo_path)))
            import_helpers.import_notetype(col, repo_path, name)
            for c in nt_conflicts:
                if c.resolved:
                    result.conflicts_resolved += 1
                else:
                    result.conflicts_unresolved += 1
            result.notetypes_exported += 1
            result.notetypes_imported += 1
        elif anki_nt:
            write_notetype(notetypes_dir, anki_nt)
            for p in notetype_paths(notetypes_dir, name):
                changed_files.add(str(p.relative_to(repo_path)))
            result.notetypes_exported += 1
        elif git_nt:
            import_helpers.import_notetype(col, repo_path, name)
            result.notetypes_imported += 1

    notes_changed = result.notes_exported + result.notes_imported
    notetypes_changed = result.notetypes_exported + result.notetypes_imported

    # Always store tracking data for quick_has_changes()
    meta["last_note_count"] = db.scalar("SELECT COUNT(*) FROM notes WHERE id > 0") or 0
    meta["last_max_mod"] = db.scalar("SELECT MAX(mod) FROM notes WHERE id > 0") or 0
    try:
        meta["last_commit_sha"] = str(repo.head.commit)
    except (ValueError, Exception):
        meta["last_commit_sha"] = ""

    if notes_changed > 0 or notetypes_changed > 0:
        if progress_callback:
            progress_callback("Committing changes...")
        meta["last_export_time"] = int(time.time())
        meta["note_checksums"] = new_anki_checksums
        meta["collection_path"] = str(col.path)

        save_meta(repo_path, meta)

        stage_files(repo, list(changed_files))
        create_snapshot_commit(repo, list(changed_files))

        if remote_url:
            if progress_callback:
                progress_callback("Pushing to remote...")
            push_to_remote(repo, remote_url)
    else:
        save_meta(repo_path, meta)

    result.commit_count = get_commit_count(repo)
    result.duration_seconds = time.perf_counter() - _start
    _logger.info(
        "Sync took %.2fs: %d notes exported, %d notes imported, "
        "%d notetypes exported, %d notetypes imported",
        result.duration_seconds,
        result.notes_exported,
        result.notes_imported,
        result.notetypes_exported,
        result.notetypes_imported,
    )
    if progress_callback:
        progress_callback(
            f"Sync complete ({result.duration_seconds:.1f}s)"
        )

    return result
