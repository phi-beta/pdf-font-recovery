"""
Verification of recovered fonts.

Three checks, in increasing order of what they cost and what they catch:

INK
    A font can carry a cmap entry for a character whose outline is EMPTY -
    exactly what a PDF subsetter produces for a character the document never
    set.  The cmap says the character exists; the glyph says it is blank.
    Only rasterising reveals which is true.  This is the check that found the
    real defect in the source corpus: 604 characters that looked present
    and rendered as nothing.

PLACEHOLDER
    Worse than blank is a placeholder - a shared missing-glyph box.  A
    character whose outline is identical to several other characters' is a
    box, not a letterform.  Copying one across is how a broken font spreads.

METADATA
    Family/subfamily names, style bits and the weight class must agree, so
    that an operating system groups the weights of a family together.
"""
from __future__ import annotations

import collections
import os
from dataclasses import dataclass, field

from fontTools.pens.basePen import NullPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont


@dataclass
class FontCheck:
    """The verification result for one font file."""

    path: str
    family: str = ""
    subfamily: str = ""
    weight: int = 0
    italic: bool = False
    glyphs: int = 0
    upem: int = 0
    #: Characters that are present in the cmap and render ink.
    ok: list[str] = field(default_factory=list)
    #: Characters present in the cmap whose outline is empty.
    blank: list[str] = field(default_factory=list)
    #: Characters not in the cmap at all.
    missing: list[str] = field(default_factory=list)
    #: Characters whose outline is a shared missing-glyph box.
    placeholders: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def ok_count(self) -> int:
        return len(self.ok)

    def to_dict(self) -> dict:
        return {
            "file": self.name,
            "family": self.family,
            "subfamily": self.subfamily,
            "weight": self.weight,
            "italic": self.italic,
            "glyphs": self.glyphs,
            "upem": self.upem,
            "ok": len(self.ok),
            "blank": len(self.blank),
            "missing": len(self.missing),
            "placeholders": len(self.placeholders),
            "problems": list(self.problems),
        }


def glyph_bounds(font, name: str) -> tuple[int, int, int, int]:
    """The bounding box of a TrueType glyph, or (0,0,0,0) if it has none."""
    glyf = font["glyf"]
    try:
        glyph = glyf[name]
    except KeyError:
        return (0, 0, 0, 0)
    if hasattr(glyph, "xMin"):
        return (glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax)
    try:
        pen = BoundsPen(NullPen())
        glyph.draw(pen, glyf)
        return pen.bounds or (0, 0, 0, 0)
    except Exception:
        return (0, 0, 0, 0)


def has_ink(font, name: str) -> bool:
    """TrueType only: does this glyph have contours?"""
    try:
        return getattr(font["glyf"][name], "numberOfContours", 0) != 0
    except Exception:
        return False


def find_placeholder_characters(font) -> set[str]:
    """Characters whose glyph is a shared missing-glyph box.

    A placeholder is detected by its SHARED bounding box: several characters
    whose outlines are byte-for-byte the same box are all placeholders, not
    letterforms.  The trial in the source corpus had exactly this - its ten
    digits were all one box - which is why they had to be excluded from any
    copy-across.

    A single character with a unique box is NOT a placeholder; a genuine
    letterform can be a simple shape.
    """
    cmap = font.getBestCmap()
    boxes: collections.Counter = collections.Counter()
    for _char, name in cmap.items():
        boxes[glyph_bounds(font, name)] += 1
    if not boxes:
        return set()
    common, count = boxes.most_common(1)[0]
    if count <= 3:
        return set()
    # The cmap is keyed by CODE POINT, so the result is a set of code points.
    # The caller turns them into characters.
    return {
        code
        for code, name in cmap.items()
        if has_ink(font, name) and glyph_bounds(font, name) == common
    }


def check_font(path: str, test_characters: str) -> FontCheck:
    """Run every check against one font file."""
    from PIL import Image, ImageDraw, ImageFont

    result = FontCheck(path=path)
    try:
        font = TTFont(path)
    except Exception as exc:
        result.problems.append(f"will not open: {type(exc).__name__}: {exc}")
        return result

    try:
        result.family = font["name"].getDebugName(1) or ""
        result.subfamily = font["name"].getDebugName(2) or ""
    except Exception:
        pass
    try:
        result.weight = font["OS/2"].usWeightClass
        result.italic = bool(font["OS/2"].fsSelection & 0x01)
    except Exception:
        pass
    try:
        result.upem = font["head"].unitsPerEm
    except Exception:
        pass

    cmap = font.getBestCmap()
    result.glyphs = len(cmap)

    # The placeholder detection is TrueType-only: it reads the glyf table.
    if "glyf" in font:
        try:
            result.placeholders = sorted(
                chr(code) for code in find_placeholder_characters(font)
            )
        except Exception:
            result.placeholders = []

    try:
        pil_font = ImageFont.truetype(path, 48)
    except Exception as exc:
        result.problems.append(f"will not rasterise: {type(exc).__name__}: {exc}")
        return result

    for char in test_characters:
        if ord(char) not in cmap:
            result.missing.append(char)
            continue
        # A space is legitimately empty.  Whitespace glyphs have no outline
        # by definition, so testing them for ink would report every font in
        # the world as having a blank character.
        if char.isspace():
            result.ok.append(char)
            continue
        image = Image.new("L", (90, 90), 255)
        draw = ImageDraw.Draw(image)
        draw.text((10, 10), char, font=pil_font, fill=0)
        bbox = image.point(lambda value: 255 - value).getbbox()
        if bbox is None:
            result.blank.append(char)
        else:
            result.ok.append(char)

    return result


def check_directory(
    directory: str, test_characters: str, *, extensions: tuple[str, ...] = (".ttf", ".otf")
) -> list[FontCheck]:
    """Check every font in a directory, in sorted order."""
    paths = []
    for name in sorted(os.listdir(directory)):
        if name.lower().endswith(extensions):
            paths.append(os.path.join(directory, name))
    return [check_font(path, test_characters) for path in paths]


def summarise(checks: list[FontCheck]) -> dict:
    """Aggregate the per-font checks into a summary."""
    return {
        "fonts": len(checks),
        "total_blank": sum(len(c.blank) for c in checks),
        "total_missing": sum(len(c.missing) for c in checks),
        "total_placeholders": sum(len(c.placeholders) for c in checks),
        "fonts_with_problems": sum(1 for c in checks if c.problems),
    }


def format_report(checks: list[FontCheck]) -> str:
    """A human-readable report of the checks."""
    lines = []
    for check in checks:
        flag = "  <-- BLANK OUTLINES" if check.blank else ""
        lines.append(
            f"  {check.name:32s} ok={check.ok_count:3d} blank={len(check.blank):3d} "
            f"missing={len(check.missing):3d}{flag}"
        )
        if check.blank:
            lines.append(f"      blank: {''.join(check.blank)!r}")
        if check.placeholders:
            lines.append(f"      placeholder box: {''.join(check.placeholders)!r}")
        for problem in check.problems:
            lines.append(f"      PROBLEM: {problem}")
    summary = summarise(checks)
    lines.append(
        f"\n{summary['fonts']} fonts, {summary['total_blank']} blank-outline "
        f"characters, {summary['total_placeholders']} placeholder characters"
    )
    return "\n".join(lines)
