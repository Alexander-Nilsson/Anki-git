from aqt.qt import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)
from git import GitCommandError

from ..config import SYNC_MODE_CHOICES, AnkiGitConfig
from ..engine.checksums import load_meta
from ..engine.git_ops import get_existing_remote_url, open_repo


class SettingsDialog(QDialog):
    def __init__(self, config: AnkiGitConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("AnkiGit Settings")
        self.setMinimumWidth(550)
        self._setup_ui()
        self._load_config()
        self._load_status()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        repo_group = QGroupBox("Repository", self)
        repo_layout = QFormLayout(repo_group)
        repo_path_layout = QHBoxLayout()
        self._repo_path_input = QLineEdit(self)
        self._repo_path_input.setPlaceholderText("/path/to/anki-repo")
        browse_btn = QPushButton("Browse...", self)
        browse_btn.clicked.connect(self._browse_repo)
        repo_path_layout.addWidget(self._repo_path_input)
        repo_path_layout.addWidget(browse_btn)
        repo_layout.addRow("Repo path:", repo_path_layout)
        remote_layout = QHBoxLayout()
        self._check_remote_btn = QPushButton("Check Remote", self)
        self._check_remote_btn.clicked.connect(self._check_remote)
        self._remote_check_label = QLabel("", self)
        remote_layout.addWidget(self._check_remote_btn)
        remote_layout.addWidget(self._remote_check_label)
        remote_layout.addStretch()
        repo_layout.addRow("Remote:", remote_layout)
        layout.addWidget(repo_group)

        sync_group = QGroupBox("Sync Behavior", self)
        sync_layout = QFormLayout(sync_group)
        self._auto_startup_cb = QCheckBox("Import from repo when Anki starts", self)
        sync_layout.addRow(self._auto_startup_cb)
        self._auto_close_cb = QCheckBox("Export to repo when Anki closes", self)
        sync_layout.addRow(self._auto_close_cb)

        self._auto_push_cb = QCheckBox("Push to remote after export", self)
        sync_layout.addRow(self._auto_push_cb)

        self._sync_mode_combo = QComboBox(self)
        for value, label in SYNC_MODE_CHOICES:
            self._sync_mode_combo.addItem(label, value)
        sync_layout.addRow("Conflict resolution:", self._sync_mode_combo)

        mode_note = QLabel(
            "Always ask: prompts when Anki and repo disagree\n"
            "Prefer Anki: auto-resolves using the Anki version\n"
            "Prefer Repo: auto-resolves using the repo version\n"
            "Accept all: auto-merges when safe; Anki wins on real conflicts"
        )
        mode_note.setStyleSheet("color: #888; font-size: 11px;")
        sync_layout.addRow("", mode_note)

        layout.addWidget(sync_group)

        status_group = QGroupBox("Status", self)
        status_layout = QFormLayout(status_group)
        self._status_value = QLabel("", self)
        self._status_value.setStyleSheet("font-weight: bold;")
        status_layout.addRow("Last sync:", self._status_value)
        layout.addWidget(status_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_config(self):
        self._repo_path_input.setText(self.config.repo_path)
        self._auto_startup_cb.setChecked(self.config.auto_sync_on_startup)
        self._auto_close_cb.setChecked(self.config.auto_snapshot_on_close)
        self._auto_push_cb.setChecked(self.config.auto_push_after_snapshot)

        idx = self._sync_mode_combo.findData(self.config.sync_mode)
        if idx >= 0:
            self._sync_mode_combo.setCurrentIndex(idx)

    def _load_status(self):
        """Read sync status from the global status string and meta.json."""
        import datetime
        from pathlib import Path

        repo_path_str = self._repo_path_input.text().strip()
        if not repo_path_str:
            self._status_value.setText("No repo configured")
            return
        meta = load_meta(Path(repo_path_str))
        ts = meta.get("last_export_time")
        if ts:
            t = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
            ago = datetime.datetime.now(datetime.UTC) - t
            if ago.total_seconds() < 60:
                self._status_value.setText("Just now")
            elif ago.total_seconds() < 3600:
                self._status_value.setText(f"{int(ago.total_seconds() // 60)}m ago")
            else:
                local_t = t.astimezone()
                self._status_value.setText(local_t.strftime("%Y-%m-%d %H:%M"))
        else:
            self._status_value.setText("Never synced")

    def _save_and_accept(self):
        self.config.repo_path = self._repo_path_input.text().strip()
        self.config.auto_sync_on_startup = self._auto_startup_cb.isChecked()
        self.config.auto_snapshot_on_close = self._auto_close_cb.isChecked()
        self.config.auto_push_after_snapshot = self._auto_push_cb.isChecked()
        self.config.sync_mode = self._sync_mode_combo.currentData()
        self.accept()

    def _browse_repo(self):
        path = QFileDialog.getExistingDirectory(self, "Select Git Repository Path")
        if path:
            self._repo_path_input.setText(path)

    def _check_remote(self):
        from pathlib import Path

        path_str = self._repo_path_input.text().strip()
        if not path_str:
            self._remote_check_label.setText("No repo path set")
            return

        repo_path = Path(path_str)
        repo = open_repo(repo_path)
        if repo is None:
            self._remote_check_label.setText("Not a Git repository")
            return

        url = get_existing_remote_url(repo)
        if not url:
            self._remote_check_label.setText("No remote 'origin' configured")
            return

        self._check_remote_btn.setEnabled(False)
        self._check_remote_btn.setText("Verifying...")
        self._remote_check_label.setText("")
        QApplication.processEvents()

        try:
            repo.git.ls_remote("origin")
            self._remote_check_label.setText(f"Connected: {url}")
            self._remote_check_label.setStyleSheet("color: green;")
        except GitCommandError as e:
            self._remote_check_label.setText(f"Cannot reach remote: {str(e)[:80]}")
            self._remote_check_label.setStyleSheet("color: red;")
        except Exception as e:
            self._remote_check_label.setText(f"Error: {str(e)[:80]}")
            self._remote_check_label.setStyleSheet("color: red;")
        finally:
            self._check_remote_btn.setEnabled(True)
            self._check_remote_btn.setText("Check Remote")
