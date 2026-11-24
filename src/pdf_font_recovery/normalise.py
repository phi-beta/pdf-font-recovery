"""
Metadata normalisation for recovered fonts.

A PDF-embedded font carries the subsetter's view of the world: a mangled
family name ("ABCDEF+Altone"), a subset prefix on the PostScript name, and
often no OS/2 table at all.  Windows uses these fields to group the weights
of a family together, so getting them right is what makes a recovered font
behave like a real one in a design tool.

Two rules that are easy to get wrong:

REGULAR / ITALIC / BOLD ARE MUTUALLY EXCLUSIVE
    The OS/2 ``fsSelection`` bits for those three styles cannot be set at
    the same time.  Setting REGULAR and BOLD together produces a font that
    some applications render as bold and others as regular.

head.macStyle MUST AGREE WITH OS/2
    The two tables carry the same information in different encodings.  When
    they disagree, the behaviour of the font depends on which table the
    consuming application reads.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fontTools.ttLib import TTFont, newTable


@dataclass
class NormaliseResult:
    """What normalisation changed in one font."""

    path: str
    family: str = ""
    subfamily: str = ""
    weight: int = 0
    italic: bool = False
    glyphs: int = 0
    upem: int = 0
    missing: str = ""
    notes: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "file": os.path.basename(self.path),
            "family": self.family,
            "subfamily": self.subfamily,
            "weight": self.weight,
            "italic": self.italic,
            "glyphs": self.glyphs,
            "upem": self.upem,
            "missing": self.missing,
            "notes": list(self.notes or []),
        }


def ensure_os2(font, weight: int, italic: bool, upem: int, *, vendor_id: str = "PFRC") -> None:
    """Add an OS/2 table when the font has none."""
    if "OS/2" in font:
        return
    os2 = newTable("OS/2")
    os2.version = 4
    os2.xAvgCharWidth = upem // 2
    os2.usWeightClass = weight
    os2.usWidthClass = 5
    os2.fsType = 0
    os2.ySubscriptXSize = upem // 10
    os2.ySubscriptYSize = upem // 10
    os2.ySubscriptXOffset = 0
    os2.ySubscriptYOffset = upem // 10
    os2.ySuperscriptXSize = upem // 10
    os2.ySuperscriptYSize = upem // 10
    os2.ySuperscriptXOffset = 0
    os2.ySuperscriptYOffset = upem // 4
    os2.yStrikeoutSize = upem // 10
    os2.yStrikeoutPosition = int(upem * 0.25)
    os2.sFamilyClass = 0
    os2.panose = b"\x00" * 10
    os2.ulUnicodeRange1 = 0
    os2.ulUnicodeRange2 = 0
    os2.ulUnicodeRange3 = 0
    os2.ulUnicodeRange4 = 0
    os2.achVendID = vendor_id
    os2.fsSelection = 0x40
    os2.usFirstCharIndex = 0
    os2.usLastCharIndex = 0
    os2.sTypoAscender = int(upem * 0.8)
    os2.sTypoDescender = int(-upem * 0.2)
    os2.sTypoLineGap = 0
    os2.usWinAscent = int(upem * 0.8)
    os2.usWinDescent = int(upem * 0.2)
    font["OS/2"] = os2


def set_names(
    font, family: str, subfamily: str, weight: int, italic: bool, *, unique_suffix: str = ""
) -> None:
    """Write the name table and the style bits for a family/subfamily pair."""
    full = family if subfamily == "Regular" else f"{family} {subfamily}"
    ps = (family + "-" + subfamily).replace(" ", "")
    unique = f"{ps};{unique_suffix}" if unique_suffix else ps

    name_table = font["name"]
    for name_id, value in {
        1: family,
        2: subfamily,
        3: unique,
        4: full,
        6: ps,
        16: family,
        17: subfamily,
    }.items():
        name_table.setName(value, name_id, 3, 1, 0x409)
        name_table.setName(value, name_id, 1, 0, 0)

    mac = 0
    if weight >= 700:
        mac |= 0x01
    if italic:
        mac |= 0x02
    font["head"].macStyle = mac

    # REGULAR / ITALIC / BOLD bits are mutually exclusive.
    fs_selection = font["OS/2"].fsSelection & ~0b1100001
    if italic:
        fs_selection |= 0x01
    elif weight >= 700:
        fs_selection |= 0x20
    else:
        fs_selection |= 0x40
    font["OS/2"].fsSelection = fs_selection
    font["OS/2"].usWeightClass = weight


def normalise_font(
    path: str,
    family: str,
    subfamily: str,
    weight: int,
    italic: bool,
    test_characters: str,
    *,
    unique_suffix: str = "",
    vendor_id: str = "PFRC",
    reproducible_timestamp: int | None = None,
) -> NormaliseResult:
    """Normalise one font file in place and report what changed.

    ``reproducible_timestamp`` pins the head timestamps.  It must be passed
    at EVERY save point in the pipeline, not just the first one: fontTools
    re-stamps ``head.modified`` on each save, so a font that is repaired
    after it was built would carry a fresh timestamp and break the
    reproducibility of the run.
    """
    result = NormaliseResult(path=path)
    notes: list[str] = []

    font = TTFont(path)
    upem = font["head"].unitsPerEm
    if "cmap" not in font:
        from .extract import _build_cmap_from_glyph_names

        built = _build_cmap_from_glyph_names(font)
        notes.append(f"cmap built from glyph names ({built})")
    ensure_os2(font, weight, italic, upem, vendor_id=vendor_id)
    set_names(font, family, subfamily, weight, italic, unique_suffix=unique_suffix)
    if reproducible_timestamp is not None:
        from .extract import pin_timestamps

        pin_timestamps(font, reproducible_timestamp)
    font.save(path)

    verify = TTFont(path)
    cmap = verify.getBestCmap()
    missing = "".join(c for c in test_characters if ord(c) not in cmap)

    result.family = verify["name"].getDebugName(1) or ""
    result.subfamily = verify["name"].getDebugName(2) or ""
    result.weight = verify["OS/2"].usWeightClass
    result.italic = bool(verify["OS/2"].fsSelection & 0x01)
    result.glyphs = len(cmap)
    result.upem = upem
    result.missing = missing
    result.notes = notes
    return result
