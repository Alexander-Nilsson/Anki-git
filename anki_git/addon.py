import datetime
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anki_git.config import AnkiGitConfig
from anki_git.engine.exporter import export_collection
from anki_git.engine.git_ops import get_existing_remote_url, open_repo

_config = None
_logger = logging.getLogger("anki_git")


def _get_remote_url(repo_path: Path, enabled: bool = True) -> str:
    if not enabled:
        return ""
    repo = open_repo(repo_path)
    return get_existing_remote_url(repo) if repo else ""


def _run_query_op(mw: Any, op: Callable, on_success: Callable, on_failure: Callable, progress_text: str) -> None:
    from aqt.operations import QueryOp
    QueryOp(
        parent=mw,
        op=op,
        success=on_success,
    ).failure(on_failure).with_progress(
        progress_text
    ).run_in_background()


def _import(name: str) -> Any:
    import importlib
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _apply_log_level():
    """Apply the log_level from config to the anki_git logger."""
    config = load_config()
    level_name = config.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    _logger.setLevel(level)


_logging_setup_done = False

def _setup_logging():
    global _logging_setup_done
    if _logging_setup_done:
        return
    try:
        log_dir = Path(__file__).parent / "user_files"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "anki_git.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
        )
        _logger.addHandler(file_handler)
        _apply_log_level()
        _logger.info("AnkiGit logger initialized. Logging to %s", log_path)
        _logging_setup_done = True
    except Exception as e:
        print(f"AnkiGit: Could not setup file logging: {e}")


def load_config() -> AnkiGitConfig:
    global _config
    if _config is not None:
        return _config
    aqt = _import("aqt")
    if aqt is None:
        _config = AnkiGitConfig()
        return _config
    try:
        raw = aqt.mw.addonManager.getConfig(__name__.split(".")[0])
        _config = AnkiGitConfig.from_dict(raw) if raw else AnkiGitConfig()
    except Exception:
        _logger.exception("Failed to load config, using defaults")
        _config = AnkiGitConfig()
    return _config


def save_config(config: AnkiGitConfig) -> None:
    global _config
    _config = config
    _apply_log_level()
    aqt = _import("aqt")
    if aqt is not None:
        try:
            aqt.mw.addonManager.writeConfig(
                __name__.split(".")[0], config.to_dict()
            )
        except Exception as e:
            _logger.warning("Failed to save config: %s", e)



def snapshot_action() -> None:
    from aqt.qt import QMessageBox

    from anki_git.engine.diff import compute_export_diff_delta
    from anki_git.ui import DiffDialog

    config = load_config()
    from aqt import mw

    if not config.repo_path:
        QMessageBox.warning(
            mw, "AnkiGit",
            "Please set a repository path in "
            "Tools > AnkiGit > Settings first."
        )
        return

    if mw is None or mw.col is None:
        QMessageBox.critical(mw, "AnkiGit", "No collection is open.")
        return

    repo_path = Path(config.repo_path).expanduser().resolve()

    def get_diff_with_progress(col):
        _logger.info("Computing export diff (delta)...")
        data = compute_export_diff_delta(
            col, repo_path,
            progress_callback=lambda text: mw.taskman.run_on_main(
                lambda: mw.progress.update(label=text)
            )
        )
        _logger.info(
            "Diff computed: %d notes, %d notetypes changed",
            len(data.report.note_diffs), len(data.report.notetype_diffs)
        )
        if not data.report.has_changes:
            return data, None

        _logger.info("Converting report to UI data...")
        mw.taskman.run_on_main(
            lambda: mw.progress.update(label="Preparing preview...")
        )
        from anki_git.ui.diff import report_to_diff_data
        ui_data = report_to_diff_data(data.report)
        _logger.info("UI data ready (%d items)", len(ui_data))
        return data, ui_data

    def on_diff_done(result_tuple):
        _logger.info("on_diff_done reached")
        data, ui_data = result_tuple
        if not data or not data.report.has_changes:
            _logger.info("No changes to export")
            QMessageBox.information(
                mw, "AnkiGit",
                "No changes detected. Nothing to export."
            )
            return

        _logger.info("Opening DiffDialog...")
        diff_dialog = DiffDialog(ui_data, mw, accept_text="Commit")
        _logger.info("DiffDialog initialized")
        if not diff_dialog.exec():
            _logger.info("User discarded changes")
            return

        _logger.info("User accepted changes, starting export...")

        def do_export(col):
            return export_collection(
                col,
                repo_path,
                remote_url=_get_remote_url(repo_path, config.auto_push_after_snapshot),
                progress_callback=lambda text: mw.taskman.run_on_main(
                    lambda: mw.progress.update(label=text)
                ),
                export_data=data,
            )

        def on_export_done(result):
            if result.error:
                _logger.error("Export failed: %s", result.error)
                QMessageBox.critical(
                    mw, "AnkiGit Snapshot", f"Error: {result.error}"
                )
                return

            msg = (
                f"Snapshot complete.\n"
                f"Notes changed: {result.notes_changed}\n"
                f"Notetypes changed: {result.notetypes_changed}\n"
                f"Total commits: {result.commit_count}\n"
                f"Duration: {result.duration_seconds:.1f}s"
            )
            QMessageBox.information(mw, "AnkiGit Snapshot", msg)

        def on_export_failed(e):
            error_msg = str(e) if e else "Unknown error"
            _logger.error("Export operation failed: %s", error_msg)
            if mw:
                QMessageBox.critical(mw, "AnkiGit", f"Snapshot failed: {error_msg}")

        _run_query_op(mw, do_export, on_export_done, on_export_failed, "Taking Snapshot...")

    def on_diff_failed(e):
        error_msg = str(e) if e else "Unknown error"
        _logger.error("Failed to compute export diff: %s", error_msg)
        if mw:
            QMessageBox.critical(mw, "AnkiGit", f"Failed to compute diff: {error_msg}")

    _run_query_op(mw, get_diff_with_progress, on_diff_done, on_diff_failed, "Reviewing Changes...")


