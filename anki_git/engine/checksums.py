import hashlib
import json
from pathlib import Path

from anki.collection import Collection
from git.repo import Repo

from .constants import META_DIR

META_FILE = "meta.json"

_CONTENT_PATHS = ("decks", "notetypes")


def content_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def load_meta(repo_root: Path) -> dict:
    meta_path = repo_root / META_DIR / META_FILE
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text(encoding="utf-8"))


def save_meta(repo_root: Path, meta: dict) -> None:
    meta_path = repo_root / META_DIR / META_FILE
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _content_has_changes(repo: Repo) -> bool:
    """Check if content directories (decks/, notetypes/) have changes.

    Ignores meta.json and other internal files — only checks for real
    content changes that would require an import or export.
    """
    for path in _CONTENT_PATHS:
        if repo.is_dirty(path=path):
            return True
    return any(
        f.startswith("decks/") or f.startswith("notetypes/")
        for f in repo.untracked_files
    )


def quick_repo_has_changes(repo_path: Path) -> bool | None:
    """Quick check if the repo has changes since last sync.

    Only checks git state — no Anki collection access needed.
    Returns False if definitely no repo changes, True if changes exist,
    or None if there is no baseline yet (first run).
    """
    from .git_ops import open_repo

    meta = load_meta(repo_path)
    if not meta:
        return None

    repo = open_repo(repo_path)
    if repo is None:
        return True

    # Check for new commits
    last_sha = meta.get("last_commit_sha")
    try:
        current_sha = repo.head.commit.hexsha
        if last_sha and current_sha != last_sha:
            return True
    except (ValueError, Exception):
        # Fresh repo or detached HEAD issues
        pass

    return bool(_content_has_changes(repo))


def quick_has_changes(col: Collection, repo_path: Path) -> bool | None:
    """Quick check if anything has changed since last sync/export.

    Compares note count + MAX(mod) against stored baseline, and repo HEAD
    SHA against stored SHA.  Returns False if definitely nothing changed
    (full sync can be skipped), True if changes likely exist, or None if
    there is no baseline yet (first run).
    """
    from .git_ops import open_repo

    meta = load_meta(repo_path)
    last_count = meta.get("last_note_count")
    last_max_mod = meta.get("last_max_mod")
    last_sha = meta.get("last_commit_sha")

    if last_count is None or last_max_mod is None:
        return None

    db = col.db
    assert db is not None
    count = db.scalar("SELECT COUNT(*) FROM notes WHERE id > 0") or 0
    max_mod = db.scalar("SELECT MAX(mod) FROM notes WHERE id > 0") or 0

    if count != last_count or max_mod != last_max_mod:
        return True

    repo = open_repo(repo_path)
    if repo is None:
        return True

    # Check for new commits
    try:
        current_sha = repo.head.commit.hexsha
        if last_sha and current_sha != last_sha:
            return True
    except (ValueError, Exception):
        pass

    return bool(_content_has_changes(repo))
