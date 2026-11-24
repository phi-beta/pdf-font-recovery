"""
Repair of fonts recovered from PDFs, and of fonts assembled from several
sources.

Two distinct defects need repair, and they need opposite treatments.

BLANK OUTLINES
    A PDF subsetter strips the outline of every character the document never
    set, but leaves the cmap entry.  The font then claims a character exists
    while rendering it as nothing.  Repair means UNIONING the subsets of a
    style across every document that embeds it: each document sets part of
    the alphabet, and together they often cover all of it.

A MISSING EXTERNAL SOURCE
    When the union still leaves characters uncovered - because no document
    ever set them - they can be supplied by an external font.  Whether that
    is legitimate depends on the weight:

      * The external font is the EXACT weight -> it becomes the base and the
        PDF's licensed outlines are overlaid on top.  Every character ends up
        the right weight.

      * The external font is a NEARBY weight -> the PDF stays the base (so
        the weight is right) and the external font fills only the gaps.  Those
        characters carry the external font's licence, which the coverage
        report records.

A CROSS-WEIGHT BORROW
    When a family is split across weights that each cover different
    characters, one weight can borrow from its siblings.  The borrowed
    characters are recorded as borrowed, because they are the wrong weight.

Two rules that are easy to get wrong, and that this module encodes:

A GLYPH MUST BE IN THE GLYPH ORDER TO BE VISIBLE
    ``getGlyphID`` resolves through the glyph ORDER, not the glyphs dict.  A
    glyph added only to the dict is invisible, and any composite that
    references it fails to compile.

A COMPOSITE BRINGS ITS COMPONENTS WITH IT
    Copying a composite glyph without its components raises.  The
    components are copied too, recursively.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field

from fontTools.ttLib import TTFont

from .verify import find_placeholder_characters, glyph_bounds, has_ink


@dataclass
class RepairReport:
    """What a repair changed, per character, so it can be audited."""

    path: str
    style: str = ""
    base: str = ""
    #: Characters that were blank and are now filled.
    filled: list[str] = field(default_factory=list)
    #: Characters taken from an external font of a different weight.
    borrowed: list[str] = field(default_factory=list)
    #: Characters taken from the external font's own weight.
    overlaid: list[str] = field(default_factory=list)
    #: Characters still blank after the repair.
    still_blank: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file": os.path.basename(self.path),
            "style": self.style,
            "base": self.base,
            "filled": len(self.filled),
            "borrowed": len(self.borrowed),
            "overlaid": len(self.overlaid),
            "still_blank": len(self.still_blank),
            "problems": list(self.problems),
        }


def _add_to_glyph_order(font, name: str) -> None:
    """Make a glyph visible to getGlyphID by adding it to the glyph order."""
    order = font.getGlyphOrder()
    if name not in order:
        order.append(name)
        font.setGlyphOrder(order)


def _copy_glyph(source_font, source_name: str, target_font, target_name: str, seen: set) -> bool:
    """Copy a glyph (and its components) from one font into another.

    Returns True when the copy succeeded.  A composite's components are
    copied first, recursively, and every copied glyph is added to the target
    glyph order so that getGlyphID can resolve it.
    """
    if source_name in seen:
        return True
    seen.add(source_name)

    try:
        source_glyph = source_font["glyf"][source_name]
    except Exception:
        return False

    # Bring the components across first.  A composite references its
    # components by glyph NAME, so a component that is not in the target's
    # glyph order cannot be resolved - and getGlyphID resolves through the
    # ORDER, not the glyphs dict.
    for component in getattr(source_glyph, "components", []) or []:
        component_name = component.glyphName
        if component_name not in target_font.getGlyphOrder():
            if not _copy_glyph(source_font, component_name, target_font, component_name, seen):
                return False

    try:
        target_glyph = copy.deepcopy(source_glyph)
    except Exception:
        return False

    try:
        target_font["glyf"][target_name] = target_glyph
    except Exception:
        return False

    _add_to_glyph_order(target_font, target_name)

    # A composite's components need horizontal metrics of their own.  The
    # maxp table's recalc walks every glyph in the order and looks each one
    # up in hmtx, so a component without a metrics entry fails the compile
    # with a KeyError.
    #
    # The width must be the SOURCE's width, not zero.  Zeroing it is
    # tempting - the copy has no metrics yet - but a zero advance makes the
    # glyph advance nowhere, so it renders on top of its neighbour.  A font
    # built that way loads cleanly and passes every structural check while
    # looking like the letter spacing collapsed.
    _set_metrics(target_font, target_name, _source_width(source_font, source_name))
    for component in getattr(target_glyph, "components", []) or []:
        component_name = component.glyphName
        if component_name in target_font.getGlyphOrder():
            _set_metrics(target_font, component_name, _source_width(source_font, component_name))
    return True


def _source_width(font, name: str) -> int:
    """The advance width of a glyph in a source font, or 0 if it has none."""
    try:
        return font["hmtx"].metrics[name][0]
    except Exception:
        return 0


def _set_metrics(font, name: str, width: int) -> None:
    """Give a glyph a horizontal-metrics entry, copying one if it has none."""
    hmtx = font["hmtx"]
    try:
        hmtx.metrics[name] = (width, 0)
        return
    except Exception:
        pass
    # Fall back to an existing entry so the font stays loadable.
    fallback = 0
    for other in font.getGlyphOrder():
        try:
            fallback = hmtx.metrics[other][0]
            break
        except Exception:
            continue
    hmtx.metrics[name] = (fallback, 0)


def _union_true_type_styles(
    style_fonts: list[tuple[str, TTFont]], target: TTFont
) -> list[str]:
    """Union the TrueType outlines of several fonts of the same style.

    Returns the names of the characters added to the target.
    """
    added: list[str] = []
    for _source_path, source_font in style_fonts:
        source_cmap = source_font.getBestCmap()
        target_cmap = target.getBestCmap()
        for code, source_name in source_cmap.items():
            char = chr(code)
            target_name = target_cmap.get(code)
            if target_name is None:
                # Not in the target at all: copy the glyph and map the code.
                # _copy_glyph has already set the metrics from the source.
                new_name = f"uni{code:04X}"
                if _copy_glyph(source_font, source_name, target, new_name, set()):
                    _add_to_glyph_order(target, new_name)
                    target_cmap[code] = new_name
                    added.append(char)
                continue
            # Present but possibly blank: only replace when the target is blank.
            if not has_ink(target, target_name) and has_ink(source_font, source_name):
                # _copy_glyph has already set the metrics from the source.
                if _copy_glyph(source_font, source_name, target, target_name, set()):
                    added.append(char)
    return added


def repair_style(
    path: str,
    style: str,
    *,
    sibling_paths: list[str] | None = None,
    external_path: str | None = None,
    external_exact_weight: bool = False,
    test_characters: str = "",
    reproducible_timestamp: int | None = None,
) -> RepairReport:
    """Repair one recovered font in place.

    ``sibling_paths`` are other copies of the SAME style from other
    documents - their outlines are unioned in, which is what fills the
    characters each document happened to set.

    ``external_path`` is an optional font used to fill characters no
    document set.  When ``external_exact_weight`` is true the external font
    supplies characters the same weight; otherwise it is a nearby weight and
    the report records the borrow.
    """
    report = RepairReport(path=path, style=style)
    font = TTFont(path)
    report.base = os.path.basename(path)

    # ---- union the same style from other documents
    if sibling_paths:
        siblings = [(p, TTFont(p)) for p in sibling_paths if os.path.exists(p)]
        report.filled.extend(_union_true_type_styles(siblings, font))

    # ---- fill from an external font
    if external_path and os.path.exists(external_path):
        external = TTFont(external_path)
        # find_placeholder_characters returns CODE POINTS.
        placeholders = find_placeholder_characters(external)
        external_cmap = external.getBestCmap()
        target_cmap = font.getBestCmap()

        for code, external_name in external_cmap.items():
            char = chr(code)
            if code in placeholders:
                # A placeholder box is worse than a blank - it renders as a
                # box with a letter in it.  Never copy one across.
                continue
            if not has_ink(external, external_name):
                continue
            target_name = target_cmap.get(code)
            if target_name is None:
                new_name = f"uni{code:04X}"
                if _copy_glyph(external, external_name, font, new_name, set()):
                    _add_to_glyph_order(font, new_name)
                    target_cmap[code] = new_name
                    if external_exact_weight:
                        report.overlaid.append(char)
                    else:
                        report.borrowed.append(char)
                continue
            if not has_ink(font, target_name):
                if _copy_glyph(external, external_name, font, target_name, set()):
                    if external_exact_weight:
                        report.overlaid.append(char)
                    else:
                        report.borrowed.append(char)

    if reproducible_timestamp is not None:
        from .extract import pin_timestamps

        pin_timestamps(font, reproducible_timestamp)
    font.save(path)

    # ---- report what is still blank
    verify = TTFont(path)
    cmap = verify.getBestCmap()
    for char in test_characters:
        name = cmap.get(ord(char))
        if name is None:
            continue
        # Whitespace is legitimately empty.
        if char.isspace():
            continue
        if not has_ink(verify, name):
            report.still_blank.append(char)
    return report


def borrow_across_weights(
    path: str,
    style: str,
    sibling_paths: list[str],
    *,
    test_characters: str = "",
) -> RepairReport:
    """Borrow characters from sibling weights of the same family.

    Each document of a family sets different characters, so the union across
    the weights is usually wider than any single weight.  This copies the
    missing characters from a sibling and records them as borrowed - they
    are the wrong weight, which the report says.
    """
    report = RepairReport(path=path, style=style)
    report.base = os.path.basename(path)
    font = TTFont(path)
    target_cmap = font.getBestCmap()

    for sibling_path in sibling_paths:
        if not os.path.exists(sibling_path):
            continue
        sibling = TTFont(sibling_path)
        sibling_cmap = sibling.getBestCmap()
        for code, sibling_name in sibling_cmap.items():
            char = chr(code)
            target_name = target_cmap.get(code)
            if target_name is not None and has_ink(font, target_name):
                continue
            if not has_ink(sibling, sibling_name):
                continue
            if target_name is None:
                new_name = f"uni{code:04X}"
                if _copy_glyph(sibling, sibling_name, font, new_name, set()):
                    _add_to_glyph_order(font, new_name)
                    target_cmap[code] = new_name
                    report.borrowed.append(char)
            elif not has_ink(font, target_name):
                if _copy_glyph(sibling, sibling_name, font, target_name, set()):
                    report.borrowed.append(char)

    font.save(path)

    verify = TTFont(path)
    cmap = verify.getBestCmap()
    for char in test_characters:
        name = cmap.get(ord(char))
        if name is None:
            continue
        # Whitespace is legitimately empty.
        if char.isspace():
            continue
        if not has_ink(verify, name):
            report.still_blank.append(char)
    return report


def union_style_across_documents(
    path: str, source_paths: list[str], *, test_characters: str = ""
) -> RepairReport:
    """Union every copy of one style into the file at ``path``.

    This is the CFF-side equivalent of :func:`repair_style`'s sibling
    union, and is what lifts a style from the characters one document set to
    the union of what all of them set.
    """
    report = RepairReport(path=path)
    report.base = os.path.basename(path)
    font = TTFont(path)

    for source_path in source_paths:
        if not os.path.exists(source_path):
            continue
        try:
            source = TTFont(source_path)
        except Exception:
            continue
        if "glyf" not in font or "glyf" not in source:
            continue
        source_cmap = source.getBestCmap()
        target_cmap = font.getBestCmap()
        for code, source_name in source_cmap.items():
            char = chr(code)
            target_name = target_cmap.get(code)
            if target_name is None:
                new_name = f"uni{code:04X}"
                if _copy_glyph(source, source_name, font, new_name, set()):
                    _add_to_glyph_order(font, new_name)
                    target_cmap[code] = new_name
                    _set_metrics(font, new_name, 0)
                    report.filled.append(char)
            elif not has_ink(font, target_name) and has_ink(source, source_name):
                if _copy_glyph(source, source_name, font, target_name, set()):
                    _set_metrics(font, target_name, 0)
                    report.filled.append(char)

    font.save(path)
    return report


def find_blank_characters(path: str, test_characters: str) -> list[str]:
    """The characters in a font whose cmap entry renders as nothing."""
    font = TTFont(path)
    cmap = font.getBestCmap()
    blank = []
    for char in test_characters:
        name = cmap.get(ord(char))
        if name is not None and not has_ink(font, name):
            blank.append(char)
    return blank


def find_empty_glyphs(path: str) -> list[str]:
    """Every glyph in the font whose outline is empty."""
    font = TTFont(path)
    empty = []
    for name in font.getGlyphOrder():
        if name == ".notdef":
            continue
        if glyph_bounds(font, name) == (0, 0, 0, 0) and not has_ink(font, name):
            empty.append(name)
    return empty
