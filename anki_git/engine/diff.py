"""Diff engine: compute field-level diffs between note/notetype states."""

import difflib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anki.collection import Collection

from anki_git.engine.constants import DECKS_DIR, NOTETYPES_DIR
from anki_git.formats.notes_md import Note, parse_notes_file
from anki_git.formats.notetype_yaml import Notetype

_logger = logging.getLogger("anki_git")


@dataclass
class FieldDiff:
    field_name: str
    old_value: str
    new_value: str
    diff_lines: list[str] = field(default_factory=list)


@dataclass
class NoteDiff:
    nid: int
    deck: str
    notetype: str
    change_type: str  # "modified", "added", "deleted"
    field_diffs: list[FieldDiff] = field(default_factory=list)
    old_tags: list[str] = field(default_factory=list)
    new_tags: list[str] = field(default_factory=list)
    tags_changed: bool = False
    old_deck: str | None = None
    old_notetype: str | None = None

    @property
    def deck_changed(self) -> bool:
        return self.old_deck is not None and self.old_deck != self.deck

    @property
    def notetype_changed(self) -> bool:
        return self.old_notetype is not None and self.old_notetype != self.notetype

    @property
    def added_lines(self) -> int:
        return sum(
            1 for fd in self.field_diffs
            for line in fd.diff_lines
            if line.startswith("+") and not line.startswith("+++")
        )

    @property
    def deleted_lines(self) -> int:
        return sum(
            1 for fd in self.field_diffs
            for line in fd.diff_lines
            if line.startswith("-") and not line.startswith("---")
        )


@dataclass
class ComponentChange:
    component_type: str  # "field", "template", "css"
    name: str
    status: str  # "added", "removed", "modified"
    old_value: str = ""
    new_value: str = ""


@dataclass
class NotetypeDiff:
    name: str
    change_type: str  # "modified", "added", "deleted"
    component_changes: list[ComponentChange] = field(default_factory=list)
    fields_diff: str = ""
    css_diff: str = ""


@dataclass
class DiffReport:
    note_diffs: list[NoteDiff] = field(default_factory=list)
    notetype_diffs: list[NotetypeDiff] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.note_diffs) + len(self.notetype_diffs)

    @property
    def has_changes(self) -> bool:
        return self.total_changes > 0


@dataclass
class ImportDiffData:
    """Combines a display DiffReport with raw parsed data for the import phase.

    The raw data (anki_notes, repo_notes, checksums) is computed once during
    the diff phase and passed to pull_from_repo() to avoid re-scanning.
    """
    report: DiffReport
    anki_notes: dict[int, Note] = field(default_factory=dict)
    repo_notes: dict[int, Note] = field(default_factory=dict)
    repo_notetypes: dict[str, Notetype] = field(default_factory=dict)
    anki_checksums: dict[str, str] = field(default_factory=dict)
    git_checksums: dict[str, str] = field(default_factory=dict)


@dataclass
class ExportDiffData:
    """Combines a display DiffReport with serialized note data for the export phase.

    The serialized data (note_entries, checksums) is computed once during
    the diff phase and passed to export_collection() to avoid re-scanning.
    """
    report: DiffReport
    note_entries: dict[int, tuple[int, str, "Note"]] = field(default_factory=dict)
    note_checksums: dict[str, str] = field(default_factory=dict)
    all_nids: set[int] = field(default_factory=set)
    changed_notetype_names: list[str] = field(default_factory=list)
    notetypes: dict[str, Notetype] = field(default_factory=dict)
    collection_path: str = ""
    last_max_mod: int = 0
    last_note_count: int = 0


def _unified_diff(old: str, new: str, name: str) -> list[str]:
    return list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=name,
            tofile=name,
            lineterm="",
        )
    )


