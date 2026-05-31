"""Shared import helpers used by both importer.py and sync.py.

Extracted to eliminate code duplication between one-way import and two-way
sync. All functions operate on Anki collection objects (not aqt) so they
remain testable without the Anki Qt runtime.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from anki.collection import Collection
from anki.notes import NoteId

from anki_git.engine.constants import DECKS_DIR, NOTETYPES_DIR

if TYPE_CHECKING:
    from anki_git.engine.importer import ImportResult
    from anki_git.formats.notes_md import Note
    from anki_git.formats.notetype_yaml import Notetype

_logger = logging.getLogger("anki_git")


def compute_anki_checksums(col: Collection) -> dict[str, str]:
    """Compute checksums for Anki notes using the same Note.serialize()
    format as git checksums, so conflict detection works correctly.
    """
    from anki_git.engine.checksums import content_hash
    from anki_git.engine.export_helpers import capture_single_note

    checksums = {}
    db = col.db
    assert db is not None
    for row in db.list("SELECT id FROM notes WHERE id > 0"):
        nid = NoteId(row)
        captured = capture_single_note(col, nid)
        if captured is None:
            continue
        serialized, _ = captured
        checksums[str(nid)] = content_hash(serialized)
    return checksums


def compute_git_checksums(repo_path: Path) -> tuple[dict[str, str], dict[int, "Note"]]:
    from anki_git.engine.checksums import content_hash
    from anki_git.formats.notes_md import parse_notes_file

    checksums = {}
    notes: dict[int, Note] = {}
    decks_dir = repo_path / DECKS_DIR
    for notes_file in sorted(decks_dir.rglob("*.md")):
        for note_data in parse_notes_file(notes_file):
            notes[note_data.nid] = note_data
            checksums[str(note_data.nid)] = content_hash(note_data.serialize())
    return checksums, notes


def load_all_repo_notes(repo_path: Path) -> dict[int, "Note"]:
    """Build {nid: Note} lookup dict from all repo files.

    This is the O(n) alternative to scanning all files per-note (O(n*m)).
    """
    from anki_git.formats.notes_md import parse_notes_file

    notes: dict[int, Note] = {}

    decks_dir = repo_path / DECKS_DIR
    for notes_file in sorted(decks_dir.rglob("*.md")):
        for note_data in parse_notes_file(notes_file):
            notes[note_data.nid] = note_data
    return notes


def import_single_note(col: Collection, repo_path: Path, nid: int,
                       notes_lookup: dict[int, "Note"] | None = None) -> bool:
    """Import a single note from repo into Anki. Returns True on success.

    Optionally accepts a pre-built notes_lookup dict; if not provided,
    builds one (less efficient for batch imports).
    """
    if notes_lookup is None:
        notes_lookup = load_all_repo_notes(repo_path)

    note_data = notes_lookup.get(nid)
    if note_data is None:
        _logger.warning("Note %d not found in repo files", nid)
        return False

    try:
        existing = col.get_note(NoteId(nid))
    except Exception:
        _logger.debug("Note %d not found in Anki, will create", nid)
        existing = None

    if existing is not None:
        for key, value in note_data.fields.items():
            if key in existing:
                existing[key] = value
        existing.tags = note_data.tags
        try:
            col.update_note(existing)
            return True
        except Exception as e:
            _logger.warning("Failed to update note %d: %s", nid, e)
            return False
    else:
        model_id = col.models.id_for_name(note_data.notetype)
        if model_id is None:
            _logger.warning("Notetype '%s' not found for nid %d",
                            note_data.notetype, nid)
            return False
        new_note = col.new_note(model_id)  # pyright: ignore[reportArgumentType]
        for key, value in note_data.fields.items():
            if key in new_note:
                new_note[key] = value
        new_note.tags = note_data.tags
        try:
            deck_id = col.decks.id(note_data.deck)
            if deck_id is not None:
                col.add_note(new_note, deck_id)
            else:
                _logger.warning("Deck '%s' not found for nid %d",
                                note_data.deck, nid)
                return False
            return True
        except Exception as e:
            _logger.warning("Failed to create note %d: %s", nid, e)
            return False


def delete_note_from_anki(col: Collection, nid: int) -> bool:
    """Remove a note from Anki by nid. Returns True on success."""
    try:
        col.get_note(NoteId(nid))
        col.remove_notes([NoteId(nid)])
        return True
    except Exception:
        _logger.warning("Failed to delete note %d from Anki", nid)
        return False


def delete_note_from_repo(repo_path: Path, nid: int) -> bool:
    """Remove a note's file from the repo. Returns True if deleted."""
    decks_dir = repo_path / DECKS_DIR
    for f in decks_dir.rglob(f"{nid}.md"):
        try:
            f.unlink()
            return True
        except Exception as e:
            _logger.warning("Failed to delete repo file %s: %s", f, e)
            return False
    return False


