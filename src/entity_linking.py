"""Deterministic, offline entity linking + consistency check.

Runs separately after the pipeline over ``digital_edition_complete.json`` (see
``scripts/link_entities.py``); not wired into ``process_book``. On the
Person/Location/Species entities from NER it does two things:

1. LINKING – resolves every Person / Location / Species entity emitted by the
   NER stage against the *edition humboldt digital* authority register
   (:mod:`src.entity_register`), attaching the eHD id and the chained
   authority URI (VIAF/GND for persons, GeoNames for places, GBIF for plants).
   Minerals among the Species entities have no plant-register match and are
   left unlinked – which is the intended outcome.

       entity_type  ->  register kind
       -----------      -------------
       Person           person
       Location         place
       Species          plant

2. CONSISTENCY – a cross-page quality gate over the whole journal. Because the
   linker pins each mention to a stable id, we can audit internal agreement of
   the NER + linking output and surface:

     * normalization_conflict – one surface form is given DIFFERENT
       ``normalized_form`` values across the journal (an NER inconsistency,
       e.g. "L. calcareus" -> "Aspicilia calcarea" on one page but
       "Lichen calcareus" on another);
     * link_conflict – mentions the NER itself calls the same entity (same
       normalized_form) resolve to DIFFERENT register ids;
     * ambiguous_link – a single mention matched several register candidates
       (homonyms) and was disambiguated only weakly – flagged for review;
     * variant_merge – several distinct surface forms resolve to the SAME
       register id (the good case; confirms the linker merged spelling
       variants such as "Faujas de St Fond" / "Fauj. de St Fond").

   Plus per-type coverage statistics (linked / total).

This stage is pure-Python: no LLM, no network. It is fully deterministic and
therefore reproducible and cheap to re-run. It fails open: a missing register
or an unmatched entity never raises, it just leaves entities unlinked.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import Entity, PageResult
from .entity_register import EntityRegister, norm_key

logger = logging.getLogger(__name__)

# NER entity_type -> register kind. Types not listed here are never linked.
DEFAULT_TYPE_TO_KIND: Dict[str, str] = {
    "Person": "person",
    "Location": "place",
    "Species": "plant",
}

# A surname/fuzzy match below this score is attached but also reported as
# ambiguous_link for human review.
_REVIEW_SCORE = 0.75


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------

def link_entity(
    entity: Entity,
    register: EntityRegister,
    type_to_kind: Dict[str, str] = DEFAULT_TYPE_TO_KIND,
    *,
    fuzzy: bool = True,
    fuzzy_cutoff: float = 0.9,
) -> Entity:
    """Annotate one entity in place with register authority links.

    Leaves the entity untouched (unlinked) when its type is not linkable or no
    register match is found. Returns the same entity for convenience.
    """
    kind = type_to_kind.get(entity.entity_type)
    if not kind:
        return entity
    match = register.lookup(
        kind, entity.text, entity.normalized_form,
        fuzzy=fuzzy, fuzzy_cutoff=fuzzy_cutoff,
    )
    if match is None:
        return entity
    e = match.entry
    entity.ehd_id = e.ehd_id
    entity.ehd_url = e.ehd_url
    entity.authority_uri = e.authority_uri
    entity.authority_label = e.authority_label or e.label
    entity.link_method = match.method
    entity.link_score = match.score
    entity.link_ambiguous = match.ambiguous
    entity.link_candidates = list(match.candidates)
    return entity


def link_entities(
    entities: List[Entity],
    register: EntityRegister,
    type_to_kind: Dict[str, str] = DEFAULT_TYPE_TO_KIND,
    *,
    fuzzy: bool = True,
    fuzzy_cutoff: float = 0.9,
) -> List[Entity]:
    """Link a page's worth of entities in place. Returns the same list."""
    for ent in entities:
        link_entity(ent, register, type_to_kind,
                    fuzzy=fuzzy, fuzzy_cutoff=fuzzy_cutoff)
    return entities


