"""Two styles must never write to the same file.

Two styles with the same family, subfamily and weight produce the same
output name.  The second one silently overwrites the first: the output
holds one file where there should be two, and the report lists both
styles as built.  Nothing else in the pipeline notices.

The Mundial family is the real case - the Hair and the Thin are both at
weight 200 - so a manifest that gets this wrong loses a whole weight.
"""

import pytest

from pdf_font_recovery.manifest import ManifestError, load_manifest


def _write(tmp_path, text):
    path = tmp_path / "manifest.yaml"
    path.write_text(text, encoding="utf-8")
    return path


BASE = """
version: 1
name: test
inputs:
  - "nowhere/*.pdf"
output_dir: "out"
families:
  Mundial:
    styles:
%s
"""


def test_two_styles_with_the_same_output_name_are_rejected(tmp_path):
    text = BASE % (
        "      Mundial-Hair:\n"
        "        family: Mundial\n"
        "        subfamily: Thin\n"
        "        weight: 200\n"
        "        output_name: Mundial-Thin\n"
        "      Mundial-Thin:\n"
        "        family: Mundial\n"
        "        subfamily: Thin\n"
        "        weight: 200\n"
    )
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(str(_write(tmp_path, text)))
    message = str(excinfo.value)
    assert "Mundial-Hair" in message
    assert "Mundial-Thin" in message


def test_distinct_output_names_are_accepted(tmp_path):
    text = BASE % (
        "      Mundial-Hair:\n"
        "        family: Mundial\n"
        "        subfamily: Hair\n"
        "        weight: 200\n"
        "      Mundial-Thin:\n"
        "        family: Mundial\n"
        "        subfamily: Thin\n"
        "        weight: 200\n"
    )
    manifest = load_manifest(str(_write(tmp_path, text)))
    stems = {s.stem for s in manifest.families["Mundial"].styles.values()}
    assert stems == {"Mundial-Hair", "Mundial-Thin"}


def test_the_same_weight_in_two_families_needs_distinct_output_names(tmp_path):
    """Two families may both have a Thin, but they may not share a FILE.

    The output name is the file name, so two families that both have a
    "Thin" have to name their files differently.  The guard catches the
    collision rather than letting one family overwrite the other's file.
    """
    text = """
version: 1
name: test
inputs:
  - "nowhere/*.pdf"
output_dir: "out"
families:
  Mundial:
    styles:
      Thin:
        family: Mundial
        subfamily: Thin
        weight: 200
        output_name: Mundial-Thin
  Altone:
    styles:
      Thin:
        family: Altone
        subfamily: Thin
        weight: 200
        output_name: Altone-Thin
"""
    manifest = load_manifest(str(_write(tmp_path, text)))
    assert len(manifest.families) == 2
    stems = {
        spec.stem
        for family in manifest.families.values()
        for spec in family.styles.values()
    }
    assert stems == {"Mundial-Thin", "Altone-Thin"}
