"""
Cross-weight borrowing for CFF fonts.

A PDF corpus sets different characters in different documents, and
different documents often use different weights of the same family.  So
the union of the characters available across the family's weights is
usually wider than the characters any one weight carries.

A weight that is missing a character can BORROW it from a sibling weight.
The borrowed glyph is the right letterform in the WRONG weight, and the
report says so - a font with borrowed figures looks right and is not what
its name claims.

This is the CFF counterpart of the TrueType sibling union in
:mod:`pdf_font_recovery.repair`.  It works on charstrings rather than on
glyf outlines, which makes it considerably more delicate.

THE FOUR THINGS THAT GO WRONG

1.  CharStrings are INDEX-based.  ``CharStrings.charStrings`` is an index
    into an array, not a name->object mapping, and assigning into it
    raises.  The write path converts it to a plain dict first.

2.  A donor charstring's subroutine indices refer to the DONOR's private
    dictionary.  The target's private dictionary is different, so the
    subr calls resolve to the wrong code - or to nothing.  Every donor
    charstring is desubroutinized before it is moved, which inlines the
    calls and removes the dependency.

3.  The private dict and the global subrs live on the TOP DICT, not on
    the CharStrings object.  A charstring needs them to resolve its
    calls, so both are set explicitly on every copied charstring.

4.  Every glyph needs an hmtx entry, and a borrowed glyph needs the width
    of the glyph it came from.  The width is measured from the charstring
    with a T2WidthExtractor and scaled from the CFF's own design units.

The borrowed glyphs are named ``uniXXXX.borrow`` so they can never collide
with the style's own glyphs, and so a reader can see at a glance which
glyphs are not this weight.
"""
from __future__ import annotations

import copy
import os

from fontTools.misc.psCharStrings import T2WidthExtractor
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

#: The suffix appended to a borrowed glyph's name.
BORROW_MARK = "borrow"


def desubroutinize(cff) -> None:
    """Inline every subroutine call in a CFF font, in place.

    A charstring's subroutine indices are relative to the private
    dictionary of the font it came from.  Moving a charstring into a
    different font without inlining its calls means the indices resolve
    against the wrong dictionary.

    fontTools provides this as ``fontTools.cffLib.transforms.desubroutinize``;
    the local import keeps the module's dependency surface small and makes
    the failure mode explicit.
    """
    from fontTools.cffLib.transforms import desubroutinize as _desubroutinize

    _desubroutinize(cff)


def _unicode_of(glyph_name: str) -> str | None:
    """The text a glyph name maps to, or None if it maps to nothing.

    Handles the AGL names (``A``, ``uni0041``), the ``uXXXX`` form, and
    the ``uniXXXX.suffix`` form this module creates for borrowed glyphs.
    """
    if not glyph_name:
        return None
    name = glyph_name
    # A borrowed glyph is named uniXXXX.borrow.
    if "." in name:
        name = name.split(".", 1)[0]
    if name.startswith("uni") and len(name) >= 7:
        try:
            return chr(int(name[3:7], 16))
        except ValueError:
            return None
    if name.startswith("u") and 5 <= len(name) <= 7:
        try:
            return chr(int(name[1:], 16))
        except ValueError:
            return None
    return name if len(name) == 1 else None


def _char_key(text: str) -> str:
    """The key a glyph is filed under.

    A ligature maps to several characters; it is filed under the first
    one, so a single code point never points at a two-character glyph.
    """
    return text if len(text) == 1 else text[0]


def _readable_charstrings(font: TTFont) -> tuple[object, object, dict]:
    """The font's charstrings, made writable.

    Returns (charstrings, top_dict, private_dict).  The charstrings
    object is converted from its indexed form to a plain dict so that
    glyphs can be added by name.
    """
    cff = font["CFF "].cff
    desubroutinize(cff)
    top = cff[cff.fontNames[0]]
    charstrings = top.CharStrings
    if getattr(charstrings, "charStringsAreIndexed", False):
        plain = {name: charstrings[name] for name in charstrings.keys()}
        charstrings.charStrings = plain
        charstrings.charStringsAreIndexed = False
    return charstrings, top, cff


def collect_style_glyphs(paths: list[str]) -> dict[str, tuple]:
    """The characters each font in ``paths`` provides.

    Returns a map of character -> (charstring, top_dict, cff_set), with
    the FIRST file in ``paths`` winning for any character it provides.
    The order of ``paths`` is therefore significant, and callers pass a
    sorted list.
    """
    out: dict[str, tuple] = {}
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            font = TTFont(path)
            if "CFF " not in font:
                continue
            charstrings, top, cff = _readable_charstrings(font)
        except Exception:
            continue
        for glyph_name in list(charstrings.keys()):
            text = _unicode_of(glyph_name)
            if not text:
                continue
            key = _char_key(text)
            if key in out:
                continue
            out[key] = (charstrings[glyph_name], top, cff)
    return out