def compute_note_diff(old_note: Note | None, new_note: Note | None) -> NoteDiff:
    if old_note is None:
        assert new_note is not None
        return NoteDiff(
            nid=new_note.nid,
            deck=new_note.deck,
            notetype=new_note.notetype,
            change_type="added",
            field_diffs=[
                FieldDiff(
                    field_name=f,
                    old_value="",
                    new_value=v,
                    diff_lines=_unified_diff("", v, f),
                )
                for f, v in new_note.fields.items()
            ],
            new_tags=new_note.tags,
        )

    if new_note is None:
        assert old_note is not None
        return NoteDiff(
            nid=old_note.nid,
            deck=old_note.deck,
            notetype=old_note.notetype,
            change_type="deleted",
            field_diffs=[
                FieldDiff(
                    field_name=f,
                    old_value=v,
                    new_value="",
                    diff_lines=_unified_diff(v, "", f),
                )
                for f, v in old_note.fields.items()
            ],
            old_tags=old_note.tags,
        )

    field_diffs: list[FieldDiff] = []
    all_field_names = list(dict.fromkeys(list(old_note.fields.keys()) + list(new_note.fields.keys())))

    for fname in all_field_names:
        old_val = old_note.fields.get(fname, "")
        new_val = new_note.fields.get(fname, "")
        if old_val != new_val:
            field_diffs.append(
                FieldDiff(
                    field_name=fname,
                    old_value=old_val,
                    new_value=new_val,
                    diff_lines=_unified_diff(old_val, new_val, fname),
                )
            )

    old_tags_set = set(old_note.tags)
    new_tags_set = set(new_note.tags)
    tags_changed = old_tags_set != new_tags_set
    deck_changed = old_note.deck != new_note.deck
    nt_changed = old_note.notetype != new_note.notetype

    return NoteDiff(
        nid=new_note.nid,
        deck=new_note.deck,
        notetype=new_note.notetype,
        change_type="modified" if (field_diffs or tags_changed or deck_changed or nt_changed) else "unchanged",
        field_diffs=field_diffs,
        old_tags=old_note.tags,
        new_tags=new_note.tags,
        tags_changed=tags_changed,
        old_deck=old_note.deck,
        old_notetype=old_note.notetype,
    )


def _notetype_to_canonical(nt: Notetype) -> dict[str, Any]:
    return {
        "name": nt.name,
        "id": nt.id,
        "fields": [
            {"name": f.name, "ord": f.ord, "font": f.font, "size": f.size, "sticky": f.sticky, "rtl": f.rtl}
            for f in nt.fields
        ],
        "templates": [
            {"name": t.name, "ord": t.ord, "qfmt": t.qfmt, "afmt": t.afmt}
            for t in nt.templates
        ],
    }


def _diff_field_lists(old_fields: list[Any], new_fields: list[Any]) -> list[ComponentChange]:
    changes = []
    old_by_name = {f.name: f for f in old_fields}
    new_by_name = {f.name: f for f in new_fields}
    all_names = set(old_by_name) | set(new_by_name)
    for name in sorted(all_names):
        of = old_by_name.get(name)
        nf = new_by_name.get(name)
        if of is None:
            assert nf is not None
            changes.append(ComponentChange(
                component_type="field", name=name, status="added",
                new_value=f"ord={nf.ord}"
            ))
        elif nf is None:
            assert of is not None
            changes.append(ComponentChange(
                component_type="field", name=name, status="removed",
                old_value=f"ord={of.ord}"
            ))
        elif (of.ord, of.font, of.size, of.sticky, of.rtl) != (nf.ord, nf.font, nf.size, nf.sticky, nf.rtl):
            changes.append(ComponentChange(
                component_type="field", name=name, status="modified",
                old_value=f"ord={of.ord} font={of.font} size={of.size}",
                new_value=f"ord={nf.ord} font={nf.font} size={nf.size}",
            ))
    return changes


def _diff_template_lists(old_tmpls: list[Any], new_tmpls: list[Any]) -> list[ComponentChange]:
    changes = []
    old_by_name = {t.name: t for t in old_tmpls}
    new_by_name = {t.name: t for t in new_tmpls}
    all_names = set(old_by_name) | set(new_by_name)
    for name in sorted(all_names):
        ot = old_by_name.get(name)
        nt = new_by_name.get(name)
        if ot is None:
            changes.append(ComponentChange(
                component_type="template", name=name, status="added"
            ))
        elif nt is None:
            changes.append(ComponentChange(
                component_type="template", name=name, status="removed"
            ))
        elif ot.qfmt != nt.qfmt or ot.afmt != nt.afmt:
            changes.append(ComponentChange(
                component_type="template", name=name, status="modified",
                old_value=f"--- front.html\n{ot.qfmt}\n--- back.html\n{ot.afmt}",
                new_value=f"--- front.html\n{nt.qfmt}\n--- back.html\n{nt.afmt}",
            ))
    return changes


