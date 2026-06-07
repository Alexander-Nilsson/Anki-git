import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from anki.collection import Collection

if TYPE_CHECKING:
    from anki_git.engine.diff import ExportDiffData

from anki_git.engine import export_helpers
from anki_git.engine.checksums import content_hash, load_meta, save_meta
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


class ExportResult:
    def __init__(
        self,
        notes_changed: int = 0,
        notetypes_changed: int = 0,
        notes_deleted_from_repo: int = 0,
        changed_decks: dict[str, int] | None = None,
        changed_notetypes: list[str] | None = None,
        error: str = "",
        duration_seconds: float = 0.0,
        commit_count: int = 0,
    ):
        self.notes_changed = notes_changed
        self.notetypes_changed = notetypes_changed
        self.notes_deleted_from_repo = notes_deleted_from_repo
        self.changed_decks = changed_decks or {}
        self.changed_notetypes = changed_notetypes or []
        self.error = error
        self.duration_seconds = duration_seconds
        self.commit_count = commit_count


@dataclass
class CapturedExport:
    """Data read from the Anki collection during the capture phase.

    All note data is serialized to strings so the write phase can run
    without access to the collection object.
    """
    notetypes: dict[str, Notetype]
    changed_notetype_names: list[str]
    nids: set[int]
    all_nids: set[int]
    note_entries: list[tuple[int, str, Note]]
    note_checksums: dict[str, str]
    collection_path: str
    last_max_mod: int
    last_note_count: int


def _note_file_path(repo_path: Path, note: Note) -> Path:
    deck_parts = note.deck.split("::")
    return repo_path / DECKS_DIR / Path(*deck_parts) / f"{note.nid}.md"