def import_action() -> None:
    from aqt.qt import QMessageBox

    from anki_git.engine.diff import compute_import_diff_delta
    from anki_git.engine.importer import pull_from_repo
    from anki_git.ui import ConflictResolutionDialog, DiffDialog

    config = load_config()
    from aqt import mw

    if not config.repo_path:
        QMessageBox.warning(
            mw, "AnkiGit",
            "Please set a repository path in "
            "Tools > AnkiGit > Settings first."
        )
        return

    if mw is None or mw.col is None:
        QMessageBox.critical(mw, "AnkiGit", "No collection is open.")
        return

    repo_path = Path(config.repo_path).expanduser().resolve()
    if not (repo_path / ".git").exists():
        QMessageBox.warning(
            mw, "AnkiGit",
            "No Git repository found. Take a snapshot first."
        )
        return

    def get_diff_with_progress(col):
        data = compute_import_diff_delta(
            col, repo_path,
            progress_callback=lambda text: mw.taskman.run_on_main(
                lambda: mw.progress.update(label=text)
            )
        )
        if not data.report.has_changes:
            return data, None

        mw.taskman.run_on_main(
            lambda: mw.progress.update(label="Preparing preview...")
        )
        from anki_git.ui.diff import report_to_diff_data
        ui_data = report_to_diff_data(data.report)
        return data, ui_data

    def on_diff_done(result_tuple):
        data, ui_data = result_tuple
        if not data or not data.report.has_changes:
            QMessageBox.information(
                mw, "AnkiGit",
                "No changes detected. Nothing to import."
            )
            return

        diff_dialog = DiffDialog(ui_data, mw, accept_text="Import Changes")
        if not diff_dialog.exec():
            return

        selected_nids = diff_dialog.get_checked_nids()
        selected_notetypes = diff_dialog.get_checked_notetypes()

        if not selected_nids and not selected_notetypes:
            QMessageBox.information(
                mw, "AnkiGit",
                "No items selected. Nothing to import."
            )
            return

        # Auto-include notetypes referenced by selected notes
        for note in data.repo_notes.values():
            if str(note.nid) in selected_nids and note.notetype not in selected_notetypes:
                selected_notetypes.add(note.notetype)

        # Filter all pre-computed data to only include selected items
        _logger.info("DEBUG import_action: selected_nids=%d, selected_notetypes=%d",
                     len(selected_nids), len(selected_notetypes))
        nids_checksums_before = len(data.anki_checksums)
        data.anki_checksums = {
            k: v for k, v in data.anki_checksums.items() if k in selected_nids
        }
        data.git_checksums = {
            k: v for k, v in data.git_checksums.items() if k in selected_nids
        }
        data.repo_notes = {
            k: v for k, v in data.repo_notes.items() if str(k) in selected_nids
        }
        data.repo_notetypes = {
            k: v for k, v in data.repo_notetypes.items() if k in selected_notetypes
        }
        _logger.info("DEBUG import_action: filtered anki_checksums %d->%d, git_checksums %d->%d",
                     nids_checksums_before, len(data.anki_checksums),
                     nids_checksums_before, len(data.git_checksums))
        _logger.info("DEBUG import_action: first5_selected_nids=%s", sorted(selected_nids)[:5])

        def do_import(col):
            col_path = Path(col.path)
            backup_path = (
                repo_path / ".anki_git" / "backups"
                / f"pre-import-{int(datetime.datetime.now(datetime.UTC).timestamp())}.anki2"
            )
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(col_path.read_bytes())

            def handle_conflicts_bg(report):
                def _show():
                    diag = ConflictResolutionDialog(report, mw)
                    diag.exec()
                    return diag.resolved_report
                from anki_git.ui.utils import run_on_main_sync
                return run_on_main_sync(mw, _show)

            return pull_from_repo(
                col, repo_path,
                sync_mode=config.sync_mode,
                conflict_callback=handle_conflicts_bg,
                anki_checksums=data.anki_checksums,
                git_checksums=data.git_checksums,
                git_notes_lookup=data.repo_notes,
                repo_notetypes=data.repo_notetypes,
            )

        def on_import_done(result):
            parts = []
            if result.notes_updated:
                parts.append(f"Notes updated: {result.notes_updated}")
            if result.notes_created:
                parts.append(f"Notes created: {result.notes_created}")
            if result.notetypes_updated:
                parts.append(f"Notetypes updated: {result.notetypes_updated}")
            if result.notetypes_created:
                parts.append(f"Notetypes created: {result.notetypes_created}")
            if result.warnings:
                _logger.warning(
                    "Import warnings: %s", result.warnings
                )
                parts.append("Warnings: " + "; ".join(result.warnings[:5]))
            if result.errors:
                _logger.error("Import errors: %s", result.errors)
                parts.append("Errors: " + "; ".join(result.errors[:5]))
            msg = "\n".join(parts) if parts else "Import complete."
            QMessageBox.information(mw, "AnkiGit Import", msg)
            mw.reset()

        def on_import_failed(e):
            error_msg = str(e) if e else "Unknown error"
            _logger.error("Import operation failed: %s", error_msg)
            if mw:
                QMessageBox.critical(mw, "AnkiGit", f"Import failed: {error_msg}")

        _run_query_op(mw, do_import, on_import_done, on_import_failed, "Pulling from Repo...")

    def on_diff_failed(e):
        error_msg = str(e) if e else "Unknown error"
        _logger.error("Failed to compute import diff: %s", error_msg)
        if mw:
            QMessageBox.critical(mw, "AnkiGit", f"Failed to compute diff: {error_msg}")

    _run_query_op(mw, get_diff_with_progress, on_diff_done, on_diff_failed, "Reviewing Changes...")



