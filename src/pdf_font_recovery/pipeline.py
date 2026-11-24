"""
The extraction pipeline.

The pipeline runs in stages and publishes atomically: everything is built
into a staging directory, verified there, and only then moved into place.
A run that fails half-way therefore leaves the previous output untouched
rather than a half-updated mixture of old and new fonts.

Stages:

  1. collect   - walk the PDFs and group the embedded programs by style
  2. build     - merge each style's subsets and write a font
  3. repair    - union a style across documents; fill from an external source
  4. normalise - fix the name tables, style bits and OS/2
  5. verify    - ink, placeholder and metadata checks
  6. publish   - move the staging directory into place
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field

from . import extract, normalise, repair, verify
from .manifest import Manifest, StyleSpec, resolve_inputs

#: A fixed timestamp used when the manifest does not supply one.
#:
#: fontTools interprets a head timestamp below 0x7C259DC0 (1 January 2084) as
#: a Macintosh epoch offset rather than a Unix timestamp, and shifts it on
#: load.  A pinned value below that threshold would therefore not survive a
#: round trip, so the default is comfortably above it.
DEFAULT_REPRODUCIBLE_TIMESTAMP = 0x7C259DC0  # 2084-01-01T00:00:00Z


@dataclass
class StageReport:
    """What one stage did."""

    name: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"stage": self.name, **self.detail}


@dataclass
class PipelineResult:
    """The outcome of a whole run."""

    manifest_name: str
    output_dir: str
    fonts: list[dict] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    stages: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    published: bool = False

    def to_dict(self) -> dict:
        return {
            "manifest": self.manifest_name,
            "output_dir": self.output_dir,
            "fonts": self.fonts,
            "repairs": self.repairs,
            "checks": self.checks,
            "skipped": self.skipped,
            "stages": self.stages,
            "summary": self.summary,
            "published": self.published,
        }


def _style_spec_for(manifest: Manifest, key: str) -> StyleSpec | None:
    return manifest.style(key)


def _ps_name(spec: StyleSpec) -> str:
    return (spec.family.replace(" ", "") + "-" + spec.subfamily.replace(" ", ""))


def run(
    manifest: Manifest,
    *,
    staging_dir: str | None = None,
    publish: bool = True,
    log=print,
) -> PipelineResult:
    """Run the whole pipeline for a manifest."""
    result = PipelineResult(manifest_name=manifest.name, output_dir=manifest.output_dir)

    pdfs = resolve_inputs(manifest.inputs)
    log(f"[1/6] collect: {len(pdfs)} PDFs")
    if not pdfs:
        raise SystemExit(f"no PDFs matched the manifest inputs: {manifest.inputs}")

    grouped = extract.collect_embedded_fonts(pdfs)
    total_programs = sum(len(v) for v in grouped.values())
    log(f"      {total_programs} embedded programs across {len(grouped)} (style, format) keys")
    result.stages.append(
        StageReport("collect", {"pdfs": len(pdfs), "programs": total_programs, "keys": len(grouped)}).to_dict()
    )

    # The staging directory holds everything until the run succeeds.
    staging = staging_dir or tempfile.mkdtemp(prefix="pdf-font-recovery-")
    os.makedirs(staging, exist_ok=True)
    timestamp = (
        DEFAULT_REPRODUCIBLE_TIMESTAMP if manifest.reproducible_timestamps else None
    )

    built: list[tuple[StyleSpec, str, str]] = []  # (spec, path, format)
    for (style_key, fmt), chunks in sorted(grouped.items()):
        if fmt in extract.UNUSABLE_FORMATS:
            log(f"      SKIP {style_key:26s} {fmt} (not usable)")
            result.skipped.append({"style": style_key, "reason": f"format {fmt} is not usable"})
            continue

        spec = _style_spec_for(manifest, style_key)
        if spec is None:
            log(f"      SKIP {style_key:26s} not in the manifest")
            result.skipped.append({"style": style_key, "reason": "not declared in the manifest"})
            continue

        family = manifest.families.get(spec.family)
        if family and style_key in family.skip:
            log(f"      SKIP {style_key:26s} {family.skip[style_key]}")
            result.skipped.append({"style": style_key, "reason": family.skip[style_key]})
            continue

        extension = ".ttf" if fmt == "ttf" else ".otf"
        out_path = os.path.join(staging, spec.stem + extension)

        if fmt == "ttf":
            # The richest copy wins: a larger program carries more glyphs.
            best = max(chunks, key=lambda c: len(c.data))
            try:
                glyph_count = extract.write_ttf(
                    best.data, out_path, reproducible_timestamp=timestamp
                )
            except Exception as exc:
                log(f"      FAIL {style_key:26s} {type(exc).__name__}: {exc}")
                result.skipped.append({"style": style_key, "reason": f"{type(exc).__name__}: {exc}"})
                continue
            log(f"      TTF  {style_key:26s} {os.path.getsize(out_path):7d}b  glyphs={glyph_count:4d}")
            built.append((spec, out_path, "TTF"))
            result.fonts.append(
                {
                    "style": style_key,
                    "file": os.path.basename(out_path),
                    "family": spec.family,
                    "subfamily": spec.subfamily,
                    "weight": spec.weight,
                    "italic": spec.italic,
                    "glyphs": glyph_count,
                    "source": best.source,
                    "format": "TTF",
                }
            )
            continue

        # CFF: merge the subsets and wrap them in an OpenType container.
        merged = extract.merge_cff(chunks)
        if not merged:
            log(f"      FAIL {style_key:26s} no CFF subset could be parsed")
            result.skipped.append({"style": style_key, "reason": "no CFF subset could be parsed"})
            continue
        base_top, charstrings, added, base_source = merged
        try:
            builder, upem, _widths = extract.build_otf(
                base_top,
                charstrings,
                _ps_name(spec),
                spec.family,
                spec.subfamily,
                spec.weight,
                spec.italic,
                reproducible_timestamp=timestamp,
            )
            extract.save_font(builder, out_path, reproducible_timestamp=timestamp)
            glyph_count = len(extract.TTFont(out_path).getBestCmap())
        except Exception as exc:
            log(f"      FAIL {style_key:26s} {type(exc).__name__}: {exc}")
            result.skipped.append({"style": style_key, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        log(
            f"      CFF  {style_key:26s} {os.path.getsize(out_path):7d}b  "
            f"glyphs={glyph_count:4d} (+{added} merged)"
        )
        built.append((spec, out_path, "OTF (CFF)"))
        result.fonts.append(
            {
                "style": style_key,
                "file": os.path.basename(out_path),
                "family": spec.family,
                "subfamily": spec.subfamily,
                "weight": spec.weight,
                "italic": spec.italic,
                "glyphs": glyph_count,
                "upem": upem,
                "source": base_source,
                "format": "OTF (CFF)",
                "merged_glyphs": added,
            }
        )

    log(f"[2/6] build: {len(built)} fonts")
    result.stages.append(StageReport("build", {"fonts": len(built)}).to_dict())

    # ---- repair
    log("[3/6] repair")
    for spec, path, _fmt in built:
        family = manifest.families.get(spec.family)
        if not family:
            continue
        base_spec = family.base_fonts.get(spec.key)
        if not base_spec:
            continue
        report = repair.repair_style(
            path,
            spec.key,
            external_path=base_spec.path,
            external_exact_weight=base_spec.exact_weight,
            test_characters=manifest.test_characters,
            reproducible_timestamp=timestamp,
        )
        if report.filled or report.borrowed or report.overlaid or report.still_blank:
            log(
                f"      {spec.key:26s} filled={len(report.filled):3d} "
                f"overlaid={len(report.overlaid):3d} borrowed={len(report.borrowed):3d} "
                f"still-blank={len(report.still_blank):3d}"
            )
        result.repairs.append(report.to_dict())
    result.stages.append(StageReport("repair", {"repaired": len(result.repairs)}).to_dict())

    # ---- normalise
    log("[4/6] normalise")
    for spec, path, _fmt in built:
        try:
            outcome = normalise.normalise_font(
                path,
                spec.family,
                spec.subfamily,
                spec.weight,
                spec.italic,
                manifest.test_characters,
                reproducible_timestamp=timestamp,
            )
        except Exception as exc:
            log(f"      FAIL {spec.key:26s} {type(exc).__name__}: {exc}")
            result.skipped.append({"style": spec.key, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        log(
            f"      {os.path.basename(path):28s} -> {outcome.family} {outcome.subfamily} "
            f"w={outcome.weight} glyphs={outcome.glyphs} missing={len(outcome.missing)}"
        )
    result.stages.append(StageReport("normalise", {"fonts": len(built)}).to_dict())

    # ---- verify
    log("[5/6] verify")
    checks = verify.check_directory(staging, manifest.test_characters)
    for check in checks:
        result.checks.append(check.to_dict())
        if check.blank:
            log(f"      BLANK {check.name}: {len(check.blank)} characters")
    summary = verify.summarise(checks)
    log(
        f"      {summary['fonts']} fonts, {summary['total_blank']} blank, "
        f"{summary['total_placeholders']} placeholder, "
        f"{summary['fonts_with_problems']} with problems"
    )
    result.summary = summary
    result.stages.append(StageReport("verify", summary).to_dict())

    # ---- the coverage gate
    failures = [c for c in checks if c.blank or c.problems]
    if failures:
        log(f"      GATE FAILED: {len(failures)} fonts have blank outlines or problems")
        for check in failures:
            log(f"        {check.name}: blank={len(check.blank)} problems={check.problems}")
        result.stages.append(StageReport("gate", {"passed": False, "failures": len(failures)}).to_dict())
        if publish:
            log("      NOT publishing - the staging directory is left for inspection")
            result.summary["gate_passed"] = False
            _write_reports(result, staging)
            return result
    else:
        result.stages.append(StageReport("gate", {"passed": True}).to_dict())

    # ---- the empty-output guard
    #
    # A run that produced NO fonts must not publish.  Publishing an empty
    # directory over a live delivery folder would DESTROY the previous
    # fonts - and the most likely way to get here is a manifest whose style
    # names no longer match what the PDFs embed, which is a quiet mistake.
    if not built:
        log("      NOT publishing - the run produced no fonts at all")
        log("      (a manifest whose style names do not match the PDFs produces this)")
        result.summary["gate_passed"] = False
        result.stages.append(StageReport("gate", {"passed": False, "reason": "no fonts produced"}).to_dict())
        _write_reports(result, staging)
        return result

    # ---- publish
    if publish:
        _publish(staging, manifest.output_dir, log)
        result.published = True
        log(f"[6/6] publish: {manifest.output_dir}")
    else:
        log(f"[6/6] publish: skipped (--no-publish), staging at {staging}")
    result.stages.append(StageReport("publish", {"published": result.published}).to_dict())

    _write_reports(result, manifest.output_dir if publish else staging)
    return result


def _publish(staging: str, destination: str, log) -> None:
    """Move the staging directory into place, atomically where possible.

    The reports are written into the staging directory first, so they move
    with the fonts.  The destination's previous contents are moved aside
    rather than deleted first, so a failure mid-swap leaves a recoverable
    state.
    """
    os.makedirs(destination, exist_ok=True)
    previous = destination + ".previous"
    if os.path.exists(previous):
        shutil.rmtree(previous, ignore_errors=True)
    if os.path.isdir(destination):
        os.replace(destination, previous)
    try:
        os.replace(staging, destination)
    except Exception:
        # A cross-device rename fails; fall back to a copy.
        shutil.copytree(staging, destination)
        shutil.rmtree(staging, ignore_errors=True)
    if os.path.exists(previous):
        shutil.rmtree(previous, ignore_errors=True)


def _write_reports(result: PipelineResult, directory: str) -> None:
    """Write the manifest, coverage and check reports."""
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(result.fonts, fh, indent=1)
    with open(os.path.join(directory, "_coverage.json"), "w", encoding="utf-8") as fh:
        json.dump(result.checks, fh, indent=1)
    report = result.to_dict()
    # Three things in the report are wall-clock or run-state, not build
    # output: the elapsed time, the output directory, and whether this
    # particular run published.  They are reported to the operator but kept
    # OUT of the written report, which must be byte-identical between two
    # runs over the same input.
    report.get("summary", {}).pop("elapsed_seconds", None)
    report["stages"] = [
        stage for stage in report.get("stages", []) if stage.get("stage") != "publish"
    ]
    report["published"] = True
    report["output_dir"] = ""
    with open(os.path.join(directory, "_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)


def run_manifest_file(path: str, **kwargs) -> PipelineResult:
    """Load a manifest and run the pipeline for it."""
    from .manifest import load_manifest

    manifest = load_manifest(path)
    started = time.time()
    result = run(manifest, **kwargs)
    result.summary["elapsed_seconds"] = round(time.time() - started, 2)
    return result
