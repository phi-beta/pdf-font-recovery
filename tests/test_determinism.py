"""
Tests for determinism.

The property: two runs over the same input produce BYTE-IDENTICAL output.
This is what makes the tool usable in CI and in a version-controlled
artefact - a font library whose bytes change on every run cannot be
diffed, and a change in the bytes cannot be reviewed.

The defect this test exists to catch: fontTools stamps head.created and
head.modified with the current time on save, and the ``checkSumAdjustment``
field cascades from them.  Two runs over identical input then differ in
8 bytes per font even though every glyph is identical.  The fix is to pin
the timestamps and turn off the timestamp recalculation.

The test therefore asserts the property directly - it does not assert that
a particular timestamp was used, so it stays honest if the timestamp
policy changes.
"""
from __future__ import annotations

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from pdf_font_recovery import extract, pipeline


def _outline():
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((50, 700))
    pen.lineTo((500, 700))
    pen.lineTo((500, 0))
    pen.closePath()
    return pen.glyph()


def _make_font(path: str) -> None:
    """Write a small but complete font for the test."""
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "space", "A", "B"])
    builder.setupCharacterMap({32: "space", 65: "A", 66: "B"})
    outline = _outline()
    builder.setupGlyf({".notdef": outline, "space": outline, "A": outline, "B": outline})
    builder.setupHorizontalMetrics(
        {".notdef": (500, 0), "space": (250, 0), "A": (600, 0), "B": (600, 0)}
    )
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWeightClass=400)
    builder.setupNameTable(
        {
            "familyName": "Test",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Test-Regular;1.000",
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


def _bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def test_pinned_timestamps_produce_identical_bytes(tmp_path):
    """Two saves of the same font with pinned timestamps are byte-identical."""
    first = str(tmp_path / "a.ttf")
    second = str(tmp_path / "b.ttf")

    _make_font(first)
    font = TTFont(first)
    extract.pin_timestamps(font, 3600000000)
    font.save(first)

    _make_font(second)
    font = TTFont(second)
    extract.pin_timestamps(font, 3600000000)
    font.save(second)

    assert _bytes(first) == _bytes(second), "pinned timestamps must produce identical bytes"


def test_the_timestamp_is_embedded_in_the_bytes(tmp_path):
    """The control: the head timestamp is part of the file's bytes.

    This is what makes the pinning tests meaningful.  Two fonts built with
    DIFFERENT pinned timestamps differ in their bytes, so the timestamp
    really does contribute to the output and pinning it really does make
    the output reproducible.

    (Two builds in the SAME second are identical even without pinning,
    because fontTools stamps the head table with a second-resolution clock
    at build time.  That is why the reproducibility defect only shows up
    across runs minutes apart - and why a test that compares two quick
    builds would not have caught it.)
    """
    first = str(tmp_path / "a.ttf")
    second = str(tmp_path / "b.ttf")

    _make_font(first)
    font = TTFont(first)
    extract.pin_timestamps(font, 0x7C259DC0)
    font.save(first)

    _make_font(second)
    font = TTFont(second)
    extract.pin_timestamps(font, 0x7C259DC0 + 3600)
    font.save(second)

    assert _bytes(first) != _bytes(second), (
        "the head timestamp must contribute to the file's bytes, "
        "otherwise pinning it cannot make the output reproducible"
    )


def test_pinned_timestamp_is_recorded_in_the_font(tmp_path):
    # fontTools treats a head timestamp below 0x7C259DC0 (1 January 2084) as
    # a Macintosh epoch offset and shifts it on load, so a pinned value must
    # sit above that threshold to survive a round trip.
    #
    # The value used here sits a DAY above the boundary rather than ON it.
    # Whether "below" includes the boundary itself has changed between
    # fontTools releases, so a value exactly on the threshold survives on
    # one version and is shifted on the next - which is what the CI run
    # showed.
    timestamp = 0x7C259DC0 + 86400
    path = str(tmp_path / "a.ttf")
    _make_font(path)
    font = TTFont(path)
    extract.pin_timestamps(font, timestamp)
    font.save(path)

    reread = TTFont(path)
    assert reread["head"].created == timestamp
    assert reread["head"].modified == timestamp


def test_the_default_timestamp_survives_a_round_trip(tmp_path):
    """The tool's own default has to survive being written and read back."""
    path = str(tmp_path / "a.ttf")
    _make_font(path)
    font = TTFont(path)
    extract.pin_timestamps(font, pipeline.DEFAULT_REPRODUCIBLE_TIMESTAMP)
    font.save(path)

    reread = TTFont(path)
    assert reread["head"].created == pipeline.DEFAULT_REPRODUCIBLE_TIMESTAMP
    assert reread["head"].modified == pipeline.DEFAULT_REPRODUCIBLE_TIMESTAMP


def test_pinned_font_still_loads(tmp_path):
    """Pinning the timestamps must not break the font."""
    path = str(tmp_path / "a.ttf")
    _make_font(path)
    font = TTFont(path)
    extract.pin_timestamps(font, 3600000000)
    font.save(path)

    reread = TTFont(path)
    assert reread.getBestCmap()
    assert len(reread.getGlyphOrder()) == 4


def test_save_font_applies_the_pin(tmp_path):
    path = str(tmp_path / "a.ttf")
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A"])
    builder.setupCharacterMap({65: "A"})
    outline = _outline()
    builder.setupGlyf({".notdef": outline, "A": outline})
    builder.setupHorizontalMetrics({".notdef": (500, 0), "A": (600, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWeightClass=400)
    builder.setupNameTable(
        {
            "familyName": "T",
            "styleName": "Regular",
            "uniqueFontIdentifier": "T-Regular",
            "fullName": "T Regular",
            "psName": "T-Regular",
            "version": "Version 1.000",
        },
        windows=True,
        mac=True,
    )
    builder.setupPost()
    builder.setupMaxp()

    extract.save_font(builder, path, reproducible_timestamp=3600000000)
    assert TTFont(path)["head"].created == 3600000000


def test_write_ttf_with_a_pin_is_reproducible(tmp_path):
    """The TrueType path honours the pin too."""
    source = str(tmp_path / "src.ttf")
    _make_font(source)
    with open(source, "rb") as fh:
        data = fh.read()

    first = str(tmp_path / "a.ttf")
    second = str(tmp_path / "b.ttf")
    extract.write_ttf(data, first, reproducible_timestamp=3600000000)
    extract.write_ttf(data, second, reproducible_timestamp=3600000000)

    assert _bytes(first) == _bytes(second)
    assert TTFont(first)["head"].created == 3600000000
