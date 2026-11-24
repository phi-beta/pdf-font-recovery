"""
Letter-spacing verification for recovered fonts.

A font can be structurally perfect and still render with the letters on
top of each other.  The advance width is how far the pen moves before the
next glyph is drawn; if a glyph's advance is wrong, the text overlaps -
while the font still loads, still has a valid name table, and still
passes every structural check.

THE HARD PART: DESIGN IS NOT A DEFECT

A glyph whose advance is narrower than its own ink is NORMAL in an
italic.  The letter leans into its neighbour on purpose.  Measured
against the publisher's own files, Mundial Black Italic has 21 of 62
letters advancing narrower than their ink - and the recovered font has
exactly 21 too.

So "advance < ink" proves nothing on its own.  What proves a defect is
one of:

1.  A ZERO advance.  No real glyph advances nowhere.  This is the
    signature of a metrics entry created as a placeholder and never
    filled in - the defect this module exists to catch.

2.  An advance far below the ink, measured against the font's OWN
    median.  One letter with an advance/ink ratio of 0.000 among
    siblings at 1.2 is broken; a whole font at 0.6 is a typeface whose
    designer wanted it tight.  The median makes the check relative to
    the font, so it works on any typeface.

3.  An advance that disagrees with a DECLARED REFERENCE font.  This is
    the authoritative check: compare the recovered font against the
    source it was recovered from, like for like.  An italic must be
    compared with an italic - comparing an oblique to an upright tells
    you about the slant, not about the recovery.

The reference check is what distinguishes the two cases above from a
typeface that is simply tight.

THE REFERENCE'S OWN DEFECTS

A reference can be wrong too.  The Altone Trial's ten digits are all one
placeholder box, so its advances for those characters are the advance of
a box, not of a figure.  Comparing against it would flag the recovered
font's CORRECT figures as wrong.  So characters whose reference glyph is
blank, or is a shared placeholder box, are excluded from the
comparison - and the exclusion is reported, because a comparison that
silently drops characters is a comparison you cannot trust.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont

from .verify import find_placeholder_characters

#: The characters the spacing check measures.  Letters and figures: the
#: punctuation is too varied across typefaces to compare meaningfully.
MEASURED_CHARACTERS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)

#: An advance narrower than this fraction of the glyph's own ink is a
#: defect regardless of anything else.  The tightest legitimate case
#: measured across the reference corpus is 0.615 (Mundial Italic's
#: descender), so this leaves about a fifth of headroom.
MIN_ADVANCE_TO_INK = 0.50

#: A glyph whose advance/ink ratio is below this fraction of the FONT'S
#: OWN median ratio is inconsistent with its siblings, and is a defect.
#: The relative form is what lets the check work on any typeface: a font
#: that is tight throughout is fine, a font with one collapsed letter is
#: not.
MIN_RELATIVE_TO_MEDIAN = 0.50

#: The bounds the median advance/ink ratio of a recovered font must fall
#: within, when it is compared against a reference font.  A systematic
#: shift outside this band means the widths came from the wrong source.
REFERENCE_MEDIAN_TOLERANCE = (0.90, 1.10)

#: A single character whose advance disagrees with the reference by more
#: than this factor is reported as a defect.
REFERENCE_CHARACTER_TOLERANCE = 0.80


@dataclass
class SpacingCheck:
    """The spacing result for one font file."""

    path: str
    family: str = ""
    subfamily: str = ""
    #: The reference the font was compared against, if any.
    reference: str = ""
    #: The median advance/ink ratio over the measured characters.
    median_ratio: float = 0.0
    #: The median advance/em, for the reference comparison.
    median_advance_em: float = 0.0
    #: The median advance/em of the reference.
    reference_median_advance_em: float = 0.0
    #: Characters whose advance is zero.
    zero_advance: list[str] = field(default_factory=list)
    #: Characters whose advance is far below their ink.
    too_narrow: list[str] = field(default_factory=list)
    #: Characters inconsistent with their own font's siblings.
    inconsistent: list[str] = field(default_factory=list)
    #: Characters whose advance disagrees with the reference.
    mismatched: list[str] = field(default_factory=list)
    #: Characters excluded from the reference comparison, with why.
    excluded: list[str] = field(default_factory=list)
    #: The characters compared against the reference.
    compared: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "file": self.name,
            "family": self.family,
            "subfamily": self.subfamily,
            "reference": os.path.basename(self.reference) if self.reference else "",
            "median_ratio": round(self.median_ratio, 4),
            "median_advance_em": round(self.median_advance_em, 4),
            "reference_median_advance_em": round(self.reference_median_advance_em, 4),
            "zero_advance": "".join(self.zero_advance),
            "too_narrow": "".join(self.too_narrow),
            "inconsistent": "".join(self.inconsistent),
            "mismatched": "".join(self.mismatched),
            "excluded": "".join(self.excluded),
            "compared": self.compared,
            "problems": list(self.problems),
        }


def measure_font(path: str) -> dict[str, dict]:
    """The advance and ink width of every measured character in a font.

    Returns a map of character -> {"advance", "ink", "advance_em"}.

    The ink width is measured by drawing the glyph, so it works for
    TrueType and CFF alike - a CFF glyph has no bounding box to read.
    """
    font = TTFont(path)
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    glyph_set = font.getGlyphSet()

    out: dict[str, dict] = {}
    for char in MEASURED_CHARACTERS:
        name = cmap.get(ord(char))
        if name is None:
            continue
        try:
            advance = hmtx[name][0]
        except KeyError:
            continue
        pen = BoundsPen(glyph_set)
        try:
            glyph_set[name].draw(pen)
        except Exception:
            continue
        if pen.bounds is None:
            # No outline: the ink width is zero.  The character is still
            # measured - a zero advance is a problem, and a blank glyph
            # is the ink check's business, not this one's.
            ink = 0
        else:
            ink = pen.bounds[2] - pen.bounds[0]
        out[char] = {
            "advance": advance,
            "ink": ink,
            "advance_em": advance / float(upem),
        }
    return out


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _reference_characters_to_exclude(reference_path: str) -> set[str]:
    """Characters the REFERENCE cannot be trusted for.

    A reference glyph with no ink, or one that is a shared placeholder
    box, cannot tell us what a character should advance by.
    """
    excluded: set[str] = set()
    try:
        font = TTFont(reference_path)
    except Exception:
        return excluded

    placeholders = find_placeholder_characters(font)
    cmap = font.getBestCmap()
    glyph_set = font.getGlyphSet()

    for code, name in cmap.items():
        char = chr(code)
        if char not in MEASURED_CHARACTERS:
            continue
        if code in placeholders:
            excluded.add(char)
            continue
        pen = BoundsPen(glyph_set)
        try:
            glyph_set[name].draw(pen)
        except Exception:
            excluded.add(char)
            continue
        if pen.bounds is None:
            excluded.add(char)
    return excluded


def check_spacing(
    path: str, reference_path: str | None = None
) -> SpacingCheck:
    """Check one font's letter spacing.

    With a ``reference_path``, the font is also compared against that
    reference - like for like, so an oblique must be compared with an
    oblique.
    """
    result = SpacingCheck(path=path)
    try:
        font = TTFont(path)
        result.family = font["name"].getDebugName(1) or ""
        result.subfamily = font["name"].getDebugName(2) or ""
    except Exception as exc:
        result.problems.append(f"will not open: {type(exc).__name__}: {exc}")
        return result

    try:
        measurements = measure_font(path)
    except Exception as exc:
        result.problems.append(f"will not measure: {type(exc).__name__}: {exc}")
        return result

    if not measurements:
        result.problems.append("no measurable characters")
        return result

    # ---- the self-contained checks
    ratios = [
        m["advance"] / m["ink"]
        for m in measurements.values()
        if m["ink"] > 0
    ]
    result.median_ratio = _median(ratios)
    result.median_advance_em = _median([m["advance_em"] for m in measurements.values()])

    for char, m in sorted(measurements.items()):
        if m["advance"] == 0:
            result.zero_advance.append(char)
        elif m["ink"] > 0 and m["advance"] < m["ink"] * MIN_ADVANCE_TO_INK:
            result.too_narrow.append(char)

    # A glyph that is inconsistent with its own font's siblings.  This is
    # what catches a single collapsed letter in an otherwise sound font.
    if result.median_ratio > 0:
        floor = result.median_ratio * MIN_RELATIVE_TO_MEDIAN
        for char, m in sorted(measurements.items()):
            if m["ink"] <= 0:
                continue
            if m["advance"] / m["ink"] < floor:
                result.inconsistent.append(char)

    if result.zero_advance:
        result.problems.append(
            f"{len(result.zero_advance)} glyph(s) have a ZERO advance: "
            f"{''.join(result.zero_advance)!r}"
        )
    if result.too_narrow:
        result.problems.append(
            f"{len(result.too_narrow)} glyph(s) advance narrower than "
            f"{MIN_ADVANCE_TO_INK:.0%} of their ink: {''.join(result.too_narrow)!r}"
        )
    if result.inconsistent:
        result.problems.append(
            f"{len(result.inconsistent)} glyph(s) are inconsistent with their "
            f"font's own spacing (median ratio {result.median_ratio:.2f}): "
            f"{''.join(result.inconsistent)!r}"
        )

    # ---- the reference comparison
    if reference_path:
        _check_against_reference(result, measurements, reference_path)

    return result


def _check_against_reference(
    result: SpacingCheck,
    measurements: dict[str, dict],
    reference_path: str,
) -> None:
    """Compare a font's advances against a reference font's."""
    if not os.path.exists(reference_path):
        result.problems.append(f"the reference font is missing: {reference_path}")
        return

    result.reference = reference_path
    try:
        reference = measure_font(reference_path)
    except Exception as exc:
        result.problems.append(
            f"the reference font will not measure: {type(exc).__name__}: {exc}"
        )
        return
    if not reference:
        result.problems.append("the reference font has no measurable characters")
        return

    excluded = _reference_characters_to_exclude(reference_path)

    ratios = []
    for char, mine in sorted(measurements.items()):
        theirs = reference.get(char)
        if theirs is None:
            continue
        if char in excluded:
            result.excluded.append(char)
            continue
        if theirs["advance_em"] <= 0:
            result.excluded.append(char)
            continue
        ratio = mine["advance_em"] / theirs["advance_em"]
        ratios.append(ratio)
        if ratio < REFERENCE_CHARACTER_TOLERANCE:
            result.mismatched.append(char)

    result.compared = len(ratios)
    result.reference_median_advance_em = _median(
        [m["advance_em"] for m in reference.values() if m["advance_em"] > 0]
    )

    if not ratios:
        result.problems.append(
            "no characters could be compared against the reference"
        )
        return

    median = _median(ratios)
    low, high = REFERENCE_MEDIAN_TOLERANCE
    if not low <= median <= high:
        result.problems.append(
            f"the font's letters are {median:.2f}x the reference's, which is "
            f"outside the {low:.2f}-{high:.2f} band - the widths came from "
            f"the wrong source"
        )
    if result.mismatched:
        result.problems.append(
            f"{len(result.mismatched)} character(s) advance less than "
            f"{REFERENCE_CHARACTER_TOLERANCE:.0%} of the reference's: "
            f"{''.join(result.mismatched)!r}"
        )


def check_directory(
    directory: str, references: dict[str, str] | None = None
) -> list[SpacingCheck]:
    """Check every font in a directory, in sorted order.

    ``references`` maps a font FILE NAME to the path of the reference it
    should be compared against.
    """
    references = references or {}
    checks = []
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith((".ttf", ".otf")):
            continue
        checks.append(check_spacing(os.path.join(directory, name), references.get(name)))
    return checks


def summarise(checks: list[SpacingCheck]) -> dict:
    """Aggregate the per-font spacing checks."""
    return {
        "fonts": len(checks),
        "zero_advance": sum(len(c.zero_advance) for c in checks),
        "too_narrow": sum(len(c.too_narrow) for c in checks),
        "inconsistent": sum(len(c.inconsistent) for c in checks),
        "mismatched": sum(len(c.mismatched) for c in checks),
        "compared": sum(c.compared for c in checks),
        "fonts_with_problems": sum(1 for c in checks if c.problems),
    }


def format_report(checks: list[SpacingCheck]) -> str:
    """A human-readable report of the spacing checks."""
    lines = []
    for check in checks:
        marker = "" if check.ok else "  <-- SPACING"
        reference = f" ref={os.path.basename(check.reference)}" if check.reference else ""
        lines.append(
            f"  {check.name:32s} median={check.median_ratio:.2f} "
            f"compared={check.compared:3d}{reference}{marker}"
        )
        if check.zero_advance:
            lines.append(f"      zero advance: {''.join(check.zero_advance)!r}")
        if check.too_narrow:
            lines.append(f"      too narrow: {''.join(check.too_narrow)!r}")
        if check.inconsistent:
            lines.append(f"      inconsistent: {''.join(check.inconsistent)!r}")
        if check.mismatched:
            lines.append(f"      disagrees with the reference: {''.join(check.mismatched)!r}")
        if check.excluded:
            lines.append(
                f"      excluded from the comparison (the reference is a "
                f"placeholder or blank): {''.join(check.excluded)!r}"
            )
        for problem in check.problems:
            lines.append(f"      PROBLEM: {problem}")
    summary = summarise(checks)
    lines.append(
        f"\n{summary['fonts']} fonts, {summary['zero_advance']} zero advances, "
        f"{summary['inconsistent']} inconsistent letters, "
        f"{summary['mismatched']} disagreeing with their reference"
    )
    return "\n".join(lines)
