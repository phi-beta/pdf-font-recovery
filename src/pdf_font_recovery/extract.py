"""
Extraction of embedded fonts from PDF documents.

PDF documents embed fonts as *subsets*: a program that contains only the
glyphs the document happened to set, and often no usable cmap, name table
or metrics at all.  Two shapes appear:

  * TrueType - PyMuPDF returns a complete, installable TTF with a real cmap.
  * CFF - PyMuPDF returns a bare CFF table holding only the glyphs that
    document used: no cmap, no name table, no metrics.  We merge every
    subset of a style into one CFF, then wrap the result in a fresh OpenType
    container with a generated cmap, name table, OS/2 and post table so the
    result is installable.

Three details that matter and are easy to get wrong:

UNITS PER EM
    The embedded CFFs use a FontMatrix of 1/2048 (the original Type1 outline
    space), not 1/1000.  If the container is built at 1000 units/em while the
    CFF keeps its 1/2048 matrix, every glyph renders at roughly half size and
    the advance widths do not match the outlines.  We build the container at
    the CFF's own design units so the FontMatrix and the charstring widths
    agree with no scaling.

WIDTHS
    A decompiled charstring carries no width until it has been run through a
    T2WidthExtractor with the font's nominal/default widths.  We do that
    explicitly rather than guessing.

CHARSTRINGS ARE INDEXED
    ``CharStrings.charStrings`` is an *index* into an array of charstrings,
    not a name->object mapping.  The supported accessor is
    ``CharStrings[name]``, which resolves the index.  Assigning into an
    index-based CharStrings raises - which is why the write path converts it
    to a plain dict first (see :func:`_mutable_charstrings`).
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field

from fontTools.agl import toUnicode
from fontTools.cffLib import (
    CFFFontSet,
    CharStrings,
    GlobalSubrsIndex,
    PrivateDict,
    TopDict,
    TopDictIndex,
)
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.psCharStrings import T2WidthExtractor
from fontTools.ttLib import TTFont, newTable

#: Formats PyMuPDF reports that we cannot use.
UNUSABLE_FORMATS = frozenset({"cid", "t1"})


@dataclass
class EmbeddedFont:
    """One embedded program found in the corpus."""

    #: The subset name with any PDF prefix stripped, e.g. "Mundial-Regular".
    style: str
    #: The format extension PyMuPDF reported, e.g. "ttf", "otf", "cff".
    fmt: str
    #: The raw font program.
    data: bytes
    #: The PDF the program came from.
    source: str
    #: The page the program was first referenced on.
    page: int = 0


@dataclass
class ExtractedFont:
    """A built font, ready to write."""

    style: str
    path: str
    family: str
    subfamily: str
    weight: int
    italic: bool
    fmt: str
    glyphs: int
    upem: int
    source: str
    merged_glyphs: int = 0
    notes: list[str] = field(default_factory=list)


def collect_embedded_fonts(pdf_paths: list[str]) -> dict[tuple[str, str], list[EmbeddedFont]]:
    """Walk every PDF and group the embedded programs by (style, format).

    The returned dict's insertion order follows the sorted input order, which
    is what makes the merge deterministic: the first source that provides a
    glyph wins, so an unstable input order would make the output unstable.
    """
    import pymupdf

    grouped: dict[tuple[str, str], list[EmbeddedFont]] = {}
    for pdf_path in pdf_paths:
        try:
            doc = pymupdf.open(pdf_path)
        except Exception:
            continue
        try:
            for page_no in range(len(doc)):
                try:
                    fonts = doc.get_page_fonts(page_no, full=True)
                except Exception:
                    continue
                for entry in fonts:
                    xref = entry[0]
                    if not xref:
                        continue
                    try:
                        info = doc.extract_font(xref)
                    except Exception:
                        continue
                    name, fext, _ftype, data = info[0], info[1], info[2], info[3]
                    if not data or not name:
                        continue
                    # PDF subset names carry a six-letter prefix, e.g.
                    # "ABCDEF+Mundial-Regular".
                    style = name.split("+")[-1]
                    key = (style, fext)
                    grouped.setdefault(key, []).append(
                        EmbeddedFont(
                            style=style,
                            fmt=fext,
                            data=data,
                            source=os.path.basename(pdf_path),
                            page=page_no,
                        )
                    )
        finally:
            doc.close()
    return grouped


def merge_cff(chunks: list[EmbeddedFont]) -> tuple[object, dict, int, str] | None:
    """Merge bare CFF byte strings into one glyph set.

    Returns ``(base_top_dict, merged_charstrings, added, base_source)`` or
    ``None`` if no chunk could be parsed.

    NOTE: CharStrings.charStrings is an *index* into an array of charstrings,
    not a name->object mapping.  The supported accessor is CharStrings[name],
    which resolves the index.
    """
    parsed = []
    for chunk in chunks:
        try:
            cff = CFFFontSet()
            cff.decompile(io.BytesIO(chunk.data), None)
            top = cff[cff.fontNames[0]]
        except Exception:
            continue
        parsed.append((top, chunk.source))
    if not parsed:
        return None

    # The base is the richest subset: it carries the most complete private
    # dict, and therefore the most complete local subrs.
    base, base_source = max(parsed, key=lambda x: len(x[0].CharStrings.keys()))
    merged = {gn: base.CharStrings[gn] for gn in base.CharStrings.keys()}
    added = 0
    for top, _src in parsed:
        if top is base:
            continue
        for gn in top.CharStrings.keys():
            if gn not in merged:
                merged[gn] = top.CharStrings[gn]
                added += 1
    return base, merged, added, base_source


def cff_metrics(top, charstrings: dict | None = None) -> tuple[int, int, int]:
    """Return (design_units, nominalWidthX, defaultWidthX) implied by the CFF.

    The design units are what the CFF's own widths are measured in.  They
    come from the FontMatrix: a matrix of 1/2048 means the widths are in
    2048-unit space.

    A CFF with NO FontMatrix is the common case for an embedded PDF font,
    and then the design units have to be INFERRED.  Guessing is not safe:
    a wrong guess scales every width, and the font still loads and passes
    every structural check while the letters land on top of each other.

    The inference uses the font's own widths.  A design space has to be
    big enough to hold the font's widest advance, so the smallest
    conventional space (1000) that fits is the answer:

      * the PDF's Cellofy has a widest advance of 983 - it fits in 1000
        units, so it is a 1000-unit font.  The publisher's own file agrees:
        upem 1000, same advances.
      * the PDF's Mundial has a widest advance of about 2010 - it does not
        fit in 1000, so it is a 2048-unit font.

    When there is no FontMatrix and no charstrings to measure, 2048 is
    used: the embedded fonts in this corpus are overwhelmingly Type1
    outlines in 2048-unit space.
    """
    font_matrix = (top.rawDict or {}).get("FontMatrix")
    if font_matrix and font_matrix[0]:
        design_units = int(round(1.0 / font_matrix[0]))
    else:
        design_units = _infer_design_units(top, charstrings)
    private = top.Private.rawDict or {}
    return design_units, private.get("nominalWidthX", 0), private.get("defaultWidthX", 0)


#: The conventional design spaces, smallest first.  The first one that can
#: hold the font's widest advance is the answer.
CONVENTIONAL_DESIGN_UNITS = (1000, 2048)


def _infer_design_units(top, charstrings: dict | None) -> int:
    """Infer the CFF's design units from the widths it actually carries."""
    widest = 0
    if charstrings:
        try:
            from fontTools.misc.psCharStrings import T2WidthExtractor

            private = top.Private.rawDict or {}
            local = getattr(top.Private, "Subrs", None) or []
            for charstring in charstrings.values():
                try:
                    extractor = T2WidthExtractor(
                        local,
                        top.GlobalSubrs,
                        private.get("nominalWidthX", 0),
                        private.get("defaultWidthX", 0),
                        top.Private,
                    )
                    extractor.execute(charstring)
                    widest = max(widest, int(extractor.width or 0))
                except Exception:
                    continue
        except Exception:
            widest = 0

    if widest <= 0:
        # Nothing to measure: fall back to the widest advance in the
        # private dict, which is the font's own statement about its
        # width space.
        private = top.Private.rawDict or {}
        widest = max(
            int(private.get("nominalWidthX", 0) or 0),
            int(private.get("defaultWidthX", 0) or 0),
        )

    for units in CONVENTIONAL_DESIGN_UNITS:
        if widest <= units:
            return units
    # Wider than any conventional space: round up to the next power of two.
    units = 1000
    while units < widest:
        units *= 2
    return units