def link_results(
    results: List[PageResult],
    register: EntityRegister,
    type_to_kind: Dict[str, str] = DEFAULT_TYPE_TO_KIND,
    *,
    fuzzy: bool = True,
    fuzzy_cutoff: float = 0.9,
) -> List[PageResult]:
    """Link every entity across all pages in place. Returns the same list."""
    for page in results:
        link_entities(page.entities, register, type_to_kind,
                      fuzzy=fuzzy, fuzzy_cutoff=fuzzy_cutoff)
    return results


# ---------------------------------------------------------------------------
# Consistency check (cross-page)
# ---------------------------------------------------------------------------

@dataclass
class _Mention:
    page: int
    folio: str
    text: str
    norm_text: str
    normalized_form: Optional[str]
    ehd_id: Optional[str]
    link_method: Optional[str]
    link_score: Optional[float]
    link_ambiguous: bool
    candidates: List[str]


def _gather(results: List[PageResult], kind_types: Dict[str, str]) -> Dict[str, List[_Mention]]:
    """Collect mentions per linkable entity_type across all pages."""
    by_type: Dict[str, List[_Mention]] = defaultdict(list)
    for page in results:
        for e in page.entities:
            if e.entity_type not in kind_types:
                continue
            by_type[e.entity_type].append(_Mention(
                page=page.page_number,
                folio=page.folio_label,
                text=e.text,
                norm_text=norm_key(e.text),
                normalized_form=e.normalized_form,
                ehd_id=e.ehd_id,
                link_method=e.link_method,
                link_score=e.link_score,
                link_ambiguous=bool(e.link_ambiguous),
                candidates=list(e.link_candidates or []),
            ))
    return by_type


