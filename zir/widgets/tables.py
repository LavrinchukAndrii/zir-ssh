"""Refilling DataTables only when their content really changed."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable


def cell_key(value) -> object:
    """Comparable form of a cell: Text equality ignores its base style (the colour)."""
    if isinstance(value, Text):
        return (value.plain, str(value.style), tuple(value.spans))
    return value


def fill(table: DataTable, rows: list[tuple]) -> None:
    """Replace all rows, unless they are the same as on screen. The cursor row and
    the scroll position stay where they were (clear() resets both)."""
    key = [tuple(cell_key(cell) for cell in row) for row in rows]
    if getattr(table, "_zir_rows", None) == key:
        return
    table._zir_rows = key
    cursor, x, y = table.cursor_row, table.scroll_x, table.scroll_y
    table.clear()
    for values in rows:
        table.add_row(*values)
    if rows:
        table.move_cursor(row=min(cursor, len(rows) - 1), animate=False)
    if x or y:
        # After the cursor scrolled into view, so the user's own scrolling wins.
        table.call_after_refresh(table.scroll_to, x, y, animate=False)
