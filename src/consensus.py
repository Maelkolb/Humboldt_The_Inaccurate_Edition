"""Drift-proof merge of several independent transcriptions of one region.

Agreement is enforced in code, not by the model:
  1. Tokenise and align the candidates (medoid pivot + ``difflib``).
  2. Lock every span where all candidates agree (reproduced verbatim).
  3. Emit the disagreements (substitutions, dropped words, single-candidate
     spans) as numbered gaps ``[[M1]]`` … with their candidate variants.
  4. The resolver fills only the gaps; the final text is assembled in code, so
     locked spans cannot change.

Everything except the resolver call is pure and unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import difflib

# A token is a maximal run of non-whitespace characters. Whitespace is dropped
# for alignment (it is not a meaningful transcription difference and the CER
# metric collapses it anyway); newlines inside LOCKED runs are recovered from the
# pivot's original text so the diplomatic line layout survives.
_TOKEN_RE = re.compile(r"\S+")

# Markers shown to the resolver. ``∅`` denotes "this candidate omits the span".
_EMPTY = "∅"


@dataclass
class Skeleton:
    """A merged region: locked text pieces interleaved with numbered gaps."""
    # Ordered pieces: ("lock", text) or ("mark", marker_id)
    pieces: List[Tuple[str, str]] = field(default_factory=list)
    # marker_id -> RAW candidate variants, one per candidate ("" = omit), with
    # duplicates kept so vote counts stay correct for the majority fallback.
    variants: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def has_markers(self) -> bool:
        return bool(self.variants)

    def render_template(self, prefix: str = "") -> str:
        """The skeleton as a single string with ``[[<prefix>Mk]]`` placeholders.

        ``prefix`` namespaces the gap ids so several regions' skeletons can be
        resolved in one whole-page call without id collisions."""
        out: List[str] = []
        for kind, val in self.pieces:
            out.append(val if kind == "lock" else f"[[{prefix}{val}]]")
        return _tidy(" ".join(p for p in out if p != ""))

    def render_markers(self, prefix: str = "") -> str:
        """Human/LLM-readable list of each gap's DISTINCT candidate variants,
        each annotated with how many independent readers chose it (×N) so the
        resolver can weigh agreement when the ink is ambiguous."""
        from collections import Counter
        lines = []
        for mid, vs in self.variants.items():
            counts = Counter(vs)
            # Most-agreed first, then by length, for a stable, informative order.
            ordered = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
            shown = " | ".join(
                f"{(v if v != '' else _EMPTY)} (×{n})" for v, n in ordered
            )
            lines.append(f"{prefix}{mid}: {shown}")
        return "\n".join(lines)

    def assemble(self, choices: Dict[str, str], prefix: str = "") -> str:
        """Fill the gaps with ``choices`` (``<prefix>marker_id`` -> text); fall
        back to each gap's majority variant when a choice is missing."""
        out: List[str] = []
        for kind, val in self.pieces:
            if kind == "lock":
                out.append(val)
            else:
                chosen = choices.get(prefix + val)
                if chosen is None:
                    chosen = _majority(self.variants.get(val, [""]))
                if chosen and chosen != _EMPTY:
                    out.append(chosen)
        return _tidy(" ".join(p for p in out if p != ""))

    def best_guess(self) -> str:
        """No-LLM assembly: every gap resolved to its majority variant."""
        return self.assemble({})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

ResolveFn = Callable[[str, str], Dict[str, str]]
"""(skeleton_template, markers_text) -> {marker_id: chosen_text}."""


def build_skeleton(candidates: Sequence[Optional[str]]) -> Skeleton:
    """Align the candidate readings and build a locked skeleton with gaps."""
    texts = [c for c in (candidates or []) if c is not None and c.strip()]
    skel = Skeleton()
    if not texts:
        return skel
    if len(texts) == 1 or len(set(texts)) == 1:
        skel.pieces = [("lock", _tidy(texts[0]))]
        return skel

    pivot_idx = _medoid_index(texts)
    pivot = texts[pivot_idx]
    others = [t for i, t in enumerate(texts) if i != pivot_idx]
    return _build(pivot, others)


def merge_candidates(
    candidates: Sequence[Optional[str]],
    resolve: Optional[ResolveFn] = None,
) -> str:
    """Merge candidate readings into one. When the candidates disagree and a
    ``resolve`` callback is supplied, the gaps are filled by it (the LLM); on any
    failure the per-gap majority variant is used."""
    skel = build_skeleton(candidates)
    if not skel.has_markers:
        return skel.render_template()
    if resolve is None:
        return skel.best_guess()
    try:
        choices = resolve(skel.render_template(), skel.render_markers())
        if not isinstance(choices, dict):
            choices = {}
    except Exception:
        choices = {}
    return skel.assemble(choices)


# ---------------------------------------------------------------------------
# Alignment internals (pure)
# ---------------------------------------------------------------------------

