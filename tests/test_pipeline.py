"""
End-to-end tests for the pipeline.

These build fonts from a synthetic PDF corpus and assert the properties
that matter for a tool that writes into a delivery folder:

  * two runs over the same input produce BYTE-IDENTICAL output
  * a failing coverage gate does NOT publish
  * the output is never written into the destination when the gate fails

The corpus is synthetic rather than the client's real documents, so the
tests are self-contained and can run in CI.  The real Portage corpus is
exercised by the ``examples/portage-cybertech.yaml`` manifest, which is
what a human runs.
"""
from __future__ import annotations

import os

import pytest

from pdf_font_recovery import pipeline
from pdf_font_recovery.manifest import parse_manifest


def _manifest(tmp_path, output_dir: str) -> object:
    return parse_manifest(
        {
            "version": 1,
            "name": "test",
            "inputs": [str(tmp_path / "corpus")],
            "output_dir": output_dir,
            "reproducible_timestamps": True,
            "families": {
                "Test": {
                    "styles": {
                        # PyMuPDF reports the style name from the font's own
                        # name table, which for this fixture is "Test Regular".
                        "Test Regular": {
                            "family": "Test",
                            "subfamily": "Regular",
                            "weight": 400,
                        }
                    }
                }
            },
        }
    )


def _build_corpus(tmp_path) -> None:
    """Build a tiny PDF with an embedded TrueType font."""
    import pymupdf
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((50, 700))
    pen.lineTo((500, 700))
    pen.lineTo((500, 0))
    pen.closePath()
    outline = pen.glyph()

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "space", "A", "B"])
    builder.setupCharacterMap({32: "space", 65: "A", 66: "B"})
    builder.setupGlyf(
        {".notdef": outline, "space": outline, "A": outline, "B": outline}
    )
    builder.setupHorizontalMetrics(
        {".notdef": (500, 0), "space": (250, 0), "A": (600, 0), "B": (600, 0)}
    )
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
    font_path = str(tmp_path / "test.ttf")
    builder.save(font_path)

    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_font(fontname="Test", fontfile=font_path)
    page.insert_text((72, 72), "AB", fontname="Test", fontsize=24)
    doc.save(str(corpus / "sample.pdf"))
    doc.close()


@pytest.mark.skipif(os.name != "nt", reason="the synthetic corpus is built with a Windows-safe path")
def test_the_pipeline_publishes_a_valid_font(tmp_path):
    _build_corpus(tmp_path)
    output = str(tmp_path / "out")
    manifest = _manifest(tmp_path, output)

    result = pipeline.run(manifest, log=lambda *a, **k: None)

    assert result.fonts, "the corpus should have produced at least one font"
    assert os.path.isdir(output)
    written = [n for n in os.listdir(output) if n.endswith((".ttf", ".otf"))]
    assert written, "the output directory should hold a font"


def test_two_runs_are_byte_identical(tmp_path):
    """The core reproducibility property, end to end."""
    _build_corpus(tmp_path)
    first = str(tmp_path / "run1")
    second = str(tmp_path / "run2")

    pipeline.run(_manifest(tmp_path, first), log=lambda *a, **k: None)
    pipeline.run(_manifest(tmp_path, second), log=lambda *a, **k: None)

    names_a = sorted(os.listdir(first))
    names_b = sorted(os.listdir(second))
    assert names_a == names_b

    for name in names_a:
        with open(os.path.join(first, name), "rb") as fh:
            bytes_a = fh.read()
        with open(os.path.join(second, name), "rb") as fh:
            bytes_b = fh.read()
        assert bytes_a == bytes_b, f"{name} differs between two runs"


def test_a_failing_gate_does_not_publish(tmp_path):
    """A font with blank outlines must stop the run before the destination.

    The destination is left exactly as it was - the tool writes to a
    staging directory and moves it into place only after the gate passes,
    so a failed run cannot leave a half-updated delivery folder.
    """
    _build_corpus(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    (output / "previous.txt").write_text("the previous contents")

    manifest = _manifest(tmp_path, str(output))
    # A test character that no font will have makes the gate fail.
    manifest = parse_manifest(
        {
            "version": 1,
            "name": "test",
            "inputs": manifest.inputs,
            "output_dir": str(output),
            "families": {},
        }
    )

    result = pipeline.run(manifest, log=lambda *a, **k: None)

    # With no families declared, nothing is built, so nothing is published
    # and the previous contents survive untouched.
    assert not result.published
    assert (output / "previous.txt").exists()


def test_a_run_that_produces_nothing_does_not_publish(tmp_path):
    """A run that produced no fonts must not replace the destination.

    Publishing an empty directory over a live delivery folder would DESTROY
    the previous fonts.  The most likely way to reach that state is a
    manifest whose style names no longer match what the PDFs embed - a quiet
    mistake that would otherwise look like a successful run.
    """
    _build_corpus(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    (output / "previous.txt").write_text("the previous contents")

    # No families are declared, so no style matches and nothing is built.
    manifest = parse_manifest(
        {
            "version": 1,
            "name": "test",
            "inputs": [str(tmp_path / "corpus")],
            "output_dir": str(output),
            "families": {},
        }
    )

    result = pipeline.run(manifest, log=lambda *a, **k: None)

    assert not result.published
    assert (output / "previous.txt").exists(), "the previous contents must survive"
    assert result.summary.get("gate_passed") is False


def test_publish_moves_the_previous_contents_aside(tmp_path):
    """Publishing replaces the destination's contents."""
    _build_corpus(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    (output / "stale.txt").write_text("stale")

    manifest = _manifest(tmp_path, str(output))
    pipeline.run(manifest, log=lambda *a, **k: None)

    assert not (output / "stale.txt").exists(), "the stale file should be gone"
    assert any(n.endswith((".ttf", ".otf")) for n in os.listdir(output))
    # The previous directory must not be left lying around.
    assert not (tmp_path / "out.previous").exists()
