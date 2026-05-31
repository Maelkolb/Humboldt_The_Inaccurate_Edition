"""
Ground-Truth Matching – Humboldt Journal Edition (optional, behind --ground-truth-tei)
=======================================================================================
For each detected region on a page, query Gemini to find the corresponding
text segment in an externally-provided **ground-truth TEI** (typically the
scholarly transcription from https://edition-humboldt.de/).

The result is stored on each :class:`~src.models.Region` in two new fields:

  * ``ground_truth_content``    — the matched GT text
  * ``ground_truth_confidence`` — 0..1 confidence the model assigned

The HTML viewer can then offer a three-way toggle in the transcription panel:
**Gemini** / **Ground Truth** / **Diff**.

Workflow per page
-----------------
1. Parse the GT TEI (once per book).
2. Index parsed pages by their normalised folio label (``"[1r]"`` → ``"1r"``).
3. For each processed page, look up the matching GT page.
4. Call Gemini with: page image + region bboxes/types + Gemini transcriptions
   + the GT page's full text (main text + marginal notes).
5. Apply the returned mapping to the regions.

Pages with no matching GT folio are left untouched (no ground_truth fields
populated).
"""

from __future__ import annotations

import base64
import difflib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai import types

from .json_utils import parse_json_robust
from .models import PageResult, Region
from .tei_parser import parse_tei_file, parse_tei_string

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Folio-label normalisation
# ---------------------------------------------------------------------------

_FOL_BRACKETS_RE = re.compile(r"[\[\]\s]+")


def _norm_folio(label: str) -> str:
    """Normalise a folio label so "1r", "[1r]", " 1r " all match."""
    if not label:
        return ""
    return _FOL_BRACKETS_RE.sub("", label).lower()


# ---------------------------------------------------------------------------
# GT index
# ---------------------------------------------------------------------------

def _build_gt_index(
    gt_tei: str | Path | None = None,
    *,
    gt_xml_string: Optional[str] = None,
) -> Dict[str, PageResult]:
    """
    Parse the ground-truth TEI once and return a dict mapping
    ``normalised_folio_label → PageResult``.
    """
    if gt_xml_string is not None:
        results = parse_tei_string(gt_xml_string)
    elif gt_tei is not None:
        results = parse_tei_file(gt_tei)
    else:
        raise ValueError("Either gt_tei or gt_xml_string must be provided")

    idx: Dict[str, PageResult] = {}
    for r in results:
        key = _norm_folio(r.folio_label)
        if key:
            idx[key] = r
    logger.info("Built GT index: %d folios available.", len(idx))
    return idx


def _folio_key_variants(label: str) -> List[str]:
    """Candidate lookup keys for a folio label, most-specific first, so a
    minor format difference (leading zeros, a stray ``fol.`` prefix) between
    the image filename and the TEI ``@n`` doesn't lose the match."""
    key = _norm_folio(label)
    variants = [key]
    # Drop a leading "fol."/"f." prefix the TEI sometimes carries.
    stripped = re.sub(r"^(?:fol\.?|f\.?)\s*", "", key)
    if stripped and stripped != key:
        variants.append(stripped)
    # Tolerate leading zeros: "067r" <-> "67r".
    m = re.match(r"0*(\d+)([rv]?)$", stripped or key)
    if m:
        variants.append(m.group(1) + m.group(2))
    seen, out = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def gt_lookup(
    gt_index: Dict[str, PageResult], folio_label: str
) -> Optional[PageResult]:
    """Look up a GT page by folio label, tolerating minor key differences."""
    for key in _folio_key_variants(folio_label):
        page = gt_index.get(key)
        if page is not None:
            return page
    return None