def import_notetype(col: Collection, repo_path: Path, nt_name: str) -> bool:
    """Import a single notetype from repo into Anki."""
    from anki_git.formats.notetype_yaml import read_all_notetypes

    notetypes_dir = repo_path / NOTETYPES_DIR
    repo_nt = read_all_notetypes(notetypes_dir)
    nt = repo_nt.get(nt_name)
    if nt is None:
        return False

    existing = col.models.by_name(nt_name)
    if existing:
        existing["flds"] = [
            {"name": f.name, "ord": f.ord, "font": f.font,
             "size": f.size, "sticky": f.sticky, "rtl": f.rtl}
            for f in nt.fields
        ]
        existing["tmpls"] = [
            {"name": t.name, "ord": t.ord, "qfmt": t.qfmt,
             "afmt": t.afmt, "bqfmt": t.bqfmt, "bafmt": t.bafmt}
            for t in nt.templates
        ]
        existing["css"] = nt.css
        col.models.save(existing)
    else:
        new_nt = col.models.new(nt_name)
        for f in nt.fields:
            field = col.models.new_field(f.name)
            field["rtl"] = f.rtl
            col.models.add_field(new_nt, field)
        for t in nt.templates:
            tmpl = col.models.new_template(t.name)
            tmpl["qfmt"] = t.qfmt
            tmpl["afmt"] = t.afmt
            tmpl["bqfmt"] = t.bqfmt
            tmpl["bafmt"] = t.bafmt
            col.models.add_template(new_nt, tmpl)
        new_nt["css"] = nt.css
        col.models.add_dict(new_nt)
    return True


def import_notetypes(col: Collection, repo_path: Path, result: "ImportResult",
                     repo_notetypes: dict[str, "Notetype"] | None = None) -> None:
    """Import all notetypes from repo into Anki, updating result in-place.

    Only counts a notetype as updated if its content actually differs from
    the collection's current version (compares fields, templates, and CSS).
    If repo_notetypes is provided, skips the filesystem scan.
    """
    from anki_git.formats.notetype_yaml import Notetype, read_all_notetypes

    if repo_notetypes is None:
        notetypes_dir = repo_path / NOTETYPES_DIR
        repo_notetypes = read_all_notetypes(notetypes_dir)

    for name in sorted(repo_notetypes.keys()):
        repo_nt = repo_notetypes[name]
        existing_dict = col.models.by_name(name)

        if existing_dict:
            existing_nt = Notetype.from_anki_dict(existing_dict)
            if existing_nt == repo_nt:
                continue

        if existing_dict:
            existing_dict["flds"] = [
                {"name": f.name, "ord": f.ord, "font": f.font,
                 "size": f.size, "sticky": f.sticky, "rtl": f.rtl}
                for f in repo_nt.fields
            ]
            existing_dict["tmpls"] = [
                {"name": t.name, "ord": t.ord, "qfmt": t.qfmt,
                 "afmt": t.afmt, "bqfmt": t.bqfmt, "bafmt": t.bafmt}
                for t in repo_nt.templates
            ]
            existing_dict["css"] = repo_nt.css
            col.models.save(existing_dict)
            result.notetypes_updated += 1
        else:
            new_nt = col.models.new(name)
            for f in repo_nt.fields:
                field = col.models.new_field(f.name)
                field["rtl"] = f.rtl
                col.models.add_field(new_nt, field)
            for t in repo_nt.templates:
                tmpl = col.models.new_template(t.name)
                tmpl["qfmt"] = t.qfmt
                tmpl["afmt"] = t.afmt
                tmpl["bqfmt"] = t.bqfmt
                tmpl["bafmt"] = t.bafmt
                col.models.add_template(new_nt, tmpl)
            new_nt["css"] = repo_nt.css
            col.models.add_dict(new_nt)
            result.notetypes_created += 1