def settings_action() -> None:
    from aqt import mw

    from anki_git.ui import SettingsDialog
    config = load_config()
    dialog = SettingsDialog(config, mw)
    if dialog.exec():
        save_config(dialog.config)


def show_menu() -> None:
    from aqt import mw
    from aqt.qt import QAction, QMenu

    if mw is None:
        return

    parent_menu = None
    for action in mw.form.menubar.actions():
        if action.text().strip().lower() == "tools":
            parent_menu = action.menu()
            break

    if parent_menu is None:
        parent_menu = QMenu("AnkiGit", mw)

    export_act = QAction("Export to Repo", mw)
    export_act.triggered.connect(snapshot_action)
    parent_menu.addAction(export_act)

    import_act = QAction("Import from Repo", mw)
    import_act.triggered.connect(import_action)
    parent_menu.addAction(import_act)

    parent_menu.addSeparator()

    settings_act = QAction("Settings...", mw)
    settings_act.triggered.connect(settings_action)
    parent_menu.addAction(settings_act)

    if parent_menu not in (a.menu() for a in mw.form.menubar.actions()):
        mw.form.menubar.addMenu(parent_menu)


_menu_shown = False


def _try_fetch(remote) -> None:
    try:
        remote.fetch()
    except Exception:
        _logger.warning("Remote fetch failed", exc_info=True)


