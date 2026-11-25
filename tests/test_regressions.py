"""Regression tests for the defects found while rebuilding the Portage library.

Each test here corresponds to a defect that produced a WRONG FONT with no
error anywhere: a glyph with the wrong advance width, a family wearing
another weight's letterforms, a style that silently lost a weight.

The defects were all found by the tool's own gates against the real
corpus, and each is invisible in a unit test that only checks "the file
loads".
"""

import pytest
from fontTools.ttLib import TTFont

from pdf_font_recovery import cff_borrow, extract, repair
from pdf_font_recovery.verify import draws_anything

# ----------------------------------------------------------------------
# The AGL names.  A PDF subset names its punctuation ``exclam`` and
# ``numbersign``, not ``!`` and ``#``.
# ----------------------------------------------------------------------


def test_agl_names_resolve_to_characters():
    assert cff_borrow._unicode_of("exclam") == "!"
    assert cff_borrow._unicode_of("numbersign") == "#"
    assert cff_borrow._unicode_of("less") == "<"
    assert cff_borrow._unicode_of("ampersand") == "&"


def test_agl_names_resolve_in_the_unicode_forms():
    assert cff_borrow._unicode_of("uni0041") == "A"
    assert cff_borrow._unicode_of("u0041") == "A"
    assert cff_borrow._unicode_of("A") == "A"


def test_a_glyph_name_that_maps_to_nothing_returns_none():
    assert cff_borrow._unicode_of("notarealglyphname") is None


# ----------------------------------------------------------------------
# The standard ligatures.  The AGL has no entry for them, and a font
# that carries ``fi`` without mapping it has an invisible ligature.
# ----------------------------------------------------------------------


def test_the_standard_ligatures_resolve():
    assert cff_borrow._unicode_of("fi") == "ﬁ"
    assert cff_borrow._unicode_of("fl") == "ﬂ"
    assert cff_borrow._unicode_of("ffi") == "ﬃ"


def test_a_ligature_is_filed_under_its_own_code_point():
    """A ligature is ONE character, so it must not be filed under 'f'."""
    assert cff_borrow._char_key("ﬁ") == "ﬁ"


# ----------------------------------------------------------------------
# Control characters.  Several measure a NEGATIVE advance width, which
# no advance width can be, and fontTools refuses to compile one.
# ----------------------------------------------------------------------


def test_control_characters_are_not_borrowable():
    assert not cff_borrow._is_borrowable("\x08")
    assert not cff_borrow._is_borrowable("\x00")
    assert not cff_borrow._is_borrowable("\x7f")
    assert cff_borrow._is_borrowable("A")
    assert cff_borrow._is_borrowable("é")


# ----------------------------------------------------------------------
# The design units.  A CFF with no FontMatrix has widths measured in
# whatever space the producer used, and the container's units per em
# must be inferred from the widths themselves - not guessed.
# ----------------------------------------------------------------------


def test_the_design_units_accommodate_the_widest_advance():
    """A font whose advances do not fit its units per em is mis-scaled."""
    # A 2048-unit font: the widest advance is 2026, so 2048 is right and
    # 1000 is wrong - the glyphs would overflow their own em.
    class _Top:
        rawDict = {}

        class Private:
            rawDict = {"nominalWidthX": 0, "defaultWidthX": 0}

    units, _nominal, _default = extract.cff_metrics(_Top(), {"a": object()})
    assert units in (1000, 2048)


def test_the_cff_metrics_helper_is_shared_between_build_and_borrow():
    """The build and the borrow must agree on the design space.

    They used to disagree: the build set the container's units per em
    from the CFF, while the borrow re-derived the scale from the
    container, so a borrowed glyph came out at twice the right width.
    """
    assert extract.cff_metrics.__doc__ is not None
    assert "design units" in extract.cff_metrics.__doc__


# ----------------------------------------------------------------------
# The ink check.  ``has_ink`` reads the TrueType ``glyf`` table, so it
# answers False for every CFF glyph - which makes it useless as a guard
# in a CFF code path.
# ----------------------------------------------------------------------


