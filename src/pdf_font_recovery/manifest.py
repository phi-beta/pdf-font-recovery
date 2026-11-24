"""
Manifest schema for pdf-font-recovery.

A manifest is the complete, declarative description of one extraction job:
where the PDFs are, which families to expect, how each embedded style maps
onto a real font style, and where optional external (base) fonts live.

Everything that was hard-coded in the one-off Portage scripts lives here:
the style table, the skip list, the external-source map, the coverage
expectations.  The library code reads this and knows nothing about any
particular client.

Example (YAML)::

    version: 1
    name: portage-cybertech
    inputs:
      - "E:/Clients/Portage Cybertech/public docs/**/*.pdf"
    output_dir: "E:/Clients/Portage Cybertech/artifacts/all/fonts"

    families:
      Altone:
        styles:
          Altone-Regular:
            family: Altone
            subfamily: Regular
            weight: 400
            italic: false
          # ...
        # An external base font used to fill characters the PDFs never set.
        base_fonts:
          Altone-Regular:
            path: "C:/TEMP/fontdl/altone/Altone Trial-Regular.ttf"
            exact_weight: true
            license: personal-use trial
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass(frozen=True)
class StyleSpec:
    """One embedded style: the PDF subset name mapped onto a real font style."""

    key: str
    family: str
    subfamily: str
    weight: int = 400
    italic: bool = False
    #: PostScript-ish output filename stem. Defaults to the key.
    output_name: str | None = None
    #: Source document for this style, e.g. "Proxima Nova" (not a Portage font).
    origin: str | None = None

    @property
    def stem(self) -> str:
        return self.output_name or self.key

    @property
    def is_bold(self) -> bool:
        return self.weight >= 700 and not self.italic


@dataclass(frozen=True)
class BaseFontSpec:
    """An external font used to supply characters the PDFs never set.

    ``exact_weight`` says whether this file is the *same* weight as the style
    it backs.  When it is, the external font is the base and the PDF outlines
    are overlaid on top (the PDF carries the correct licensed letterforms).
    When it is not - the only trial available is Regular but the style is
    SemiBold - the PDF is the base and the external font fills the gaps,
    which means those characters carry the external font's licence.
    """

    path: str
    exact_weight: bool = False
    license: str | None = None


@dataclass(frozen=True)
class FamilySpec:
    """A family: its styles, plus any external base fonts and exclusions."""

    name: str
    styles: dict[str, StyleSpec] = field(default_factory=dict)
    base_fonts: dict[str, BaseFontSpec] = field(default_factory=dict)
    #: Styles to skip entirely, with a reason recorded in the report.
    skip: dict[str, str] = field(default_factory=dict)
    #: Characters expected to be present.  The coverage gate warns when a
    #: family falls short of its own expectation.
    expect_characters: str | None = None


@dataclass(frozen=True)
class Manifest:
    """A complete extraction job."""

    version: int
    name: str
    inputs: list[str]
    output_dir: str
    families: dict[str, FamilySpec] = field(default_factory=dict)
    #: Characters the coverage gate tests on every produced font.
    test_characters: str = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        " .,;:!?'\"()[]{}-\u2013\u2014/&@#%+=*<>|~^_$\u20ac\u00a3\u00a5\u00b0\u2022\u2026"
        "\u00e9\u00e8\u00ea\u00eb\u00e0\u00e2\u00e4\u00e7\u00f4\u00fb\u00f9\u00fc"
        "\u00ef\u00ee\u00f6\u00c9\u00c0\u00c7\u00dc\u00d1\u00df\u0153\u00e6"
        "\u201c\u201d\u2018\u2019"
    )
    #: Emit deterministic timestamps derived from the source rather than "now".
    reproducible_timestamps: bool = True

    def style(self, key: str) -> StyleSpec | None:
        for family in self.families.values():
            if key in family.styles:
                return family.styles[key]
        return None

    def all_styles(self) -> list[StyleSpec]:
        out: list[StyleSpec] = []
        for family in self.families.values():
            out.extend(family.styles.values())
        return sorted(out, key=lambda s: s.key)


class ManifestError(ValueError):
    """Raised when a manifest is malformed or internally inconsistent."""


def _parse_style(key: str, raw: dict[str, Any]) -> StyleSpec:
    if not isinstance(raw, dict):
        raise ManifestError(f"style {key!r} must be a mapping, got {type(raw).__name__}")
    for required in ("family", "subfamily"):
        if required not in raw:
            raise ManifestError(f"style {key!r} is missing required key {required!r}")
    weight = int(raw.get("weight", 400))
    if not 1 <= weight <= 1000:
        raise ManifestError(f"style {key!r} has an out-of-range weight {weight}")
    return StyleSpec(
        key=key,
        family=str(raw["family"]),
        subfamily=str(raw["subfamily"]),
        weight=weight,
        italic=bool(raw.get("italic", False)),
        output_name=raw.get("output_name"),
        origin=raw.get("origin"),
    )


def _parse_base_font(key: str, raw: dict[str, Any]) -> BaseFontSpec:
    if not isinstance(raw, dict):
        raise ManifestError(f"base font {key!r} must be a mapping")
    if "path" not in raw:
        raise ManifestError(f"base font {key!r} is missing required key 'path'")
    return BaseFontSpec(
        path=str(raw["path"]),
        exact_weight=bool(raw.get("exact_weight", False)),
        license=raw.get("license"),
    )


def load_manifest(path: str) -> Manifest:
    """Load and validate a manifest from a YAML file."""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ManifestError("the manifest must be a mapping at the top level")
    return parse_manifest(raw)


def parse_manifest(raw: dict[str, Any]) -> Manifest:
    """Validate a manifest mapping and return the typed form."""
    version = int(raw.get("version", 1))
    if version != 1:
        raise ManifestError(f"unsupported manifest version {version}; expected 1")
    name = raw.get("name")
    if not name:
        raise ManifestError("the manifest needs a 'name'")
    inputs = raw.get("inputs")
    if not inputs or not isinstance(inputs, list):
        raise ManifestError("the manifest needs a non-empty 'inputs' list")
    output_dir = raw.get("output_dir")
    if not output_dir:
        raise ManifestError("the manifest needs an 'output_dir'")

    families: dict[str, FamilySpec] = {}
    for fam_name, fam_raw in (raw.get("families") or {}).items():
        if not isinstance(fam_raw, dict):
            raise ManifestError(f"family {fam_name!r} must be a mapping")
        styles = {}
        for key, style_raw in (fam_raw.get("styles") or {}).items():
            styles[key] = _parse_style(key, style_raw)
        base_fonts = {}
        for key, base_raw in (fam_raw.get("base_fonts") or {}).items():
            base_fonts[key] = _parse_base_font(key, base_raw)
        skip = {str(k): str(v) for k, v in (fam_raw.get("skip") or {}).items()}
        families[fam_name] = FamilySpec(
            name=fam_name,
            styles=styles,
            base_fonts=base_fonts,
            skip=skip,
            expect_characters=fam_raw.get("expect_characters"),
        )

    return Manifest(
        version=version,
        name=str(name),
        inputs=[str(p) for p in inputs],
        output_dir=str(output_dir),
        families=families,
        test_characters=str(raw.get("test_characters") or Manifest.test_characters),
        reproducible_timestamps=bool(raw.get("reproducible_timestamps", True)),
    )


def resolve_inputs(patterns: list[str]) -> list[str]:
    """Expand the manifest's input patterns into a sorted, de-duplicated list.

    The order is stable so that two runs over the same corpus produce the
    same output - the first source that provides a glyph wins, so an
    unstable order would make the output unstable.
    """
    import glob

    found: set[str] = set()
    for pattern in patterns:
        # A directory is walked recursively for PDFs; anything else is a glob.
        if os.path.isdir(pattern):
            for dirpath, _dirnames, filenames in os.walk(pattern):
                for name in filenames:
                    if name.lower().endswith(".pdf"):
                        found.add(os.path.join(dirpath, name))
        else:
            for hit in glob.glob(pattern, recursive=True):
                if os.path.isfile(hit) and hit.lower().endswith(".pdf"):
                    found.add(hit)
    return sorted(found)
