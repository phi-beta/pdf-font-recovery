"""
Tests for the advance widths of a repaired font.

A glyph's advance width is how far the pen moves before the next glyph is
drawn.  If a copied glyph gets the WRONG advance, the font still loads,
still has a valid name table and still passes every structural check - and
the text renders with the letters on top of each other.

This is the defect that a font checker cannot see, because nothing about
the font is malformed.  Only the rendering is wrong.

The rule: a glyph copied from another font must carry that font's advance
width.  A width of zero is the specific failure - it makes the pen stand
still, so the next glyph is drawn on top of this one.
"""
from __future__ import annotations

import pytest
from fontTools.ttLib import TTFont

from pdf_font_recovery import repair
from tests.conftest import box_glyph, build_font, empty_glyph


def _widths(path):
    font = TTFont(path)
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    return {char: hmtx[cmap[ord(char)]][0] for char in "ABC" if ord(char) in cmap}


def test_a_copied_glyph_keeps_the_source_width(tmp_path):
    """The width must come from the source font, not be defaulted to zero."""
    source = str(tmp_path / "source.ttf")
    target = str(tmp_path / "target.ttf")

    # The source has A and B with distinct, non-zero widths.
    build_font(
        source,
        {
            "A": box_glyph(0, 0, 500, 700),
            "B": box_glyph(0, 0, 500, 700),
        },
        {65: "A", 66: "B"},
    )
    source_font = TTFont(source)
    source_font["hmtx"].metrics["A"] = (600, 0)
    source_font["hmtx"].metrics["B"] = (700, 0)
    source_font.save(source)

    # The target has only A, with a blank outline, and no B at all.
    build_font(target, {"A": empty_glyph()}, {65: "A"})

    repair.repair_style(
        target,
        "Test-Regular",
        sibling_paths=[source],
        test_characters="AB",
    )

    widths = _widths(target)
    assert widths["A"] == 600, "A's width must be the source's width, not zero"
    assert widths.get("B") == 700, "B's width must be the source's width, not zero"


def test_a_glyph_absent_from_the_target_gets_the_source_width(tmp_path):
    """A glyph that is NOT in the target at all is copied, with its width.

    The copied glyph is given a generated name, and the width must travel
    with it.  A zero width here is what makes the letter sit on top of
    its neighbour.
    """
    source = str(tmp_path / "source.ttf")
    target = str(tmp_path / "target.ttf")

    build_font(
        source,
        {
            "A": box_glyph(0, 0, 500, 700),
            "B": box_glyph(0, 0, 500, 700),
        },
        {65: "A", 66: "B"},
    )
    source_font = TTFont(source)
    source_font["hmtx"].metrics["A"] = (600, 0)
    source_font["hmtx"].metrics["B"] = (700, 0)
    source_font.save(source)

    build_font(target, {"A": empty_glyph()}, {65: "A"})

    repair.repair_style(target, "Test-Regular", sibling_paths=[source], test_characters="AB")

    font = TTFont(target)
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    assert 66 in cmap, "B should have been copied in"
    assert hmtx[cmap[66]][0] == 700, "the copied glyph must carry the source's width"


def test_no_glyph_ends_up_with_a_zero_advance(tmp_path):
    """No mapped glyph may have an advance of zero.

    A zero advance is the signature of the defect: the glyph is drawn but
    the pen never moves, so it lands on top of its neighbour.
    """
    source = str(tmp_path / "source.ttf")
    target = str(tmp_path / "target.ttf")

    build_font(
        source,
        {
            "A": box_glyph(0, 0, 500, 700),
            "B": box_glyph(0, 0, 500, 700),
        },
        {65: "A", 66: "B"},
    )
    build_font(target, {"A": empty_glyph()}, {65: "A"})

    repair.repair_style(target, "Test-Regular", sibling_paths=[source], test_characters="AB")

    font = TTFont(target)
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    zeros = [
        char
        for char in "AB"
        if ord(char) in cmap and hmtx[cmap[ord(char)]][0] == 0
    ]
    assert zeros == [], f"these glyphs have a zero advance: {zeros}"


def test_an_external_font_supplies_the_width_too(tmp_path):
    """A glyph filled from an external source keeps the EXTERNAL width.

    The external font's metrics are the right ones - it is a different
    face, but it is the same typeface at the same weight, so its advances
    are the correct ones for the characters it supplies.
    """
    external = str(tmp_path / "external.ttf")
    target = str(tmp_path / "target.ttf")

    build_font(
        external,
        {
            "A": box_glyph(0, 0, 500, 700),
        },
        {65: "A"},
    )
    external_font = TTFont(external)
    external_font["hmtx"].metrics["A"] = (640, 0)
    external_font.save(external)

    build_font(target, {"A": empty_glyph()}, {65: "A"})

    repair.repair_style(
        target,
        "Test-Regular",
        external_path=external,
        external_exact_weight=True,
        test_characters="A",
    )

    font = TTFont(target)
    width = font["hmtx"].metrics[font.getBestCmap()[65]][0]
    assert width == 640, f"the external width should be used, got {width}"


@pytest.mark.parametrize("style", ["Test-Regular"])
def test_the_repaired_font_still_renders(tmp_path, style):
    """The end-to-end check: a repaired font renders its characters."""
    from PIL import Image, ImageDraw, ImageFont

    source = str(tmp_path / "source.ttf")
    target = str(tmp_path / "target.ttf")
    build_font(
        source,
        {"A": box_glyph(50, 0, 450, 700)},
        {65: "A"},
    )
    build_font(target, {"A": empty_glyph()}, {65: "A"})

    repair.repair_style(target, style, sibling_paths=[source], test_characters="A")

    pil_font = ImageFont.truetype(target, 48)
    image = Image.new("L", (100, 80), 255)
    ImageDraw.Draw(image).text((10, 10), "A", font=pil_font, fill=0)
    bbox = image.point(lambda v: 255 - v).getbbox()
    assert bbox is not None, "A should render ink after the repair"
    assert bbox[2] - bbox[0] > 10, "A should render with a plausible width"