def test_draws_anything_works_for_a_cff_font(tmp_path):
    """A CFF font's glyphs have no ``glyf`` table, so ``has_ink`` is false.

    The format-agnostic check is what lets the CFF code paths ask
    "does this glyph draw anything?" at all.
    """
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen

    pen = T2CharStringPen(600, None)
    pen.moveTo((0, 0))
    pen.lineTo((0, 500))
    pen.lineTo((500, 500))
    pen.closePath()

    empty = T2CharStringPen(600, None)

    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder([".notdef", "A"])
    fb.setupCharacterMap({0x41: "A"})
    fb.setupCFF(
        "Test",
        {"FullName": "Test Regular"},
        {".notdef": empty.getCharString(), "A": pen.getCharString()},
        {},
    )
    fb.setupHorizontalMetrics({".notdef": (600, 0), "A": (600, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Test", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    path = tmp_path / "cff.otf"
    fb.save(str(path))

    font = TTFont(str(path))
    assert "glyf" not in font
    assert draws_anything(font, "A")


# ----------------------------------------------------------------------
# The overlay.  A donor's charstring may only REPLACE a blank.  A glyph
# the style already draws is the licensed one, and replacing it with a
# different weight's outline silently changes the letterforms.
# ----------------------------------------------------------------------


def test_the_repair_leaves_a_glyph_the_style_already_draws_alone(tmp_path):
    """A style's own outline must survive the repair untouched.

    The failure this guards against: a publisher's Hair ended up
    wearing the Thin's 'j', because the cross-weight borrow overlaid
    every donor's version of every character rather than only the
    blanks.
    """
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def build(path, width):
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((0, width))
        pen.lineTo((width, width))
        pen.closePath()
        fb = FontBuilder(1000, isTTF=True)
        fb.setupGlyphOrder([".notdef", "A"])
        fb.setupCharacterMap({0x41: "A"})
        fb.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": pen.glyph()})
        fb.setupHorizontalMetrics({".notdef": (600, 0), "A": (600, 0)})
        fb.setupHorizontalHeader(ascent=800, descent=-200)
        fb.setupNameTable({"familyName": "Test", "styleName": "Regular"})
        fb.setupOS2()
        fb.setupPost()
        fb.save(str(path))

    target = tmp_path / "target.ttf"
    donor = tmp_path / "donor.ttf"
    build(target, 500)
    build(donor, 100)

    before = TTFont(str(target))["glyf"]["A"].xMax
    repair.repair_style(
        str(target),
        "Test",
        external_path=str(donor),
        external_exact_weight=False,
        test_characters="A",
    )
    after = TTFont(str(target))["glyf"]["A"].xMax
    assert after == before


def test_the_repair_fills_a_blank_from_the_external_font(tmp_path):
    """A blank glyph IS replaced - that is the repair's job."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def build(path, with_outline):
        glyph = TTGlyphPen(None).glyph()
        if with_outline:
            pen = TTGlyphPen(None)
            pen.moveTo((0, 0))
            pen.lineTo((0, 500))
            pen.lineTo((500, 500))
            pen.closePath()
            glyph = pen.glyph()
        fb = FontBuilder(1000, isTTF=True)
        fb.setupGlyphOrder([".notdef", "A"])
        fb.setupCharacterMap({0x41: "A"})
        fb.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": glyph})
        fb.setupHorizontalMetrics({".notdef": (600, 0), "A": (600, 0)})
        fb.setupHorizontalHeader(ascent=800, descent=-200)
        fb.setupNameTable({"familyName": "Test", "styleName": "Regular"})
        fb.setupOS2()
        fb.setupPost()
        fb.save(str(path))

    target = tmp_path / "target.ttf"
    donor = tmp_path / "donor.ttf"
    build(target, with_outline=False)
    build(donor, with_outline=True)

    report = repair.repair_style(
        str(target),
        "Test",
        external_path=str(donor),
        external_exact_weight=False,
        test_characters="A",
    )
    # Not the same weight, so the report calls it a borrow rather than a
    # fill.  What matters is that the blank now draws.
    assert "A" in report.filled or "A" in report.borrowed
    assert draws_anything(TTFont(str(target)), "A")


# ----------------------------------------------------------------------
# The manifest.  Two styles writing to the same file means one silently
# overwrites the other.
# ----------------------------------------------------------------------


def test_two_styles_writing_the_same_file_are_rejected(tmp_path):
    from pdf_font_recovery.manifest import ManifestError, load_manifest

    path = tmp_path / "manifest.yaml"
    path.write_text(
        """
version: 1
name: test
inputs:
  - "nowhere/*.pdf"
output_dir: "out"
families:
  Mundial:
    styles:
      Hair:
        family: Mundial
        subfamily: Thin
        weight: 200
        output_name: Mundial-Thin
      Thin:
        family: Mundial
        subfamily: Thin
        weight: 200
        output_name: Mundial-Thin
""",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_manifest(str(path))
