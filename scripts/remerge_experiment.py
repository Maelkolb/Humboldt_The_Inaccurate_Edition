#!/usr/bin/env python3
"""
Re-merge experiment (cheap merge-config search)
===============================================
The candidate readings (``ensemble_readings``) produced by a finished ensemble
run are fixed and stored in the per-page JSON. This harness re-runs ONLY the
alignment-locked MERGE step over those fixed candidates with several resolver
configurations and scores each against the ground truth — so we can find the best
merge config without paying for detection or the 3 candidate reads again.

Baselines (no API): the run's own ``content``, no-LLM majority, and the
oracle@markers ceiling. Resolver variants (API): per-region with different
models / images, and a single whole-page merge.

Usage:
    python scripts/remerge_experiment.py --json output6/json \
        --images "<…>/H0017682_humboldt/images" --variants v1,v2
"""

import argparse
import dataclasses
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google import genai

from src import config, load_results_from_json
from src.logging_setup import configure_logging
from src.consensus import build_skeleton, _distinct
from src.imaging import load_image_rgb, crop_region_bytes, page_image_bytes
from src.llm import generate_json
from src.transcription import _MERGE_PROMPT, _TYPE_HINTS
from src.html_edition.textcmp import (
    cer_wer_vs_gt_book, GT_SCORE_TYPES, _norm_for_metrics, _edit_distance,
)

logger = logging.getLogger("remerge")

READER_MODEL = "gemini-3-flash-preview"   # proven ink reader
BEST_MODEL = config.MODEL_ID_BEST          # gemini-3.5-flash
THINKING = config.THINKING_LEVEL_MERGE     # "high"

_PAGE_MERGE_PROMPT = """\
You are resolving automated transcriptions of one page of Alexander von
Humboldt's journal. The full page image is attached and is the ONLY authority —
read the actual ink. Preserve original period spelling; do NOT modernise or
expand abbreviations.

Below are several REGIONS. Each has a SKELETON whose certain text is already
filled in and is CORRECT (do not change it); the uncertain spots are gaps
[[id]]. For each gap you are given the candidate readings (separated by " | ";
∅ = a transcriber read nothing). For EACH gap, return the reading that matches
the ink in that region of the page. If a gap's text belongs to a neighbouring
region (it appears in only one candidate), return "" to drop it.

{blocks}

Respond ONLY with a JSON object mapping every gap id to its chosen text, e.g.
{{"R0M1": "...", "R3M2": "..."}}
"""


# ---------------------------------------------------------------------------
# Per-region resolvers
# ---------------------------------------------------------------------------

def _scored(regions):
    return [r for r in regions
            if r.region_type in GT_SCORE_TYPES and not getattr(r, "is_visual", False)
            and (r.ground_truth_content or "").strip()
            and (r.ensemble_readings or [r.content])]


def _cands(r):
    return [c for c in (r.ensemble_readings or [r.content or ""]) if (c or "").strip()]


def _others_bboxes(regions, idx):
    return [rr.bbox for rr in regions if rr.region_index != idx and rr.bbox]


page_img_path_cache: Dict[int, str] = {}


def resolve_region_imgs(client, model, region, images):
    """Per-region resolve with an explicit image list (crop and/or page)."""
    skel = build_skeleton(_cands(region))
    if not skel.has_markers:
        return skel.render_template()
    if not images:
        return skel.best_guess()
    rt = region.region_type
    prompt = _MERGE_PROMPT.format(
        region_type=rt, hint=_TYPE_HINTS.get(rt, "A region of the journal page."),
        skeleton=skel.render_template(), markers=skel.render_markers(),
    )
    res = generate_json(client, model, prompt, thinking_level=THINKING,
                        temperature=config.TEMPERATURE_MERGE,
                        images=images, default={}, stage="remerge")
    return skel.assemble(res if isinstance(res, dict) else {})


# ---------------------------------------------------------------------------
# Baselines (no API)
# ---------------------------------------------------------------------------

def merged_majority(region):
    return build_skeleton(_cands(region)).best_guess()


def merged_oracle(region):
    skel = build_skeleton(_cands(region))
    if not skel.has_markers:
        return skel.render_template()
    gt = region.ground_truth_content or ""
    g = _norm_for_metrics(gt)
    ch: Dict[str, str] = {}
    for mid, vs in skel.variants.items():
        best = None
        bv = None
        for v in _distinct(vs):
            trial = dict(ch)
            trial[mid] = v
            h = _norm_for_metrics(skel.assemble(trial))
            d = _edit_distance(list(h), list(g))
            if best is None or d < best:
                best, bv = d, v
        ch[mid] = bv
    return skel.assemble(ch)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def score(results, merged_by_page):
    """merged_by_page: list (per page) of {region_index: text}. Returns metric."""
    pages = []
    for res, mp in zip(results, merged_by_page):
        regs = [dataclasses.replace(r, content=mp.get(r.region_index, r.content))
                for r in res.regions]
        pages.append(regs)
    return cer_wer_vs_gt_book(pages)


def run_variant(name, fn_region, results, page_images, workers):
    """fn_region(region, page_img, regions) -> merged text. Runs concurrently."""
    merged_by_page = []
    for res, page_img in zip(results, page_images):
        regs = _scored(res.regions)
        out: Dict[int, str] = {}
        if regs and page_img is not None:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                fut = {pool.submit(fn_region, r, page_img, res.regions): r for r in regs}
                for f in as_completed(fut):
                    out[fut[f].region_index] = f.result()
        merged_by_page.append(out)
    m = score(results, merged_by_page)
    return m


