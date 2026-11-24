"""
Tests for the manifest layer.

The manifest is what makes the tool generic: everything that was hard-coded
in the one-off scripts lives there.  These tests pin the validation rules,
because a manifest that is accepted when it should be rejected produces a
font library with the wrong names in it.
"""
from __future__ import annotations

import pytest

from pdf_font_recovery.manifest import (
    ManifestError,
    parse_manifest,
    resolve_inputs,
)

MINIMAL = {
    "version": 1,
    "name": "test",
    "inputs": ["*.pdf"],
    "output_dir": "/tmp/out",
}


def test_minimal_manifest_parses():
    manifest = parse_manifest(MINIMAL)
    assert manifest.name == "test"
    assert manifest.inputs == ["*.pdf"]
    assert manifest.output_dir == "/tmp/out"
    assert manifest.families == {}


def test_missing_name_is_rejected():
    raw = {k: v for k, v in MINIMAL.items() if k != "name"}
    with pytest.raises(ManifestError, match="name"):
        parse_manifest(raw)


def test_missing_inputs_is_rejected():
    raw = {k: v for k, v in MINIMAL.items() if k != "inputs"}
    with pytest.raises(ManifestError, match="inputs"):
        parse_manifest(raw)


def test_empty_inputs_is_rejected():
    with pytest.raises(ManifestError, match="inputs"):
        parse_manifest({**MINIMAL, "inputs": []})


def test_missing_output_dir_is_rejected():
    raw = {k: v for k, v in MINIMAL.items() if k != "output_dir"}
    with pytest.raises(ManifestError, match="output_dir"):
        parse_manifest(raw)


def test_unsupported_version_is_rejected():
    with pytest.raises(ManifestError, match="version"):
        parse_manifest({**MINIMAL, "version": 2})


def test_style_needs_family_and_subfamily():
    raw = {
        **MINIMAL,
        "families": {"Test": {"styles": {"A-Bold": {"family": "Test"}}}},
    }
    with pytest.raises(ManifestError, match="subfamily"):
        parse_manifest(raw)


def test_style_weight_out_of_range_is_rejected():
    raw = {
        **MINIMAL,
        "families": {
            "Test": {"styles": {"A-Bold": {"family": "T", "subfamily": "Bold", "weight": 5000}}}
        },
    }
    with pytest.raises(ManifestError, match="weight"):
        parse_manifest(raw)


def test_base_font_needs_a_path():
    raw = {
        **MINIMAL,
        "families": {"Test": {"base_fonts": {"A-Bold": {"exact_weight": True}}}},
    }
    with pytest.raises(ManifestError, match="path"):
        parse_manifest(raw)


def test_style_lookup_finds_the_style():
    raw = {
        **MINIMAL,
        "families": {
            "Altone": {
                "styles": {
                    "Altone-Regular": {"family": "Altone", "subfamily": "Regular", "weight": 400}
                }
            }
        },
    }
    manifest = parse_manifest(raw)
    spec = manifest.style("Altone-Regular")
    assert spec is not None
    assert spec.family == "Altone"
    assert spec.weight == 400
    assert spec.italic is False
    assert manifest.style("Nope") is None


def test_all_styles_is_sorted():
    raw = {
        **MINIMAL,
        "families": {
            "A": {"styles": {"Z-1": {"family": "A", "subfamily": "1"}}},
            "B": {"styles": {"M-2": {"family": "B", "subfamily": "2"}}},
        },
    }
    manifest = parse_manifest(raw)
    assert [s.key for s in manifest.all_styles()] == ["M-2", "Z-1"]


def test_is_bold_excludes_italics():
    raw = {
        **MINIMAL,
        "families": {
            "A": {
                "styles": {
                    "A-Bold": {"family": "A", "subfamily": "Bold", "weight": 700},
                    "A-BoldItalic": {
                        "family": "A",
                        "subfamily": "Bold Italic",
                        "weight": 700,
                        "italic": True,
                    },
                }
            }
        },
    }
    manifest = parse_manifest(raw)
    assert manifest.style("A-Bold").is_bold is True
    # An italic face's boldness is carried by the weight, not by macStyle.
    assert manifest.style("A-BoldItalic").is_bold is False


def test_resolve_inputs_is_sorted_and_deduplicated(tmp_path):
    for name in ("b.pdf", "a.pdf", "c.txt"):
        (tmp_path / name).write_bytes(b"x")
    found = resolve_inputs([str(tmp_path / "*.pdf")])
    assert len(found) == 2
    assert found == sorted(found)
    # The same file matched twice is listed once.
    found_again = resolve_inputs([str(tmp_path / "*.pdf"), str(tmp_path / "a.pdf")])
    assert len(found_again) == 2


def test_resolve_inputs_walks_a_directory(tmp_path):
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "x.pdf").write_bytes(b"x")
    (tmp_path / "y.pdf").write_bytes(b"y")
    found = resolve_inputs([str(tmp_path)])
    assert len(found) == 2
