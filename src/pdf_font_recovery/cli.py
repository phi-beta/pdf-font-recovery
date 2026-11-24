"""
Command-line interface for pdf-font-recovery.

    pdf-font-recovery extract --manifest manifest.yaml
    pdf-font-recovery verify --dir ./fonts
    pdf-font-recovery inspect --manifest manifest.yaml

``extract`` runs the whole pipeline and publishes atomically.  ``verify``
checks an existing directory of fonts without touching it - which is what a
CI job runs against the committed output.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import pipeline, spacing, verify
from .manifest import ManifestError, load_manifest, resolve_inputs


def _cmd_extract(args) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except (ManifestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = pipeline.run(
        manifest,
        publish=not args.no_publish,
        staging_dir=args.staging,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=1))
    else:
        print()
        print(f"output: {result.output_dir}")
        print(f"fonts:  {len(result.fonts)}")
        summary = result.summary
        if summary:
            print(
                f"verify: {summary.get('total_blank', 0)} blank, "
                f"{summary.get('total_placeholders', 0)} placeholder, "
                f"{summary.get('fonts_with_problems', 0)} with problems"
            )
        print(f"gate:   {'passed' if result.summary.get('gate_passed', True) else 'FAILED'}")

    if result.summary.get("gate_passed") is False:
        return 1
    return 0


def _cmd_verify(args) -> int:
    checks = verify.check_directory(args.dir, args.characters or None or _default_characters())
    if args.json:
        print(json.dumps([c.to_dict() for c in checks], indent=1))
    else:
        print(verify.format_report(checks))
    summary = verify.summarise(checks)
    # The exit code is the verdict: a blank outline is a failure, not a note.
    if summary["total_blank"] or summary["fonts_with_problems"]:
        return 1
    return 0


def _cmd_spacing(args) -> int:
    """Check the letter spacing of a directory of fonts.

    This is the check that catches a font whose letters overlap: a wrong
    advance width is invisible to every structural check, so the font
    loads, has a valid name table, and renders wrong.

    ``--reference`` names a reference font for the whole directory; a
    manifest is the better way to give each style its own reference.
    """
    checks = spacing.check_directory(args.dir, args.reference)
    if args.json:
        print(json.dumps([c.to_dict() for c in checks], indent=1))
    else:
        print(spacing.format_report(checks))
    summary = spacing.summarise(checks)
    if summary["fonts_with_problems"]:
        return 1
    return 0


def _default_characters() -> str:
    from .manifest import Manifest

    return Manifest.test_characters


def _cmd_inspect(args) -> int:
    """Report what is embedded in the PDFs, without building anything."""
    from . import extract

    try:
        manifest = load_manifest(args.manifest)
    except (ManifestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pdfs = resolve_inputs(manifest.inputs)
    print(f"{len(pdfs)} PDFs")
    grouped = extract.collect_embedded_fonts(pdfs)
    for (style, fmt), chunks in sorted(grouped.items()):
        sources = sorted({c.source for c in chunks})
        declared = "declared" if manifest.style(style) else "NOT IN MANIFEST"
        print(
            f"  {style:28s} {fmt:5s} {len(chunks):3d} copies  "
            f"{len(sources):2d} docs  {declared}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf-font-recovery",
        description="Deterministically extract, repair and verify fonts embedded in PDFs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    extract_parser = sub.add_parser("extract", help="run the full extraction pipeline")
    extract_parser.add_argument("--manifest", required=True, help="path to the manifest YAML")
    extract_parser.add_argument(
        "--no-publish",
        action="store_true",
        help="build into the staging directory but do not move it into place",
    )
    extract_parser.add_argument(
        "--staging", help="use this staging directory instead of a temporary one"
    )
    extract_parser.add_argument("--json", action="store_true", help="print the report as JSON")
    extract_parser.set_defaults(func=_cmd_extract)

    verify_parser = sub.add_parser("verify", help="check the fonts in a directory")
    verify_parser.add_argument("--dir", required=True, help="directory of fonts to check")
    verify_parser.add_argument("--characters", help="the characters to test")
    verify_parser.add_argument("--json", action="store_true", help="print the report as JSON")
    verify_parser.set_defaults(func=_cmd_verify)

    spacing_parser = sub.add_parser(
        "spacing",
        help="check the letter spacing of the fonts in a directory",
    )
    spacing_parser.add_argument("--dir", required=True, help="directory of fonts to check")
    spacing_parser.add_argument(
        "--reference",
        help="a reference font to compare against - the publisher's own file",
    )
    spacing_parser.add_argument("--json", action="store_true", help="print the report as JSON")
    spacing_parser.set_defaults(func=_cmd_spacing)

    inspect_parser = sub.add_parser(
        "inspect", help="report what is embedded in the PDFs without building anything"
    )
    inspect_parser.add_argument("--manifest", required=True, help="path to the manifest YAML")
    inspect_parser.set_defaults(func=_cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
