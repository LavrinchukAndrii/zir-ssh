"""Symbols for the classic Windows console, whose fonts lack Textual's border glyphs."""

from zir import glyphs


def test_plain_replaces_what_the_console_font_lacks():
    assert glyphs.plain("📋 Snippets", limited=True) == "Snippets"
    assert glyphs.plain("✓ all good", limited=True) == "√ all good"
    assert glyphs.plain("✕ Close", limited=True) == "× Close"
    assert glyphs.plain("Мої сніпети ✎", limited=True) == "Мої сніпети "
    assert glyphs.plain("📋 Snippets", limited=False) == "📋 Snippets"


def test_adapted_borders_use_only_console_glyphs(monkeypatch):
    from textual import _border

    original = dict(_border.BORDER_CHARS)
    monkeypatch.setattr(glyphs, "LIMITED", True)
    try:
        glyphs.adapt_textual()
        used = {c for rows in _border.BORDER_CHARS.values() for row in rows for c in row}
        assert not used & set(glyphs.BORDER_PIECES)
        from textual.style import Style

        top = _border.get_box("tall", Style(), Style(), Style())[0]  # the cache was cleared
        assert "▔" not in "".join(segment.text for segment in top)
    finally:
        _border.BORDER_CHARS.clear()
        _border.BORDER_CHARS.update(original)
        _border.get_box.cache_clear()
