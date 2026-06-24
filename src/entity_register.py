"""
Authority Register (edition humboldt digital) – Humboldt Journal Edition
========================================================================
Loads the curated person / place / plant registers published by the
*edition humboldt digital* (eHD, BBAW, CC BY-SA 4.0) into fast in-memory
lookup indices, so the pipeline can resolve the entities its NER stage emits
to stable authority records:

  * Person  -> eHD person id + VIAF URI
  * Location -> eHD place id  + GeoNames URI (+ historical alt names)
  * Species -> eHD plant id   + GBIF taxon URI (plants only; minerals stay
               unlinked, which is the correct behaviour)

The eHD data layout (a local clone of telota/edition-humboldt-digital):

    data/index/person/H*.xml     one <person> per file, VIAF in idno[@type=uri]
    data/index/place/H*.xml      one <place>  per file, GeoNames in idno
    data/index/plants/[A-Z].xml  bulk lists of <item> (reg/alt labels + GBIF)

Parsing ~14k tiny XML files is a one-off cost, so :meth:`EntityRegister.load`
caches a compact JSON index next to the repo and reuses it on later runs.

This module is pure-stdlib + lxml (already a dependency); it performs no
network calls and no LLM calls.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from lxml import etree

logger = logging.getLogger(__name__)

_TEI = "{http://www.tei-c.org/ns/1.0}"
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"

# Bump when the index schema below changes so stale caches are rebuilt.
_INDEX_VERSION = 2


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_ABBREV_DOTS = re.compile(r"\.")
_WS = re.compile(r"\s+")
# soft hyphenation across line breaks, e.g. "Trow- bridge" / "Croton\ntinctor."
_SOFT_HYPHEN = re.compile(r"-\s+")


def _strip_diacritics(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def norm_key(s: Optional[str], *, fold_diacritics: bool = True) -> str:
    """Normalise a name for keying/lookup.

    Collapses whitespace and line-broken hyphenation, drops abbreviation dots,
    casefolds, and (by default) strips diacritics. Returns "" for falsy input.
    """
    if not s:
        return ""
    s = s.replace("\n", " ")
    s = _SOFT_HYPHEN.sub("", s)          # join "Trow- bridge" -> "Trowbridge"
    s = _ABBREV_DOTS.sub("", s)          # "D. fullon." -> "D fullon"
    if fold_diacritics:
        s = _strip_diacritics(s)
    s = s.casefold()
    s = _WS.sub(" ", s).strip()
    return s


def _last_token(s: str) -> str:
    """Heuristic surname: last whitespace token of a normalised name."""
    toks = norm_key(s).split()
    return toks[-1] if toks else ""


# ---------------------------------------------------------------------------
# Records & match results
# ---------------------------------------------------------------------------

@dataclass
class RegisterEntry:
    """One authority record from the eHD register."""
    ehd_id: str                       # e.g. "H0000010"
    kind: str                         # "person" | "place" | "plant"
    label: str                        # canonical (reg) label
    authority_uri: Optional[str] = None   # VIAF / GeoNames / GBIF URI
    authority_label: Optional[str] = None # e.g. GBIF "Abies Mill."
    alt_labels: List[str] = field(default_factory=list)
    surname: Optional[str] = None     # persons only
    birth: Optional[str] = None
    death: Optional[str] = None
    note: Optional[str] = None

    @property
    def ehd_url(self) -> Optional[str]:
        """Resolvable eHD page URL, or None for plants.

        Persons and places have stable eHD ids (``H#######``) with their own
        edition pages. Plant taxa live only in the bulk A–Z register and carry
        a synthetic id here, so there is no per-taxon eHD page — their real
        resolvable authority is the GBIF URI in ``authority_uri``.
        """
        if re.fullmatch(r"H\d+", self.ehd_id or ""):
            return f"https://edition-humboldt.de/v11/{self.ehd_id}"
        return None


@dataclass
class LinkMatch:
    """Result of matching one NER entity against the register."""
    entry: RegisterEntry
    method: str       # "exact" | "normalized_form" | "alt" | "surname" | "fuzzy"
    score: float      # 0..1
    matched_on: str   # the key string that matched
    ambiguous: bool = False
    candidates: List[str] = field(default_factory=list)  # other ehd_ids at same rank


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

class EntityRegister:
    """In-memory, indexed view over the eHD authority registers."""

    def __init__(self) -> None:
        self.entries: Dict[str, RegisterEntry] = {}          # ehd_id -> entry
        # name-key -> list of ehd_ids (lists handle homonyms)
        self._person_name: Dict[str, List[str]] = {}
        self._person_surname: Dict[str, List[str]] = {}
        self._place_name: Dict[str, List[str]] = {}
        self._plant_name: Dict[str, List[str]] = {}

    # -- size helpers -------------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    def counts(self) -> Dict[str, int]:
        c = {"person": 0, "place": 0, "plant": 0}
        for e in self.entries.values():
            c[e.kind] = c.get(e.kind, 0) + 1
        return c

    # -- index maintenance --------------------------------------------------

    def _add(self, entry: RegisterEntry) -> None:
        self.entries[entry.ehd_id] = entry
        if entry.kind == "person":
            for nm in (entry.label, *entry.alt_labels):
                k = norm_key(nm)
                if k:
                    self._person_name.setdefault(k, []).append(entry.ehd_id)
            if entry.surname:
                self._person_surname.setdefault(norm_key(entry.surname), []).append(entry.ehd_id)
        elif entry.kind == "place":
            for nm in (entry.label, *entry.alt_labels):
                k = norm_key(nm)
                if k:
                    self._place_name.setdefault(k, []).append(entry.ehd_id)
        elif entry.kind == "plant":
            for nm in (entry.label, *entry.alt_labels):
                k = norm_key(nm)
                if k:
                    self._plant_name.setdefault(k, []).append(entry.ehd_id)

    def _dedup_indices(self) -> None:
        for idx in (self._person_name, self._person_surname,
                    self._place_name, self._plant_name):
            for k, ids in idx.items():
                seen, uniq = set(), []
                for i in ids:
                    if i not in seen:
                        seen.add(i)
                        uniq.append(i)
                idx[k] = uniq

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(
        cls,
        repo_dir: str | Path,
        cache_path: str | Path | None = None,
        *,
        rebuild: bool = False,
    ) -> "EntityRegister":
        """Load the register, using a JSON cache when available.

        ``repo_dir`` is a local clone of telota/edition-humboldt-digital (the
        directory that contains ``data/index/``). If ``cache_path`` is given
        (or defaulted), a compiled index is written there and reused.
        """
        repo_dir = Path(repo_dir)
        index_dir = _resolve_index_dir(repo_dir)
        if cache_path is None:
            cache_path = repo_dir / "ehd_register_index.json"
        cache_path = Path(cache_path)

        if cache_path.exists() and not rebuild:
            try:
                reg = cls._from_cache(cache_path)
                logger.info("Loaded eHD register from cache %s (%s).",
                            cache_path.name, reg.counts())
                return reg
            except Exception as exc:  # corrupt/stale cache -> rebuild
                logger.warning("Register cache unusable (%s); rebuilding.", exc)

        reg = cls._from_repo(index_dir)
        try:
            reg._to_cache(cache_path)
            logger.info("Wrote eHD register cache: %s", cache_path)
        except Exception as exc:
            logger.warning("Could not write register cache (%s).", exc)
        return reg

    @classmethod
    def _from_repo(cls, index_dir: Path) -> "EntityRegister":
        reg = cls()
        t0 = time.time()
        n_person = reg._load_persons(index_dir / "person")
        n_place = reg._load_places(index_dir / "place")
        n_plant = reg._load_plants(index_dir / "plants")
        reg._dedup_indices()
        logger.info(
            "Built eHD register from %s in %.1fs: %d persons, %d places, %d plants.",
            index_dir, time.time() - t0, n_person, n_place, n_plant,
        )
        return reg

    def _load_persons(self, folder: Path) -> int:
        if not folder.is_dir():
            logger.warning("No person register at %s", folder)
            return 0
        n = 0
        for fp in folder.glob("*.xml"):
            try:
                root = etree.parse(str(fp)).getroot()
            except etree.XMLSyntaxError:
                continue
            for person in root.iter(f"{_TEI}person"):
                ehd_id = person.get(_XML_ID) or fp.stem
                surname = (person.findtext(f".//{_TEI}surname") or "").strip()
                forename = (person.findtext(f".//{_TEI}forename") or "").strip()
                reg_name = person.find(f'.//{_TEI}persName[@type="reg"]')
                label = _person_label(reg_name, surname, forename)
                if not label:
                    continue
                alt = [
                    _persname_text(pn)
                    for pn in person.findall(f".//{_TEI}persName")
                    if pn.get("type") != "reg"
                ]
                alt = [a for a in alt if a]
                uri = _first_idno_uri(person)
                self._add(RegisterEntry(
                    ehd_id=ehd_id, kind="person", label=label,
                    authority_uri=uri,
                    alt_labels=alt,
                    surname=surname or _last_token(label),
                    birth=(person.findtext(f"{_TEI}birth") or "").strip() or None,
                    death=(person.findtext(f"{_TEI}death") or "").strip() or None,
                    note=_clean(person.findtext(f"{_TEI}note")),
                ))
                n += 1
        return n

    def _load_places(self, folder: Path) -> int:
        if not folder.is_dir():
            logger.warning("No place register at %s", folder)
            return 0
        n = 0
        for fp in folder.glob("*.xml"):
            try:
                root = etree.parse(str(fp)).getroot()
            except etree.XMLSyntaxError:
                continue
            for place in root.iter(f"{_TEI}place"):
                ehd_id = place.get(_XML_ID) or fp.stem
                reg = place.find(f'{_TEI}placeName[@type="reg"]')
                label = (reg.text or "").strip() if reg is not None else ""
                if not label:
                    continue
                alt = [
                    (a.text or "").strip()
                    for a in place.findall(f'{_TEI}placeName[@type="alt"]')
                ]
                alt = [a for a in alt if a]
                self._add(RegisterEntry(
                    ehd_id=ehd_id, kind="place", label=label,
                    authority_uri=_first_idno_uri(place),
                    alt_labels=alt,
                    note=_clean(place.findtext(f"{_TEI}note")),
                ))
                n += 1
        return n

    def _load_plants(self, folder: Path) -> int:
        if not folder.is_dir():
            logger.warning("No plant register at %s", folder)
            return 0
        n = 0
        for fp in sorted(folder.glob("*.xml")):
            try:
                root = etree.parse(str(fp)).getroot()
            except etree.XMLSyntaxError:
                continue
            for item in root.iter(f"{_TEI}item"):
                reg = item.find(f'{_TEI}label[@type="reg"]')
                label = (reg.text or "").strip() if reg is not None else ""
                if not label:
                    continue
                alt = [
                    (a.text or "").strip()
                    for a in item.findall(f'{_TEI}label[@type="alt"]')
                ]
                alt = [a for a in alt if a]
                gbif_uri, gbif_name = _plant_gbif(item)
                # Plant items carry no xml:id in the bulk lists; synthesise a
                # stable id from the GBIF id when present, else from the label.
                ehd_id = (
                    f"plant:gbif:{gbif_uri.rsplit('/', 1)[-1]}" if gbif_uri
                    else f"plant:{norm_key(label).replace(' ', '_')}"
                )
                # Skip duplicate ids (same taxon listed under variants); keep first.
                if ehd_id in self.entries:
                    existing = self.entries[ehd_id]
                    for a in [label, *alt]:
                        if a and a not in existing.alt_labels and a != existing.label:
                            existing.alt_labels.append(a)
                            k = norm_key(a)
                            if k:
                                self._plant_name.setdefault(k, []).append(ehd_id)
                    continue
                self._add(RegisterEntry(
                    ehd_id=ehd_id, kind="plant", label=label,
                    authority_uri=gbif_uri, authority_label=gbif_name,
                    alt_labels=alt,
                ))
                n += 1
        return n

    # -- cache (de)serialisation -------------------------------------------

    def _to_cache(self, path: Path) -> None:
        payload = {
            "version": _INDEX_VERSION,
            "entries": [asdict(e) for e in self.entries.values()],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        tmp.replace(path)

    @classmethod
    def _from_cache(cls, path: Path) -> "EntityRegister":
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("version") != _INDEX_VERSION:
            raise ValueError(f"index version {payload.get('version')} != {_INDEX_VERSION}")
        reg = cls()
        for d in payload["entries"]:
            reg._add(RegisterEntry(**d))
        reg._dedup_indices()
        return reg

    # -- lookup -------------------------------------------------------------

    def lookup(
        self,
        kind: str,
        text: str,
        normalized_form: Optional[str] = None,
        *,
        fuzzy: bool = True,
        fuzzy_cutoff: float = 0.9,
    ) -> Optional[LinkMatch]:
        """Resolve one entity of ``kind`` ("person"/"place"/"plant").

        Tries, in order: the NER ``normalized_form`` (already standardised),
        then the raw ``text``, then kind-specific fallbacks (place/plant alt
        names; person surname), then optional fuzzy matching. Returns the best
        :class:`LinkMatch`, or ``None`` when nothing clears the bar.
        """
        if kind == "person":
            return self._lookup_person(text, normalized_form, fuzzy, fuzzy_cutoff)
        if kind == "place":
            return self._lookup_named(
                self._place_name, text, normalized_form, fuzzy, fuzzy_cutoff)
        if kind == "plant":
            return self._lookup_named(
                self._plant_name, text, normalized_form, fuzzy, fuzzy_cutoff)
        return None

    def _resolve_ids(self, ids: List[str], method: str, key: str) -> LinkMatch:
        ids = list(dict.fromkeys(ids))
        entry = self.entries[ids[0]]
        return LinkMatch(
            entry=entry,
            method=method,
            score=1.0 if not method.startswith("fuzzy") else float(method.split(":")[1]),
            matched_on=key,
            ambiguous=len(ids) > 1,
            candidates=ids[1:] if len(ids) > 1 else [],
        )

    def _lookup_named(
        self,
        index: Dict[str, List[str]],
        text: str,
        normalized_form: Optional[str],
        fuzzy: bool,
        cutoff: float,
    ) -> Optional[LinkMatch]:
        for cand, method in ((normalized_form, "normalized_form"), (text, "exact")):
            k = norm_key(cand)
            if k and k in index:
                return self._resolve_ids(index[k], method, k)
        if fuzzy:
            return _fuzzy_lookup(index, self.entries, [normalized_form, text], cutoff)
        return None

    def _lookup_person(
        self,
        text: str,
        normalized_form: Optional[str],
        fuzzy: bool,
        cutoff: float,
    ) -> Optional[LinkMatch]:
        # 1) full-name match (normalized_form preferred, then raw text)
        for cand, method in ((normalized_form, "normalized_form"), (text, "exact")):
            k = norm_key(cand)
            if k and k in self._person_name:
                return self._resolve_ids(self._person_name[k], method, k)
        # 2) surname match (from normalized_form first, then raw text).
        #    When several persons share the surname, rank them by how well
        #    their full label matches the (normalized) mention, so e.g.
        #    "Johan Christian Fabricius" picks "Johann Christian Fabricius"
        #    rather than an unrelated "David Fabricius".
        for cand in (normalized_form, text):
            sk = _last_token(cand or "")
            if sk and sk in self._person_surname:
                ids = list(dict.fromkeys(self._person_surname[sk]))
                full = norm_key(cand)
                ranked = sorted(
                    ids,
                    key=lambda i: SequenceMatcher(None, full, norm_key(self.entries[i].label)).ratio(),
                    reverse=True,
                )
                top = ranked[0]
                top_score = SequenceMatcher(
                    None, full, norm_key(self.entries[top].label)).ratio()
                runner = (
                    SequenceMatcher(None, full, norm_key(self.entries[ranked[1]].label)).ratio()
                    if len(ranked) > 1 else 0.0
                )
                # ambiguous only when the best two are genuinely close
                ambiguous = len(ranked) > 1 and (top_score - runner) < 0.10
                m = LinkMatch(
                    entry=self.entries[top],
                    method="surname",
                    score=round(0.6 if ambiguous else max(0.7, min(0.9, top_score)), 3),
                    matched_on=sk,
                    ambiguous=ambiguous,
                    candidates=[i for i in ranked[1:] if ambiguous],
                )
                return m
        # 3) fuzzy over full-name index
        if fuzzy:
            return _fuzzy_lookup(
                self._person_name, self.entries, [normalized_form, text], cutoff)
        return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _resolve_index_dir(repo_dir: Path) -> Path:
    """Find the data/index directory inside (or below) ``repo_dir``."""
    candidates = [
        repo_dir / "data" / "index",
        repo_dir / "index",
        repo_dir,
    ]
    for c in candidates:
        if (c / "person").is_dir() or (c / "plants").is_dir():
            return c
    # one level down (e.g. an unzipped "...-main" wrapper dir)
    for sub in repo_dir.glob("*"):
        if sub.is_dir() and (sub / "data" / "index" / "person").is_dir():
            return sub / "data" / "index"
    return repo_dir / "data" / "index"


def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = _WS.sub(" ", s).strip()
    return s or None


def _first_idno_uri(el) -> Optional[str]:
    idno = el.find(f'{_TEI}idno[@type="uri"]')
    if idno is None:
        idno = el.find(f'.//{_TEI}idno[@type="uri"]')
    return (idno.text or "").strip() if idno is not None and idno.text else None


def _persname_text(pn) -> str:
    sur = (pn.findtext(f"{_TEI}surname") or "").strip()
    fore = (pn.findtext(f"{_TEI}forename") or "").strip()
    if sur or fore:
        return f"{fore} {sur}".strip()
    return (pn.text or "").strip()


def _person_label(reg_name, surname: str, forename: str) -> str:
    if reg_name is not None:
        txt = _persname_text(reg_name)
        if txt:
            return txt
    return f"{forename} {surname}".strip()


def _plant_gbif(item) -> Tuple[Optional[str], Optional[str]]:
    for lg in item.findall(f"{_TEI}linkGrp"):
        desc = lg.findtext(f"{_TEI}desc") or ""
        if "GBIF" in desc:
            ptr = lg.find(f"{_TEI}ptr")
            if ptr is not None and ptr.get("target"):
                return ptr.get("target").strip(), (ptr.get("n") or "").strip() or None
    return None, None


def _fuzzy_lookup(
    index: Dict[str, List[str]],
    entries: Dict[str, RegisterEntry],
    candidates: List[Optional[str]],
    cutoff: float,
) -> Optional[LinkMatch]:
    """Best fuzzy match of any candidate string against ``index`` keys."""
    best_key, best_score = None, cutoff
    keys = index.keys()
    for cand in candidates:
        k = norm_key(cand)
        if not k:
            continue
        # cheap length gate before the O(n) ratio scan
        for key in keys:
            if abs(len(key) - len(k)) > 4:
                continue
            score = SequenceMatcher(None, k, key).ratio()
            if score > best_score:
                best_score, best_key = score, key
    if best_key is None:
        return None
    m = LinkMatch(
        entry=entries[index[best_key][0]],
        method=f"fuzzy:{best_score:.2f}",
        score=round(best_score, 3),
        matched_on=best_key,
        ambiguous=len(index[best_key]) > 1,
        candidates=index[best_key][1:],
    )
    return m
