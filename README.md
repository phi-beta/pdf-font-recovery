# pdf-font-recovery

Deterministically extract, repair and verify fonts embedded in PDF documents.

PDF documents embed fonts as **subsets**: a font program containing only the
glyphs the document happened to set, often with no usable cmap, no name table
and no metrics. That program is not installable. Extract one and you get a
font that renders some characters and silently produces nothing for the rest.

This tool recovers a usable font from those subsets, repairs the defects
subsetting leaves behind, verifies the result, and publishes it atomically.

## What it does

```
1. collect   walk the PDFs and group the embedded programs by style
2. build     merge each style's subsets and write a font
3. repair    union a style across documents; fill from an external source
4. normalise fix the name tables, style bits and OS/2
5. verify    ink, placeholder and metadata checks
6. publish   move the staging directory into place
```

Everything is driven by a **manifest**, so the tool knows nothing about any
particular document set. The Portage CyberTech corpus is the first fixture.

## Install

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"      # Windows
./.venv/bin/pip install -e ".[dev]"                        # POSIX
```

Requires Python 3.11+.

## Use

```bash
# what is embedded in these PDFs?  (builds nothing)
pdf-font-recovery inspect --manifest examples/portage-cybertech.yaml

# run the full pipeline
pdf-font-recovery extract --manifest examples/portage-cybertech.yaml

# build into a staging directory but do not touch the destination
pdf-font-recovery extract --manifest my.yaml --no-publish --staging ./build

# check an existing directory of fonts (this is what CI runs)
pdf-font-recovery verify --dir ./fonts

# check the letter spacing of a directory of fonts
pdf-font-recovery spacing --dir ./fonts
```

`extract` exits 1 when the coverage or spacing gate fails. `verify` exits 1
when any font has a blank outline. `spacing` exits 1 when any font has a
broken advance width.

## Letter spacing

A font can be structurally perfect and still render with the letters on
top of each other. The advance width is how far the pen moves before the
next glyph is drawn; get it wrong and the letters overlap - while the
font still loads, still has a valid name table, and still passes every
structural check. The ink check catches blank *outlines*; nothing catches
a wrong *advance*.

So the tool measures the advance of every letter and figure, and:

- **a zero advance is always a defect.** No real glyph advances nowhere.
  This is the signature of a metrics entry created as a placeholder and
  never filled in.
- **a letter that is collapsed relative to its own font's siblings is a
  defect.** The check is relative to the font's own median, so it works
  on any typeface - a font that is tight throughout is fine, a font with
  one collapsed letter is not.
- **a font that disagrees with a reference is a defect.** Name a
  `reference` for a style and the recovered font is compared with it,
  like for like:

  ```yaml
  styles:
    Mundial-BlackItalic:
      family: Mundial
      subfamily: Black Italic
      weight: 900
      italic: true
      reference: "C:/fonts/Fontspring-DEMO-8-mundial-black-italic-tty.otf"
  ```

  The reference is what makes the check authoritative rather than merely
  self-consistent.

**Tight spacing is not automatically a defect.** Italics lean into their
neighbours on purpose: Mundial Black Italic has 21 of 62 letters advancing
narrower than their own ink, in the publisher's own file. A gate that
treated "advance < ink" as a defect would reject every good italic. What
proves a defect is a *mismatch* against the source, not a tight letter.

A reference can be wrong too. The Altone Trial's ten digits are all one
placeholder box, so its advances for those characters are the advance of
a box rather than of a figure; comparing against it would flag the
recovered font's correct figures as wrong. Those characters are excluded
from the comparison - and the exclusion is reported, because a comparison
that silently drops characters cannot be trusted.

The results land in `_spacing.json` beside the fonts.

## The manifest

```yaml
version: 1
name: my-corpus

inputs:
  - "E:/docs/**/*.pdf"

output_dir: "E:/delivery/fonts"

families:
  Altone:
    styles:
      Altone-Regular:
        family: Altone
        subfamily: Regular
        weight: 400
        italic: false
    # An external font used to supply characters the PDFs never set.
    base_fonts:
      Altone-Regular:
        path: "C:/fonts/Altone Trial-Regular.ttf"
        exact_weight: true
        license: personal-use trial
    # Styles to skip, with the reason recorded in the report.
    skip:
      Arial-Black: already installed on Windows
```

Run `pdf-font-recovery inspect` to see the style names PyMuPDF reports for
a corpus — those are the keys the manifest uses.

## The properties it guarantees

**Reproducible.** Two runs over the same input produce **byte-identical**
output. fontTools stamps `head.created`/`head.modified` with the current
time on every save, and the `checkSumAdjustment` cascades from them, so
without intervention two runs differ in 8 bytes per font. The tool pins the
timestamps at every save point and disables the timestamp recalculation.

**Atomic.** Everything is built in a staging directory and moved into place
only after the coverage gate passes. A failed run leaves the previous
delivery untouched — including the case where a run produced *no* fonts at
all, which would otherwise replace a live folder with an empty one.

**Honest.** A font can carry a cmap entry for a character whose outline is
empty — exactly what a subsetter produces for a character no document set.
Every ordinary font check passes it. This tool rasterises every character
and counts the ones that render nothing. A **placeholder** — a shared
missing-glyph box — is detected too, and never copied across: a placeholder
is worse than a blank, because it renders as a box with a letter in it.

**Attributed.** The report records, per font, where the characters came from:
the PDF outlines, an external font of the same weight, or a borrowed
nearby weight. A recovered font that mixes sources says so.

## The defects it handles

| Defect | What it is | What the tool does |
|---|---|---|
| Empty outline | The subsetter stripped the outline but left the cmap entry | Unions the subsets across every document that embeds the style |
| Missing cmap | The font was embedded without one | Rebuilds it from the AGL glyph names |
| Missing OS/2 | The font was embedded without one | Adds it |
| Placeholder glyph | Several characters share one bounding box | Detects it and refuses to copy it across |
| CFF subsets | The PDF holds bare CFF with no container | Merges the charstrings and wraps them in a fresh OpenType container |
| Wrong units per em | The CFF uses a 1/2048 matrix, not 1/1000 | Builds the container at the CFF's own design units |
| Missing widths | A decompiled charstring has no width until measured | Runs a `T2WidthExtractor` over every glyph |
| Mangled names | The subsetter's name, with a prefix | Writes a proper family/subfamily/style-bit set |

Two details that are easy to get wrong and are handled here:

- **CharStrings are index-based.** `CharStrings.charStrings` is an index
  into an array, not a name→object mapping. Assigning into one raises.
  The write path converts it to a plain dict first.
- **A glyph must be in the glyph order to be visible.** `getGlyphID`
  resolves through the order, not the glyphs dict, and the `maxp` table's
  recalc walks every glyph in the order looking each one up in `hmtx`. A
  copied composite must bring its components *and* their metrics.

## Development

```bash
./.venv/Scripts/python.exe -m pytest tests/ -q
./.venv/Scripts/python.exe -m ruff check src/ tests/
```

The test suite builds a synthetic PDF corpus, so it is self-contained and
runs in CI without any client documents.

## Licence

MIT. The fonts it produces carry whatever licence their source material
carried — see the report's `base_fonts.license` entries.
