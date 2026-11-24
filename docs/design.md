# Design notes

Why the tool is shaped this way. The short version: every decision below
exists because the naive version of it produced a font that looked fine and
was not.

## Reproducibility

The failure this fixes was measured, not theorised. Two extraction runs
over the same 26-PDF corpus produced 23 files of which **9 were
byte-identical and 14 differed** — with identical file sizes, so a
size-based check would have passed.

The cause is `head.created` and `head.modified`, which fontTools sets from
the current time on every save. `head.checkSumAdjustment` is derived from
the table checksums, so it moves with them.

Two findings shaped the fix:

- The timestamp has to be pinned at **every** save point, not just the
  first. A font is built, then repaired, then normalised — three saves.
  Pinning only the build produced files whose `modified` still carried the
  repair's wall-clock time.
- The pin value must be **above 0x7C259DC0** (1 January 2084). fontTools
  treats a `head` timestamp below that as a Macintosh epoch *offset* rather
  than a Unix timestamp, and shifts it on load. A pin of 3600000000 — the
  value the one-off scripts used — is silently transformed on the way in.
  The test now asserts the value survives a load unchanged.

`font.recalcTimestamp = False` prevents fontTools from overwriting the pin
at save time. `head.checkSumAdjustment` is then computed from the pinned
values, so it is stable too.

Ordering was already deterministic and is preserved deliberately: PDF
enumeration is `sorted()`, and style iteration is `sorted()`. The CFF merge
is first-source-wins over that stable order.

The reports are separated into two kinds. `_manifest.json`, `_coverage.json`
and `_report.json` are **build artefacts** — they must be byte-identical
between runs, so they exclude the elapsed time, the output directory and
the publish flag. The publish state is a fact about the *run*, not about
the *build*.

## The coverage gate

The gate fails the run when any font has a character that renders no ink.

This catches the defect that a conventional font check misses. A PDF
subsetter that strips an outline leaves the cmap entry in place, so the
font has a complete-looking charset, a valid name table and valid metrics.
It renders nothing for that character. Only rasterising the character
finds it.

The gate also refuses to publish a run that produced **no fonts**. The
likely cause is a manifest whose style names have drifted from what the
PDFs embed — a quiet mistake that would otherwise replace a live delivery
folder with an empty directory.

## Placeholders are not glyphs

A placeholder is a shared missing-glyph box: several characters whose
outlines have the same bounding box. It is detected by that shared box,
not by a comparison with a reference glyph.

Placeholders are detected and reported but **never copied across**. A
placeholder renders as a box with a letter in it, which is worse than a
blank because it looks like a font problem the user cannot see. When an
external source supplies the character, the tool checks the external font
for placeholders first and refuses to import them.

The one-off work found this the hard way: the Altone Trial's ten digits
were all the same placeholder box, so importing it wholesale would have
replaced ten real figures with ten identical boxes.

## Whitespace is not blank

A space has no outline by definition. Testing it for ink reports every
font in the world as having a blank character, which would leave the gate
permanently red for a reason that is not a defect. The check exempts
whitespace explicitly.

This was found by the tool itself, on the real corpus: the first full run
reported one blank character per font, and it was the space, in every one.

## External sources

An external font may be used to supply characters the PDFs never set —
accented forms, currency signs, a full Latin set where the documents only
set English.

The rules are conservative:

- An external source is only used for characters that are **missing or
  blank**, never to overwrite a character that already has ink.
- The external font's own placeholders are excluded from the import.
- The external source is recorded in the report, with its licence.

Where an exact weight is unavailable, a nearby weight may be used. That is
recorded too, because a font with borrowed figures looks right and is not
what its name says.

## What is deliberately not in the tool

**No network access.** Every source of a glyph is either a PDF in the
manifest's inputs or a local file the manifest names. Acquisition is the
operator's business; the tool's job is to assemble what it is given, and
to record where each character came from.

**No implicit style matching.** The manifest names the styles explicitly.
PyMuPDF's style names can be inspected with `inspect`, and the manifest
uses those names. A style that is not declared is not built — silently
guessing would make the tool's output depend on heuristics that change
between PDF library versions.

**No overwriting of the destination without a passing gate.** Publication
moves a staging directory into place. The previous directory is moved
aside, not deleted, so a mistake is recoverable.
