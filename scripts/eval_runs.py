#!/usr/bin/env python3
"""
Variance-aware accuracy eval (average of N runs)
================================================
Single pipeline runs vary by ±~0.5 CER (book) / ±5–6 CER (folio) because both the
transcription AND the LLM ground-truth matching are re-sampled each run. Comparing
two single runs is therefore unreliable. This tool scores several run output
directories and reports **mean ± std**, under two metrics:

  * matched   — each region vs its per-run LLM-matched GT (what the viewer shows;
                biased + varied by the matcher). Uses ``textcmp.cer_wer_vs_gt_book``.
  * canonical — the whole page's transcription vs the whole page's GT taken
                straight from the TEI (deterministic, identical across runs, no
                matcher in the loop). This isolates transcription quality.

Usage:
    # aggregate existing run dirs
    python scripts/eval_runs.py --dirs output6,output7 \
        --ground-truth-tei "<…>/H0017682.xml"

    # produce N fresh runs (GT matching + geo skipped to save cost/variance),
    # then aggregate
    python scripts/eval_runs.py --repeat 3 --images "<…>/images" \
        --ground-truth-tei "<…>/H0017682.xml" --end 5
"""

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.models import PageResult
from src.html_edition.textcmp import (
    cer_wer_vs_gt_book, _norm_for_metrics, _edit_distance,
)
from src.ground_truth import _build_gt_index, gt_lookup, _canonical_gt_text


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _load_pages(run_dir: Path) -> List[PageResult]:
    jdir = run_dir / "json" if (run_dir / "json").is_dir() else run_dir
    return [PageResult.from_dict(json.load(open(f, encoding="utf-8")))
            for f in sorted(jdir.glob("page_*.json"))]


def _canonical_score(pages: List[PageResult], gt_index) -> Optional[Dict[str, float]]:
    """Matching-free, book-level CER/WER: each page's full transcription (regions
    in detection/reading order) vs the page's canonical GT from the TEI
    (deterministic, identical across runs). A strict, matcher-independent
    comparator — pessimistic on structurally complex folios (index pages) since it
    compares against the entire TEI page text, but stable across runs."""
    char_e = char_r = word_e = word_r = 0
    scored = 0
    for p in pages:
        gt_page = gt_lookup(gt_index, p.folio_label) if gt_index else None
        if gt_page is None:
            continue
        ref = _norm_for_metrics(_canonical_gt_text(gt_page))
        if not ref:
            continue
        hyp = _norm_for_metrics("\n".join(
            r.content for r in p.regions
            if r.content and not getattr(r, "is_visual", False)
        ))
        char_e += _edit_distance(list(hyp), list(ref))
        char_r += len(ref)
        rw, hw = ref.split(" "), (hyp.split(" ") if hyp else [])
        word_e += _edit_distance(hw, rw)
        word_r += len(rw)
        scored += 1
    if not char_r:
        return None
    return {"cer": char_e / char_r, "wer": word_e / word_r, "pages": scored}


def score_dir(run_dir: Path, gt_index) -> Dict[str, Optional[Dict]]:
    pages = _load_pages(run_dir)
    return {
        "matched": cer_wer_vs_gt_book([p.regions for p in pages]),
        "canonical": _canonical_score(pages, gt_index),
    }


def _agg(label: str, vals: List[Dict], key: str) -> None:
    cers = [v[key]["cer"] * 100 for v in vals if v.get(key)]
    wers = [v[key]["wer"] * 100 for v in vals if v.get(key)]
    if not cers:
        print(f"  {label:10s}  (no data)")
        return
    def ms(xs):
        m = statistics.mean(xs)
        s = statistics.pstdev(xs) if len(xs) > 1 else 0.0
        return m, s
    cm, cs = ms(cers)
    wm, ws = ms(wers)
    print(f"  {label:10s}  CER {cm:5.1f}% ± {cs:.1f}   WER {wm:5.1f}% ± {ws:.1f}   "
          f"(n={len(cers)} runs)")


# ---------------------------------------------------------------------------
# Optional: produce N fresh runs (transcription only — GT match + geo skipped)
# ---------------------------------------------------------------------------

def _produce_runs(args) -> List[Path]:
    import os
    from google import genai
    from src import config, process_book

    api_key = os.environ.get("GEMINI_API_KEY") or config.GEMINI_API_KEY
    if not api_key:
        sys.exit("GEMINI_API_KEY not set.")
    client = genai.Client(api_key=api_key)

    dirs: List[Path] = []
    for i in range(args.repeat):
        out = Path(f"{args.out_prefix}_run{i+1}")
        print(f"\n=== Run {i+1}/{args.repeat} → {out} ===")
        process_book(
            client=client, image_folder=args.images, output_folder=out,
            entity_types=config.ENTITY_TYPES, model_id=args.model,
            thinking_level=config.THINKING_LEVEL,
            start_page=args.start, end_page=args.end,
            run_geo_validation=False,          # cost + variance not needed for eval
            ground_truth_tei=None,             # score canonical from TEI ourselves
        )
        dirs.append(out)
    return dirs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    from src.logging_setup import configure_logging
    configure_logging("WARNING")
    p = argparse.ArgumentParser()
    p.add_argument("--dirs", default=None, help="comma-list of existing run dirs")
    p.add_argument("--repeat", type=int, default=0, help="produce N fresh runs first")
    p.add_argument("--out-prefix", default="output_eval")
    p.add_argument("--images", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--start", type=int, default=None)
    p.add_argument("--end", type=int, default=None)
    p.add_argument("--ground-truth-tei", default=None,
                   help="TEI for the canonical (matching-free) metric")
    args = p.parse_args()
    if args.model is None:
        from src import config
        args.model = config.MODEL_ID

    gt_index = None
    if args.ground_truth_tei:
        gt_index = _build_gt_index(args.ground_truth_tei)

    dirs: List[Path] = []
    if args.repeat:
        if not args.images:
            sys.exit("--repeat needs --images")
        dirs = _produce_runs(args)
    if args.dirs:
        dirs += [Path(d.strip()) for d in args.dirs.split(",") if d.strip()]
    if not dirs:
        sys.exit("Provide --dirs and/or --repeat.")

    results = []
    print("\nPer-run scores:")
    for d in dirs:
        sc = score_dir(d, gt_index)
        results.append(sc)
        m, c = sc["matched"], sc["canonical"]
        mt = f"matched CER {m['cer']*100:.1f}% WER {m['wer']*100:.1f}%" if m else "matched —"
        cn = f"canonical CER {c['cer']*100:.1f}% WER {c['wer']*100:.1f}%" if c else "canonical —"
        print(f"  {d.name:18s} {mt} | {cn}")

    print("\nAcross runs (mean ± std):")
    _agg("matched", results, "matched")
    _agg("canonical", results, "canonical")


if __name__ == "__main__":
    main()
