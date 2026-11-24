"""
Tests for the CFF cross-weight borrow.

The borrow copies a character from one weight of a family into another
weight that lacks it.  It works on charstrings, which is delicate in four
specific ways - each of which is a way to produce a font that loads and
renders the WRONG shape:

  * a donor charstring's subroutine indices refer to the donor's private
    dictionary, so the donor must be desubroutinized
  * the private dict and global subrs live on the TOP DICT, so they must
    be set on every copied charstring
  * the cmap must be EXTENDED, not rebuilt from the glyph names - a
    rebuild silently drops every character whose name does not round-trip
  * a borrowed glyph must be in the glyph ORDER, or getGlyphID cannot
    resolve it and the compile raises a KeyError naming the glyph

The fixture builds two CFF fonts of one family with different characters.
"""
from __future__ import annotations

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.ttLib import TTFont

from pdf_font_recovery import cff_borrow


def _cff_font(path: str, characters: dict[str, int]) -> None:
    """Build a small CFF font with the given characters.

    ``characters`` maps a character to its width in design units.
    """
    builder = FontBuilder(1000, isTTF=False)

    glyph_order = [".notdef"] + sorted(characters)
    builder.setupGlyphOrder(glyph_order)

    charstrings = {}
    for name in glyph_order:
        pen = T2CharStringPen(width=characters.get(name, 500), glyphSet=None)
        pen.moveTo((50, 0))
        pen.lineTo((50, 700))
        pen.lineTo((450, 700))
        pen.lineTo((450, 0))
        pen.closePath()
        charstrings[name] = pen.getCharString()

    builder.setupCFF(
        "Test-Regular",
        {"FullName": "Test Regular"},
        charstrings,
        {},
    )
    builder.setupCharacterMap({ord(char): char for char in characters})
    builder.setupHorizontalMetrics({name: (characters.get(name, 500), 0) for name in glyph_order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWeightClass=400)
    builder.setupNameTable(
        {
            "familyName": "Test",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Test-Regular",
            "fullName": "Test Regular",
            "psName": "Test-Regular",
            "version": "Version 1.000",
        },
        windows=True,
        mac=True,
    )
    builder.setupPost()
    builder.setupMaxp()
    builder.save(path)


def test_a_missing_character_is_borrowed(tmp_path):
    """A weight that lacks a character gets it from a sibling weight."""
    regular = str(tmp_path / "regular.otf")
    bold = str(tmp_path / "bold.otf")
    _cff_font(regular, {"A": 600, "B": 600})
    _cff_font(bold, {"A": 600})

    outcome = cff_borrow.borrow_characters(bold, [regular])

    assert outcome["errors"] == [], f"the borrow reported errors: {outcome['errors']}"
    assert outcome["borrowed"] >= 1, "B should have been borrowed"

    font = TTFont(bold)
    cmap = font.getBestCmap()
    assert 66 in cmap, "B should now be mapped in the bold weight"
    assert "borrow" in cmap[66], "a borrowed glyph should be marked as one"


def test_the_borrow_keeps_the_characters_the_font_already_had(tmp_path):
    """The existing cmap is EXTENDED, never rebuilt.

    A rebuild from the glyph names drops every character whose name does
    not round-trip - a font goes from 73 mapped characters to 49 with no
    error anywhere.
    """
    regular = str(tmp_path / "regular.otf")
    bold = str(tmp_path / "bold.otf")
    _cff_font(regular, {"A": 600, "B": 600, "C": 600})
    _cff_font(bold, {"A": 600, "B": 600})

    before = len(TTFont(bold).getBestCmap())
    cff_borrow.borrow_characters(bold, [regular])
    after = len(TTFont(bold).getBestCmap())

    assert after >= before, "the borrow must not lose characters"


def test_a_borrowed_glyph_is_in_the_glyph_order(tmp_path):
    """A borrowed glyph must be resolvable by getGlyphID.

    getGlyphID resolves through the glyph ORDER.  A borrowed glyph that
    is not in the order is invisible: the cmap points at it and the
    lookup raises a KeyError naming the glyph rather than the cause.
    """
    regular = str(tmp_path / "regular.otf")
    bold = str(tmp_path / "bold.otf")
    _cff_font(regular, {"A": 600, "B": 600})
    _cff_font(bold, {"A": 600})

    cff_borrow.borrow_characters(bold, [regular])

    font = TTFont(bold)
    cmap = font.getBestCmap()
    for name in cmap.values():
        # getGlyphID is what the rasteriser and the compile both use.
        assert font.getGlyphID(name) >= 0, f"{name} is not resolvable"


def test_a_borrowed_glyph_has_metrics(tmp_path):
    """A borrowed glyph needs a horizontal-metrics entry of its own.

    The maxp recalc walks every glyph in the order and looks each one up
    in hmtx, so a borrowed glyph without an entry fails the compile.
    """
    regular = str(tmp_path / "regular.otf")
    bold = str(tmp_path / "bold.otf")
    _cff_font(regular, {"A": 600, "B": 640})
    _cff_font(bold, {"A": 600})

    cff_borrow.borrow_characters(bold, [regular])

    font = TTFont(bold)
    hmtx = font["hmtx"]
    for name in font.getGlyphOrder():
        assert name in hmtx.metrics, f"{name} has no metrics entry"
        assert hmtx.metrics[name][0] > 0, f"{name} has a zero advance"


def test_the_borrow_is_deterministic(tmp_path):
    """Two borrows over the same inputs produce the same result."""
    import shutil

    regular = str(tmp_path / "regular.otf")
    bold = str(tmp_path / "bold.otf")
    _cff_font(regular, {"A": 600, "B": 600, "C": 600})
    _cff_font(bold, {"A": 600, "B": 600})

    first = str(tmp_path / "first.otf")
    second = str(tmp_path / "second.otf")
    shutil.copy(bold, first)
    shutil.copy(bold, second)

    outcome_one = cff_borrow.borrow_characters(first, [regular])
    outcome_two = cff_borrow.borrow_characters(second, [regular])

    assert outcome_one["borrowed"] == outcome_two["borrowed"]
    assert outcome_one["overlay"] == outcome_two["overlay"]


@pytest.mark.parametrize("name", ["A"])
def test_the_module_exposes_its_helpers(name):
    """The module's small helpers are part of its contract."""
    assert cff_borrow._unicode_of("A") == "A"
    assert cff_borrow._unicode_of("uni0041") == "A"
    assert cff_borrow._unicode_of("uni0041.borrow") == "A"
    assert cff_borrow._unicode_of(".notdef") is None
    assert cff_borrow._char_key("AB") == "A"