def check_entity_consistency(
    results: List[PageResult],
    register: Optional[EntityRegister] = None,
    type_to_kind: Dict[str, str] = DEFAULT_TYPE_TO_KIND,
) -> Dict[str, Any]:
    """Audit linked entities across the whole journal.

    Returns a structured report::

        {
          "coverage": { "Person": {linked, total, pct}, ... },
          "issues":   [ {issue_type, severity, entity_type, ...}, ... ],
          "summary":  { counts per issue_type / severity },
        }

    The report is deterministic and references pages by ``page_number`` /
    ``folio_label`` so individual cases are easy to look up.
    """
    by_type = _gather(results, type_to_kind)
    issues: List[Dict[str, Any]] = []
    coverage: Dict[str, Dict[str, Any]] = {}

    def label_for(ehd_id: str) -> str:
        if register and ehd_id in register.entries:
            return register.entries[ehd_id].label
        return ehd_id

    for etype, mentions in by_type.items():
        total = len(mentions)
        linked = sum(1 for m in mentions if m.ehd_id)
        coverage[etype] = {
            "linked": linked,
            "total": total,
            "pct": round(100.0 * linked / total, 1) if total else 0.0,
        }

        # -- normalization_conflict: same surface form, different normalized_form
        by_surface: Dict[str, set] = defaultdict(set)
        surface_pages: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
        for m in mentions:
            if not m.norm_text:
                continue
            nf = (m.normalized_form or "").strip()
            by_surface[m.norm_text].add(nf)
            surface_pages[m.norm_text].append((m.page, nf))
        for surface, forms in by_surface.items():
            forms = {f for f in forms if f}
            if len(forms) > 1:
                example = next(m for m in mentions if m.norm_text == surface)
                issues.append({
                    "issue_type": "normalization_conflict",
                    "severity": "warning",
                    "entity_type": etype,
                    "surface_form": example.text,
                    "normalized_forms": sorted(forms),
                    "pages": sorted({p for p, _ in surface_pages[surface]}),
                    "description": (
                        f"Surface form '{example.text}' is normalized "
                        f"inconsistently to {sorted(forms)} across the journal."
                    ),
                })

        # -- link_conflict: same normalized_form, different ehd_id
        by_nf: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(list))
        for m in mentions:
            nf = norm_key(m.normalized_form or m.text)
            if nf and m.ehd_id:
                by_nf[nf][m.ehd_id].append(m.page)
        for nf, id_pages in by_nf.items():
            if len(id_pages) > 1:
                issues.append({
                    "issue_type": "link_conflict",
                    "severity": "warning",
                    "entity_type": etype,
                    "normalized_form": nf,
                    "linked_ids": [
                        {"ehd_id": i, "label": label_for(i), "pages": sorted(set(ps))}
                        for i, ps in id_pages.items()
                    ],
                    "description": (
                        f"Mentions normalized as '{nf}' link to "
                        f"{len(id_pages)} different register ids."
                    ),
                })

        # -- ambiguous_link / weak link: flag for review
        seen_amb: set = set()
        for m in mentions:
            if not m.ehd_id:
                continue
            weak = (m.link_score is not None and m.link_score < _REVIEW_SCORE)
            if m.link_ambiguous or weak:
                key = (m.norm_text, m.ehd_id)
                if key in seen_amb:
                    continue
                seen_amb.add(key)
                issues.append({
                    "issue_type": "ambiguous_link",
                    "severity": "info",
                    "entity_type": etype,
                    "surface_form": m.text,
                    "linked_id": m.ehd_id,
                    "linked_label": label_for(m.ehd_id),
                    "link_method": m.link_method,
                    "link_score": m.link_score,
                    "other_candidates": [
                        {"ehd_id": c, "label": label_for(c)} for c in m.candidates
                    ],
                    "pages": sorted({mm.page for mm in mentions if mm.norm_text == m.norm_text}),
                    "description": (
                        f"'{m.text}' linked to {m.ehd_id} ({label_for(m.ehd_id)}) "
                        f"via {m.link_method} (score {m.link_score}); "
                        f"{len(m.candidates)} other candidate(s)."
                    ),
                })

        # -- variant_merge: distinct surface forms -> same ehd_id (good signal)
        by_id_surfaces: Dict[str, set] = defaultdict(set)
        by_id_pages: Dict[str, set] = defaultdict(set)
        for m in mentions:
            if m.ehd_id and m.text:
                by_id_surfaces[m.ehd_id].add(m.text.replace("\n", " ").strip())
                by_id_pages[m.ehd_id].add(m.page)
        for ehd_id, surfaces in by_id_surfaces.items():
            distinct = {s for s in surfaces if s}
            if len(distinct) > 1:
                issues.append({
                    "issue_type": "variant_merge",
                    "severity": "info",
                    "entity_type": etype,
                    "ehd_id": ehd_id,
                    "label": label_for(ehd_id),
                    "surface_forms": sorted(distinct),
                    "pages": sorted(by_id_pages[ehd_id]),
                    "description": (
                        f"{len(distinct)} surface variants merged onto "
                        f"{ehd_id} ({label_for(ehd_id)}): {sorted(distinct)}."
                    ),
                })

        # -- species_name_conflict (plant only): abbreviated and full forms of
        #    the SAME binomial that the NER normalized differently. Group by
        #    species epithet (last token); only flag within a group whose
        #    genera are abbreviation-compatible, so two genuinely different
        #    species sharing an epithet are not falsely merged.
        if type_to_kind.get(etype) == "plant":
            by_epithet: Dict[str, List[_Mention]] = defaultdict(list)
            for m in mentions:
                ep = _epithet(m.text)
                if ep:
                    by_epithet[ep].append(m)
            for ep, ms in by_epithet.items():
                norms = {(m.normalized_form or "").strip() for m in ms}
                norms = {n for n in norms if n}
                if len(norms) > 1 and _genera_compatible(_genus(m.text) for m in ms):
                    issues.append({
                        "issue_type": "species_name_conflict",
                        "severity": "warning",
                        "entity_type": etype,
                        "epithet": ep,
                        "surface_forms": sorted(
                            {m.text.replace("\n", " ").strip() for m in ms}),
                        "normalized_forms": sorted(norms),
                        "pages": sorted({m.page for m in ms}),
                        "description": (
                            f"Species epithet '{ep}' is normalized "
                            f"inconsistently to {sorted(norms)} across "
                            f"abbreviated/full mentions of the same name."
                        ),
                    })

    # -- summary -----------------------------------------------------------
    sev_counts: Dict[str, int] = defaultdict(int)
    type_counts: Dict[str, int] = defaultdict(int)
    for it in issues:
        sev_counts[it["severity"]] += 1
        type_counts[it["issue_type"]] += 1

    report = {
        "coverage": coverage,
        "issues": issues,
        "summary": {
            "total_issues": len(issues),
            "by_severity": dict(sev_counts),
            "by_issue_type": dict(type_counts),
        },
    }
    return report


