import difflib
import logging
from typing import Any

from aqt import mw
from aqt.qt import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    Qt,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

_logger = logging.getLogger("anki_git")


def get_token_diff(old_str: str, new_str: str):
    max_line_len = 5000
    if len(old_str) > max_line_len or len(new_str) > max_line_len:
        return (
            old_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
            new_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    sm = difflib.SequenceMatcher(None, old_str, new_str)
    res_old = []
    res_new = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        o_val = old_str[i1:i2].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        n_val = new_str[j1:j2].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if tag == 'equal':
            res_old.append(o_val)
            res_new.append(n_val)
        elif tag == 'replace':
            res_old.append(f'<span class="highlight-del">{o_val}</span>')
            res_new.append(f'<span class="highlight-add">{n_val}</span>')
        elif tag == 'delete':
            res_old.append(f'<span class="highlight-del">{o_val}</span>')
        elif tag == 'insert':
            res_new.append(f'<span class="highlight-add">{n_val}</span>')
    return "".join(res_old), "".join(res_new)


def format_diff_line(prefix: str, content: str, line_no: str, cls: str, is_html: bool = False) -> str:
    if not is_html:
        content = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""
    <tr>
        <td class="gutter">{line_no}</td>
        <td class="prefix">{prefix}</td>
        <td class="content {cls}">{content}</td>
    </tr>
    """


def _count_lines(data: dict) -> tuple:
    """Return (added_lines, removed_lines) for a diff data item."""
    added = 0
    removed = 0
    for field in data.get("fields", []):
        for hunk in field.get("hunks", []):
            added += len(hunk.get("added", "").splitlines())
            removed += len(hunk.get("removed", "").splitlines())
    return added, removed


def _build_deck_tree(notes: list[dict]) -> dict:
    """Build nested dict from deck::path::parts.

    Returns: {deck_part: {children: {}, notes: [], note_count: N, added: N, removed: N}}
    """
    root = {"children": {}, "notes": [], "note_count": 0, "added": 0, "removed": 0, "name": ""}

    for item in notes:
        parts = item.get("deck", "Unknown").split("::")
        node = root
        for part in parts:
            if part not in node["children"]:
                node["children"][part] = {
                    "children": {}, "notes": [], "note_count": 0,
                    "added": 0, "removed": 0, "name": part,
                }
            node = node["children"][part]
        node["notes"].append(item)
        add, rem = _count_lines(item)
        node["note_count"] += 1
        node["added"] += add
        node["removed"] += rem

    _aggregate_counts(root)
    return root


def _aggregate_counts(node: dict) -> None:
    """Propagate counts from children up to parent."""
    for child in node["children"].values():
        _aggregate_counts(child)
        node["note_count"] += child["note_count"]
        node["added"] += child["added"]
        node["removed"] += child["removed"]


def report_to_diff_data(report) -> list[dict[str, Any]]:
    """Convert engine DiffReport to the structure expected by DiffDialog."""
    data = []

    max_field_len = 20000

    for nd in report.note_diffs:
        fields = []
        for fd in nd.field_diffs:
            old_val = fd.old_value
            new_val = fd.new_value

            if len(old_val) > max_field_len or len(new_val) > max_field_len:
                hunks = [{
                    "removed": (
                        "(Field too large to diff - see raw file)" if old_val else ""
                    ),
                    "added": (
                        "(Field too large to diff - see raw file)" if new_val else ""
                    ),
                    "context_before": "",
                    "context_after": ""
                }]
            else:
                s = difflib.SequenceMatcher(
                    None, old_val.splitlines(), new_val.splitlines()
                )
                hunks = []
                for tag, i1, i2, j1, j2 in s.get_opcodes():
                    if tag == 'equal':
                        continue
                    hunks.append({
                        "removed": "\n".join(old_val.splitlines()[i1:i2]),
                        "added": "\n".join(new_val.splitlines()[j1:j2]),
                        "context_before": "\n".join(
                            old_val.splitlines()[max(0, i1-2):i1]
                        ),
                        "context_after": "\n".join(
                            old_val.splitlines()[i2:i2+2]
                        )
                    })
            fields.append({"name": fd.field_name, "hunks": hunks})

        if nd.tags_changed:
            old_tags_str = " ".join(nd.old_tags) if nd.old_tags else ""
            new_tags_str = " ".join(nd.new_tags) if nd.new_tags else ""
            fields.append({
                "name": "Tags",
                "hunks": [{
                    "removed": old_tags_str,
                    "added": new_tags_str,
                    "context_before": "",
                    "context_after": ""
                }]
            })

        data.append({
            "type": "note",
            "status": nd.change_type,
            "id": str(nd.nid),
            "notetype": nd.notetype,
            "deck": nd.deck,
            "fields": fields
        })

    for ntd in report.notetype_diffs:
        fields = []
        if ntd.component_changes:
            for cc in ntd.component_changes:
                if cc.status == "modified":
                    old_lines = cc.old_value.splitlines()
                    new_lines = cc.new_value.splitlines()
                    s = difflib.SequenceMatcher(None, old_lines, new_lines)
                    hunks = []
                    for tag, i1, i2, j1, j2 in s.get_opcodes():
                        if tag == 'equal':
                            continue
                        hunks.append({
                            "removed": "\n".join(old_lines[i1:i2]),
                            "added": "\n".join(new_lines[j1:j2]),
                            "context_before": "\n".join(old_lines[max(0, i1-2):i1]),
                            "context_after": "\n".join(old_lines[i2:i2+2])
                        })
                elif cc.status == "added":
                    hunks = [{
                        "removed": "",
                        "added": cc.new_value,
                        "context_before": "",
                        "context_after": ""
                    }]
                else:
                    hunks = [{
                        "removed": cc.old_value,
                        "added": "",
                        "context_before": "",
                        "context_after": ""
                    }]
                fields.append({
                    "name": f"{cc.component_type}: {cc.name} ({cc.status})",
                    "hunks": hunks
                })
        else:
            if ntd.fields_diff:
                fields.append({
                    "name": "YAML",
                    "hunks": [{
                        "removed": "",
                        "added": ntd.fields_diff,
                        "context_before": "",
                        "context_after": ""
                    }]
                })
            if ntd.css_diff:
                fields.append({
                    "name": "CSS",
                    "hunks": [{
                        "removed": "",
                        "added": ntd.css_diff,
                        "context_before": "",
                        "context_after": ""
                    }]
                })
        data.append({
            "type": "template",
            "status": ntd.change_type,
            "id": ntd.name,
            "notetype": "yaml/css",
            "fields": fields
        })
    return data


class DiffDialog(QDialog):
    def __init__(self, diff_data: list[dict[str, Any]], parent=None, accept_text: str = "Commit"):
        super().__init__(parent)
        self.diff_data = diff_data
        self._accept_text = accept_text
        self._tree_items: list[tuple] = []  # (tree_item, data_or_node)

        self.setWindowTitle("Review Changes")
        self.resize(1000, 700)
        self._setup_ui()
        self._apply_style()
        self._populate_tree()

    @classmethod
    def from_report(cls, report, parent=None, accept_text: str = "Commit"):
        return cls(report_to_diff_data(report), parent, accept_text=accept_text)

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Toolbar
        self.toolbar = QFrame()
        self.toolbar.setObjectName("toolbar")
        self.toolbar.setFixedHeight(40)
        toolbar_layout = QHBoxLayout(self.toolbar)
        toolbar_layout.setContentsMargins(15, 0, 15, 0)

        total_count = len(self.diff_data)

        total_added = 0
        total_removed = 0
        for item in self.diff_data:
            for field in item.get("fields", []):
                for hunk in field.get("hunks", []):
                    total_added += len(hunk.get("added", "").splitlines())
                    total_removed += len(hunk.get("removed", "").splitlines())

        stats_label = QLabel(f"{total_count} items")
        stats_label.setObjectName("statsLabel")
        toolbar_layout.addWidget(stats_label)

        self.select_all_btn = QPushButton("Deselect All")
        self.select_all_btn.setObjectName("selectAllBtn")
        self.select_all_btn.clicked.connect(self._toggle_select_all)
        toolbar_layout.addWidget(self.select_all_btn)

        toolbar_layout.addWidget(QLabel(f'<span style="color: #2ecc71;">+{total_added}</span>'
                                        f' / <span style="color: #e74c3c;">-{total_removed}</span>'
                                        f' <span style="color: #888;">lines</span>'))

        toolbar_layout.addStretch()

        self.discard_btn = QPushButton("Discard")
        self.discard_btn.setObjectName("discardBtn")
        self.discard_btn.clicked.connect(self.reject)
        toolbar_layout.addWidget(self.discard_btn)

        self.commit_btn = QPushButton(self._accept_text)
        self.commit_btn.setObjectName("commitBtn")
        self.commit_btn.clicked.connect(self.accept)
        toolbar_layout.addWidget(self.commit_btn)

        main_layout.addWidget(self.toolbar)

        # Main Content (Sidebar + Diff)
        content_widget = QWidget()
        content_layout = QHBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Sidebar tree
        self.sidebar = QTreeWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(260)
        self.sidebar.setHeaderHidden(True)
        self.sidebar.setAnimated(True)
        self.sidebar.setIndentation(16)
        self.sidebar.currentItemChanged.connect(self._on_item_changed)
        content_layout.addWidget(self.sidebar)

        # Border
        border = QFrame()
        border.setFixedWidth(1)
        border.setObjectName("panelBorder")
        content_layout.addWidget(border)

        # Main Diff Panel
        diff_panel = QWidget()
        diff_panel_layout = QVBoxLayout(diff_panel)
        diff_panel_layout.setContentsMargins(0, 0, 0, 0)
        diff_panel_layout.setSpacing(0)

        # Diff Header
        self.diff_header = QFrame()
        self.diff_header.setObjectName("diffHeader")
        self.diff_header.setFixedHeight(60)
        header_layout = QHBoxLayout(self.diff_header)
        header_layout.setContentsMargins(20, 10, 20, 10)

        title_layout = QVBoxLayout()
        self.header_title = QLabel("Select an item")
        self.header_title.setObjectName("headerTitle")
        self.header_subtitle = QLabel("")
        self.header_subtitle.setObjectName("headerSubtitle")
        title_layout.addWidget(self.header_title)
        title_layout.addWidget(self.header_subtitle)
        header_layout.addLayout(title_layout)

        header_layout.addStretch()

        self.change_counts = QLabel("")
        self.change_counts.setObjectName("changeCounts")
        header_layout.addWidget(self.change_counts)

        diff_panel_layout.addWidget(self.diff_header)

        # Diff Scroll Area
        self.diff_browser = QTextBrowser()
        self.diff_browser.setObjectName("diffBrowser")
        self.diff_browser.setOpenExternalLinks(False)
        diff_panel_layout.addWidget(self.diff_browser)

        content_layout.addWidget(diff_panel)
        main_layout.addWidget(content_widget, 1)

    def _apply_style(self):
        is_dark = mw.pm.night_mode() if mw else False
        bg_color = "#1a1a1a" if is_dark else "#ffffff"
        text_color = "#ffffff" if is_dark else "#1a1a1a"
        border_color = "#333333" if is_dark else "#e0e0e0"
        muted_color = "#888888"
        active_bg = "#2a2a2a" if is_dark else "#f5f5f5"

        qss = f"""
            QDialog {{ background-color: {bg_color}; color: {text_color}; }}
            #toolbar {{ border-bottom: 1px solid {border_color}; background-color: {bg_color}; }}
            #sidebar {{ border: none; background-color: {bg_color}; outline: none;
                       font-size: 12px; }}
            #sidebar::item {{ padding: 4px 8px; }}
            #sidebar::item:selected {{
                background-color: {active_bg};
                border-left: 2px solid #3498db;
            }}
            #panelBorder {{ background-color: {border_color}; }}
            #diffHeader {{ border-bottom: 1px solid {border_color}; background-color: {bg_color}; }}
            #headerTitle {{ font-weight: bold; font-size: 14px; color: {text_color}; }}
            #headerSubtitle {{ color: {muted_color}; font-size: 11px; }}
            #changeCounts {{ font-weight: bold; }}
            #diffBrowser {{ border: none; background-color: {bg_color}; }}
            #statsLabel {{ font-weight: bold; }}
            #selectAllBtn {{ background-color: transparent; border: 1px solid {border_color}; padding: 4px 12px; border-radius: 4px; color: {text_color}; margin-left: 10px; }}
            #discardBtn {{ background-color: transparent; border: 1px solid {border_color}; padding: 4px 12px; border-radius: 4px; color: {text_color}; }}
            #commitBtn {{ background-color: #3498db; color: white; border: none; padding: 4px 12px; border-radius: 4px; font-weight: bold; }}
            #commitBtn:hover {{ background-color: #2980b9; }}
        """
        self.setStyleSheet(qss)

        diff_qss = f"""
            body {{ font-family: monospace; font-size: 12px; color: {text_color}; background-color: {bg_color}; margin: 0; }}
            table {{ border-collapse: collapse; width: 100%; }}
            .gutter {{ width: 40px; color: {muted_color}; text-align: right; padding-right: 10px; border-right: 1px solid {border_color}; -webkit-user-select: none; }}
            .prefix {{ width: 20px; text-align: center; font-weight: bold; }}
            .content {{ white-space: pre-wrap; padding-left: 5px; }}
            .line-add-bg {{ background-color: rgba(59, 109, 17, 0.18); color: #3B6D11; }}
            .line-del-bg {{ background-color: rgba(162, 45, 45, 0.18); color: #c0392b; }}
            .highlight-add {{ background-color: rgba(59, 109, 17, 0.45); }}
            .highlight-del {{ background-color: rgba(162, 45, 45, 0.45); }}
            .field-label {{ background-color: {active_bg}; color: {muted_color}; font-size: 10px; font-weight: bold; padding: 4px 10px; margin-top: 10px; text-transform: uppercase; }}
        """
        doc = self.diff_browser.document()
        if doc is not None:
            doc.setDefaultStyleSheet(diff_qss)

    def _toggle_select_all(self):
        any_unchecked = any(
            item.checkState(0) != Qt.CheckState.Checked
            for item, _ in self._tree_items
        )
        new_state = Qt.CheckState.Checked if any_unchecked else Qt.CheckState.Unchecked
        for item, _ in self._tree_items:
            item.setCheckState(0, new_state)
        self.select_all_btn.setText(
            "Deselect All" if new_state == Qt.CheckState.Checked else "Select All"
        )

    def get_checked_nids(self) -> set[str]:
        result: set[str] = set()
        for item, data in self._tree_items:
            if data["type"] == "note" and item.checkState(0) == Qt.CheckState.Checked:
                result.add(data["id"])
        return result

    def get_checked_notetypes(self) -> set[str]:
        result: set[str] = set()
        for item, data in self._tree_items:
            if data["type"] == "template" and item.checkState(0) == Qt.CheckState.Checked:
                result.add(data["id"])
        return result

    def _populate_tree(self):
        notes = [d for d in self.diff_data if d["type"] == "note"]
        templates = [d for d in self.diff_data if d["type"] == "template"]

        self._tree_items = []

        self.sidebar.setUpdatesEnabled(False)

        if notes:
            decks_root = QTreeWidgetItem(self.sidebar, ["Decks"])
            decks_root.setFlags(decks_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            decks_root.setExpanded(True)
            self._build_deck_tree_nodes(notes, decks_root)

        if templates:
            nt_root = QTreeWidgetItem(self.sidebar, ["Notetypes"])
            nt_root.setFlags(nt_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            nt_root.setExpanded(True)
            for item in templates:
                add, rem = _count_lines(item)
                label = f"{item['id']}  +{add}/-{rem}"
                leaf = QTreeWidgetItem(nt_root, [label])
                leaf.setData(0, Qt.ItemDataRole.UserRole, item)
                leaf.setData(0, Qt.ItemDataRole.UserRole + 1, True)
                leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                leaf.setCheckState(0, Qt.CheckState.Checked)
                self._tree_items.append((leaf, item))

        self.sidebar.setUpdatesEnabled(True)
        if self._tree_items:
            self.sidebar.setCurrentItem(self._tree_items[0][0])

    def _build_deck_tree_nodes(self, notes: list[dict], parent: QTreeWidgetItem):
        tree = _build_deck_tree(notes)

        def add_children(node: dict, parent_item: QTreeWidgetItem):
            for name in sorted(node["children"]):
                child = node["children"][name]
                label = f"{name}  +{child['added']}/-{child['removed']} ({child['note_count']})"
                item = QTreeWidgetItem(parent_item, [label])
                item.setData(0, Qt.ItemDataRole.UserRole, child)
                item.setData(0, Qt.ItemDataRole.UserRole + 1, False)
                item.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                )
                if child["children"]:
                    item.setExpanded(False)
                add_children(child, item)
                for note_item in child["notes"]:
                    n_add, n_rem = _count_lines(note_item)
                    n_label = f"  {note_item['id']}  +{n_add}/-{n_rem}"
                    leaf = QTreeWidgetItem(item, [n_label])
                    leaf.setData(0, Qt.ItemDataRole.UserRole, note_item)
                    leaf.setData(0, Qt.ItemDataRole.UserRole + 1, True)
                    leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    leaf.setCheckState(0, Qt.CheckState.Checked)
                    self._tree_items.append((leaf, note_item))

        add_children(tree, parent)

    def _on_item_changed(self, current, previous):
        if current is None:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        is_leaf = current.data(0, Qt.ItemDataRole.UserRole + 1)

        if data is None:
            self.diff_browser.setHtml("")
            self.header_title.setText("")
            self.header_subtitle.setText("")
            self.change_counts.setText("")
            return

        if is_leaf:
            self.header_title.setText(str(data.get("id", "")))
            self.header_subtitle.setText(
                data.get("deck") or data.get("notetype") or data.get("type", "")
            )
            self._render_diff(data)
        else:
            self.header_title.setText(data.get("name", ""))
            self.header_subtitle.setText(
                f"{data['note_count']} notes  +{data['added']}/-{data['removed']}"
            )
            self.change_counts.setText(f"+{data['added']} / -{data['removed']}")
            self._render_deck_summary(data)

    def _render_diff(self, data):
        html = ["<body><table>"]
        total_added = 0
        total_removed = 0

        for field in data.get("fields", []):
            html.append(
                f'<tr><td colspan="3" class="field-label">'
                f'{field["name"].upper()}</td></tr>'
            )

            old_ln = 1
            new_ln = 1

            for hunk in field.get("hunks", []):
                for line in hunk.get("context_before", "").splitlines():
                    html.append(
                        format_diff_line(" ", line, f"{old_ln}", "")
                    )
                    old_ln += 1
                    new_ln += 1

                removed_lines = hunk.get("removed", "").splitlines()
                added_lines = hunk.get("added", "").splitlines()

                max_lines = max(len(removed_lines), len(added_lines))
                for i in range(max_lines):
                    if i < len(removed_lines) and i < len(added_lines):
                        old_t, new_t = get_token_diff(
                            removed_lines[i], added_lines[i]
                        )
                        html.append(
                            format_diff_line(
                                "-", old_t, f"{old_ln}",
                                "line-del-bg", is_html=True
                            )
                        )
                        html.append(
                            format_diff_line(
                                "+", new_t, f"{new_ln}",
                                "line-add-bg", is_html=True
                            )
                        )
                        old_ln += 1
                        new_ln += 1
                        total_removed += 1
                        total_added += 1
                    elif i < len(removed_lines):
                        html.append(
                            format_diff_line(
                                "-", removed_lines[i], f"{old_ln}",
                                "line-del-bg"
                            )
                        )
                        old_ln += 1
                        total_removed += 1
                    elif i < len(added_lines):
                        html.append(
                            format_diff_line(
                                "+", added_lines[i], f"{new_ln}",
                                "line-add-bg"
                            )
                        )
                        new_ln += 1
                        total_added += 1

                for line in hunk.get("context_after", "").splitlines():
                    html.append(
                        format_diff_line(" ", line, f"{old_ln}", "")
                    )
                    old_ln += 1
                    new_ln += 1

        html.append("</table></body>")
        self.diff_browser.setHtml("".join(html))
        self.change_counts.setText(f"+{total_added} / -{total_removed}")
        self.change_counts.setStyleSheet(
            f"color: {'#3B6D11' if total_added > 0 else '#888'};"
        )

    def _render_deck_summary(self, node: dict):
        is_dark = mw.pm.night_mode() if mw else False
        text_color = "#ffffff" if is_dark else "#1a1a1a"
        muted = "#888888"
        add_color = "#3B6D11"
        del_color = "#c0392b"

        html = [
            f'<html><body style="color: {text_color}; font-family: sans-serif; '
            f'font-size: 13px; margin: 20px;">'
            f'<h2>{node["name"] or "Decks"}</h2>'
            f'<p style="color: {muted};">'
            f'{node["note_count"]} note{"s" if node["note_count"] != 1 else ""} changed'
            f' &mdash; '
            f'<span style="color: {add_color};">+{node["added"]}</span> / '
            f'<span style="color: {del_color};">-{node["removed"]}</span> lines'
            f'</p>'
        ]

        if node["children"]:
            html.append('<ul>')
            for name in sorted(node["children"]):
                child = node["children"][name]
                html.append(
                    f'<li><strong>{name}</strong> &mdash; '
                    f'{child["note_count"]} notes, '
                    f'<span style="color: {add_color};">+{child["added"]}</span> / '
                    f'<span style="color: {del_color};">-{child["removed"]}</span>'
                    f'</li>'
                )
            html.append('</ul>')

        if node["notes"]:
            html.append(f'<p style="color: {muted};">Notes in this deck:</p><ul>')
            for item in node["notes"]:
                n_add, n_rem = _count_lines(item)
                status_dot = {
                    "added": "+", "deleted": "-", "modified": "~"
                }.get(item["status"], "?")
                html.append(
                    f'<li>{status_dot} {item["id"]} '
                    f'(<span style="color: {add_color};">+{n_add}</span> / '
                    f'<span style="color: {del_color};">-{n_rem}</span>)'
                    f' &mdash; {item["notetype"]}</li>'
                )
            html.append('</ul>')

        html.append('</body></html>')
        self.diff_browser.setHtml("".join(html))
        self.change_counts.setText(f"+{node['added']} / -{node['removed']}")
        self.change_counts.setStyleSheet(f"color: {add_color};")
