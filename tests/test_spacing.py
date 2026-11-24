"""
Tests for the letter-spacing gate.

The gate has to do two things at once, and the second is harder than the
first:

  1.  Catch a font whose advances are broken - a zero advance, a letter
      collapsed relative to its own font's siblings.

  2.  NOT flag a font whose typeface is simply tight.  Italics lean into
      their neighbours on purpose, so a gate that treats "advance
      narrower than ink" as a defect would reject every good italic.

The second requirement is why the gate compares against a REFERENCE
font: the recovered font is measured against the publisher's own file
for the same weight, and a mismatch is the defect.

Tests are built on the shared fixtures in conftest.py.
"""
from __future__ import annotations

import pytest
from fontTools.ttLib import TTFont

from pdf_font_recovery import spacing
from tests.conftest import build_font, empty_glyph, varied_box_glyph


def _font_with_widths(path: str, widths: dict[str, int]) -> None:
    """A font whose glyphs have the given advance widths.

    Every glyph is mapped to its own code point, so the spacing check can
    measure each of them.
    """
    names = list(widths)
    build_font(
        path,
        {name: varied_box_glyph(i) for i, name in enumerate(names)},
        {ord(name): name for name in names},
    )
    font = TTFont(path)
    for name, width in widths.items():
        font["hmtx"].metrics[name] = (width, 0)
    font["head"].unitsPerEm = 1000
    font.save(path)
    font.close()


def test_a_zero_advance_is_a_defect(tmp_path):
    """The defect this gate exists to catch.

    A glyph with a zero advance is drawn ON TOP of its neighbour: the
    pen does not move.  The font still loads, still has a valid name
    table, and passes every structural check.
    """
    path = str(tmp_path / "broken.ttf")
    widths = {"A": 600, "B": 600, "C": 600, "D": 600, "E": 600}
    widths["B"] = 0  # the placeholder that never got filled in
    _font_with_widths(path, widths)

    check = spacing.check_spacing(path)

    assert "B" in check.zero_advance
    assert not check.ok
    assert any("ZERO advance" in p for p in check.problems)


def test_a_sound_font_passes(tmp_path):
    path = str(tmp_path / "sound.ttf")
    widths = {chr(ord("A") + i): 600 for i in range(26)}
    _font_with_widths(path, widths)

    check = spacing.check_spacing(path)

    assert check.zero_advance == []
    assert check.too_narrow == []
    assert check.ok, check.problems


def test_a_tight_but_consistent_font_is_not_flagged(tmp_path):
    """A typeface that is tight THROUGHOUT is a design decision.

    Every glyph advances less than its own ink - that is what a tight
    typeface looks like.  The gate must not call that a defect, because
    the relative check is what makes the gate usable on any typeface.
    """
    path = str(tmp_path / "tight.ttf")
    # The ink is 500 wide but the advance is 400: narrower than the ink,
    # for every single letter.
    letters = [chr(ord("A") + i) for i in range(26)]
    build_font(
        path,
        {name: varied_box_glyph(i) for i, name in enumerate(letters)},
        {ord(name): name for name in letters},
    )
    font = TTFont(path)
    for name in list(font["hmtx"].metrics):
        font["hmtx"].metrics[name] = (400, 0)
    font["head"].unitsPerEm = 1000
    font.save(path)
    font.close()

    check = spacing.check_spacing(path)

    assert check.too_narrow == [], "a tight-but-consistent font is not a defect"
    assert check.ok, check.problems


def test_one_collapsed_letter_among_good_ones_is_flagged(tmp_path):
    """The relative check catches a single bad letter.

    This is the case the absolute check cannot see: the font's other 25
    letters are perfect, and one has an advance of 60 against an ink of
    500.
    """
    path = str(tmp_path / "one-bad.ttf")
    widths = {chr(ord("A") + i): 600 for i in range(26)}
    widths["M"] = 60
    _font_with_widths(path, widths)

    check = spacing.check_spacing(path)

    assert "M" in check.inconsistent
    assert not check.ok


def test_the_reference_comparison_confirms_a_good_recovery(tmp_path):
    """A recovered font that matches its source is clean."""
    recovered = str(tmp_path / "recovered.ttf")
    source = str(tmp_path / "source.ttf")
    widths = {chr(ord("A") + i): 600 for i in range(26)}
    _font_with_widths(recovered, widths)
    _font_with_widths(source, widths)

    check = spacing.check_spacing(recovered, source)

    assert check.compared > 0
    assert check.mismatched == []
    assert check.ok, check.problems