def _fetch_remote_background(repo_path: Path) -> None:
    """Fire-and-forget git fetch from origin in a daemon thread."""
    import threading

    from anki_git.engine.git_ops import open_repo

    repo = open_repo(repo_path)
    if repo is None:
        return
    try:
        remote = repo.remote("origin")
        threading.Thread(
            target=lambda: _try_fetch(remote),
            daemon=True,
        ).start()
    except (ValueError, Exception):
        pass


def _run_startup_import(config: AnkiGitConfig) -> None:
    """Show import diff on startup. Let user accept/reject repo changes.

    Runs a synchronous git-only check first — only shows a progress dialog
    if changes are actually detected in the repo.
    """
    from aqt import mw
    from aqt.qt import QMessageBox

    from anki_git.engine.checksums import quick_repo_has_changes
    from anki_git.engine.diff import compute_import_diff_delta
    from anki_git.ui import DiffDialog

    repo_path = Path(config.repo_path).expanduser().resolve()

    # Synchronous git-only check — no collection access, no dialog
    try:
        has_changes = quick_repo_has_changes(repo_path)
        _logger.info("quick_repo_has_changes returned: %s", has_changes)
        if has_changes is False or has_changes is None:
            _logger.info("No repo changes, skipping startup import")
            return
    except Exception:
        _logger.warning(
            "quick_repo_has_changes failed on startup", exc_info=True
        )
        return

    # Changes detected — proceed with delta diff inside a QueryOp (shows progress)
    def do_startup_check(col):
        _logger.info("Computing startup import diff...")
        data = compute_import_diff_delta(
            col, repo_path,
            progress_callback=lambda text: mw.taskman.run_on_main(
                lambda: mw.progress.update(label=text)
            ),
        )
        if not data.report.has_changes:
            return data, None

        mw.taskman.run_on_main(
            lambda: mw.progress.update(label="Preparing preview...")
        )
        from anki_git.ui.diff import report_to_diff_data
        ui_data = report_to_diff_data(data.report)
        return data, ui_data

    def on_diff_done(result):
        if result is None:
            _logger.info("No repo changes to import on startup")
            return

        data, ui_data = result
        if not data or not data.report.has_changes:
            _logger.info("No repo changes to import on startup")
            return

        diff_dialog = DiffDialog(ui_data, mw, accept_text="Import Changes")
        if not diff_dialog.exec():
            _logger.info("User discarded startup import changes")
            return

        selected_nids = diff_dialog.get_checked_nids()
        selected_notetypes = diff_dialog.get_checked_notetypes()

        if not selected_nids and not selected_notetypes:
            _logger.info("No items selected on startup, skipping import")
            return

        for note in data.repo_notes.values():
            if str(note.nid) in selected_nids and note.notetype not in selected_notetypes:
                selected_notetypes.add(note.notetype)

        data.anki_checksums = {
            k: v for k, v in data.anki_checksums.items() if k in selected_nids
        }
        data.git_checksums = {
            k: v for k, v in data.git_checksums.items() if k in selected_nids
        }
        data.repo_notes = {
            k: v for k, v in data.repo_notes.items() if str(k) in selected_nids
        }
        data.repo_notetypes = {
            k: v for k, v in data.repo_notetypes.items() if k in selected_notetypes
        }

        _logger.info("User accepted startup import, starting import...")

        def do_import(col):
            col_path = Path(col.path)
            backup_path = (
                repo_path / ".anki_git" / "backups"
                / f"pre-import-{int(datetime.datetime.now(datetime.UTC).timestamp())}.anki2"
            )
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(col_path.read_bytes())

            def handle_conflicts_bg(report):
                def _show():
                    from anki_git.ui.conflicts import ConflictResolutionDialog
                    diag = ConflictResolutionDialog(report, mw)
                    diag.exec()
                    return diag.resolved_report
                from anki_git.ui.utils import run_on_main_sync
                return run_on_main_sync(mw, _show)

            from anki_git.engine.importer import pull_from_repo
            result = pull_from_repo(
                col, repo_path,
                sync_mode=config.sync_mode,
                conflict_callback=handle_conflicts_bg,
                anki_checksums=data.anki_checksums,
                git_checksums=data.git_checksums,
                git_notes_lookup=data.repo_notes,
                repo_notetypes=data.repo_notetypes,
            )

            return result

        def on_import_done(result):
            parts = []
            if result.notes_updated:
                parts.append(f"Notes updated: {result.notes_updated}")
            if result.notes_created:
                parts.append(f"Notes created: {result.notes_created}")
            if result.notetypes_updated:
                parts.append(f"Notetypes updated: {result.notetypes_updated}")
            if result.notetypes_created:
                parts.append(f"Notetypes created: {result.notetypes_created}")
            if result.warnings:
                parts.append("Warnings: " + "; ".join(result.warnings[:3]))
            if result.errors:
                _logger.error("Import errors: %s", result.errors)
                parts.append("Errors: " + "; ".join(result.errors[:5]))
            msg = "\n".join(parts) if parts else "Import complete."
            QMessageBox.information(mw, "AnkiGit Import", msg)
            mw.reset()

        def on_import_failed(e):
            error_msg = str(e) if e else "Unknown error"
            _logger.error("Startup import failed: %s", error_msg)
            if mw:
                QMessageBox.critical(mw, "AnkiGit", f"Import failed: {error_msg}")

        _run_query_op(mw, do_import, on_import_done, on_import_failed, "Importing from repo...")

    def on_diff_failed(e):
        error_msg = str(e) if e else "Unknown error"
        _logger.error("Startup diff failed: %s", error_msg)
        if mw:
            QMessageBox.critical(mw, "AnkiGit", f"Failed to compute diff: {error_msg}")

    _run_query_op(mw, do_startup_check, on_diff_done, on_diff_failed, "Checking for changes...")


