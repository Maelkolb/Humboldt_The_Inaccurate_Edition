from __future__ import annotations

import re
import difflib
import unicodedata
import html as html_lib
from typing import Any, Dict, List, Optional, Tuple

from ..models import Region
from .markup import render_plain

_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")

def _tokenize(text: str) -> List[str]:
    """Split text into a list of tokens (words, whitespace, single chars).

    Whitespace is preserved as its own token so diff output keeps original
    spacing.
    """
    if not text:
        return []
    return _TOKEN_RE.findall(text)

def render_diff(gemini_text: str, gt_text: str) -> str:
    """Render a word-level diff between Gemini's transcription and the
    ground-truth transcription as inline HTML.

    Conventions follow ``git diff`` semantics with the ground-truth treated
    as the canonical "after" state:

      * Tokens common to both: rendered plain.
      * Tokens present only in Gemini (i.e. Gemini wrongly added /
        misread): wrapped in ``<span class="diff-del">…</span>`` with a
        strike-through.
      * Tokens present only in the ground-truth (i.e. Gemini missed them):
        wrapped in ``<span class="diff-ins">…</span>`` with an underline.

    Whitespace differences are smoothed out — only word-level edits show
    as deletions/insertions.
    """
    if not gemini_text and not gt_text:
        return ""
    a = _tokenize(gemini_text or "")
    b = _tokenize(gt_text or "")

    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: List[str] = []

    def _esc_token(t: str) -> str:
        # Preserve newlines so multi-line diffs stay readable in HTML
        return html_lib.escape(t).replace("\n", "<br>\n")

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for tok in a[i1:i2]:
                out.append(_esc_token(tok))
        elif tag == "delete":
            chunk = "".join(_esc_token(t) for t in a[i1:i2])
            if chunk.strip():
                out.append(
                    f'<span class="diff-del" title="Only in Gemini transcription">'
                    f'{chunk}</span>'
                )
            else:
                out.append(chunk)
        elif tag == "insert":
            chunk = "".join(_esc_token(t) for t in b[j1:j2])
            if chunk.strip():
                out.append(
                    f'<span class="diff-ins" title="Only in ground truth">'
                    f'{chunk}</span>'
                )
            else:
                out.append(chunk)
        elif tag == "replace":
            del_chunk = "".join(_esc_token(t) for t in a[i1:i2])
            ins_chunk = "".join(_esc_token(t) for t in b[j1:j2])
            if del_chunk.strip():
                out.append(
                    f'<span class="diff-del" title="Gemini">{del_chunk}</span>'
                )
            if ins_chunk.strip():
                out.append(
                    f'<span class="diff-ins" title="Ground truth">{ins_chunk}</span>'
                )
    return "".join(out)

def render_gt_plain(text: str) -> str:
    """Render ground-truth text with the same editorial markup we use for
    Gemini text (``~~``, ``<u>``, ``[?]``), but without entity highlighting
    — entities in GT are out-of-scope for the viewer's runtime."""
    return render_plain(text or "")

_WS_COLLAPSE_RE = re.compile(r"\s+")

def _norm_ws(text: str) -> str:
    """Collapse whitespace runs to single spaces and strip ends."""
    return _WS_COLLAPSE_RE.sub(" ", (text or "")).strip()

_METRIC_FOLD_CASE = True

_METRIC_DROP_PUNCT = True

_METRIC_FOLD_DIACRITICS = False

_METRIC_MARKUP_RE = re.compile(r"</?u>|~~|\[[^\]]*\]")

_METRIC_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

_METRIC_HYPHEN_BREAK_RE = re.compile(r"\u00ad|-\s+")

_METRIC_GLYPHS = str.maketrans({
    "ſ": "s",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
})

def _strip_diacritics(text: str) -> str:
    """Drop combining marks (é → e). Used for CER/WER only when the diacritic
    fold flag is enabled."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )

def _norm_for_metrics(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "")
    t = _METRIC_MARKUP_RE.sub(" ", t)        # editorial apparatus
    t = t.translate(_METRIC_GLYPHS)          # long-s, ligatures, dash variants
    t = _METRIC_HYPHEN_BREAK_RE.sub("", t)   # rejoin line-broken words
    if _METRIC_FOLD_DIACRITICS:
        t = _strip_diacritics(t)
    if _METRIC_DROP_PUNCT:
        t = _METRIC_PUNCT_RE.sub("", t)
    if _METRIC_FOLD_CASE:
        t = t.lower()
    return _norm_ws(t)

def _edit_distance(a: List[str], b: List[str]) -> int:
    """Levenshtein distance over two token sequences (chars or words)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = cur
    return prev[-1]