def _coerce_match_list(parsed: Any) -> List[Any]:
    """Coerce a parsed LLM response into the list of per-region match objects.

    Gemini usually returns a bare JSON array, but intermittently wraps it in
    an object (e.g. ``{"regions": [...]}`` / ``{"matches": [...]}``). Treating
    that as "no matches" is what makes the Ground-Truth / Diff tabs vanish on
    some pages, so we unwrap it here. Returns the matches list, or ``[]``.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in ("matches", "regions", "results", "items", "data",
                  "ground_truth", "mappings", "output"):
            v = parsed.get(k)
            if isinstance(v, list):
                return v
        # Any list-of-objects value (first wins).
        for v in parsed.values():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v
        # A single bare match object → wrap it.
        if "region_index" in parsed:
            return [parsed]
    return []


def _gt_page_text_for_prompt(gt_page: PageResult) -> str:
    """Render a GT page as a single human-readable block for the prompt.

    Structure preserved: page_number, then main text, then each marginal note
    on its own line prefixed with its position label.
    """
    parts: List[str] = []

    # Page number first (if any)
    for r in gt_page.regions:
        if r.region_type == "page_number" and r.content:
            parts.append(f"[page_number] {r.content.strip()}")
            break

    # Main text (concatenated body)
    main_parts = []
    for r in gt_page.regions:
        if r.region_type in ("main_text", "entry_heading", "bibliographic_ref",
                             "calculation", "observation_table", "coordinates",
                             "instrument_list"):
            if r.region_type == "entry_heading":
                main_parts.append(f"[head] {r.content}")
            elif r.content:
                main_parts.append(r.content)
    if main_parts:
        parts.append("\n".join(main_parts))

    # Marginal notes / pasted slips
    for r in gt_page.regions:
        if r.region_type in ("marginal_note", "pasted_slip"):
            place = r.marginal_position or r.position or "inline"
            tag = "pasted_slip" if r.is_pasted_slip else "marginal_note"
            if not r.content:
                continue
            parts.append(f"[{tag} place={place}] {r.content}")

    # Figures
    for r in gt_page.regions:
        if r.region_type == "sketch":
            parts.append(f"[sketch] {r.content or '[figure]'}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Verbatim-fidelity guard
# ---------------------------------------------------------------------------
# The matching LLM is only ever allowed to *select* which span of the
# authoritative GT belongs to a region — never to rewrite it. Every value the
# model returns is therefore snapped back onto a verbatim slice of the GT TEI
# text before it is stored, so ``ground_truth_content`` is guaranteed to be
# the original ground truth, character for character.

def _canonical_gt_text(gt_page: PageResult) -> str:
    """Authoritative, verbatim GT text for a page: the raw region contents
    (no labels), in document order, joined by newlines. This is the only text
    a region's ``ground_truth_content`` is ever allowed to be a slice of."""
    return "\n".join(
        r.content for r in gt_page.regions if r.content
    )


def _normalize_with_map(s: str) -> Tuple[str, List[int]]:
    """Return a comparison string plus an index map back to *s*.

    The comparison string is whitespace-collapsed (runs → single space) AND
    casefolded, so matching is case- and layout-insensitive. ``idx_map[i]`` is
    the index in *s* that comparison-char ``i`` came from — letting a matched
    span be sliced verbatim from the original.

    Casefolding can change a character's length (e.g. German ``ß`` → ``ss``),
    so a single source char may map to several comparison chars; each of them
    points back to that one source index. Building the map here (rather than
    casefolding afterwards) keeps positions and the map exactly aligned.
    """
    chars: List[str] = []
    idx_map: List[int] = []
    prev_ws = False
    for i, ch in enumerate(s):
        if ch.isspace():
            if prev_ws:
                continue
            chars.append(" ")
            idx_map.append(i)
            prev_ws = True
        else:
            for fc in ch.casefold():
                chars.append(fc)
                idx_map.append(i)
            prev_ws = False
    return "".join(chars), idx_map