def compute_widths(top, charstrings: dict, upem: int) -> dict[str, int]:
    """Advance width of every glyph, scaled from the CFF design space to upem."""
    design_units, nominal, default = cff_metrics(top, charstrings)
    local = top.Private.Subrs if getattr(top.Private, "Subrs", None) else []
    glob = top.GlobalSubrs if top.GlobalSubrs is not None else []
    scale = upem / float(design_units)
    widths: dict[str, int] = {}
    for name, charstring in charstrings.items():
        width = None
        try:
            extractor = T2WidthExtractor(local, glob, nominal, default, top.Private)
            extractor.execute(charstring)
            width = extractor.width
        except Exception:
            width = None
        if width is None:
            width = default
        widths[name] = int(round(width * scale))
    return widths


def _mutable_charstrings(top) -> dict:
    """Return the TopDict's charstrings as a plain dict, writable by name.

    A decompiled CharStrings is index-based and cannot be assigned into.
    Setting ``charStringsAreIndexed = False`` converts it to a plain dict,
    which is what the write path needs.
    """
    charstrings = top.CharStrings
    if getattr(charstrings, "charStringsAreIndexed", False):
        plain = {name: charstrings[name] for name in charstrings.keys()}
        charstrings.charStringsAreIndexed = False
        charstrings.charStrings = plain
    return charstrings.charStrings