def _build(pivot: str, others: List[str]) -> Skeleton:
    pivot_spans = list(_TOKEN_RE.finditer(pivot))
    pivot_toks = [m.group(0) for m in pivot_spans]
    n = len(pivot_toks)

    # For each other reading, the tokens occupying each pivot slot (with any
    # tokens inserted *before* that slot folded in as a prefix), plus a trailing
    # bucket for tokens after the last pivot token.
    columns: List[List[List[str]]] = []   # columns[o][i] = list of tokens
    tails: List[List[str]] = []
    for other in others:
        cells, tail = _columnize(pivot_toks, [m.group(0) for m in _TOKEN_RE.finditer(other)])
        columns.append(cells)
        tails.append(tail)

    skel = Skeleton()
    mark_n = 0
    i = 0
    while i < n:
        if _agree(i, pivot_toks, columns):
            # Extend the locked run, then emit the pivot's original substring so
            # its spacing / newlines are preserved.
            j = i
            while j < n and _agree(j, pivot_toks, columns):
                j += 1
            start = pivot_spans[i].start()
            end = pivot_spans[j - 1].end()
            skel.pieces.append(("lock", pivot[start:end]))
            i = j
        else:
            j = i
            while j < n and not _agree(j, pivot_toks, columns):
                j += 1
            mark_n += 1
            mid = f"M{mark_n}"
            pv = " ".join(pivot_toks[i:j])
            variants = [_tidy(pv)]
            for o, cells in enumerate(columns):
                ov = " ".join(t for slot in cells[i:j] for t in slot)
                variants.append(_tidy(ov))
            skel.variants[mid] = variants
            skel.pieces.append(("mark", mid))
            i = j

    # Trailing tokens present past the pivot in some readings. Resolve by MAJORITY
    # here instead of as an LLM-adjudicated gap: a lone whole-page read that runs on
    # into the next region (overflow) is outvoted by the boundary-faithful readers
    # (the masked crop, the structured read) and dropped, while a tail the majority
    # share — the pivot simply read less — is kept. The resolver tends to
    # over-include such trailing text, so it never sees this decision.
    if any(t for t in tails):
        # One vote per candidate: the pivot has no tail by construction ("").
        tail_votes = [""] + [_tidy(" ".join(t)) for t in tails]
        winner = _majority(tail_votes)
        if winner:
            skel.pieces.append(("lock", winner))

    return skel


def _columnize(
    pivot_toks: List[str], other_toks: List[str]
) -> Tuple[List[List[str]], List[str]]:
    """Map ``other_toks`` onto the pivot token slots via difflib opcodes.

    Returns ``(cells, tail)`` where ``cells[i]`` is the list of other tokens
    aligned to (or inserted before) pivot slot ``i``, and ``tail`` is everything
    inserted after the last pivot token.
    """
    cells: List[List[str]] = [[] for _ in pivot_toks]
    tail: List[str] = []
    sm = difflib.SequenceMatcher(a=pivot_toks, b=other_toks, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                cells[i1 + k].append(other_toks[j1 + k])
        elif tag == "replace":
            block = other_toks[j1:j2]
            if (i2 - i1) == (j2 - j1):
                for k in range(i2 - i1):
                    cells[i1 + k].append(other_toks[j1 + k])
            else:
                # Length mismatch: dump the whole replacement chunk on the first
                # slot; later slots in the block stay empty (treated as omitted).
                cells[i1].extend(block)
        elif tag == "delete":
            pass  # pivot tokens with no counterpart -> empty cells (omission)
        elif tag == "insert":
            block = other_toks[j1:j2]
            if i1 < len(pivot_toks):
                cells[i1] = block + cells[i1]   # prefix: inserted before slot i1
            else:
                tail.extend(block)
    return cells, tail


def _agree(i: int, pivot_toks: List[str], columns: List[List[List[str]]]) -> bool:
    """True iff every other reading has exactly the pivot token at slot ``i``."""
    want = [pivot_toks[i]]
    return all(cells[i] == want for cells in columns)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _medoid_index(texts: List[str]) -> int:
    """Index of the candidate with the smallest total token-edit distance to the
    others — the most representative reading, least likely to be the contaminated
    outlier."""
    toks = [[m.group(0) for m in _TOKEN_RE.finditer(t)] for t in texts]
    best_i, best_cost = 0, None
    for i in range(len(texts)):
        cost = sum(_edit_distance(toks[i], toks[j]) for j in range(len(texts)) if j != i)
        if best_cost is None or cost < best_cost:
            best_i, best_cost = i, cost
    return best_i


def _edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
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
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ca == cb else 1)))
        prev = cur
    return prev[-1]


def _distinct(variants: List[str]) -> List[str]:
    """Distinct variants, order-preserving."""
    seen: set = set()
    out: List[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _majority(variants: List[str]) -> str:
    """Most common variant; ties broken by the longest."""
    if not variants:
        return ""
    from collections import Counter
    counts = Counter(variants)
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


_WS_RE = re.compile(r"[ \t]+")


def _tidy(text: str) -> str:
    """Collapse runs of spaces/tabs but keep newlines; trim ends."""
    text = _WS_RE.sub(" ", text or "")
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()
