import contextlib
import logging
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, Repo

_logger = logging.getLogger("anki_git")


def validate_repo_path(repo_path: Path) -> bool:
    """Validate that repo_path is a reasonable directory path.

    Returns True if the path looks valid (doesn't verify git init state).
    """
    if not str(repo_path).strip():
        return False
    resolved = repo_path.resolve()
    if resolved.exists() and not resolved.is_dir():
        _logger.error("Repo path exists but is not a directory: %s", repo_path)
        return False
    return True


def init_repo(repo_path: Path) -> Repo:
    repo_path.mkdir(parents=True, exist_ok=True)
    _logger.info("Initializing new Git repository at %s", repo_path)
    return Repo.init(repo_path)


def open_repo(repo_path: Path) -> Repo | None:
    try:
        return Repo(repo_path)
    except (InvalidGitRepositoryError, GitCommandError, Exception):
        return None


def get_or_init_repo(repo_path: Path) -> Repo:
    if not validate_repo_path(repo_path):
        raise ValueError(f"Invalid repository path: {repo_path}")
    repo = open_repo(repo_path)
    if repo is None:
        repo = init_repo(repo_path)
    return repo


def stage_all(repo: Repo) -> None:
    repo.git.add(all=True)


def stage_files(repo: Repo, paths: list[str]) -> None:
    """Stage specific file paths relative to the repo root."""
    if not paths:
        return
    repo.index.add(paths)


def create_snapshot_commit(
    repo: Repo,
    changed_files: list[str],
) -> None:
    if not changed_files:
        return
    sorted_files = sorted(changed_files)
    if len(sorted_files) <= 5:
        subject = ", ".join(sorted_files)
    else:
        subject = f"{len(sorted_files)} files changed"
    body = "\n".join(f"- {f}" for f in sorted_files)
    message = f"{subject}\n\n{body}" if len(sorted_files) > 5 else subject
    repo.index.commit(message)


def push_to_remote(repo: Repo, remote_url: str) -> tuple[bool, str]:
    """Push to remote, returning (success, error_message).

    Fetches first to reduce chance of non-fast-forward rejection.
    Never raises — all errors are logged and returned as strings.
    """
    if not remote_url:
        return True, ""
    _logger.info("Pushing to remote: %s", remote_url)
    try:
        try:
            remote = repo.remote("origin")
            if remote.url != remote_url:
                _logger.info("Updating remote origin URL to %s", remote_url)
                remote.set_url(remote_url)
        except ValueError:
            _logger.info("Creating remote origin with URL %s", remote_url)
            remote = repo.create_remote("origin", remote_url)

        branch = repo.active_branch.name

        # Fetch before push to reduce divergence
        with contextlib.suppress(Exception):
            remote.fetch()

        try:
            remote.push(refspec=f"{branch}:{branch}")
        except GitCommandError as e:
            stderr = getattr(e, "stderr", "") or ""
            if "non-fast-forward" in stderr:
                msg = (
                    "Push rejected: remote has commits not present locally.\n"
                    "If you made changes from another device, use "
                    "'Import from Repo' first, then retry the export."
                )
            else:
                msg = f"Push failed: {stderr.strip() or e}"
            _logger.error(msg)
            return False, msg
    except Exception as e:
        msg = f"Failed to push to remote: {e}"
        _logger.error(msg)
        return False, msg

    return True, ""


def is_dirty(repo: Repo) -> bool:
    return repo.is_dirty() or bool(repo.untracked_files)


def get_commit_count(repo: Repo) -> int:
    try:
        return len(list(repo.iter_commits()))
    except Exception:
        _logger.exception("Failed to get commit count")
        return 0


def ensure_gitignore(repo_root: Path) -> None:
    gitignore = repo_root / ".gitignore"
    lines = [".anki_git/"]
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    new_lines = [line for line in lines if line not in existing]
    if new_lines:
        gitignore.write_text(
            "\n".join(existing + new_lines) + "\n",
            encoding="utf-8",
        )


def get_existing_remote_url(repo: Repo) -> str:
    """Return the URL of the 'origin' remote if it exists."""
    try:
        return repo.remote("origin").url
    except (ValueError, Exception):
        _logger.debug("No remote 'origin' configured")
        return ""


_CHANGED_PREFIXES = ("decks/", "notetypes/")


def _unquote_git_path(path: str) -> str:
    """Unquote a C-style quoted path from git diff/status output.

    Git quotes paths containing non-ASCII or special characters using
    C-style octal escapes (e.g. ``"decks/\344\270\255/test.md"``).
    This function converts quoted paths back to proper Unicode strings
    and passes through unquoted paths unchanged.
    """
    if not path.startswith('"'):
        return path
    inner = path[1:-1]
    result_bytes = bytearray()
    i = 0
    while i < len(inner):
        if inner[i] == "\\":
            if i + 3 < len(inner) and inner[i + 1] in "0123":
                result_bytes.append(int(inner[i + 1 : i + 4], 8))
                i += 4
            elif i + 1 < len(inner):
                c = inner[i + 1]
                if c == "n":
                    result_bytes.append(10)
                elif c == "t":
                    result_bytes.append(9)
                elif c == "r":
                    result_bytes.append(13)
                elif c == "\\":
                    result_bytes.append(ord("\\"))
                elif c == '"':
                    result_bytes.append(ord('"'))
                else:
                    result_bytes.append(ord(c))
                i += 2
            else:
                result_bytes.append(ord("\\"))
                i += 1
        else:
            result_bytes.append(ord(inner[i]))
            i += 1
    return result_bytes.decode("utf-8")


def _is_content_path(path: str) -> bool:
    return path.startswith(_CHANGED_PREFIXES)


def get_changed_repo_files(repo_path: Path, last_commit_sha: str | None = None) -> tuple[set[Path], set[Path]]:
    """Find changed and deleted repo files since the last import.

    Uses git status (for uncommitted changes: staged, unstaged, untracked)
    and git diff (for committed changes since last_commit_sha).

    Returns (changed_files, deleted_files) — sets of Paths relative to repo root,
    filtered to 'decks/' and 'notetypes/' content directories.
    """
    repo = Repo(repo_path)
    changed: set[Path] = set()
    deleted: set[Path] = set()

    # Committed changes since last import
    if last_commit_sha:
        try:
            for line in repo.git.diff("--name-status", last_commit_sha, "HEAD").splitlines():
                parts = line.split("\t", 1)
                if len(parts) < 2:
                    continue
                status, path = parts[0][0], _unquote_git_path(parts[1])
                if not _is_content_path(path):
                    continue
                if status == "D":
                    deleted.add(Path(path))
                else:
                    changed.add(Path(path))
        except Exception:
            _logger.warning("git diff against last_commit_sha failed", exc_info=True)

    # Uncommitted changes: staged, unstaged, untracked
    try:
        for line in repo.git.status("--porcelain").splitlines():
            if not line.strip():
                continue
            index_status = line[0]
            wt_status = line[1]
            path = _unquote_git_path(line[3:])

            if not _is_content_path(path):
                continue

            if index_status == "?" and wt_status == "?":
                changed.add(Path(path))
                continue

            actual_status = index_status if index_status != " " else wt_status
            if actual_status == "D":
                deleted.add(Path(path))
            elif actual_status in ("M", "A", "R") and path not in {str(p) for p in deleted}:
                changed.add(Path(path))
    except Exception:
        _logger.warning("git status --porcelain failed", exc_info=True)

    return changed, deleted