def compute_notetype_diff(
    old_nt: Notetype | None, new_nt: Notetype | None
) -> NotetypeDiff | None:
    if old_nt is None:
        assert new_nt is not None
        return NotetypeDiff(
            name=new_nt.name, change_type="added",
            component_changes=[ComponentChange(
                component_type="field", name="(new notetype)", status="added",
                new_value=f"{len(new_nt.fields)} fields, {len(new_nt.templates)} templates",
            )],
            fields_diff="(new notetype)",
        )

    if new_nt is None:
        assert old_nt is not None
        return NotetypeDiff(
            name=old_nt.name, change_type="deleted",
            component_changes=[ComponentChange(
                component_type="field", name="(deleted notetype)", status="removed",
                old_value=f"{len(old_nt.fields)} fields, {len(old_nt.templates)} templates",
            )],
            fields_diff="(deleted notetype)",
        )

    changes: list[ComponentChange] = []
    has_diff = False

    # Compare fields
    field_changes = _diff_field_lists(old_nt.fields, new_nt.fields)
    changes.extend(field_changes)
    if field_changes:
        has_diff = True

    # Compare templates
    tmpl_changes = _diff_template_lists(old_nt.templates, new_nt.templates)
    changes.extend(tmpl_changes)
    if tmpl_changes:
        has_diff = True

    # Compare CSS
    if old_nt.css != new_nt.css:
        has_diff = True
        css_diff_lines = list(difflib.unified_diff(
            old_nt.css.splitlines(keepends=True),
            new_nt.css.splitlines(keepends=True),
            fromfile=f"{new_nt.name}.css",
            tofile=f"{new_nt.name}.css",
            lineterm="",
        ))
        css_diff = "\n".join(css_diff_lines)
        changes.append(ComponentChange(
            component_type="css", name="style.css", status="modified",
            old_value=old_nt.css, new_value=new_nt.css,
        ))
    else:
        css_diff = ""

    if not has_diff:
        return None

    import json
    old_ser = json.dumps(_notetype_to_canonical(old_nt), indent=2, ensure_ascii=False, sort_keys=True)
    new_ser = json.dumps(_notetype_to_canonical(new_nt), indent=2, ensure_ascii=False, sort_keys=True)
    fields_diff = "\n".join(_unified_diff(old_ser, new_ser, f"{new_nt.name}.json"))

    return NotetypeDiff(
        name=new_nt.name,
        change_type="modified",
        component_changes=changes,
        fields_diff=fields_diff,
        css_diff=css_diff,
    )


def compute_export_diff(col: Collection, repo_path: Path, progress_callback: Callable | None = None) -> DiffReport:
    """Compare current Anki collection vs repo state for export preview."""
    from anki_git.formats.notetype_yaml import read_all_notetypes as _read_nt

    report = DiffReport()

    if progress_callback:
        progress_callback("Reading notetypes...")

    notetypes_dir = repo_path / NOTETYPES_DIR
    decks_dir = repo_path / DECKS_DIR

    old_notetypes = _read_nt(notetypes_dir)
    current_notetypes: dict[str, Notetype] = {}
    for nt_dict in col.models.all():
        nt = Notetype.from_anki_dict(nt_dict)
        current_notetypes[nt.name] = nt

    all_nt_names = set(list(old_notetypes.keys()) + list(current_notetypes.keys()))
    for name in sorted(all_nt_names):
        old_nt = old_notetypes.get(name)
        new_nt = current_notetypes.get(name)
        diff = compute_notetype_diff(old_nt, new_nt)
        if diff:
            report.notetype_diffs.append(diff)

    nids = col.db.list("SELECT id FROM notes WHERE id > 0")
    total = len(nids)
    repo_notes_by_id: dict[int, Note] = {}
    if decks_dir.exists():
        files = list(decks_dir.rglob("*.md"))
        for i, notes_file in enumerate(files):
            if progress_callback and i % 50 == 0:
                progress_callback(f"Scanning repo... {i}/{len(files)}")
            for n in parse_notes_file(notes_file):
                repo_notes_by_id[n.nid] = n

    seen_ids = set()
    for i, nid in enumerate(nids):
        if progress_callback and i % 20 == 0:
            progress_callback(f"Diffing notes... {i}/{total}")
        try:
            note_obj = col.get_note(nid)
        except Exception:
            _logger.warning("Failed to get note %d in export diff", nid, exc_info=True)
            continue
        nt_name = note_obj.note_type()["name"]
        try:
            cards = note_obj.cards()
            if not cards:
                continue
            deck_name = col.decks.name(cards[0].did)
        except Exception:
            _logger.warning("Failed to get deck for note %d in export diff", nid, exc_info=True)
            continue
        fields = dict(note_obj.items())
        new_note = Note(nid=nid, notetype=nt_name, tags=list(note_obj.tags), deck=deck_name, fields=fields)
        seen_ids.add(nid)

        old_note = repo_notes_by_id.get(nid)
        nd = compute_note_diff(old_note, new_note)
        if nd.change_type != "unchanged":
            report.note_diffs.append(nd)

    for nid, old_note in repo_notes_by_id.items():
        if nid not in seen_ids:
            nd = compute_note_diff(old_note, None)
            report.note_diffs.append(nd)

    return report