def borrow_characters(
    path: str,
    donor_paths: list[str],
    *,
    reproducible_timestamp: int | None = None,
) -> dict:
    """Borrow missing characters from other weights of the same family.

    The font at ``path`` keeps its own glyphs; every character it lacks is
    taken from the union of the donors.

    Returns a dict with the counts, for the report.
    """
    result = {"borrowed": 0, "overlay": 0, "errors": []}

    font = TTFont(path)
    charstrings, top, _cff = _readable_charstrings(font)
    private = top.Private
    global_subrs = top.GlobalSubrs

    # The style's own glyphs, by character.
    own: dict[str, str] = {}
    for glyph_name in charstrings.keys():
        text = _unicode_of(glyph_name)
        if not text:
            continue
        own.setdefault(_char_key(text), glyph_name)

    union = collect_style_glyphs(donor_paths)

    # ---- overlay the donors' versions of the style's OWN characters
    #
    # The style's own glyphs win on a tie, but a donor may carry a better
    # outline for a character the style sets as a blank placeholder.
    for key, (donor_charstring, _donor_top, _donor_cff) in union.items():
        glyph_name = own.get(key)
        if glyph_name is None:
            continue
        try:
            copied = copy.deepcopy(donor_charstring)
            copied.private = private
            copied.globalSubrs = global_subrs
            charstrings[glyph_name] = copied
            result["overlay"] += 1
        except Exception as exc:
            result["errors"].append(f"overlay {key!r}: {type(exc).__name__}: {exc}")

    # ---- borrow the characters the style does not have at all
    for key, (donor_charstring, _donor_top, _donor_cff) in union.items():
        if key in own:
            continue
        new_name = f"uni{ord(key):04X}.{BORROW_MARK}"
        if new_name in charstrings.keys():
            continue
        try:
            copied = copy.deepcopy(donor_charstring)
            copied.private = private
            copied.globalSubrs = global_subrs
            charstrings[new_name] = copied
            result["borrowed"] += 1
        except Exception as exc:
            result["errors"].append(f"borrow {key!r}: {type(exc).__name__}: {exc}")

    _extend_cmap(font, charstrings)
    top.charset = list(charstrings.keys())
    font["CFF "].cff = _cff
    _ensure_metrics(font, charstrings, top)
    _extend_glyph_order(font, charstrings)

    if reproducible_timestamp is not None:
        from .extract import pin_timestamps

        pin_timestamps(font, reproducible_timestamp)
    font.save(path)
    return result


def _extend_glyph_order(font: TTFont, charstrings) -> None:
    """Add any charstring that is not in the glyph order to the order.

    ``getGlyphID`` resolves through the glyph ORDER, not the charstrings
    dict.  A borrowed glyph that is not in the order is invisible: the
    cmap points at it, but the lookup raises a KeyError, and the compile
    fails with a message that names the glyph rather than the cause.
    """
    order = list(font.getGlyphOrder())
    known = set(order)
    for glyph_name in charstrings.keys():
        if glyph_name in known:
            continue
        order.append(glyph_name)
        known.add(glyph_name)
    font.setGlyphOrder(order)


def _extend_cmap(font: TTFont, charstrings) -> None:
    """EXTEND the cmap with the borrowed characters.

    The existing cmap is the authority on which characters the font
    provides - it carries entries that the glyph names alone cannot
    reproduce, because a subsetter's glyph names are not always AGL names.
    So the existing mapping is KEPT and only the borrowed characters are
    added.

    Rebuilding the cmap from the glyph names instead would silently drop
    every character whose name did not round-trip - which is how a font
    goes from 73 mapped characters to 49 with no error anywhere.
    """
    existing = font.getBestCmap()
    mapping: dict[int, str] = {code: name for code, name in existing.items()}

    for glyph_name in charstrings.keys():
        text = _unicode_of(glyph_name)
        if not text:
            continue
        if len(text) == 1:
            code = ord(text)
        else:
            code = ord(text[0])
        # A borrowed character fills a gap; it never displaces a character
        # the font already provides.
        if code in mapping:
            continue
        mapping[code] = glyph_name

    if not mapping:
        return

    table = newTable("cmap")
    table.tableVersion = 0

    unicode = CmapSubtable.newSubtable(4)
    unicode.platformID, unicode.platEncID, unicode.language = 3, 1, 0
    unicode.cmap = mapping

    mac = CmapSubtable.newSubtable(0)
    mac.platformID, mac.platEncID, mac.language = 1, 0, 0
    mac.cmap = {code: name for code, name in mapping.items() if code < 256}

    table.tables = [unicode, mac]
    font["cmap"] = table


def _ensure_metrics(font: TTFont, charstrings, top) -> None:
    """Give every glyph a horizontal-metrics entry.

    A borrowed glyph needs the width of the glyph it came from, so the
    width is measured from the charstring.  The measurement is scaled from
    the CFF's own design units, which are not necessarily 1000.
    """
    hmtx = font["hmtx"]

    design_units = 1000
    font_matrix = (top.rawDict or {}).get("FontMatrix")
    if font_matrix and font_matrix[0]:
        design_units = int(round(1.0 / font_matrix[0]))
    scale = font["head"].unitsPerEm / float(design_units)

    private_raw = (top.Private.rawDict or {}) if top.Private else {}
    nominal = private_raw.get("nominalWidthX", 0)
    default = private_raw.get("defaultWidthX", 0)
    local = getattr(top.Private, "Subrs", None) or []

    for glyph_name in charstrings.keys():
        if glyph_name in hmtx.metrics:
            continue
        width = default
        try:
            extractor = T2WidthExtractor(local, top.GlobalSubrs, nominal, default, top.Private)
            extractor.execute(charstrings[glyph_name])
            width = extractor.width
        except Exception:
            width = default
        hmtx.metrics[glyph_name] = (int(round(width * scale)), 0)
