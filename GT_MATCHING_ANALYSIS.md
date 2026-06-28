# Ground-Truth Matching / Alignment — Analysis & Changes

_Investigation run overnight on `output_full` (21 folios, fixed transcriptions),
fully offline except the one LLM-model A/B. All CER numbers are book-level
micro-averages from `cer_wer_vs_gt_book` (page-concat, boundary-robust)._

## What this stage does
After the page is transcribed, **GT matching** assigns each detected region the
span of the scholarly TEI ground truth it corresponds to (powering the per-region
CER/WER metric and the viewer's Gemini / GT / Diff tabs). It is *alignment*, not
reading: it never invents text — every assigned span is snapped verbatim to the
TEI and re-lineated to the region's own line breaks.

## The lever you asked for — model upgrade (shipped)
The matcher is now the **best model at medium thinking**
(`gemini-3.5-flash`, was `gemini-3-flash-preview`):

| Matcher | Book CER | WER |
|---|---|---|
| LLM `gemini-3-flash-preview` (old) | 11.01% | 27.66% |
| **LLM `gemini-3.5-flash` medium (new default)** | **10.58%** | **27.35%** |

A real, free win (−0.43 CER). `src/pipeline.py`: `gt_model` now defaults to
`config.MODEL_ID_MERGE_DEFAULT`, `gt_thinking` to `"medium"`.

## The deterministic alignment module (new, `src/ground_truth.py`)
`align_ground_truth_to_page(regions, gt_page)` — pure, no API, reproducible.
It separates the page GT into a **reading-order main stream** (token-aligned to
the regions' transcriptions, partitioned into verbatim per-region slices by a full
opcode-walk with carry-forward, so no GT is lost) and **marginalia** (matched
structurally by content + position). This is the proper, reusable "alignment
module."

### Measured against the LLM (offline A/B on `output_full`)
| Matcher | Book CER | regions scored |
|---|---|---|
| LLM (old, stored) | 11.01% | 81 |
| **Deterministic aligner (alone)** | 13.59% | 71 |
| Confidence-gated hybrid (LLM + det override) | ~11.0% | 82 |
| Page-level routing (det where confident, else LLM) | 10.97% | 76 |
| **LLM new model** | **10.58%** | 81 |

Per-folio, the deterministic aligner **matches the LLM exactly on ~15 clean prose
pages** and **wins big on the hard index folio 28v (13.6% → 1.6%)**. But it
**loses on two classes the LLM handles with image+geometry reasoning**:
1. **Cross-type GT** (e.g. 2r: a `bibliographic_ref` region whose GT is stored in
   the TEI as a `marginal_note` — strict type-based stream separation can't cross
   that, leaving it empty).
2. **Index/register pages** (29r: short single-letter entries `U. V. R.` in a
   multi-column layout — monotonic alignment scrambles their order: 15.6% → 44%).

**Conclusion:** a pure deterministic matcher does **not** beat the LLM-on-best-model
globally, so it should not be primary.

## Decision (shipped) — LLM-best primary + deterministic safety net
- **Primary:** LLM matcher on `gemini-3.5-flash`/medium (10.58%).
- **Fallback:** `_fill_unmatched_gt` now uses the new aligner to fill only the
  regions the LLM left empty — it matches the LLM on clean pages, is reproducible,
  and can only *add* coverage (verified: 11.01% → 11.01%, no regression).
- Removed the superseded `_split_gt_main_text` (crude min-anchor fallback).
- `align_ground_truth_to_page` is public, so eval/notebooks can run a **free,
  reproducible, deterministic-only** GT pass when API cost/variance matters.

## On the Fol. 20r case you flagged
`+~~s~~als das einzige Rettungsmittel` is **not** a 2-region alignment artifact —
it is one correctly-matched `marginal_note` with a genuine **misread**
(HYP `Fütterungsmittel` vs GT `Rettungsmittel`, and HYP `+` vs GT's struck `~~s~~`).
The `+~~s~~als` you saw is the Diff view overlaying HYP's `+` against GT's `~~s~~`.
This is reading-limited (transcription quality), not something the matcher can fix.

## Recommended next steps (if pushing the aligner to primary)
1. **Cross-type matching** for the main stream: pool *all* non-pure-margin GT
   (not only same-typed) so a `bibliographic_ref`↔`marginal_note` mismatch resolves.
2. **Index-page handling:** detect register/multi-column pages (many short
   entries) and route them to the LLM (page-level routing already gets 10.97%).
3. A **smarter hybrid** that captures the 28v-style LLM failures without the
   per-region gating noise (needs a calibrated confidence signal).