def compute_import_diff(col: Collection, repo_path: Path,
                        progress_callback: Callable | None = None,
                        anki_notes: dict[int, Note] | None = None,
                        repo_notes: dict[int, Note] | None = None,
                        repo_notetypes: dict[str, Notetype] | None = None,
                        anki_checksums: dict[str, str] | None = None,
                        git_checksums: dict[str, str] | None = None) -> ImportDiffData:
    """Compare repo state vs current Anki collection for import preview.

    If anki_notes / repo_notes / repo_notetypes / checksums are provided,
    skips the corresponding filesystem or DB scans and uses the pre-computed
    data directly.  This enables the delta-import flow where the diff phase
    feeds pre-scanned data into the import phase.
    """
    from anki_git.engine.checksums import content_hash
    from anki_git.formats.notetype_yaml import read_all_notetypes as _read_nt

    report = DiffReport()

    if progress_callback:
        progress_callback("Reading notetypes...")

    if repo_notetypes is None:
        notetypes_dir = repo_path / NOTETYPES_DIR
        repo_notetypes = _read_nt(notetypes_dir)
    col_notetypes: dict[str, Notetype] = {}
    for nt_dict in col.models.all():
        nt = Notetype.from_anki_dict(nt_dict)
        col_notetypes[nt.name] = nt

    all_nt_names = set(list(repo_notetypes.keys()) + list(col_notetypes.keys()))
    for name in sorted(all_nt_names):
        old_nt = col_notetypes.get(name)
        new_nt = repo_notetypes.get(name)
        diff = compute_notetype_diff(old_nt, new_nt)
        if diff:
            report.notetype_diffs.append(diff)

    # Gather Anki notes (scan or use pre-computed)
    if anki_notes is None:
        anki_notes = {}
        nids = col.db.list("SELECT id FROM notes WHERE id > 0")
        total_col = len(nids)
        for i, nid in enumerate(nids):
            if progress_callback and i % 50 == 0:
                progress_callback(f"Analyzing collection... {i}/{total_col}")
            try:
                note_obj = col.get_note(nid)
            except Exception:
                _logger.warning("Failed to get note %d in import diff", nid, exc_info=True)
                continue
            nt_name = note_obj.note_type()["name"]
            try:
                cards = note_obj.cards()
                if not cards:
                    continue
                deck_name = col.decks.name(cards[0].did)
            except Exception:
                _logger.warning("Failed to get deck for note %d in import diff", nid, exc_info=True)
                continue
            anki_notes[nid] = Note(nid=nid, notetype=nt_name, tags=list(note_obj.tags), deck=deck_name, fields=dict(note_obj.items()))

    # Gather repo notes (scan or use pre-computed)
    if repo_notes is None:
        repo_notes = {}
        decks_dir = repo_path / DECKS_DIR
        notes_files = sorted(decks_dir.rglob("*.md")) if decks_dir.exists() else []
        total_files = len(notes_files)
        for i, notes_file in enumerate(notes_files):
            if progress_callback and i % 5 == 0:
                progress_callback(f"Diffing repo files... {i}/{total_files}")
            for rn in parse_notes_file(notes_file):
                repo_notes[rn.nid] = rn

    # Compute checksums if not already provided
    if anki_checksums is None:
        anki_checksums = {str(nid): content_hash(n.serialize()) for nid, n in anki_notes.items()}
    if git_checksums is None:
        git_checksums = {str(nid): content_hash(n.serialize()) for nid, n in repo_notes.items()}

    # Diff each pair
    seen_ids: set[int] = set()
    for nid, repo_note in repo_notes.items():
        seen_ids.add(nid)
        col_note = anki_notes.get(nid)
        nd = compute_note_diff(col_note, repo_note)
        if nd.change_type != "unchanged":
            report.note_diffs.append(nd)

    for nid, col_note in anki_notes.items():
        if nid not in seen_ids:
            nd = compute_note_diff(col_note, None)
            report.note_diffs.append(nd)

    return ImportDiffData(
        report=report,
        anki_notes=anki_notes,
        repo_notes=repo_notes,
        repo_notetypes=repo_notetypes,
        anki_checksums=anki_checksums,
        git_checksums=git_checksums,
    )


