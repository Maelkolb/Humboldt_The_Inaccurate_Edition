#!/usr/bin/env python3
"""Per-page CER/WER report + slide visualization across one or more journals.

Reads each run's stored regions (with ground_truth_content already populated by
the GT-matching stage) and computes the page-concat CER/WER metric. Writes a
markdown table and a slide-ready PNG.

Usage:
    python scripts/eval_report.py \
        England=output_england_full America=output_america_full Austria=output_austr_full
    # defaults to those three if no args are given.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src import load_results_from_json
from src.html_edition.textcmp import cer_wer_vs_gt, cer_wer_vs_gt_book

DEFAULTS = [
    ("England", "output_england_full"),
    ("America", "output_america_full"),
    ("Austria", "output_austr_full"),
]
OUT_MD = Path("eval_report.md")
OUT_PNG = Path("eval_report.png")


def _per_page(results):
    """[(folio, cer%, wer%, n_regions)] for pages that have scorable GT."""
    rows = []
    for p in sorted(results, key=lambda r: r.page_number):
        m = cer_wer_vs_gt(p.regions)
        if m:
            rows.append((p.folio_label, m["cer"] * 100, m["wer"] * 100, m["n_regions"]))
    return rows


def main() -> None:
    pairs = []
    for arg in sys.argv[1:]:
        if "=" in arg:
            label, path = arg.split("=", 1)
            pairs.append((label, path))
    pairs = pairs or DEFAULTS

    journals = []  # (label, per_page_rows, book_metric)
    for label, path in pairs:
        jdir = Path(path) / "json"
        if not jdir.is_dir():
            print(f"  (skip {label}: {jdir} not found)")
            continue
        results = load_results_from_json(str(jdir))
        rows = _per_page(results)
        book = cer_wer_vs_gt_book([r.regions for r in results])
        journals.append((label, rows, book))
        print(f"  {label}: {len(rows)} scored pages, "
              f"book CER {book['cer']*100:.1f}% / WER {book['wer']*100:.1f}%")

    if not journals:
        print("No journals to report."); return

    _write_markdown(journals)
    _plot(journals)
    print(f"\nWrote {OUT_MD} and {OUT_PNG}")


def _write_markdown(journals) -> None:
    lines = ["# Transcription accuracy vs. ground truth", ""]
    # Summary
    lines += ["## Summary (book-level, page-concat micro-average)", "",
              "| Journal | Pages | CER | WER |", "|---|---:|---:|---:|"]
    for label, rows, book in journals:
        lines.append(f"| {label} | {len(rows)} | {book['cer']*100:.1f}% | {book['wer']*100:.1f}% |")
    lines.append("")
    # Per-page, one table per journal
    for label, rows, _ in journals:
        lines += [f"## {label} — per folio", "",
                  "| Folio | CER | WER | regions |", "|---|---:|---:|---:|"]
        for folio, cer, wer, n in rows:
            lines.append(f"| {folio} | {cer:.1f}% | {wer:.1f}% | {n} |")
        lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def _plot(journals) -> None:
    labels = [j[0] for j in journals]
    book_cer = [j[2]["cer"] * 100 for j in journals]
    book_wer = [j[2]["wer"] * 100 for j in journals]
    per_page_cer = [[r[1] for r in j[1]] for j in journals]

    plt.rcParams.update({"font.size": 12, "axes.grid": True,
                         "grid.alpha": 0.3, "axes.axisbelow": True})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5.2))
    fig.suptitle("Humboldt digital edition — transcription accuracy vs. ground truth",
                 fontsize=15, fontweight="bold")

    # Panel 1: book-level CER / WER grouped bars
    import numpy as np
    x = np.arange(len(labels)); w = 0.38
    b1 = ax1.bar(x - w/2, book_cer, w, label="CER", color="#1565c0")
    b2 = ax1.bar(x + w/2, book_wer, w, label="WER", color="#e65100")
    ax1.set_xticks(x); ax1.set_xticklabels(labels)
    ax1.set_ylabel("Error rate (%)")
    ax1.set_title("Book-level (lower = better)")
    ax1.legend(frameon=False)
    for bars in (b1, b2):
        for b in bars:
            ax1.annotate(f"{b.get_height():.1f}", (b.get_x()+b.get_width()/2, b.get_height()),
                         ha="center", va="bottom", fontsize=10)

    # Panel 2: per-page CER distribution (box + jittered points)
    bp = ax2.boxplot(per_page_cer, tick_labels=labels, showfliers=False, patch_artist=True,
                     medianprops=dict(color="#222"))
    for patch in bp["boxes"]:
        patch.set(facecolor="#bbdefb", alpha=0.7)
    for i, vals in enumerate(per_page_cer, start=1):
        xs = np.random.normal(i, 0.05, size=len(vals))
        ax2.scatter(xs, vals, s=14, color="#1565c0", alpha=0.55, zorder=3)
    ax2.set_ylabel("Per-folio CER (%)")
    ax2.set_title("Per-folio CER spread")

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(OUT_PNG, dpi=160)


if __name__ == "__main__":
    main()
