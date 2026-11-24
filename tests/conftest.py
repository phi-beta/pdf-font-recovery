"""
Shared test helpers: building real fonts.

A font builder needs actual ``Glyph`` objects, not dicts - ``setupGlyf``
reads ``numberOfContours`` off each one.  These helpers draw the outlines
with a pen so the test fonts have real geometry, which is what the ink and
placeholder checks read.
"""
from __future__ import annotations

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen


def box_glyph(x_min: int, y_min: int, x_max: int, y_max: int):
    """A closed rectangular contour."""
    pen = TTGlyphPen(None)
    pen.moveTo((x_min, y_min))
    pen.lineTo((x_min, y_max))
    pen.lineTo((x_max, y_max))
    pen.lineTo((x_max, y_min))
    pen.closePath()
    return pen.glyph()


def empty_glyph():
    """A glyph with no outline at all - what a PDF subsetter leaves behind.

    Note that a DEGENERATE box - a contour with zero width and height -
    is NOT empty.  It has a contour, so ``numberOfContours`` is 1 and the
    glyph reads as having ink.  A blank glyph has no contour at all.
    """
    pen = TTGlyphPen(None)
    return pen.glyph()


def build_font(path: str, glyphs: dict, cmap: dict[int, str], *, family: str = "Test") -> None:
    """Build a TrueType font at ``path``.

    ``glyphs`` maps glyph name -> Glyph object; ``cmap`` maps code point ->
    glyph name.
    """
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", *glyphs.keys()])
    builder.setupCharacterMap(cmap)
    builder.setupGlyf({".notdef": empty_glyph(), **glyphs})
    builder.setupHorizontalMetrics(
        {name: (500, 0) for name in [".notdef", *glyphs.keys()]}
    )
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWeightClass=400)
    builder.setupNameTable(
        {
            "familyName": family,
            "styleName": "Regular",
            "uniqueFontIdentifier": f"{family}-Regular",
            "fullName": f"{family} Regular",
            "psName": f"{family}-Regular",
            "version": "Version 1.000",
        },
        windows=True,
        mac=True,
    )
    builder.setupPost()
    builder.setupMaxp()
    builder.save(path)
