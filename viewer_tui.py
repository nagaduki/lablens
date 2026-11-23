#!/usr/bin/env python3
"""lablens_core.py が出力した JSONL を閲覧する Textual TUI ビューア。

使い方:
  1) パイプ経由: python lablens_core.py ... | python viewer_tui.py
  2) ファイル指定: python viewer_tui.py ~/lablens/output/result.jsonl
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Select, Static

ALL_KEY = "__ALL__"

# 優先表示する列順と日本語ヘッダ
PREFERRED_COLUMNS: list[tuple[str, str]] = [
    ("exam_date", "検査日"),
    ("category", "区分"),
    ("sample_type", "検体"),
    ("item", "項目"),
    ("value", "結果値"),
    ("flag", "判定"),
    ("unit", "単位"),
    ("ref_range", "基準値"),
]


def load_jsonl_records(raw_text: str) -> list[dict[str, Any]]:
    """JSONLテキストから辞書レコードのみを安全に抽出する（空行や配列行は無視）。"""
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def detect_group_key(records: list[dict[str, Any]]) -> str | None:
    """絞り込み対象となるグループ列を自動判定する。"""
    for candidate in ("category", "sample_type", "exam_date"):
        if any(rec.get(candidate) for rec in records):
            return candidate
    return None


def detect_columns(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """レコードに含まれるキーから表示列リストを決定する。"""
    present_keys: list[str] = []
    for rec in records:
        for k in rec.keys():
            if k not in present_keys:
                present_keys.append(k)

    columns: list[tuple[str, str]] = []
    used: set[str] = set()
    for key, label in PREFERRED_COLUMNS:
        if key in present_keys:
            columns.append((key, label))
            used.add(key)
    for key in present_keys:
        if key not in used:
            columns.append((key, key))
    return columns


def flag_style(flag: str) -> str | None:
    """異常値フラグに応じた Rich スタイル名を返す。"""
    f = flag.strip()
    if not f:
        return None
    upper = f.upper()
    if upper.startswith("H") or "↑" in f or "+" in f:
        return "bold red"
    if upper.startswith("L") or "↓" in f:
        return "bold cyan"
    return "bold yellow"


def is_abnormal(rec: dict[str, Any]) -> bool:
    """異常値フラグが付いているレコードか判定する。"""
    return bool(str(rec.get("flag", "")).strip())


class LabLensViewer(App[None]):
    TITLE = "LabLens Result Viewer"
    CSS = """
    Screen {
        layout: vertical;
    }
    #toolbar {
        height: 3;
        padding: 0 1;
        background: $panel;
    }
    #category_select {
        width: 24;
        margin-right: 1;
    }
    #search_input {
        width: 1fr;
    }
    #status_bar {
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text-muted;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "終了"),
        Binding("a", "toggle_abnormal", "異常値のみ"),
        Binding("c", "cycle_category", "区分切替"),
        Binding("/", "focus_search", "検索"),
        Binding("escape", "focus_table", "テーブルへ", show=False),
        Binding("j", "cursor_down", "下", show=False),
        Binding("k", "cursor_up", "上", show=False),
    ]

    def __init__(self, records: list[dict[str, Any]], source_label: str) -> None:
        super().__init__()
        self.records = records
        self.source_label = source_label
        self.group_key = detect_group_key(records)
        self.columns = detect_columns(records)
        self.categories = self._collect_categories()
        self.selected_category: str = ALL_KEY
        self.abnormal_only: bool = False
        self.search_query: str = ""

    def _collect_categories(self) -> list[str]:
        if not self.group_key:
            return []
        seen: list[str] = []
        for rec in self.records:
            val = str(rec.get(self.group_key, "")).strip() or "(未分類)"
            if val not in seen:
                seen.append(val)
        return seen

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        options = [("すべて", ALL_KEY)] + [(cat, cat) for cat in self.categories]
        with Horizontal(id="toolbar"):
            yield Select(
                options,
                value=ALL_KEY,
                allow_blank=False,
                id="category_select",
            )
            yield Input(placeholder="項目名で検索 ( / キー )", id="search_input")
        yield Static("", id="status_bar")
        yield DataTable(id="lab_table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.source_label
        table = self.query_one("#lab_table", DataTable)
        for key, label in self.columns:
            table.add_column(label, key=key)
        self._refresh_table()
        table.focus()

    def _filtered_records(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        q = self.search_query.strip().lower()
        for rec in self.records:
            if self.abnormal_only and not is_abnormal(rec):
                continue
            if self.group_key and self.selected_category != ALL_KEY:
                val = str(rec.get(self.group_key, "")).strip() or "(未分類)"
                if val != self.selected_category:
                    continue
            if q:
                haystack = " ".join(str(v) for v in rec.values()).lower()
                if q not in haystack:
                    continue
            result.append(rec)
        return result

    def _refresh_table(self) -> None:
        table = self.query_one("#lab_table", DataTable)
        table.clear(columns=False)

        filtered = self._filtered_records()
        total_abnormal = sum(1 for r in self.records if is_abnormal(r))
        shown_abnormal = sum(1 for r in filtered if is_abnormal(r))

        for rec in filtered:
            f_str = str(rec.get("flag", "")).strip()
            style = flag_style(f_str)
            row_cells: list[Any] = []
            for key, _ in self.columns:
                raw = str(rec.get(key, ""))
                if style and key in ("item", "value", "flag"):
                    row_cells.append(Text(raw, style=style))
                elif key == "value":
                    row_cells.append(Text(raw, style="bold"))
                else:
                    row_cells.append(raw)
            table.add_row(*row_cells)

        mode_tag = " [異常値のみ]" if self.abnormal_only else ""
        cat_tag = (
            f" | 区分: {self.selected_category}"
            if self.selected_category != ALL_KEY
            else ""
        )
        status = self.query_one("#status_bar", Static)
        status.update(
            f"表示: {len(filtered)}/{len(self.records)}件 "
            f"(異常値: {shown_abnormal}/{total_abnormal}件){cat_tag}{mode_tag}"
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "category_select" and event.value is not None:
            self.selected_category = str(event.value)
            self._refresh_table()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search_input":
            self.search_query = event.value
            self._refresh_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search_input":
            self.action_focus_table()

    def action_toggle_abnormal(self) -> None:
        self.abnormal_only = not self.abnormal_only
        self._refresh_table()

    def action_cycle_category(self) -> None:
        if not self.categories:
            return
        all_values = [ALL_KEY] + self.categories
        idx = all_values.index(self.selected_category) if self.selected_category in all_values else 0
        next_val = all_values[(idx + 1) % len(all_values)]
        self.selected_category = next_val
        self.query_one("#category_select", Select).value = next_val
        self._refresh_table()

    def action_focus_search(self) -> None:
        self.query_one("#search_input", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one("#lab_table", DataTable).focus()

    def action_cursor_down(self) -> None:
        self.query_one("#lab_table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#lab_table", DataTable).action_cursor_up()


def _reopen_stdin_tty() -> None:
    """パイプ入力後に Textual がキー入力を受け取れるよう /dev/tty を stdin に再接続する。"""
    if sys.stdin.isatty():
        return
    try:
        tty_fd = os.open("/dev/tty", os.O_RDONLY)
        os.dup2(tty_fd, 0)
        os.close(tty_fd)
        sys.stdin = open(0, encoding="utf-8", closefd=False)
    except OSError as e:
        print(f"警告: /dev/tty の再接続に失敗しました: {e}", file=sys.stderr)


def main() -> int:
    if len(sys.argv) >= 2:
        path = Path(sys.argv[1]).expanduser()
        if not path.exists():
            print(f"エラー: ファイルが見つかりません: {path}", file=sys.stderr)
            return 1
        raw_text = path.read_text(encoding="utf-8")
        source_label = path.name
    elif not sys.stdin.isatty():
        raw_text = sys.stdin.read()
        source_label = "stdin (pipe)"
        _reopen_stdin_tty()
    else:
        print("使い方: python viewer_tui.py <result.jsonl> またはパイプでJSONLを渡してください。", file=sys.stderr)
        return 1

    records = load_jsonl_records(raw_text)
    if not records:
        print("表示できる JSONL レコードが見つかりませんでした。", file=sys.stderr)
        return 1

    app = LabLensViewer(records=records, source_label=source_label)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
