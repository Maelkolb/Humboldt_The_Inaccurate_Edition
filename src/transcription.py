"""Per-region reading and ensemble merge.

Each detected region is read from its own crop (neighbours masked) ``k_crop``
times; those candidates are combined with the injected whole-page readings
(M2/M3) and merged per region by the alignment-locked consensus in
:mod:`src.consensus`. Regions run concurrently; a failed region yields empty text.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence

from google import genai

from . import consensus
from .imaging import crop_region_bytes
from .llm import generate_json
from .models import Region, is_opposite_marginal
from .prompts import HOUSE_STYLE

logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 6

# Inline marker for an uncertain reading: ``word[?]`` or bare ``[?]``.
_UNCERTAIN_RE = re.compile(r"\w*\[\?\]")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Short, per-region type hints. Kept to one line each.
_TYPE_HINTS: Dict[str, str] = {
    "entry_heading": "A numbered entry heading, usually short (e.g. \"N. 50-52.\").",
    "main_text": "Primary journal prose in German Kurrentschrift, often switching language.",
    "marginal_note": "A note Humboldt wrote in the margin; may be a smaller or different hand.",
    "pasted_slip": "Text written on a separate slip of paper pasted onto the page.",
    "calculation": "An astronomical/mathematical computation; keep number columns and the degree/minute/second symbols.",
    "observation_table": "Tabular observational data. ALSO return table_data with the cells.",
    "instrument_list": "A list of instruments, often with prices. ALSO return table_data with the cells.",
    "crossed_out": "A struck-through block. Transcribe the text and wrap the struck passage in ~~...~~.",
    "sketch": "A hand-drawn figure. Describe what is depicted instead of transcribing; set is_visual true.",
    "page_number": "A folio number, usually one to three characters.",
}

_TRANSCRIPTION_PROMPT = """\
The attached image is ONE region cropped from a journal page (transcribe the block
in the CENTRE; ignore any text clearly cut off at the extreme top/bottom edge).

Region type: {region_type} -- {hint}

Tables (observation_table / instrument_list): put the verbatim text in content AND
fill table_data as {{"cells": [["Col1","Col2"],["v","v"]], "caption": "..."}} (row 0
= header); if the cells are unreadable, set table_data null and keep text in content.

Respond ONLY with a JSON object:
{{"content": "the transcription (or, for a sketch, a short description)",
  "languages": ["de"], "is_visual": false, "table_data": null}}
"""


# ---------------------------------------------------------------------------
# Ensemble: heterogeneous candidates + alignment-locked merge
# ---------------------------------------------------------------------------

_MERGE_PROMPT = """\
You are resolving the transcription of ONE handwritten region from Alexander von
Humboldt's journal. The cropped image of this region is attached and is the ONLY
authority — read the actual ink.

Below is a SKELETON of the reading: the parts every transcriber agreed on are
already filled in and are CORRECT — do not touch them. The uncertain spots are
gaps written [[M1]], [[M2]], …. For each gap you are given the candidate
readings the independent transcribers produced (separated by " | "; ``(×N)`` is
how many of the readers chose that option; ∅ means a transcriber read nothing).

For EACH gap, return the reading that matches the ink:
- FIRST decide from the strokes in the image which candidate matches the ink.
- The skeleton text around the gap is correct — use that surrounding context to
  judge which reading makes sense in the sentence.
- The ink and context decide. When the ink is genuinely ambiguous between
  options, prefer the one MORE readers agreed on (higher ×N).
- If the ink clearly shows none of the candidates is right, supply the correct
  reading yourself (do not invent a plausible word — only what the ink shows; use
  [?] if a word is genuinely illegible).
- If the gap's text actually belongs to a NEIGHBOURING region (it appears in only
  one candidate and is not part of this region's ink, e.g. a heading or a line
  that bled in), return "" to omit it.

Region type: {region_type} -- {hint}

SKELETON:
{skeleton}

GAPS (id: candidate variants):
{markers}

