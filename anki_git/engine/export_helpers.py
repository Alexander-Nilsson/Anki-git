"""Shared export helpers used by both exporter.py and sync.py.

Provides a single-note export routine so the note-walking + serialization
logic lives in one place instead of being duplicated in exporter.py and
sync.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from anki.collection import Collection
from anki.notes import NoteId

from .constants import DECKS_DIR

if TYPE_CHECKING:
    from ..formats.notes_md import Note

_logger = logging.getLogger("anki_git")


def capture_single_note(col: Collection, nid: int) -> tuple[str, Note] | None:
    """Read and serialize a single note from the collection, without writing to disk.

    Returns (serialized_content, Note) on success, None on failure.
    The caller is responsible for checksum comparison and file writing.
    Raises RuntimeError if the collection is closed.
    """
    from ..formats.notes_md import Note

    if col.db is None:
        raise RuntimeError("Collection closed, aborting export")

    try:
        note_obj = col.get_note(NoteId(nid))
    except RuntimeError:
        raise
    except Exception:
        _logger.warning("Failed to get note %d", nid)
        return None

    nt_dict = note_obj.note_type()
    if nt_dict is None:
        _logger.warning("Note %d has no notetype, skipping", nid)
        return None

    try:
        cards = note_obj.cards()
        if not cards:
            _logger.debug("Note %d has no cards, skipping", nid)
            return None
        deck_name = col.decks.name(cards[0].did)
    except Exception as e:
        _logger.warning("Failed to get deck for note %d: %s", nid, e)
        return None

    fields = dict(note_obj.items())
    note = Note(
        nid=nid,
        notetype=nt_dict["name"],
        tags=list(note_obj.tags),
        deck=deck_name,
        fields=fields,
    )
    serialized = note.serialize()
    return serialized, note


def export_single_note(col: Collection, repo_path: Path, nid: int) -> tuple[Path, str, Note] | None:
    """Export a single note from Anki into repo files.

    Returns (file_path, serialized_content, Note) on success, None on failure.
    The caller is responsible for checksum comparison if needed.
    """
    from ..formats.notes_md import write_note_file

    captured = capture_single_note(col, nid)
    if captured is None:
        return None
    serialized, note = captured
    deck_path_parts = note.deck.split("::")
    note_dir = repo_path / DECKS_DIR / Path(*deck_path_parts)
    path = write_note_file(note_dir, note, content=serialized)
    return path, serialized, note