def import_notes(col: Collection, repo_path: Path, result: "ImportResult",
                 nid_filter: set[int] | None = None,
                 notes_lookup: dict[int, "Note"] | None = None) -> None:
    """Import notes from repo into Anki.

    If nid_filter is provided, only import notes whose nid is in the set.
    If notes_lookup is provided, uses it instead of scanning the filesystem.
    Updates result in-place with notes_updated/notes_created/errors/warnings.
    """
    from anki_git.formats.notes_md import Note, parse_notes_file

    if notes_lookup is not None:
        note_iter: list[Note] = list(notes_lookup.values())
    else:
        decks_dir = repo_path / DECKS_DIR
        note_iter = []
        for notes_file in sorted(decks_dir.rglob("*.md")):
            note_iter.extend(parse_notes_file(notes_file))

    for note_data in note_iter:
        if nid_filter is not None and note_data.nid not in nid_filter:
            continue

        try:
            existing = col.get_note(NoteId(note_data.nid))
        except Exception:
            _logger.debug("Note %d not found in Anki during batch import", note_data.nid)
            existing = None

        if existing is not None:
            for key, value in note_data.fields.items():
                if key in existing:
                    existing[key] = value
            existing.tags = note_data.tags
            try:
                col.update_note(existing)
                result.notes_updated += 1
            except Exception as e:
                _logger.warning("Failed to update note %d: %s", note_data.nid, e)
                result.warnings.append(
                    f"Failed to update note {note_data.nid}: {e}"
                )
        else:
            model_id = col.models.id_for_name(note_data.notetype)
            if model_id is None:
                _logger.warning(
                    "Notetype '%s' not found for nid %d",
                    note_data.notetype, note_data.nid,
                )
                result.warnings.append(
                    f"Notetype '{note_data.notetype}' not found "
                    f"for nid {note_data.nid}"
                )
                continue
            new_note = col.new_note(model_id)  # pyright: ignore[reportArgumentType]
            for key, value in note_data.fields.items():
                if key in new_note:
                    new_note[key] = value
            new_note.tags = note_data.tags
            try:
                deck_id = col.decks.id(note_data.deck)
                if deck_id is not None:
                    col.add_note(new_note, deck_id)
                    result.notes_created += 1
                else:
                    result.errors.append(
                        f"Deck '{note_data.deck}' not found"
                    )
            except Exception as e:
                result.errors.append(
                    f"Failed to create note: {e}"
                )


def cleanup_stale_repo_notes(col: Collection, repo_path: Path,
                              anki_nids: set[int] | None = None) -> int:
    """Remove repo note files for notes that no longer exist in Anki.

    Optionally accepts a pre-computed set of Anki nids to avoid a DB query.
    Returns the number of files cleaned up.
    """
    if anki_nids is None:
        db = col.db
        assert db is not None
        anki_nids = set(db.list("SELECT id FROM notes WHERE id > 0"))
    decks_dir = repo_path / DECKS_DIR
    cleaned = 0
    for notes_file in sorted(decks_dir.rglob("*.md")):
        from anki_git.formats.notes_md import parse_notes_file
        for note_data in parse_notes_file(notes_file):
            if note_data.nid not in anki_nids:
                try:
                    notes_file.unlink()
                    cleaned += 1
                except Exception as e:
                    _logger.warning(
                        "Failed to delete stale note file %s: %s",
                        notes_file, e,
                    )
    return cleaned