# ---------------------------------------------------------------------------
# Alignment-gated CER/WER over all matched regions (the canonical metric)
# ---------------------------------------------------------------------------

# Every text-bearing region type whose ground truth is prose that can be scored
# character-for-character. Structural regions (sketch = visual, observation_table
# = tabular) are excluded — their GT is not free text.
GT_SCORE_TYPES = frozenset({
    "page_number", "entry_heading", "main_text", "marginal_note", "pasted_slip",
    "bibliographic_ref", "coordinates", "instrument_list", "calculation",
    "crossed_out", "catch_phrase",
})

# A region whose transcription and its matched GT share almost nothing is a
# ground-truth MATCHING failure (the wrong span was assigned), not a hard
# reading — scoring it would penalise the transcription for a matcher error. We
# therefore drop a region from the metric only when its per-region CER is at/above
# this level (effectively no alignment at all). Genuinely hard but correctly
# matched regions (CER well below this) are always kept.
BOGUS_MATCH_CER = 0.85


def cer_wer_vs_gt(
    regions: List[Region],
    *,
    region_types: Optional[frozenset] = None,
    drop_bogus: bool = True,
    bogus_cer: float = BOGUS_MATCH_CER,
) -> Optional[Dict[str, Any]]:
    """CER/WER of the transcription vs. ground truth over all matched text regions
    on a page (or any region list).

    Regions are paired with their own matched GT, normalised with
    :func:`_norm_for_metrics` (punctuation stripped, case folded, editorial
    markers removed), and a region whose own CER ≥ ``bogus_cer`` is dropped (a
    matcher failure, not a reading error). The kept regions are then
    **concatenated and aligned ONCE** as a page: this is robust to region-boundary
    mis-splits (the same text grouped differently in hyp vs GT no longer
    double-counts) and is inherently length-weighted, so small regions cannot
    inflate the score.

    Returns ``cer``, ``wer``, ``n_regions``, ``n_dropped``, ``ref_chars``,
    ``ref_words``, ``char_edits``, ``word_edits`` — or ``None`` when no region has
    usable GT.
    """
    types = region_types or GT_SCORE_TYPES
    kept_hyp: List[str] = []
    kept_ref: List[str] = []
    n = n_dropped = 0
    saw_gt = False

    for r in regions:
        if r.region_type not in types or getattr(r, "is_visual", False):
            continue
        gt = r.ground_truth_content
        if not gt or not gt.strip():
            continue
        saw_gt = True
        ref = _norm_for_metrics(gt)
        if not ref:
            continue
        hyp = _norm_for_metrics(r.content or "")
        if drop_bogus and (_edit_distance(list(hyp), list(ref)) / len(ref)) >= bogus_cer:
            n_dropped += 1
            continue
        kept_hyp.append(hyp)
        kept_ref.append(ref)
        n += 1

    if not saw_gt or not kept_ref:
        return None

    # One global alignment over the concatenated page (boundary-robust, micro).
    hyp = " ".join(kept_hyp).strip()
    ref = " ".join(kept_ref).strip()
    if not ref:
        return None
    char_edits = _edit_distance(list(hyp), list(ref))
    ref_words = ref.split(" ")
    hyp_words = hyp.split(" ") if hyp else []
    word_edits = _edit_distance(hyp_words, ref_words)
    return {
        "cer": char_edits / len(ref),
        "wer": word_edits / len(ref_words),
        "n_regions": n,
        "n_dropped": n_dropped,
        "ref_chars": len(ref),
        "ref_words": len(ref_words),
        "char_edits": char_edits,
        "word_edits": word_edits,
    }


def cer_wer_vs_gt_book(
    pages: List[List[Region]],
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Book-level micro-average: sum each page's edit counts (each page aligned
    once) so the headline weights by total text, not page count — without aligning
    one book-sized string."""
    ce = cr = we = wr = n = nd = 0
    saw = False
    for regs in pages:
        m = cer_wer_vs_gt(regs, **kwargs)
        if not m:
            continue
        saw = True
        ce += m["char_edits"]; cr += m["ref_chars"]
        we += m["word_edits"]; wr += m["ref_words"]
        n += m["n_regions"]; nd += m["n_dropped"]
    if not saw or cr == 0:
        return None
    return {
        "cer": ce / cr, "wer": we / wr,
        "n_regions": n, "n_dropped": nd,
        "ref_chars": cr, "ref_words": wr,
        "char_edits": ce, "word_edits": we,
    }