def on_profile_open() -> None:
    global _menu_shown
    if not _menu_shown:
        show_menu()
        _menu_shown = True
    config = load_config()
    if config.auto_sync_on_startup and config.repo_path:
        from aqt import mw
        if mw and mw.col:
            repo_path = Path(config.repo_path).expanduser().resolve()
            if not (repo_path / ".git").exists():
                return
            _fetch_remote_background(repo_path)
        _run_startup_import(config)


def _push_background(repo_path: Path, remote_url: str) -> None:
    """Push to remote in a daemon thread — can be killed without data loss."""
    from anki_git.engine.git_ops import open_repo, push_to_remote
    try:
        repo = open_repo(repo_path)
        if repo is not None:
            push_to_remote(repo, remote_url)
            _logger.info("Background push completed")
    except Exception:
        _logger.exception("Background push failed")


def on_profile_close() -> None:
    config = load_config()
    if not config.auto_snapshot_on_close or not config.repo_path:
        return
    from aqt import mw
    if mw is None or mw.col is None:
        return
    repo_path = Path(config.repo_path).expanduser().resolve()
    if not (repo_path / ".git").exists():
        return

    if mw.col.db is None:
        _logger.warning("Collection already closed, skipping auto-export")
        return

    from anki_git.engine.checksums import quick_has_changes
    try:
        result = quick_has_changes(mw.col, repo_path)
        if result is False:
            _logger.info("No changes detected, skipping auto-export")
            return
    except Exception:
        _logger.warning("quick_has_changes failed on close", exc_info=True)

    remote_url = _get_remote_url(repo_path, config.auto_push_after_snapshot)

    # Phase 1 — capture data from collection (synchronous, needs col open)
    from anki_git.engine.exporter import capture_export_data
    try:
        captured = capture_export_data(
            mw.col, repo_path,
            quick=True,
        )
    except Exception as e:
        _logger.warning("Failed to capture export data on close: %s", e)
        return

    if not captured.note_entries and not captured.changed_notetype_names:
        _logger.info("No changes captured, skipping write")
        return

    # Phase 2+3 — write files + git commit + push in a non-daemon thread.
    # The captured data is fully serialized, so no collection access needed.
    # Using non-daemon so the thread completes before process exit.
    import threading
    _logger.info(
        "Auto-export on close: %d notes, spawning background write",
        len(captured.note_entries),
    )

    def _write_and_push() -> None:
        from anki_git.engine.exporter import write_export_data
        try:
            result = write_export_data(
                repo_path, captured,
                remote_url=remote_url,
            )
            if result.error:
                _logger.warning("Write on close failed: %s", result.error)
                return
            _logger.info(
                "Write on close: %d notes, %d notetypes committed",
                result.notes_changed,
                result.notetypes_changed,
            )
        except Exception as e:
            _logger.warning("Write on close crashed: %s", e)

    thread = threading.Thread(target=_write_and_push, daemon=False)
    thread.start()


def init_addon() -> None:
    if _import("aqt") is not None:
        _setup_logging()
    from aqt import gui_hooks
    gui_hooks.profile_did_open.append(on_profile_open)
    gui_hooks.profile_will_close.append(on_profile_close)