Respond ONLY with a JSON object mapping EVERY gap id to its chosen text, e.g.:
{{"M1": "...", "M2": "..."}}
"""


def transcribe_regions_ensemble(
    client: genai.Client,
    detected_regions: List[Dict[str, Any]],
    model_id: str,
    thinking_level: str = "low",
    *,
    page,
    k_crop: int = 1,
    page_candidates: Optional[List[Dict[int, Dict[str, Any]]]] = None,
    merge_model: Optional[str] = None,
    merge_thinking: str = "medium",
    merge_crop_max_px: Optional[int] = None,
    merge_page_bytes: Optional[tuple] = None,
    read_temperature: Optional[float] = None,
    merge_temperature: Optional[float] = None,
    cached_content: Optional[str] = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> List[Region]:
    """Heterogeneous ensemble: combine ``k_crop`` per-region crop reads (M1, on
    ``model_id``) with the injected whole-page ``page_candidates`` (M2/M3), then
    merge each region by alignment-locked consensus — agreement is locked in code
    and only the disagreements are resolved by ``merge_model`` from the region
    crop (+ the whole page ``merge_page_bytes`` for layout context).

    ``page`` is the already-loaded RGB page (crops are cut from it). Returns
    regions ordered by ``region_index`` with ``content`` = merged reading and
    ``ensemble_readings`` = all candidate texts for that region.
    """
    if not detected_regions:
        return []

    merge_model = merge_model or model_id
    page_candidates = page_candidates or []
    k_crop = max(1, k_crop)
    merge_page_img = merge_page_bytes  # pre-encoded by the caller (or None)

    # Per-region masks: every OTHER region's bbox, whited out in this region's
    # crop so a neighbour (e.g. a heading above a column) cannot bleed in.
    bbox_by_idx = {d["region_index"]: d.get("bbox") for d in detected_regions}

    def _mask_for(idx: int) -> List[Sequence[float]]:
        return [b for i, b in bbox_by_idx.items() if i != idx and b]

    to_call: List[Dict[str, Any]] = []
    skip_results: Dict[int, Dict[str, Any]] = {}
    for det in detected_regions:
        idx = det["region_index"]
        if is_opposite_marginal(det.get("marginal_position")):
            skip_results[idx] = {
                "content": "",
                "languages": [],
                "is_visual": False,
                "table_data": None,
                "editorial_note": "Bleedthrough from opposite folio — not transcribed.",
                "ensemble_readings": None,
            }
            continue
        to_call.append(det)

    # Phase 1 — M1 crop reads (k_crop per region).
    crop_reads: Dict[int, List[Dict[str, Any]]] = {d["region_index"]: [] for d in to_call}

    def _read(det: Dict[str, Any], _sample: int) -> tuple[int, Dict[str, Any]]:
        idx = det["region_index"]
        return idx, _transcribe_one(
            client, page, det, model_id, thinking_level,
            mask_bboxes=_mask_for(idx),
            temperature=read_temperature,
        )

    if to_call:
        tasks = [(det, s) for det in to_call for s in range(k_crop)]
        workers = max(1, min(max_workers, len(tasks)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_read, det, s) for det, s in tasks]
            for fut in as_completed(futures):
                idx, data = fut.result()
                if isinstance(data, dict):
                    crop_reads[idx].append(data)

    # Phase 2 — assemble per-region candidate texts + alignment-locked merge.
    merged: Dict[int, Dict[str, Any]] = dict(skip_results)

    def _merge(det: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        idx = det["region_index"]
        reads = crop_reads.get(idx, [])
        cand_texts = [(d.get("content") or "") for d in reads]
        for pc in page_candidates:
            cand_texts.append(((pc.get(idx) or {}).get("content") or ""))
        base = reads[0] if reads else {}
        merged_text = _merge_region(
            client, page, det, cand_texts, merge_model, merge_thinking, merge_crop_max_px,
            mask_bboxes=_mask_for(idx),
            page_image=merge_page_img,
            temperature=merge_temperature,
            cached_content=cached_content,
        )
        return idx, {
            "content": merged_text,
            "languages": base.get("languages") or _langs_from(reads, page_candidates, idx),
            "is_visual": bool(base.get("is_visual", False)),
            "table_data": base.get("table_data"),
            "ensemble_readings": [t for t in cand_texts if (t or "").strip()] or None,
        }

    if to_call:
        workers = max(1, min(max_workers, len(to_call)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_merge, det) for det in to_call]
            for fut in as_completed(futures):
                idx, data = fut.result()
                merged[idx] = data

    regions: List[Region] = []
    for det in detected_regions:
        data = merged.get(det["region_index"], {})
        region = _build_region(det, data)
        region.ensemble_readings = data.get("ensemble_readings")
        regions.append(region)
    regions.sort(key=lambda r: r.region_index)
    return regions


def _merge_region(
    client: genai.Client,
    page,
    det: Dict[str, Any],
    cand_texts: List[str],
    model_id: str,
    thinking_level: str,
    crop_max_px: Optional[int],
    mask_bboxes: Optional[List[Sequence[float]]] = None,
    page_image: Optional[tuple] = None,
    temperature: Optional[float] = None,
    cached_content: Optional[str] = None,
) -> str:
    """Alignment-locked merge of one region's candidate readings. The LLM only
    resolves the disagreement gaps, against the cleaned region crop (plus the
    whole page for layout context when ``page_image`` is given); locked text is
    reassembled in code so it cannot drift."""
    skel = consensus.build_skeleton(cand_texts)
    if not skel.has_markers:
        return skel.render_template()

    bbox = det.get("bbox")
    crop = None
    if bbox:
        kwargs: Dict[str, Any] = {"mask_bboxes": mask_bboxes} if mask_bboxes else {}
        if crop_max_px:
            kwargs["max_px"] = crop_max_px
        crop = crop_region_bytes(page, bbox, **kwargs)
    if crop is None:
        return skel.best_guess()
    images = [crop] + ([page_image] if page_image else [])

    region_type = det.get("region_type", "main_text")
    hint = _TYPE_HINTS.get(region_type, "A region of the journal page.")

    def _resolve(skeleton_text: str, markers_text: str) -> Dict[str, str]:
        prompt = _MERGE_PROMPT.format(
            region_type=region_type, hint=hint,
            skeleton=skeleton_text, markers=markers_text,
        )
        result = generate_json(
            client, model_id, prompt,
            thinking_level=thinking_level,
            temperature=temperature,
            system_instruction=HOUSE_STYLE,
            images=images,
            cached_content=cached_content,
            default={},
            stage=f"merge[{region_type}]",
        )
        return result if isinstance(result, dict) else {}

    return consensus.merge_candidates(cand_texts, resolve=_resolve)


def _langs_from(
    reads: List[Dict[str, Any]],
    page_candidates: List[Dict[int, Dict[str, Any]]],
    idx: int,
) -> List[str]:
    """Union of language tags across all candidate sources for a region."""
    langs: List[str] = []
    for d in reads:
        for lg in (d.get("languages") or []):
            if lg and lg not in langs:
                langs.append(lg)
    for pc in page_candidates:
        for lg in ((pc.get(idx) or {}).get("languages") or []):
            if lg and lg not in langs:
                langs.append(lg)
    return langs


# ---------------------------------------------------------------------------
# Per-region work
# ---------------------------------------------------------------------------

def _transcribe_one(
    client: genai.Client,
    page,
    det: Dict[str, Any],
    model_id: str,
    thinking_level: str,
    margin_frac: Optional[float] = None,
    mask_bboxes: Optional[List[Sequence[float]]] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """Crop one region and transcribe it. Returns the raw transcription dict.

    ``margin_frac`` overrides the default crop margin. ``mask_bboxes`` whites out
    neighbouring regions in the crop so only this region's ink is read.
    ``temperature`` sets the sampling temperature (low = more accurate reads).
    """
    region_type = det.get("region_type", "main_text")
    bbox = det.get("bbox")

    crop_kwargs: Dict[str, Any] = {}
    if margin_frac is not None:
        crop_kwargs["margin_frac"] = margin_frac
    if mask_bboxes:
        crop_kwargs["mask_bboxes"] = mask_bboxes
    crop = crop_region_bytes(page, bbox, **crop_kwargs) if bbox else None
    if crop is None:
        # No usable bbox: leave empty for the consistency pass to handle.
        logger.debug("Region %s has no usable bbox; skipping crop.",
                     det.get("region_index"))
        return {"content": "", "languages": [], "is_visual": False,
                "table_data": None}

    hint = _TYPE_HINTS.get(region_type, "A region of the journal page.")
    prompt = _TRANSCRIPTION_PROMPT.format(region_type=region_type, hint=hint)

    result = generate_json(
        client, model_id, prompt,
        thinking_level=thinking_level,
        temperature=temperature,
        system_instruction=HOUSE_STYLE,
        images=[crop],
        default={},
        stage=f"transcription[{region_type}]",
    )
    if not isinstance(result, dict):
        result = {}
    return result


def _build_region(det: Dict[str, Any], data: Dict[str, Any]) -> Region:
    """Combine detection metadata with the transcription result."""
    idx = det["region_index"]
    region_type = det.get("region_type", "main_text")
    content = (data.get("content") or "")

    has_text = det.get("has_text", True)
    is_visual = bool(data.get("is_visual", region_type == "sketch" or not has_text))

    return Region(
        region_type=region_type,
        region_index=idx,
        content=content,
        is_visual=is_visual,
        table_data=data.get("table_data"),
        languages=data.get("languages") or [],
        editorial_note=data.get("editorial_note"),
        position=det.get("position"),
        uncertain_readings=_extract_uncertain(content),
        crossed_out_text=None,
        related_to_entry=det.get("related_entry"),
        bbox=det.get("bbox"),
        marginal_position=det.get("marginal_position"),
        writing_layer=None,
        # The close-up crop reading is both the candidate fusion compares and the
        # pre-fusion fallback for ``content``.
        region_reading=content,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_uncertain(content: str) -> List[str]:
    """All ``[?]`` markers (with their stems) in a transcription."""
    if not content:
        return []
    return [m.group(0) for m in _UNCERTAIN_RE.finditer(content)]