def _nid_from_deleted_path(path: Path) -> int | None:
    """Extract nid from a deleted decks/<deck>/<nid>.md path."""
    if path.suffix != ".md":
        return None
    try:
        return int(path.stem)
    except ValueError:
        return None


def compute_import_diff_delta(col: Collection, repo_path: Path,
                              progress_callback: Callable | None = None) -> ImportDiffData:
    """Delta-based import diff using git to find only changed files.

    Uses git status/diff to identify only the files that have actually
    changed in the repo.  Parses only those files and looks up only the
    corresponding Anki notes — avoiding a full scan of every note and file.

    Falls back to the full scan if no last_commit_sha baseline exists
    (first run) or if there are too many changed files (heuristic).
    """
    from anki_git.engine.checksums import content_hash, load_meta
    from anki_git.engine.git_ops import get_changed_repo_files

    meta = load_meta(repo_path)
    last_commit_sha = meta.get("last_commit_sha")
    last_max_mod = meta.get("last_max_mod", 0)

    # Fall back to full scan on first run (no baseline)
    if not last_commit_sha:
        _logger.info("No last_commit_sha baseline — falling back to full scan")
        return compute_import_diff(col, repo_path, progress_callback=progress_callback)

    changed, deleted = get_changed_repo_files(repo_path, last_commit_sha)

    # If nothing changed in the repo, return an empty result
    if not changed and not deleted:
        _logger.info("No changed repo files detected")
        return ImportDiffData(report=DiffReport())

    _logger.info("Delta import: %d changed, %d deleted files", len(changed), len(deleted))

    if progress_callback:
        progress_callback(f"Reading {len(changed)} changed files...")

    # Parse only changed repo files
    repo_notes: dict[int, Note] = {}
    for p in sorted(changed):
        if p.suffix != ".md" or p.parts[:1] != ("decks",):
            continue
        file_path = repo_path / p
        if file_path.exists():
            for rn in parse_notes_file(file_path):
                repo_notes[rn.nid] = rn

    # Extract nids from deleted files
    deleted_nids: set[int] = set()
    for p in deleted:
        nid = _nid_from_deleted_path(p)
        if nid is not None:
            deleted_nids.add(nid)

    # Also scan for notetypes that changed
    changed_notetype_names: set[str] = set()
    for p in list(changed) + list(deleted):
        if p.suffix in (".yaml", ".yml", ".css") and p.parts[:1] == ("notetypes",):
            changed_notetype_names.add(p.stem)

    # Read all notetypes if any changed (they're small anyway)
    repo_notetypes: dict[str, Notetype] = {}
    if changed_notetype_names:
        from anki_git.formats.notetype_yaml import read_all_notetypes as _read_nt
        repo_notetypes = _read_nt(repo_path / NOTETYPES_DIR)

    # Build set of nids we need to look up in Anki
    affected_nids: set[int] = set(repo_notes.keys()) | deleted_nids

    # Fetch only the affected Anki notes + any notes modified in Anki since
    # last sync (they might have conflicts with unchanged repo files)
    if progress_callback:
        progress_callback(f"Checking {len(affected_nids)} affected notes...")

    anki_notes: dict[int, Note] = {}
    db = col.db
    assert db is not None

    # Query affected nids individually — still much cheaper than scanning ALL
    col_nid_set: set[int] = set()
    for row in db.list("SELECT id FROM notes WHERE id > 0"):
        col_nid_set.add(row)

    for nid in affected_nids:
        if nid not in col_nid_set:
            continue
        try:
            note_obj = col.get_note(nid)
        except Exception:
            continue
        nt_name = note_obj.note_type()["name"]
        try:
            cards = note_obj.cards()
            if not cards:
                continue
            deck_name = col.decks.name(cards[0].did)
        except Exception:
            continue
        anki_notes[nid] = Note(
            nid=nid, notetype=nt_name, tags=list(note_obj.tags),
            deck=deck_name, fields=dict(note_obj.items()),
        )

    # Also fetch Anki notes that were modified since last sync and whose
    # repo counterpart is unchanged (potential conflicts)
    if progress_callback:
        progress_callback("Checking recently modified notes...")

    recently_modified_query = (
        "SELECT id FROM notes WHERE mod > ? AND id > 0"
    )
    try:
        for (nid,) in db.execute(recently_modified_query, last_max_mod):
            if nid not in affected_nids and nid not in anki_notes:
                try:
                    note_obj = col.get_note(nid)
                except Exception:
                    continue
                nt_name = note_obj.note_type()["name"]
                try:
                    cards = note_obj.cards()
                    if not cards:
                        continue
                    deck_name = col.decks.name(cards[0].did)
                except Exception:
                    continue
                # Try to find repo counterpart for proper diff pairing
                repo_file = repo_path / DECKS_DIR / deck_name.replace("::", "/") / f"{nid}.md"
                if repo_file.exists():
                    for rn in parse_notes_file(repo_file):
                        repo_notes[rn.nid] = rn
                    anki_notes[nid] = Note(
                        nid=nid, notetype=nt_name, tags=list(note_obj.tags),
                        deck=deck_name, fields=dict(note_obj.items()),
                    )
    except Exception:
        _logger.warning("Failed to fetch recently modified notes", exc_info=True)

    # Build partial checksums, filling in from base for unchanged notes
    base_checksums = meta.get("note_checksums", {})
    partial_anki: dict[str, str] = {
        str(nid): content_hash(n.serialize()) for nid, n in anki_notes.items()
    }
    partial_git: dict[str, str] = {
        str(nid): content_hash(n.serialize()) for nid, n in repo_notes.items()
    }

    # Merge with base so conflict detection sees the full picture
    merged_anki = dict(base_checksums)
    merged_anki.update(partial_anki)
    merged_git = dict(base_checksums)
    merged_git.update(partial_git)

    if progress_callback:
        progress_callback("Building diff preview...")

    return compute_import_diff(
        col, repo_path,
        progress_callback=progress_callback,
        anki_notes=anki_notes,
        repo_notes=repo_notes,
        repo_notetypes=repo_notetypes or None,
        anki_checksums=merged_anki,
        git_checksums=merged_git,
    )