def main():
    configure_logging("WARNING")
    p = argparse.ArgumentParser()
    p.add_argument("--json", default="output6/json")
    p.add_argument("--images", required=True)
    p.add_argument("--variants", default="v1,v2",
                   help="comma list of API variants: v1,v2,v3,v4")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()

    results = load_results_from_json(args.json)
    img_dir = Path(args.images)
    page_images = []
    page_paths = []
    for res in results:
        path = img_dir / res.image_filename
        if path.exists():
            img = load_image_rgb(path)
            page_images.append(img)
            page_paths.append(str(path))
            page_img_path_cache[id(img)] = str(path)
        else:
            page_images.append(None)
            page_paths.append(None)

    n_scored = sum(len(_scored(r.regions)) for r in results)
    print(f"Loaded {len(results)} pages, {n_scored} scored regions with GT+candidates.\n")

    # ---- Baselines (no API) ----
    def show(label, m):
        if m:
            print(f"  {label:34s} CER={m['cer']*100:5.1f}%  WER={m['wer']*100:5.1f}%  "
                  f"regions={m['n_regions']} dropped={m['n_dropped']}")

    print("BASELINES (no API):")
    show("output6 content (current 12.0)",
         score(results, [{r.region_index: r.content for r in res.regions} for res in results]))
    show("no-LLM majority",
         run_variant("maj", lambda r, pi, rs: merged_majority(r), results, page_images, args.workers))
    show("oracle@markers (ceiling)",
         run_variant("orc", lambda r, pi, rs: merged_oracle(r), results, page_images, args.workers))

    api_key = os.environ.get("GEMINI_API_KEY") or config.GEMINI_API_KEY
    if not api_key:
        print("\n(No GEMINI_API_KEY — skipping API variants.)")
        return
    client = genai.Client(api_key=api_key)
    want = {v.strip() for v in args.variants.split(",") if v.strip()}

    print("\nRESOLVER VARIANTS (API):")
    if "v1" in want:
        show("V1 3.5-flash + crop (reproduce)",
             run_variant("v1",
                 lambda r, pi, rs: resolve_region_imgs(
                     client, BEST_MODEL, r,
                     [crop_region_bytes(pi, r.bbox, max_px=config.MERGE_CROP_MAX_PX,
                                        mask_bboxes=_others_bboxes(rs, r.region_index))]
                     if r.bbox else []),
                 results, page_images, args.workers))
    if "v2" in want:
        show("V2 3-flash reader + crop",
             run_variant("v2",
                 lambda r, pi, rs: resolve_region_imgs(
                     client, READER_MODEL, r,
                     [crop_region_bytes(pi, r.bbox, max_px=config.MERGE_CROP_MAX_PX,
                                        mask_bboxes=_others_bboxes(rs, r.region_index))]
                     if r.bbox else []),
                 results, page_images, args.workers))
    if "v3" in want:
        def v3(r, pi, rs):
            imgs = []
            if r.bbox:
                imgs.append(crop_region_bytes(pi, r.bbox, max_px=config.MERGE_CROP_MAX_PX,
                            mask_bboxes=_others_bboxes(rs, r.region_index)))
            imgs.append(page_image_bytes(page_img_path_cache[id(pi)],
                                         max_px=config.PAGE_READ_MAX_PX))
            return resolve_region_imgs(client, BEST_MODEL, r, [i for i in imgs if i])
        show("V3 3.5-flash + crop + page",
             run_variant("v3", v3, results, page_images, args.workers))
    if "v4" in want:
        show("V4 whole-page single merge", _run_v4(client, results, page_images, page_paths))


def _run_v4(client, results, page_images, page_paths):
    """One whole-page merge call per page, namespaced markers."""
    merged_by_page = []
    for res, pi, path in zip(results, page_images, page_paths):
        out: Dict[int, str] = {}
        regs = _scored(res.regions)
        skels = {r.region_index: build_skeleton(_cands(r)) for r in regs}
        with_markers = [r for r in regs if skels[r.region_index].has_markers]
        for r in regs:
            if not skels[r.region_index].has_markers:
                out[r.region_index] = skels[r.region_index].render_template()
        if with_markers and pi is not None and path:
            blocks = []
            for r in with_markers:
                sk = skels[r.region_index]
                pre = f"R{r.region_index}M"
                blocks.append(
                    f"REGION {r.region_index} ({r.region_type}):\n"
                    f"SKELETON: {sk.render_template(prefix='R'+str(r.region_index))}\n"
                    f"GAPS:\n{sk.render_markers(prefix='R'+str(r.region_index))}"
                )
            prompt = _PAGE_MERGE_PROMPT.format(blocks="\n\n".join(blocks))
            img = page_image_bytes(path, max_px=config.PAGE_READ_MAX_PX)
            res_json = generate_json(client, BEST_MODEL, prompt, thinking_level=THINKING,
                                     temperature=config.TEMPERATURE_MERGE,
                                     images=[img], default={}, stage="remerge_page")
            choices = res_json if isinstance(res_json, dict) else {}
            for r in with_markers:
                out[r.region_index] = skels[r.region_index].assemble(
                    choices, prefix="R" + str(r.region_index))
        merged_by_page.append(out)
    return score(results, merged_by_page)


if __name__ == "__main__":
    main()
