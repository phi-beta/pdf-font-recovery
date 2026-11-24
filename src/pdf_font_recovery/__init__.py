"""
pdf-font-recovery - deterministic font recovery from PDF documents.

PDF documents embed fonts as subsets: a program holding only the glyphs the
document happened to set, often with no usable cmap, name table or metrics.
This package extracts those subsets, merges them back into installable
fonts, repairs the defects subsetting leaves behind, and verifies the
result.

The public API:

    from pdf_font_recovery import load_manifest, run

    manifest = load_manifest("manifest.yaml")
    result = run(manifest)
    print(result.summary)
"""
from __future__ import annotations

__version__ = "0.1.0"

from .manifest import (
    BaseFontSpec,
    FamilySpec,
    Manifest,
    ManifestError,
    StyleSpec,
    load_manifest,
    parse_manifest,
    resolve_inputs,
)
from .pipeline import PipelineResult, run, run_manifest_file
from .verify import FontCheck, check_directory, check_font, summarise

__all__ = [
    "BaseFontSpec",
    "FamilySpec",
    "FontCheck",
    "Manifest",
    "ManifestError",
    "PipelineResult",
    "StyleSpec",
    "__version__",
    "check_directory",
    "check_font",
    "load_manifest",
    "parse_manifest",
    "resolve_inputs",
    "run",
    "run_manifest_file",
    "summarise",
]