def _nid_from_filename(path: Path) -> int | None:
    """Extract nid from a decks/<deck>/<nid>.md path."""
    if path.suffix != ".md":
        return None
    try:
        return int(path.stem)
    except ValueError:
        return None


def _build_notetype_diff_report(
    old_notetypes: dict[str, Notetype],
    current_notetypes: dict[str, Notetype],
    report: DiffReport,
) -> None:
    """Populate report.notetype_diffs from old and current notetypes."""
    all_nt_names = set(old_notetypes) | set(current_notetypes)
    for name in sorted(all_nt_names):
        diff = compute_notetype_diff(
            old_notetypes.get(name), current_notetypes.get(name)
        )
        if diff:
            report.notetype_diffs.append(diff)


def _build_note_diff_report(
    repo_notes: dict[int, Note],
    anki_notes: dict[int, Note],
    report: DiffReport,
) -> None:
    """Populate report.note_diffs from repo and anki note dicts."""
    seen_ids: set[int] = set()
    for nid, repo_note in repo_notes.items():
        seen_ids.add(nid)
        anki_note = anki_notes.get(nid)
        nd = compute_note_diff(repo_note, anki_note)
        if nd.change_type != "unchanged":
            report.note_diffs.append(nd)

    for nid, anki_note in anki_notes.items():
        if nid not in seen_ids:
            nd = compute_note_diff(None, anki_note)
            if nd.change_type != "unchanged":
                report.note_diffs.append(nd)