def build_otf(
    top,
    charstrings: dict,
    ps_name: str,
    family: str,
    subfamily: str,
    weight: int,
    italic: bool,
    *,
    reproducible_timestamp: int | None = None,
    vendor_id: str = "PFRC",
):
    """Wrap merged CFF charstrings in a valid OpenType container.

    ``reproducible_timestamp`` pins ``head.created``/``head.modified``.  When
    it is None fontTools stamps the current time, which makes the output
    differ byte-for-byte between two runs over the same input - see
    :func:`pin_timestamps`.
    """
    glyph_order = [".notdef"] + sorted(g for g in charstrings if g != ".notdef")

    # Build the container at the CFF's own design units so that the
    # FontMatrix (1/design_units) and the charstring widths agree with no
    # scaling.
    design_units, _nominal, _default = cff_metrics(top, charstrings)
    upem = design_units

    # ---- build the CFF table by hand (FontBuilder.setupCFF wants a
    #      dict-like PrivateDict, which a decompiled TopDict's Private is not)
    font_set = CFFFontSet()
    font_set.major, font_set.minor = 1, 0
    font_set.otFont = None
    font_set.fontNames = [ps_name]
    font_set.topDictIndex = TopDictIndex()

    global_subrs = GlobalSubrsIndex()
    font_set.GlobalSubrs = global_subrs

    private = PrivateDict()
    for key, value in (top.Private.rawDict or {}).items():
        try:
            setattr(private, key, value)
        except Exception:
            pass

    top_dict = TopDict()
    top_dict.charset = glyph_order
    top_dict.Private = private
    top_dict.GlobalSubrs = global_subrs
    for key, value in (top.rawDict or {}).items():
        if key in ("CharStrings", "Private", "charset", "Encoding", "FontMatrix"):
            continue
        try:
            setattr(top_dict, key, value)
        except Exception:
            pass

    charstrings_obj = CharStrings(None, top_dict.charset, global_subrs, private, None, None)
    for name, charstring in charstrings.items():
        charstring.private = private
        charstring.globalSubrs = global_subrs
        charstrings_obj[name] = charstring
    top_dict.CharStrings = charstrings_obj
    font_set.topDictIndex.append(top_dict)

    # ---- container, in the CFF's own units per em
    builder = FontBuilder(upem, isTTF=False)
    builder.font.sfntVersion = "OTTO"
    builder.font["CFF "] = newTable("CFF ")
    builder.font["CFF "].cff = font_set

    # ---- cmap from the AGL glyph names
    cmap: dict[int, str] = {}
    for name in glyph_order:
        try:
            unicode_value = toUnicode(name)
        except Exception:
            unicode_value = None
        if unicode_value:
            for char in unicode_value:
                cmap[ord(char)] = name
    if not cmap:
        raise RuntimeError("no unicode recoverable from glyph names")
    builder.setupCharacterMap(cmap, allowFallback=True)

    names = {
        "familyName": family,
        "styleName": subfamily,
        "uniqueFontIdentifier": f"{family} {subfamily}; 1.000",
        "fullName": f"{family} {subfamily}",
        "psName": ps_name,
        "version": "Version 1.000",
        "copyright": "Recovered from embedded PDF fonts.",
    }
    builder.setupNameTable(names, windows=True, mac=True)

    widths = compute_widths(top, charstrings, upem)
    ascent = int(upem * 0.8)
    descent = int(-upem * 0.2)
    builder.setupHorizontalMetrics({name: (widths.get(name, 0), 0) for name in glyph_order})
    builder.setupHorizontalHeader(ascent=ascent, descent=descent)
    builder.setupOS2(
        sTypoAscender=ascent,
        sTypoDescender=descent,
        usWeightClass=weight,
        fsSelection=(0x01 if italic else (0x20 if weight >= 700 else 0x40)),
        achVendID=vendor_id,
    )
    # head.macStyle must agree with the OS/2 italic/bold bits
    mac = (0x02 if italic else 0) | (0x01 if (weight >= 700 and not italic) else 0)
    builder.font["head"].macStyle = mac
    builder.setupPost(italicAngle=(-12 if italic else 0))
    builder.setupMaxp()

    if reproducible_timestamp is not None:
        pin_timestamps(builder.font, reproducible_timestamp)

    return builder, upem, widths