def _snap_to_canonical(
    candidate: str,
    canonical: str,
    *,
    min_ratio: float = 0.62,
) -> Optional[str]:
    """Return the verbatim slice of *canonical* that the model's *candidate*
    corresponds to, or ``None`` when no faithful match exists.

    Matching is whitespace- and case-insensitive (so the model may reflow
    line breaks or letter case freely), but the returned text is always cut
    verbatim from *canonical* — the model can never introduce a character
    that is not in the original ground truth.
    """
    cand = (candidate or "").strip()
    if not cand or not canonical:
        return None

    cn_cmp, _ = _normalize_with_map(cand)        # casefolded candidate
    norm_cmp, idx = _normalize_with_map(canonical)  # casefolded canonical + map
    if not cn_cmp or not norm_cmp:
        return None

    last = len(idx) - 1

    # 1) Exact (whitespace/case-insensitive) substring → slice verbatim.
    pos = norm_cmp.find(cn_cmp)
    if pos >= 0:
        start = idx[pos]
        end = idx[min(pos + len(cn_cmp) - 1, last)] + 1
        return canonical[start:end].strip()

    # 2) Fuzzy: span from the first to the last matching block, accepted
    #    only when it actually resembles the candidate.
    sm = difflib.SequenceMatcher(a=norm_cmp, b=cn_cmp, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    if not blocks:
        return None
    a_start = blocks[0].a
    a_end = blocks[-1].a + blocks[-1].size
    span_cmp = norm_cmp[a_start:a_end]
    if difflib.SequenceMatcher(a=span_cmp, b=cn_cmp).ratio() < min_ratio:
        return None
    start = idx[a_start]
    end = idx[min(a_end - 1, last)] + 1
    return canonical[start:end].strip()


def _reflow_to_reference(gt_text: str, ref_text: str) -> str:
    """Re-insert line breaks into *gt_text* so its lineation mirrors
    *ref_text* (the pipeline's own transcription, which already follows the
    manuscript's line breaks).

    Only whitespace is touched: every ground-truth token is preserved
    verbatim and in its original order — orthography and tokenisation are
    never changed, just the line wrapping. This produces the line-per-line
    layout of the original page without altering the text.
    """
    if not gt_text or not ref_text or "\n" not in ref_text:
        return gt_text
    gt_tokens = gt_text.split()
    if not gt_tokens:
        return gt_text

    ref_tokens: List[str] = []
    ref_line: List[int] = []
    for li, line in enumerate(ref_text.split("\n")):
        for tok in line.split():
            ref_tokens.append(tok)
            ref_line.append(li)
    if not ref_tokens:
        return gt_text

    a = [t.casefold() for t in gt_tokens]
    b = [t.casefold() for t in ref_tokens]
    line_of: List[Optional[int]] = [None] * len(gt_tokens)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=a, b=b, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                line_of[i1 + k] = ref_line[j1 + k]
        elif tag == "replace":
            span = max(j2 - j1, 1)
            for off, k in enumerate(range(i1, i2)):
                jj = min(j1 + min(off, span - 1), len(ref_line) - 1)
                line_of[k] = ref_line[jj]
        # 'delete' (gt tokens with no ref counterpart) and 'insert' fall
        # through; deleted gt tokens inherit the running line below.

    # Carry the line index forward across any unresolved tokens.
    cur = 0
    for k in range(len(gt_tokens)):
        if line_of[k] is None:
            line_of[k] = cur
        else:
            cur = line_of[k]

    pieces = [gt_tokens[0]]
    for k in range(1, len(gt_tokens)):
        pieces.append("\n" if line_of[k] != line_of[k - 1] else " ")
        pieces.append(gt_tokens[k])
    return "".join(pieces)


# ---------------------------------------------------------------------------
# Deterministic body fallback
# ---------------------------------------------------------------------------
# The LLM matcher occasionally returns nothing for the main body on pages
# where the pipeline's transcription is badly garbled — it can't confidently
# align Gemini→GT. But the ground truth IS in the TEI, so rather than lose it
# we cut the GT main-text deterministically and assign it to the detected
# main_text regions by token alignment. No model call, always available.

_NONWS_RE = re.compile(r"\S+")


def _split_gt_main_text(
    gemini_segments: List[str], gt_text: str
) -> List[str]:
    """Split *gt_text* across the detected main_text regions, aligning by
    content. Returns one verbatim slice per segment (in order); a segment with
    no alignment anchor gets ``""`` rather than mismatched text.
    """
    gt_toks = [(m.group(), m.start(), m.end())
               for m in _NONWS_RE.finditer(gt_text)]
    if not gt_toks:
        return ["" for _ in gemini_segments]
    if len(gemini_segments) <= 1:
        return [gt_text.strip()]

    gt_words = [t[0].casefold() for t in gt_toks]
    flat, seg_of = [], []
    for si, seg in enumerate(gemini_segments):
        for w in (seg or "").split():
            flat.append(w.casefold())
            seg_of.append(si)

    anchors: List[Optional[int]] = [None] * len(gemini_segments)
    for a, b, size in difflib.SequenceMatcher(
        a=flat, b=gt_words, autojunk=False
    ).get_matching_blocks():
        for k in range(size):
            si = seg_of[a + k]
            if anchors[si] is None:
                anchors[si] = b + k

    # Cut points: segment 0 always starts at 0; later segments start at their
    # first (monotonically increasing) anchor. Unanchored segments get "".
    cuts: List[Tuple[int, int]] = [(0, 0)]
    last = 0
    for i in range(1, len(gemini_segments)):
        a = anchors[i]
        if a is not None and a > last:
            cuts.append((i, a))
            last = a

    slices = ["" for _ in gemini_segments]
    for k, (seg, start) in enumerate(cuts):
        end_word = cuts[k + 1][1] if k + 1 < len(cuts) else len(gt_toks)
        if start >= len(gt_toks) or end_word <= start:
            continue
        c0 = gt_toks[start][1]
        c1 = gt_toks[end_word - 1][2]
        slices[seg] = gt_text[c0:c1].strip()
    return slices


def _ensure_body_gt(
    regions: List[Region], gt_page: PageResult
) -> List[Region]:
    """Deterministic safety net: if the detected ``main_text`` regions came
    back with no ground truth, cut the GT main-text from the TEI and assign it
    to them directly (re-lineated to the manuscript). Other regions and any
    GT the matcher already produced are left untouched.
    """
    import dataclasses

    body_idx = [i for i, r in enumerate(regions)
                if r.region_type == "main_text"]
    if not body_idx:
        return regions
    if any(regions[i].ground_truth_content for i in body_idx):
        return regions  # matcher already populated the body — leave it

    gt_main = "\n".join(
        r.content for r in gt_page.regions
        if r.region_type == "main_text" and r.content
    ).strip()
    if not gt_main:
        return regions

    segments = [regions[i].content or "" for i in body_idx]
    slices = _split_gt_main_text(segments, gt_main)

    out = list(regions)
    assigned = 0
    for i, sl in zip(body_idx, slices):
        if not sl:
            continue
        out[i] = dataclasses.replace(
            out[i],
            ground_truth_content=_reflow_to_reference(sl, out[i].content or ""),
            ground_truth_confidence=0.5,   # deterministic, not model-scored
        )
        assigned += 1
    if assigned:
        logger.info(
            "  GT body filled deterministically from TEI "
            "(%d/%d main_text regions).", assigned, len(body_idx),
        )
    return out


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT = """\
You are matching scholarly ground-truth text from "edition humboldt digital"
to regions automatically detected on a page of Alexander von Humboldt's
travel journal.

You are given:

  1. The page image (attached).
  2. A list of detected regions — each with a bounding box (in 0..1000
     coordinates, [y_min, x_min, y_max, x_max]), a region type, and the
     pipeline's own (often inaccurate) transcription, which already follows
     the original line breaks of the manuscript.
  3. The full ground-truth text for THIS page, broken into labelled blocks
     (page_number, main text, marginal_note place=…, pasted_slip, sketch).

YOUR TASK
---------

For EACH detected region, find the matching SEGMENT of the ground-truth text
that physically corresponds to that region's bounding box on the page. Use
the image, the bbox geometry, AND the pipeline's noisy transcription as
clues to disambiguate.

Rules for matching:

  * The ground-truth main text was originally one continuous paragraph; it
    may need to be split across multiple detected regions if the pipeline
    split a long passage. Conversely, if the pipeline split one logical
    passage into two regions, you may assign overlapping segments — but
    prefer clean, non-overlapping splits.
  * Marginal notes (``[marginal_note place=…]``) should match a region with
    ``region_type == "marginal_note"`` at the corresponding margin position.
  * Pasted slips (``[pasted_slip …]``) should match ``pasted_slip`` regions.
  * Sketches (``[sketch] …``) should match ``sketch`` regions; in that case
    set ``ground_truth_content`` to the figure description (or an empty
    string when no description exists).
  * Page numbers (``[page_number] …``) should match ``page_number`` regions.
  * Opposite-folio bleedthrough regions (``marginal_position == "opposite"``)
    have no GT counterpart — set ``ground_truth_content`` to ``""`` and
    ``confidence`` to 0.
  * If a detected region has NO clear counterpart in the GT (e.g. a region
    the pipeline hallucinated), set ``ground_truth_content`` to ``""`` and
    ``confidence`` to 0.
  * If the GT contains text not covered by any detected region, that's OK —
    do not invent a region for it.

TEXT FIDELITY RULES (CRITICAL)
------------------------------

You may ONLY SELECT text from the ground truth — you may NOT rewrite it. The
``ground_truth_content`` you return for a region must be copied VERBATIM from
the "GROUND-TRUTH TEXT FOR THIS PAGE" block:

  * Copy the characters exactly: same spelling, same abbreviations, same
    punctuation, same editorial markers (``[?]``, ``[...]``, ``~~…~~``,
    ``<u>…</u>``). Do NOT expand or contract abbreviations, do NOT modernise
    spelling, do NOT add or remove anything.
  * Do NOT add commentary, footnote references, or bracketed editorial supply
    that is not already present in the GT text.
  * Your only freedom is WHICH contiguous span of the GT text to assign to
    each region (and you may leave whitespace/line breaks as they are — they
    are ignored when the selection is validated).

The returned text is automatically snapped back onto the original ground
truth, so any wording you invent that is not present verbatim in the GT will
be discarded.

LINE-BREAK FORMATTING
---------------------

Keep the ground-truth text's own line breaks. Do not re-lineate it to match
the manuscript; line breaks are not used when validating your selection.

OUTPUT FORMAT
-------------

Respond ONLY with a JSON array (one object per detected region, same order
as the input list):

[
  {{
    "region_index": 0,
    "ground_truth_content": "the GT text matched to this region, with line
                              breaks aligned to the original manuscript",
    "confidence": 0.95
  }},
  ...
]

Output the GT text WITHOUT the "[main_text]"/"[marginal_note …]" labels.

DETECTED REGIONS (JSON):
{regions_json}

GROUND-TRUTH TEXT FOR THIS PAGE:
{gt_text}
"""


# ---------------------------------------------------------------------------
# Image helper
# ---------------------------------------------------------------------------

def _load_image_bytes(image_path: str | Path) -> Tuple[bytes, str]:
    from .region_detection import load_image_as_base64
    data, mime = load_image_as_base64(image_path)
    return base64.b64decode(data), mime


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def match_ground_truth_to_page(
    client: genai.Client,
    image_path: str | Path,
    regions: List[Region],
    gt_page: PageResult,
    model_id: str,
    thinking_level: str = "medium",
) -> List[Region]:
    """
    Match a single GT page's text to the detected regions on one image.

    Returns a NEW list of regions with ``ground_truth_content`` and
    ``ground_truth_confidence`` populated. Regions for which no match is
    found are returned unchanged.
    """
    if not regions:
        return regions

    # Serialise regions for the prompt (only the fields the model needs)
    serialised = [
        {
            "region_index": r.region_index,
            "region_type": r.region_type,
            "bbox": r.bbox,
            "marginal_position": r.marginal_position,
            "is_pasted_slip": r.is_pasted_slip,
            "is_visual": r.is_visual,
            "gemini_content": r.content,
        }
        for r in regions
    ]
    regions_json = json.dumps(serialised, ensure_ascii=False, indent=2)
    gt_text = _gt_page_text_for_prompt(gt_page)
    canonical_gt = _canonical_gt_text(gt_page)

    prompt = _PROMPT.format(regions_json=regions_json, gt_text=gt_text)

    image_bytes, mime_type = _load_image_bytes(image_path)

    data: List[Any] = []
    for attempt in range(1, 3):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=[
                    types.Content(parts=[
                        types.Part(text=prompt),
                        types.Part(inline_data=types.Blob(
                            mime_type=mime_type, data=image_bytes,
                        )),
                    ])
                ],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_level=thinking_level
                    ),
                    response_mime_type="application/json",
                ),
            )
            data = _coerce_match_list(parse_json_robust(response.text))
            if data:
                break
        except Exception as exc:
            logger.error(
                "Ground-truth matching error (attempt %d/2): %s",
                attempt, exc,
            )

    if not data:
        logger.warning(
            "  GT: model returned no usable matches for this page "
            "(no Ground-Truth/Diff tabs will appear)."
        )

    # Build {region_index → (content, confidence)}
    match_map: Dict[int, Tuple[str, float]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("region_index"))
        except (TypeError, ValueError):
            continue
        gt = item.get("ground_truth_content") or ""
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        match_map[idx] = (str(gt), conf)

    # Apply matches. Every returned segment is snapped back to a verbatim
    # slice of the page's canonical GT text; a model rewrite that is not
    # present in the GT is rejected (stored as empty) rather than persisted.
    import dataclasses
    out: List[Region] = []
    matched_count = 0
    rejected_count = 0
    for r in regions:
        if r.region_index in match_map:
            raw_gt, conf = match_map[r.region_index]
            try:
                snapped = _snap_to_canonical(raw_gt, canonical_gt) if raw_gt else None
            except Exception as exc:  # never let GT matching abort a page
                logger.warning("  GT snap failed for region %s: %s",
                               r.region_index, exc)
                snapped = None
            if snapped:
                # Re-lineate to match the manuscript (the pipeline's own
                # transcription follows the image line breaks); tokens stay
                # verbatim — only whitespace changes.
                snapped = _reflow_to_reference(snapped, r.content or "")
                out.append(dataclasses.replace(
                    r,
                    ground_truth_content=snapped,
                    ground_truth_confidence=conf,
                ))
                matched_count += 1
            else:
                if raw_gt:
                    rejected_count += 1
                out.append(dataclasses.replace(
                    r,
                    ground_truth_content="",
                    ground_truth_confidence=0.0,
                ))
        else:
            out.append(r)
    logger.info(
        "  GT matched: %d / %d regions populated (%d rejected as non-verbatim)",
        matched_count, len(regions), rejected_count,
    )
    # Deterministic safety net for the main body (handles pages where the
    # matcher couldn't align a badly-garbled transcription to the GT).
    out = _ensure_body_gt(out, gt_page)
    return out