def _full_export_diff_scan(
    col: Collection,
    repo_path: Path,
    old_notetypes: dict[str, Notetype],
    current_notetypes: dict[str, Notetype],
    changed_notetype_names: list[str],
    progress_callback: Callable | None = None,
) -> ExportDiffData:
    """Full export scan: read all notes and all .md files.

    Used as fallback when no delta baseline exists (first run).
    Also serializes notes for pass-through to avoid a second scan.
    """
    from anki_git.engine.checksums import content_hash
    from anki_git.engine.export_helpers import capture_single_note

    report = DiffReport()
    decks_dir = repo_path / DECKS_DIR

    # Read all repo .md files
    repo_notes: dict[int, Note] = {}
    if decks_dir.exists():
        files = list(decks_dir.rglob("*.md"))
        for f in files:
            for rn in parse_notes_file(f):
                repo_notes[rn.nid] = rn

    nids = col.db.list("SELECT id FROM notes WHERE id > 0")
    total = len(nids)

    anki_notes: dict[int, Note] = {}
    note_entries: dict[int, tuple[int, str, Note]] = {}
    note_checksums: dict[str, str] = {}

    for i, nid in enumerate(nids):
        if progress_callback and i % 20 == 0:
            progress_callback(f"Reading notes... {i}/{total}")
        captured = capture_single_note(col, nid)
        if captured is None:
            continue
        serialized, note = captured
        checksum = content_hash(serialized)
        note_checksums[str(nid)] = checksum
        note_entries[nid] = (nid, serialized, note)
        anki_notes[nid] = note

    all_nids = set(nids)

    _build_notetype_diff_report(old_notetypes, current_notetypes, report)
    _build_note_diff_report(repo_notes, anki_notes, report)

    db = col.db

    return ExportDiffData(
        report=report,
        note_entries=note_entries,
        note_checksums=note_checksums,
        all_nids=all_nids,
        changed_notetype_names=changed_notetype_names,
        notetypes=current_notetypes,
        collection_path=str(col.path),
        last_max_mod=db.scalar("SELECT MAX(mod) FROM notes WHERE id > 0") or 0,
        last_note_count=db.scalar("SELECT COUNT(*) FROM notes WHERE id > 0") or 0,
    )