def pin_timestamps(font, timestamp: int) -> None:
    """Pin head.created/head.modified and disable the checksum recompute.

    Two runs over the same input produce byte-identical files only if the
    font carries no wall-clock state.  fontTools sets head.created and
    head.modified to "now" on save, and the ``checkSumAdjustment`` field
    cascades from them - so the two runs differ in 8 bytes even though
    every glyph is identical.  Pinning the timestamps and turning off the
    timestamp recalculation removes that.
    """
    font["head"].created = timestamp
    font["head"].modified = timestamp
    font.recalcTimestamp = False


def save_font(builder, path: str, *, reproducible_timestamp: int | None = None) -> None:
    """Save a built font, pinning the timestamps when asked."""
    if reproducible_timestamp is not None:
        pin_timestamps(builder.font, reproducible_timestamp)
    builder.save(path)


def write_ttf(data: bytes, path: str, *, reproducible_timestamp: int | None = None) -> int:
    """Write a TrueType program, returning its cmap size.

    A PDF-embedded TrueType font may have been stripped of its cmap (the
    subsetter only needs the outlines it sets).  When that happens a cmap is
    rebuilt from the AGL glyph names.
    """
    font = TTFont(io.BytesIO(data))
    if "cmap" not in font:
        _build_cmap_from_glyph_names(font)
    if reproducible_timestamp is not None:
        pin_timestamps(font, reproducible_timestamp)
    font.save(path)
    return len(font.getBestCmap())


def _build_cmap_from_glyph_names(font) -> int:
    """Create a cmap from the glyph names, for fonts embedded without one."""
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

    mapping: dict[int, str] = {}
    for name in font.getGlyphOrder():
        try:
            unicode_value = toUnicode(name)
        except Exception:
            unicode_value = None
        if unicode_value:
            for char in unicode_value:
                mapping[ord(char)] = name
    if not mapping:
        return 0
    table = newTable("cmap")
    table.tableVersion = 0
    subtable = CmapSubtable.newSubtable(4)
    subtable.platformID, subtable.platEncID, subtable.language = 3, 1, 0
    subtable.cmap = mapping
    mac_subtable = CmapSubtable.newSubtable(0)
    mac_subtable.platformID, mac_subtable.platEncID, mac_subtable.language = 1, 0, 0
    mac_subtable.cmap = mapping
    table.tables = [subtable, mac_subtable]
    font["cmap"] = table
    return len(mapping)