def capture_export_data(
    col: Collection,
    repo_path: Path,
    quick: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> CapturedExport:
    """Phase 1: read all export data from the collection.

    Must run while the collection is open.  Can raise RuntimeError if
    the collection is closed mid-operation.
    """
    if progress_callback:
        progress_callback("Loading metadata...")
    meta = load_meta(repo_path)

    notetypes_dir = repo_path / NOTETYPES_DIR

    if progress_callback:
        progress_callback("Exporting notetypes...")
    old_notetypes = read_all_notetypes(notetypes_dir)

    current_notetypes: dict[str, Notetype] = {}
    for nt_dict in col.models.all():
        nt = Notetype.from_anki_dict(nt_dict)
        current_notetypes[nt.name] = nt

    changed_notetype_names: list[str] = []
    for name, nt in current_notetypes.items():
        if nt != old_notetypes.get(name):
            changed_notetype_names.append(name)

    meta_checksums = meta.get("note_checksums", {})

    db = col.db
    if db is None:
        raise RuntimeError("Collection closed, aborting export")

    if quick and meta.get("last_max_mod"):
        if progress_callback:
            progress_callback("Checking for changed notes...")
        last_max_mod = meta["last_max_mod"]
        changed_nids = set(
            db.list("SELECT id FROM notes WHERE mod > ? AND id > 0", last_max_mod)
        )
        all_nids = set(db.list("SELECT id FROM notes WHERE id > 0"))
        nids = changed_nids & all_nids
        total = len(nids)
        _logger.info("Delta capture: %d notes changed since mod %s", total, last_max_mod)
    else:
        if progress_callback:
            progress_callback("Reading notes...")
        nids = set(db.list("SELECT id FROM notes WHERE id > 0"))
        all_nids = nids
        total = len(nids)

    note_entries: list[tuple[int, str, Note]] = []
    note_checksums = dict(meta_checksums)

    for i, nid in enumerate(sorted(nids)):
        if col.db is None:
            _logger.warning("Collection closed mid-capture, aborting after %d/%d", i, total)
            break
        if progress_callback and i % 20 == 0:
            progress_callback(f"Reading notes... {i}/{total}")
        captured = export_helpers.capture_single_note(col, nid)
        if captured is None:
            continue
        serialized, note = captured
        checksum = content_hash(serialized)
        note_checksums[str(nid)] = checksum
        note_entries.append((nid, serialized, note))

    # Remove checksums for notes deleted from Anki
    for nid_str in list(note_checksums):
        if int(nid_str) not in all_nids:
            del note_checksums[nid_str]

    last_note_count = db.scalar("SELECT COUNT(*) FROM notes WHERE id > 0") or 0
    last_max_mod = db.scalar("SELECT MAX(mod) FROM notes WHERE id > 0") or 0

    return CapturedExport(
        notetypes=current_notetypes,
        changed_notetype_names=changed_notetype_names,
        nids=nids,
        all_nids=all_nids,
        note_entries=note_entries,
        note_checksums=note_checksums,
        collection_path=str(col.path),
        last_max_mod=last_max_mod,
        last_note_count=last_note_count,
    )


def write_export_data(
    repo_path: Path,
    captured: CapturedExport,
    remote_url: str = "",
    progress_callback: Callable[[str], None] | None = None,
) -> ExportResult:
    """Phase 2: write captured export data to disk and git.

    Can run in a background thread — does not touch the Anki collection.
    """
    _start = time.perf_counter()
    result = ExportResult()

    if progress_callback:
        progress_callback("Initializing repository...")
    repo = get_or_init_repo(repo_path)
    ensure_gitignore(repo_path)

    if progress_callback:
        progress_callback("Loading metadata...")
    meta = load_meta(repo_path)
    meta_checksums = meta.get("note_checksums", {})

    notetypes_dir = repo_path / NOTETYPES_DIR
    changed_files: set[str] = set()

    if progress_callback:
        progress_callback("Exporting notetypes...")
    for name in captured.changed_notetype_names:
        nt = captured.notetypes[name]
        write_notetype(notetypes_dir, nt)
        for p in notetype_paths(notetypes_dir, name):
            changed_files.add(str(p.relative_to(repo_path)))

    notes_changed = 0
    deck_counts: dict[str, int] = {}
    for nid, serialized, note in captured.note_entries:
        checksum = captured.note_checksums[str(nid)]
        old_checksum = meta_checksums.get(str(nid))
        if old_checksum != checksum:
            notes_changed += 1
            file_path = _note_file_path(repo_path, note)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(serialized, encoding="utf-8")
            changed_files.add(str(file_path.relative_to(repo_path)))
        deck_counts[note.deck] = deck_counts.get(note.deck, 0) + 1

    if progress_callback:
        progress_callback("Cleaning up stale files...")
    cleaned = 0
    for notes_file in sorted((repo_path / DECKS_DIR).rglob("*.md")):
        try:
            nid = int(notes_file.stem)
        except ValueError:
            continue
        if nid not in captured.all_nids:
            try:
                notes_file.unlink()
                cleaned += 1
                changed_files.add(str(notes_file.relative_to(repo_path)))
            except Exception as e:
                _logger.warning("Failed to delete stale note file %s: %s", notes_file, e)
    if cleaned > 0:
        result.notes_deleted_from_repo = cleaned

    result.notes_changed = notes_changed
    result.notetypes_changed = len(captured.changed_notetype_names)
    result.changed_notetypes = list(captured.changed_notetype_names)
    result.changed_decks = deck_counts

    if notes_changed > 0 or result.notetypes_changed > 0 or cleaned > 0:
        if progress_callback:
            progress_callback("Committing changes...")
        stage_files(repo, list(changed_files))
        create_snapshot_commit(repo, list(changed_files))
        if remote_url:
            if progress_callback:
                progress_callback("Pushing to remote...")
            push_ok, push_error = push_to_remote(repo, remote_url)
            if not push_ok:
                result.error = push_error
                _logger.error("Export commit succeeded but push failed: %s", push_error)

    meta["last_export_time"] = int(time.time())
    meta["note_checksums"] = captured.note_checksums
    meta["collection_path"] = captured.collection_path
    meta["last_note_count"] = captured.last_note_count
    meta["last_max_mod"] = captured.last_max_mod
    try:
        meta["last_commit_sha"] = repo.head.commit.hexsha
    except (ValueError, Exception):
        meta["last_commit_sha"] = ""
    save_meta(repo_path, meta)

    result.commit_count = get_commit_count(repo)
    result.duration_seconds = time.perf_counter() - _start
    _logger.info(
        "Snapshot took %.2fs: %d notes changed, %d notetypes changed",
        result.duration_seconds,
        notes_changed,
        result.notetypes_changed,
    )
    if progress_callback:
        progress_callback(
            f"Snapshot complete ({result.duration_seconds:.1f}s)"
        )

    return result


def export_collection(
    col: Collection,
    repo_path: Path,
    remote_url: str = "",
    progress_callback: Callable[[str], None] | None = None,
    quick: bool = False,
    export_data: Optional["ExportDiffData"] = None,
) -> ExportResult:
    """Full export: capture data from collection then write to disk+git.

    When export_data is provided (from a prior compute_export_diff_delta()),
    skips the note/notetype re-scan and reuses the pre-computed data.
    A few scalar queries (all_nids, last_note_count, last_max_mod) are
    still issued for freshness.

    Runs both phases synchronously with progress.  For non-blocking close
    behaviour use capture_export_data() directly followed by a background
    call to write_export_data().
    """
    try:
        if export_data is not None:
            db = col.db
            if db is None:
                raise RuntimeError("Collection closed, aborting export")
            all_nids = set(db.list("SELECT id FROM notes WHERE id > 0"))
            last_note_count = db.scalar("SELECT COUNT(*) FROM notes WHERE id > 0") or 0
            last_max_mod = db.scalar("SELECT MAX(mod) FROM notes WHERE id > 0") or 0

            note_checksums = dict(export_data.note_checksums)
            for nid_str in list(note_checksums):
                if int(nid_str) not in all_nids:
                    del note_checksums[nid_str]

            captured = CapturedExport(
                notetypes=export_data.notetypes,
                changed_notetype_names=export_data.changed_notetype_names,
                nids=set(export_data.note_entries.keys()),
                all_nids=all_nids,
                note_entries=list(export_data.note_entries.values()),
                note_checksums=note_checksums,
                collection_path=export_data.collection_path or str(col.path),
                last_max_mod=last_max_mod,
                last_note_count=last_note_count,
            )
            return write_export_data(
                repo_path, captured,
                remote_url=remote_url,
                progress_callback=progress_callback,
            )

        captured = capture_export_data(
            col, repo_path,
            quick=quick,
            progress_callback=progress_callback,
        )
        return write_export_data(
            repo_path, captured,
            remote_url=remote_url,
            progress_callback=progress_callback,
        )
    except Exception as e:
        _logger.exception("Export failed with unexpected error")
        return ExportResult(error=str(e))