def _epithet(surface: str) -> str:
    """Last normalised token of a (binomial) species surface form."""
    toks = [t for t in norm_key(surface).split() if t]
    return toks[-1] if toks else ""


def _genus(surface: str) -> str:
    """First normalised token when the name has >1 token, else '' (omitted)."""
    toks = [t for t in norm_key(surface).split() if t]
    return toks[0] if len(toks) > 1 else ""


def _genera_compatible(genera) -> bool:
    """True if every non-empty genus is a prefix of the longest one.

    Treats "L." -> "Lichen", "Verruc." -> "Verrucaria" and an omitted genus as
    compatible, but two distinct genera ("Achillea" vs "Gnaphalium") as not.
    """
    nonempty = [g for g in genera if g]
    if not nonempty:
        return True
    longest = max(nonempty, key=len)
    return all(longest.startswith(g) for g in nonempty)


# ---------------------------------------------------------------------------
# Convenience: link + check, with optional JSON I/O
# ---------------------------------------------------------------------------

def link_and_check(
    results: List[PageResult],
    register: EntityRegister,
    type_to_kind: Dict[str, str] = DEFAULT_TYPE_TO_KIND,
    *,
    fuzzy: bool = True,
    fuzzy_cutoff: float = 0.9,
) -> Dict[str, Any]:
    """Link all entities in ``results`` (in place) then run the consistency
    check. Returns the consistency report."""
    link_results(results, register, type_to_kind,
                 fuzzy=fuzzy, fuzzy_cutoff=fuzzy_cutoff)
    report = check_entity_consistency(results, register, type_to_kind)
    cov = report["coverage"]
    logger.info(
        "Entity linking coverage: %s",
        " | ".join(f"{t}: {c['linked']}/{c['total']} ({c['pct']}%)"
                   for t, c in cov.items()),
    )
    s = report["summary"]
    logger.info(
        "Entity consistency: %d issue(s) — %s",
        s["total_issues"],
        ", ".join(f"{k}={v}" for k, v in s["by_issue_type"].items()) or "clean",
    )
    return report


def link_and_check_json(
    json_path: str | Path,
    register: EntityRegister,
    out_json: str | Path | None = None,
    report_path: str | Path | None = None,
    type_to_kind: Dict[str, str] = DEFAULT_TYPE_TO_KIND,
    *,
    fuzzy: bool = True,
    fuzzy_cutoff: float = 0.9,
) -> Tuple[List[PageResult], Dict[str, Any]]:
    """Post-process an existing ``digital_edition_complete.json``.

    Loads the combined JSON, links entities, runs the consistency check, and
    (optionally) writes the linked JSON and the report next to it. Returns
    ``(results, report)``. This is the entry point used by the standalone CLI
    so the step can run without re-invoking the Gemini pipeline.
    """
    json_path = Path(json_path)
    with open(json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    results = [PageResult.from_dict(d) for d in data]

    report = link_and_check(results, register, type_to_kind,
                            fuzzy=fuzzy, fuzzy_cutoff=fuzzy_cutoff)

    if out_json:
        out_json = Path(out_json)
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump([r.to_dict() for r in results], fh,
                      ensure_ascii=False, indent=2)
        logger.info("Linked edition JSON written: %s", out_json)
    if report_path:
        report_path = Path(report_path)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        logger.info("Entity consistency report written: %s", report_path)

    return results, report
