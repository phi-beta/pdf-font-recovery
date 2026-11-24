"""
Tests for the verification layer.

The ink check is the one that earns its keep.  A font can carry a cmap
entry for a character whose outline is EMPTY - exactly what a PDF subsetter
produces for a character the document never set - and every ordinary font
check passes it.  In the source corpus this hid 604 characters that looked
present and rendered as nothing.

The placeholder check catches the other direction: a character whose
outline is a shared missing-glyph box.  A placeholder is worse than a blank,
because it renders as a box with a letter in it, and copying one across is
how a broken font spreads.
"""
from __future__ import annotations

import pytest
from conftest import box_glyph, build_font, empty_glyph

from pdf_font_recovery import verify


def test_a_blank_glyph_is_reported(tmp_path):
    """A cmap entry with an empty outline is a blank, not a present character."""
    path = str(tmp_path / "blank.ttf")
    # "A" has a real outline; "B" has none.
    build_font(path, {"A": box_glyph(0, 0, 500, 700), "B": empty_glyph()}, {65: "A", 66: "B"})

    result = verify.check_font(path, "AB")
    assert "A" in result.ok
    assert "B" in result.blank
    assert result.blank == ["B"]


def test_a_character_with_no_cmap_entry_is_missing(tmp_path):
    path = str(tmp_path / "missing.ttf")
    build_font(path, {"A": box_glyph(0, 0, 500, 700)}, {65: "A"})

    result = verify.check_font(path, "AB")
    assert "A" in result.ok
    assert "B" in result.missing


def test_a_shared_box_is_a_placeholder(tmp_path):
    """Several characters with the SAME bounding box are placeholders."""
    path = str(tmp_path / "placeholder.ttf")
    # Four digits share one box; "A" has its own.
    build_font(
        path,
        {
            "A": box_glyph(0, 0, 500, 700),
            "zero": box_glyph(100, 100, 400, 600),
            "one": box_glyph(100, 100, 400, 600),
            "two": box_glyph(100, 100, 400, 600),
            "three": box_glyph(100, 100, 400, 600),
        },
        {65: "A", 48: "zero", 49: "one", 50: "two", 51: "three"},
    )

    result = verify.check_font(path, "A0123")
    assert set(result.placeholders) == {"0", "1", "2", "3"}
    # The underlying helper reports code points; the check reports characters.
    from fontTools.ttLib import TTFont

    from pdf_font_recovery.verify import find_placeholder_characters

    assert find_placeholder_characters(TTFont(path)) == {48, 49, 50, 51}


def test_a_single_shared_box_is_not_a_placeholder(tmp_path):
    """One character with a unique box is a letterform, not a placeholder."""
    path = str(tmp_path / "unique.ttf")
    build_font(
        path,
        {"A": box_glyph(0, 0, 500, 700), "B": box_glyph(0, 0, 300, 400)},
        {65: "A", 66: "B"},
    )

    result = verify.check_font(path, "AB")
    assert result.placeholders == []


def test_a_space_is_not_a_blank(tmp_path):
    """A space is legitimately empty, so it must not be reported as a blank.

    Whitespace glyphs have no outline by definition.  Testing them for ink
    would report EVERY font as having a blank character, which is how the
    coverage gate would end up permanently red for a reason that is not a
    defect.
    """
    path = str(tmp_path / "space.ttf")
    build_font(path, {"space": empty_glyph(), "A": box_glyph(0, 0, 500, 700)}, {32: "space", 65: "A"})

    result = verify.check_font(path, " A")
    assert result.blank == []
    assert " " in result.ok


def test_a_font_that_will_not_open_is_a_problem(tmp_path):
    path = str(tmp_path / "broken.ttf")
    with open(path, "wb") as fh:
        fh.write(b"not a font")

    result = verify.check_font(path, "A")
    assert result.problems
    assert "will not open" in result.problems[0]


def test_summarise_counts_the_totals(tmp_path):
    path = str(tmp_path / "a.ttf")
    build_font(path, {"A": box_glyph(0, 0, 500, 700)}, {65: "A"})

    checks = [verify.check_font(path, "AB")]
    summary = verify.summarise(checks)
    assert summary["fonts"] == 1
    assert summary["total_blank"] == 0
    assert summary["total_missing"] == 1


def test_check_directory_finds_every_font(tmp_path):
    build_font(str(tmp_path / "a.ttf"), {"A": box_glyph(0, 0, 500, 700)}, {65: "A"})
    build_font(str(tmp_path / "b.otf"), {"A": box_glyph(0, 0, 500, 700)}, {65: "A"})
    (tmp_path / "notes.txt").write_text("not a font")

    checks = verify.check_directory(str(tmp_path), "A")
    assert len(checks) == 2
    assert [c.name for c in checks] == ["a.ttf", "b.otf"]


def test_format_report_flags_blanks(tmp_path):
    path = str(tmp_path / "a.ttf")
    build_font(path, {"A": box_glyph(0, 0, 500, 700), "B": empty_glyph()}, {65: "A", 66: "B"})

    report = verify.format_report([verify.check_font(path, "AB")])
    assert "BLANK OUTLINES" in report


@pytest.mark.parametrize("char", ["A", "Z", "a", "z", "0", "9"])
def test_common_characters_rasterise(tmp_path, char):
    """A well-formed glyph produces ink for every common character."""
    path = str(tmp_path / "a.ttf")
    build_font(path, {"X": box_glyph(0, 0, 500, 700)}, {ord(char): "X"})

    result = verify.check_font(path, char)
    assert result.blank == []
    assert result.ok == [char]