def compute_export_diff_delta(
    col: Collection,
    repo_path: Path,
    progress_callback: Callable | None = None,
) -> ExportDiffData:
    """Delta-based export diff using mod timestamps and git to find only changed notes.

    Returns ExportDiffData containing the DiffReport and serialized note data
    for pass-through to export_collection().

    Falls back to full scan if no baseline exists (first run).
    """
    from anki_git.engine.checksums import content_hash, load_meta
    from anki_git.engine.export_helpers import capture_single_note
    from anki_git.engine.git_ops import get_changed_repo_files
    from anki_git.formats.notetype_yaml import read_all_notetypes as _read_nt

    meta = load_meta(repo_path)
    last_commit_sha = meta.get("last_commit_sha")
    last_max_mod = meta.get("last_max_mod", 0)
    meta_checksums = meta.get("note_checksums", {})

    if progress_callback:
        progress_callback("Reading notetypes...")

    notetypes_dir = repo_path / NOTETYPES_DIR
    old_notetypes = _read_nt(notetypes_dir)
    current_notetypes: dict[str, Notetype] = {}
    for nt_dict in col.models.all():
        nt = Notetype.from_anki_dict(nt_dict)
        current_notetypes[nt.name] = nt

    changed_notetype_names: list[str] = []
    for name, nt in current_notetypes.items():
        if nt != old_notetypes.get(name):
            changed_notetype_names.append(name)

    # Fall back to full scan on first run (no baseline)
    if not last_commit_sha:
        _logger.info("No last_commit_sha baseline — falling back to full export scan")
        return _full_export_diff_scan(
            col, repo_path, old_notetypes, current_notetypes, changed_notetype_names,
            progress_callback=progress_callback,
        )

    if progress_callback:
        progress_callback("Checking for changes...")

    db = col.db
    all_nids: set[int] = set(db.list("SELECT id FROM notes WHERE id > 0"))

    # Notes changed in Anki since last export
    changed_anki_nids: set[int] = set()
    if last_max_mod:
        try:
            changed_anki_nids = set(
                db.list("SELECT id FROM notes WHERE mod > ? AND id > 0", last_max_mod)
            )
        except Exception:
            _logger.warning("Failed to query changed notes by mod", exc_info=True)

    # Notes deleted from Anki (in meta checksums but gone from collection)
    deleted_anki_nids: set[int] = set()
    for nid_str in meta_checksums:
        if int(nid_str) not in all_nids:
            deleted_anki_nids.add(int(nid_str))

    # Notes with changed repo files
    changed_repo_files, deleted_repo_files = get_changed_repo_files(repo_path, last_commit_sha)
    changed_repo_nids: set[int] = set()
    for p in list(changed_repo_files) + list(deleted_repo_files):
        nid = _nid_from_filename(p)
        if nid is not None:
            changed_repo_nids.add(nid)

    affected_nids: set[int] = changed_anki_nids | deleted_anki_nids | changed_repo_nids

    if not affected_nids and not changed_notetype_names:
        _logger.info("No changed notes or notetypes detected")
        report = DiffReport()
        _build_notetype_diff_report(old_notetypes, current_notetypes, report)
        return ExportDiffData(report=report, all_nids=all_nids)

    _logger.info(
        "Delta export: %d Anki-changed, %d Anki-deleted, %d repo-changed",
        len(changed_anki_nids), len(deleted_anki_nids), len(changed_repo_nids),
    )

    if progress_callback:
        progress_callback(f"Processing {len(affected_nids)} affected notes...")

    # Build path lookup using rglob (fast — lists paths, doesn't read contents)
    decks_dir = repo_path / DECKS_DIR
    nid_to_path: dict[int, Path] = {}
    if decks_dir.exists():
        for p in decks_dir.rglob("*.md"):
            nid = _nid_from_filename(p)
            if nid is not None:
                nid_to_path[nid] = p

    # Parse repo notes for affected nids
    repo_notes: dict[int, Note] = {}
    for nid in affected_nids:
        path = nid_to_path.get(nid)
        if path is not None and path.exists():
            for rn in parse_notes_file(path):
                repo_notes[rn.nid] = rn

    # Fetch and serialize Anki notes for affected nids
    anki_notes: dict[int, Note] = {}
    note_entries: dict[int, tuple[int, str, Note]] = {}
    note_checksums: dict[str, str] = {}

    for nid in sorted(affected_nids):
        if nid not in all_nids:
            continue
        if progress_callback:
            progress_callback(f"Reading note {nid}...")
        captured = capture_single_note(col, nid)
        if captured is None:
            continue
        serialized, note = captured
        checksum = content_hash(serialized)
        note_checksums[str(nid)] = checksum
        note_entries[nid] = (nid, serialized, note)
        anki_notes[nid] = note

    # Build diff report
    report = DiffReport()
    _build_notetype_diff_report(old_notetypes, current_notetypes, report)
    _build_note_diff_report(repo_notes, anki_notes, report)

    # Merge checksums: base + new + stale cleanup
    merged_checksums = dict(meta_checksums)
    merged_checksums.update(note_checksums)
    for nid_str in list(merged_checksums):
        if int(nid_str) not in all_nids:
            del merged_checksums[nid_str]

    fresh_max_mod = db.scalar("SELECT MAX(mod) FROM notes WHERE id > 0") or 0
    fresh_note_count = db.scalar("SELECT COUNT(*) FROM notes WHERE id > 0") or 0

    return ExportDiffData(
        report=report,
        note_entries=note_entries,
        note_checksums=merged_checksums,
        all_nids=all_nids,
        changed_notetype_names=changed_notetype_names,
        notetypes=current_notetypes,
        collection_path=str(col.path),
        last_max_mod=fresh_max_mod,
        last_note_count=fresh_note_count,
    )
