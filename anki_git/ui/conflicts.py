import json

from aqt import mw
from aqt.qt import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
)

from anki_git.engine.conflict import ConflictReport, ConflictType


class ConflictResolutionDialog(QDialog):
    def __init__(self, report: ConflictReport, parent=None):
        super().__init__(parent)
        self.report = report
        self.resolved_report = report
        self.setWindowTitle("AnkiGit — Conflict Resolution")
        self.setMinimumSize(800, 600)
        self._conflicts_list = [
            c for c in self.report.conflicts
            if c.conflict_type == ConflictType.CONFLICT
        ]
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        if not self.report.has_conflicts:
            no_conflicts = QLabel("No conflicts detected.", self)
            layout.addWidget(no_conflicts)
        else:
            info = QLabel(
                f"{self.report.total} notes need attention. "
                f"{len(self._conflicts_list)} conflicts.",
                self,
            )
            layout.addWidget(info)

            splitter = QSplitter(Qt.Orientation.Vertical, self)

            # ── Top: conflicts table ──
            top_widget = QFrame()
            top_layout = QVBoxLayout(top_widget)
            top_layout.setContentsMargins(0, 0, 0, 0)

            self._table = QTableWidget(self)
            self._table.setColumnCount(3)
            self._table.setHorizontalHeaderLabels(
                ["NID", "Conflict Type", "Action"]
            )
            header = self._table.horizontalHeader()
            assert header is not None
            header.setStretchLastSection(True)
            self._table.setSelectionBehavior(
                QTableWidget.SelectionBehavior.SelectRows
            )
            self._table.setSelectionMode(
                QTableWidget.SelectionMode.SingleSelection
            )
            self._table.setRowCount(len(self._conflicts_list))
            self._table.setUpdatesEnabled(False)

            for i, c in enumerate(self._conflicts_list):
                self._table.setItem(
                    i, 0, QTableWidgetItem(str(c.nid))
                )
                self._table.setItem(
                    i, 1, QTableWidgetItem(c.conflict_type.value)
                )
                action_btn = QPushButton("Choose...", self._table)
                action_btn.clicked.connect(
                    lambda _, idx=i: self._show_choice_menu(idx)
                )
                self._table.setCellWidget(i, 2, action_btn)

            self._table.setUpdatesEnabled(True)
            self._table.resizeColumnsToContents()
            self._table.itemSelectionChanged.connect(self._on_selection_changed)
            top_layout.addWidget(self._table)

            bulk_group = QGroupBox("Bulk Actions", self)
            bulk_layout = QHBoxLayout(bulk_group)
            keep_anki_btn = QPushButton("Keep All Anki", self)
            keep_anki_btn.clicked.connect(
                lambda: self._resolve_all("anki")
            )
            keep_git_btn = QPushButton("Keep All Git", self)
            keep_git_btn.clicked.connect(
                lambda: self._resolve_all("git")
            )
            bulk_layout.addWidget(keep_anki_btn)
            bulk_layout.addWidget(keep_git_btn)
            top_layout.addWidget(bulk_group)

            splitter.addWidget(top_widget)

            # ── Bottom: field comparison ──
            self._comparison = QTextBrowser(self)
            self._comparison.setObjectName("comparison")
            self._comparison.setOpenExternalLinks(False)
            self._comparison.setPlaceholderText(
                "Select a conflict row to compare field values"
            )
            splitter.addWidget(self._comparison)

            splitter.setStretchFactor(0, 2)
            splitter.setStretchFactor(1, 3)

            layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_style(self):
        is_dark = mw.pm.night_mode() if mw else False
        bg = "#1a1a1a" if is_dark else "#ffffff"
        text = "#ffffff" if is_dark else "#1a1a1a"
        border = "#333333" if is_dark else "#e0e0e0"

        self.setStyleSheet(f"""
            QDialog {{ background-color: {bg}; color: {text}; }}
            QTableWidget {{ background-color: {bg}; color: {text};
                           border: 1px solid {border}; gridline-color: {border}; }}
            QTableWidget::item:selected {{ background-color: #2a82da; }}
            QHeaderView::section {{ background-color: {bg}; color: {text};
                                   border: 1px solid {border}; padding: 4px; }}
            #comparison {{ background-color: {bg}; color: {text};
                          border: 1px solid {border};
                          font-family: monospace; font-size: 12px; }}
            QGroupBox {{ color: {text}; border: 1px solid {border};
                        border-radius: 4px; margin-top: 8px;
                        padding-top: 12px; }}
            QGroupBox::title {{ color: {text}; }}
            QPushButton {{ background-color: transparent;
                           border: 1px solid {border};
                           padding: 4px 12px; border-radius: 4px;
                           color: {text}; }}
        """)

    def _on_selection_changed(self):
        sel_model = self._table.selectionModel()
        if sel_model is None:
            return
        rows = sel_model.selectedRows()
        if not rows:
            self._comparison.setHtml("")
            return
        row = rows[0].row()
        if row < 0 or row >= len(self._conflicts_list):
            return
        c = self._conflicts_list[row]
        self._render_comparison(c)

    def _render_comparison(self, c):
        try:
            anki_fields = json.loads(c.anki_content or "{}")
        except (json.JSONDecodeError, TypeError):
            anki_fields = {}
        try:
            git_fields = json.loads(c.git_content or "{}")
        except (json.JSONDecodeError, TypeError):
            git_fields = {}

        all_keys = list(dict.fromkeys(
            list(anki_fields.keys()) + list(git_fields.keys())
        ))

        is_dark = mw.pm.night_mode() if mw else False
        add_bg = "rgba(59, 109, 17, 0.18)" if is_dark else "#e6ffed"
        del_bg = "rgba(162, 45, 45, 0.18)" if is_dark else "#ffeef0"
        muted = "#888888"
        text_color = "#ffffff" if is_dark else "#1a1a1a"

        html = [
            f'<html><body style="color: {text_color}; '
            f'font-family: monospace; font-size: 12px; margin: 8px;">'
            f'<h3 style="margin: 0 0 8px 0;">Field Comparison — NID {c.nid}</h3>'
            f'<table style="width: 100%; border-collapse: collapse;">'
            f'<tr style="text-align: left;">'
            f'<th style="border-bottom: 2px solid {muted}; padding: 4px 8px;">Field</th>'
            f'<th style="border-bottom: 2px solid {muted}; padding: 4px 8px;">Anki</th>'
            f'<th style="border-bottom: 2px solid {muted}; padding: 4px 8px;">Git</th>'
            f'</tr>'
        ]

        for key in all_keys:
            av = anki_fields.get(key, "")
            gv = git_fields.get(key, "")
            if av == gv:
                html.append(
                    f'<tr><td style="padding: 4px 8px; font-weight: bold;">{key}</td>'
                    f'<td style="padding: 4px 8px;">{self._esc(av)}</td>'
                    f'<td style="padding: 4px 8px;">{self._esc(gv)}</td></tr>'
                )
            else:
                html.append(
                    f'<tr><td style="padding: 4px 8px; font-weight: bold;">{key}</td>'
                    f'<td style="padding: 4px 8px; background-color: {del_bg};">'
                    f'{self._esc(av)}</td>'
                    f'<td style="padding: 4px 8px; background-color: {add_bg};">'
                    f'{self._esc(gv)}</td></tr>'
                )

        html.append('</table></body></html>')
        self._comparison.setHtml("".join(html))

    @staticmethod
    def _esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _show_choice_menu(self, row: int):
        c = self._conflicts_list[row]
        menu = QMenu(self)
        anki_action = menu.addAction("Keep Anki Version")
        git_action = menu.addAction("Keep Git Version")
        widget = self._table.cellWidget(row, 2)
        assert isinstance(widget, QPushButton)
        chosen = menu.exec(
            widget.mapToGlobal(
                widget.rect().center()
            )
        )
        if chosen == anki_action:
            c.resolution = "anki"
            c.resolved = True
            widget.setText("Anki")
        elif chosen == git_action:
            c.resolution = "git"
            c.resolved = True
            widget.setText("Git")

    def _resolve_all(self, choice: str):
        for c in self.report.conflicts:
            if c.conflict_type == ConflictType.CONFLICT:
                c.resolution = choice
                c.resolved = True
        for row in range(self._table.rowCount()):
            btn = self._table.cellWidget(row, 2)
            if isinstance(btn, QPushButton):
                btn.setText("Anki" if choice == "anki" else "Git")