def annotate_results_with_ground_truth(
    client: genai.Client,
    results: List[PageResult],
    image_folder: str | Path,
    gt_tei: str | Path,
    *,
    model_id: str,
    thinking_level: str = "medium",
) -> List[PageResult]:
    """
    Run ground-truth matching on every page of an already-processed book.

    For each PageResult, locate the matching folio in the GT TEI by folio
    label and populate its regions' ``ground_truth_content`` fields.

    Pages whose folio is not in the GT are left untouched. Returns the same
    list (regions are replaced in place via ``PageResult.regions``).
    """
    image_folder = Path(image_folder)
    gt_index = _build_gt_index(gt_tei)

    matched_pages = 0
    for result in results:
        gt_page = gt_lookup(gt_index, result.folio_label)
        if gt_page is None:
            logger.info(
                "No GT folio matches '%s' (norm=%r); skipping",
                result.folio_label, _norm_folio(result.folio_label),
            )
            continue

        image_path = image_folder / result.image_filename
        if not image_path.exists():
            logger.warning(
                "GT match found but image missing for %s: %s",
                result.folio_label, image_path,
            )
            continue

        logger.info(
            "Matching GT for folio %s (%d regions)…",
            result.folio_label, len(result.regions),
        )
        result.regions = match_ground_truth_to_page(
            client, image_path, result.regions, gt_page,
            model_id=model_id, thinking_level=thinking_level,
        )
        matched_pages += 1

    logger.info(
        "Ground-truth matching complete: %d / %d pages matched.",
        matched_pages, len(results),
    )
    return results


def fill_missing_body_ground_truth(
    results: List[PageResult],
    gt_tei: str | Path,
) -> List[PageResult]:
    """Offline (no model calls): fill the main-text ground truth on any page
    whose body came back empty, by cutting it deterministically from the GT
    TEI. Pages whose body already has GT — and all non-body regions — are left
    untouched. Useful for repairing an existing run without re-processing.
    """
    gt_index = _build_gt_index(gt_tei)
    filled = 0
    for result in results:
        gt_page = gt_lookup(gt_index, result.folio_label)
        if gt_page is None:
            continue
        before = any(
            r.region_type == "main_text" and r.ground_truth_content
            for r in result.regions
        )
        result.regions = _ensure_body_gt(result.regions, gt_page)
        after = any(
            r.region_type == "main_text" and r.ground_truth_content
            for r in result.regions
        )
        if after and not before:
            filled += 1
    logger.info(
        "Filled missing body ground truth on %d page(s) from the TEI.", filled
    )
    return results