def test_the_reference_comparison_catches_a_wrong_width(tmp_path):
    """A recovered font that disagrees with its source is caught.

    Every glyph advances 600 against the source's 600 - except one,
    which advances 60.  The font is structurally perfect; only the
    comparison with the source reveals it.
    """
    recovered = str(tmp_path / "recovered.ttf")
    source = str(tmp_path / "source.ttf")
    good = {chr(ord("A") + i): 600 for i in range(26)}
    _font_with_widths(recovered, {**good, "M": 60})
    _font_with_widths(source, good)

    check = spacing.check_spacing(recovered, source)

    assert "M" in check.mismatched
    assert not check.ok


def test_a_systematic_shift_from_the_reference_is_flagged(tmp_path):
    """If EVERY width is wrong by the same factor, that is still a defect.

    A per-character check would see every character at the same ratio
    and pass.  The median comparison catches the systematic case.
    """
    recovered = str(tmp_path / "recovered.ttf")
    source = str(tmp_path / "source.ttf")
    good = {chr(ord("A") + i): 600 for i in range(26)}
    _font_with_widths(recovered, {k: int(v * 0.6) for k, v in good.items()})
    _font_with_widths(source, good)

    check = spacing.check_spacing(recovered, source)

    assert not check.ok
    assert any("wrong source" in p for p in check.problems)


def test_a_placeholder_in_the_reference_is_excluded(tmp_path):
    """A reference's own defect must not condemn the recovered font.

    The Altone Trial's ten digits are all one placeholder box, so its
    advances for those characters are the advance of a box, not of a
    figure.  Comparing against it would flag the recovered font's
    CORRECT figures as wrong.  So those characters are excluded - and
    the exclusion is reported, because a comparison that silently drops
    characters cannot be trusted.
    """
    recovered = str(tmp_path / "recovered.ttf")
    source = str(tmp_path / "source.ttf")

    # The source's digit glyphs are all the SAME empty box.
    names = [chr(ord("A") + i) for i in range(26)] + ["0", "1"]
    build_font(
        source,
        {name: empty_glyph() for name in names},
        {ord(name): name for name in names},
    )
    font = TTFont(source)
    for name in names:
        font["hmtx"].metrics[name] = (600, 0)
    font["head"].unitsPerEm = 1000
    font.save(source)
    font.close()

    # The recovered font has real, correct digits.
    recovered_names = [chr(ord("A") + i) for i in range(26)] + ["0", "1"]
    build_font(
        recovered,
        {name: varied_box_glyph(i) for i, name in enumerate(recovered_names)},
        {ord(name): name for name in recovered_names},
    )
    font = TTFont(recovered)
    for name in recovered_names:
        font["hmtx"].metrics[name] = (600, 0)
    font["head"].unitsPerEm = 1000
    font.save(recovered)
    font.close()

    check = spacing.check_spacing(recovered, source)

    # The digits were excluded rather than counted as mismatches.
    assert "0" in check.excluded
    assert "0" not in check.mismatched
    assert "1" not in check.mismatched


def test_a_missing_reference_is_reported_not_ignored(tmp_path):
    """A reference that is not there must be a problem, not a pass."""
    path = str(tmp_path / "font.ttf")
    widths = {chr(ord("A") + i): 600 for i in range(26)}
    _font_with_widths(path, widths)

    check = spacing.check_spacing(path, str(tmp_path / "nope.ttf"))

    assert not check.ok
    assert any("missing" in p for p in check.problems)


def test_check_directory_finds_every_font(tmp_path):
    directory = str(tmp_path)
    for name in ("a.ttf", "b.otf"):
        widths = {chr(ord("A") + i): 600 for i in range(26)}
        _font_with_widths(str(tmp_path / name), widths)
    # A file that is not a font must be ignored, not crash the sweep.
    (tmp_path / "notes.txt").write_text("not a font", encoding="utf-8")

    checks = spacing.check_directory(directory)

    assert [c.name for c in checks] == ["a.ttf", "b.otf"]
    assert all(c.ok for c in checks), [c.problems for c in checks]


def test_the_summary_counts_the_defects(tmp_path):
    good = str(tmp_path / "good.ttf")
    bad = str(tmp_path / "bad.ttf")
    _font_with_widths(good, {chr(ord("A") + i): 600 for i in range(26)})
    widths = {chr(ord("A") + i): 600 for i in range(26)}
    widths["A"] = 0
    _font_with_widths(bad, widths)

    checks = spacing.check_directory(str(tmp_path))
    summary = spacing.summarise(checks)

    assert summary["fonts"] == 2
    assert summary["zero_advance"] == 1
    assert summary["fonts_with_problems"] == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
