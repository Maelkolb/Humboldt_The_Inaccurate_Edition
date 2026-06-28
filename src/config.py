"""
Configuration for the Humboldt Journal Digital Edition pipeline.

Tailored for Alexander von Humboldt's handwritten scientific journal:
- Complex page layouts with marginalia, calculations, sketches
- Multilingual content (German, French, Latin)
- Crossed-out passages, difficult handwriting
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# API / Model
# ---------------------------------------------------------------------------

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

# Default model used for ALL stages unless overridden below.
MODEL_ID: str = os.environ.get("MODEL_ID") or "gemini-3-flash-preview"

# The most capable Flash model — used for the per-region merge resolver and the
# layout pass (both are reasoning/selection tasks). Overridable via env.
MODEL_ID_BEST: str = os.environ.get("MODEL_ID_BEST") or "gemini-3.5-flash"

# Per-stage model overrides (None → the stage default below, ultimately MODEL_ID).
# Each can also be set via an environment variable of the same name.
MODEL_ID_LAYOUT:        str | None = os.environ.get("MODEL_ID_LAYOUT")        or None  # region detection
MODEL_ID_TRANSCRIPTION: str | None = os.environ.get("MODEL_ID_TRANSCRIPTION") or None  # M1/M2/M3 reads
MODEL_ID_MERGE:         str | None = os.environ.get("MODEL_ID_MERGE")         or None  # merge + layout pass
MODEL_ID_NER:           str | None = os.environ.get("MODEL_ID_NER")           or None
MODEL_ID_GEO_VALIDATION: str | None = os.environ.get("MODEL_ID_GEO_VALIDATION") or None

# Smart-stage default (merge resolver + layout pass): the best model.
MODEL_ID_MERGE_DEFAULT: str = MODEL_ID_MERGE or MODEL_ID_BEST

# The M3 (structured whole-page) read deliberately uses a DIFFERENT model from the
# M1 crop / M2 free reads, so the ensemble gets heterogeneous candidates (two model
# families) for the alignment-locked merge to draw on. Defaults to the best model.
MODEL_ID_STRUCTURED: str = os.environ.get("MODEL_ID_STRUCTURED") or MODEL_ID_BEST

THINKING_LEVEL: str = "medium"  # default fallback
THINKING_LEVEL_LAYOUT: str = "high"          # region detection (complex layouts)
THINKING_LEVEL_TRANSCRIPTION: str = "low"    # M1/M2 reads
THINKING_LEVEL_STRUCTURED: str = os.environ.get("THINKING_LEVEL_STRUCTURED") or "medium"  # M3 (diverse)
THINKING_LEVEL_MERGE: str = os.environ.get("THINKING_LEVEL_MERGE") or "medium"  # merge + layout pass

# Sampling temperature. ``None`` = API default. Env-overridable.
def _opt_float(name: str, default):
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default

TEMPERATURE_READ: float = _opt_float("TEMPERATURE_READ", 0.2)            # M1 crop + M2 whole-page
TEMPERATURE_READ_STRUCTURED: float = _opt_float("TEMPERATURE_READ_STRUCTURED", 1.0)  # M3 (diverse)
TEMPERATURE_MERGE: float = _opt_float("TEMPERATURE_MERGE", 0.0)          # merge + layout selection

# Global cap on simultaneous Gemini calls across the whole run (every stage, every
# folio, every worker). The single throttle: raise it on a high API tier, lower it
# if you see 429s. Backoff (src/llm.py) handles any overflow.
LLM_MAX_CONCURRENCY: int = int(os.environ.get("LLM_MAX_CONCURRENCY", "8"))

# ---------------------------------------------------------------------------
# Ensemble transcription (the reading path)
# ---------------------------------------------------------------------------
# Each region gets HETEROGENEOUS candidates — k_crop per-region crop reads (M1)
# plus two whole-page reads (M2 free, M3 structured), keyed by region_index. They
# are merged by ALIGNMENT-LOCKED consensus: tokens all candidates agree on are
# locked in code; only disagreements are resolved by the best model from the crop
# (+page). A final whole-page LAYOUT pass dedups/contamination/bleed.
ENSEMBLE_K_CROP: int = int(os.environ.get("ENSEMBLE_K_CROP", "1"))
MERGE_CROP_MAX_PX: int = int(os.environ.get("MERGE_CROP_MAX_PX", "1600"))  # crop res for the merge
# Attach the whole page to each merge call (alongside the crop) for layout context.
MERGE_WITH_PAGE: bool = os.environ.get("MERGE_WITH_PAGE", "1") not in ("0", "false", "False")
# Whole-page image resolution for the M2/M3 reads, merge page-context, and layout.
PAGE_READ_MAX_PX: int = int(os.environ.get("PAGE_READ_MAX_PX", "2000"))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
IMAGE_FOLDER: Path = Path(os.environ.get("IMAGE_FOLDER", BASE_DIR / "images"))
OUTPUT_FOLDER: Path = Path(os.environ.get("OUTPUT_FOLDER", BASE_DIR / "output"))

# ---------------------------------------------------------------------------
# Entity linking – edition humboldt digital authority register
# ---------------------------------------------------------------------------
# Entity linking is a SEPARATE, OPTIONAL post-processing module that runs AFTER
# the pipeline has finished, over the produced digital_edition_complete.json
# (see scripts/link_entities.py). It does not run as part of process_book().
# These settings only provide defaults for that standalone tool.
#
# EHD_REGISTER_DIR: local clone of telota/edition-humboldt-digital (the
# directory containing data/index/). Used as the default for the linker CLI's
# --register argument.
EHD_REGISTER_DIR: str | None = os.environ.get("EHD_REGISTER_DIR") or None
# Compiled index cache (built on first use, reused afterwards). Defaults to a
# file next to the register repo.
EHD_REGISTER_CACHE: str | None = os.environ.get("EHD_REGISTER_CACHE") or None
# Default fuzzy-match cutoff for the linker (overridable via --fuzzy-cutoff).
ENTITY_LINK_FUZZY_CUTOFF: float = float(os.environ.get("ENTITY_LINK_FUZZY_CUTOFF", "0.9"))

# NER entity_type -> eHD register kind. Only these types are linked.
ENTITY_TYPE_TO_REGISTER_KIND: dict[str, str] = {
    "Person": "person",
    "Location": "place",
    "Species": "plant",
}

# ---------------------------------------------------------------------------
# Region types – custom for Humboldt's journal layout
# ---------------------------------------------------------------------------

REGION_TYPES: list[str] = [
    "entry_heading",        # numbered entry heading (e.g. "N. 50-52", "N. 9-11")
    "main_text",            # primary prose / running journal text
    "marginal_note",        # notes written by Humboldt in the margins of this page
                            # (left, right, top, bottom, or on the opposite folio margin)
    "pasted_slip",          # separate slip of paper physically pasted onto the page
    "calculation",          # astronomical/mathematical computation blocks
    "observation_table",    # structured observational data (angles, times, measurements)
    "sketch",               # pen drawings, landscape profiles, plant/animal diagrams
    "crossed_out",          # large struck-through sections (multiple lines, whole pages)
    "instrument_list",      # lists of scientific instruments (often with prices)
    "page_number",          # folio number (usually top corner)
]

# ---------------------------------------------------------------------------
# Entity types – generic across Humboldt's travel journals. Examples span all
# three corpora: England (1790, e.g. Wiltshire/wool/geology), the American
# equinoctial regions (1799–1804, e.g. Cumaná/Orinoco), and the European trip
# (1797/98: Dresden, Wien, Salzburg). Descriptions are journal-agnostic so the
# same NER config works on any of them.
# ---------------------------------------------------------------------------

ENTITY_TYPES: dict[str, str] = {
    "Person": (
        "Namentlich genannte Person: Wissenschaftler, Astronomen, Instrumentenbauer, "
        "Missionare, Beamte, Reisegefährten, Handwerker, lokale Informanten. "
        "(z.B. 'Bonpland', 'Depons' [Amerika]; 'Bouvard', 'Köhler', 'Niebuhr' [Europa]; "
        "'Anderson', 'Bird' [England]). "
        "Auch mit Titel (Fr., P., Don, Sr., S., Hr.) und abgekürzten Vornamen. "
        "KEINE generischen Bezeichnungen wie 'die Indios', 'die Bauern', 'der Wirt'."
    ),
    "Location": (
        "Konkret benannter Ort: Städte, Dörfer, Häfen, Provinzen, Landschaften, "
        "Gebirge, Flüsse, Missionen, Festungen. "
        "(z.B. 'Cumaná', 'Caracas', 'Orinoco' [Amerika]; 'Dresden', 'Wien', 'Salzburg', "
        "'Königstein', 'Elbe' [Europa]; 'Wiltshire', 'Bristol', 'Matlock' [England]). "
        "KEINE relativen Angaben wie 'im Süden' oder generische Bezeichnungen."
    ),
    "Indigenous_Group": (
        "Indigenes Volk, Ethnie, Sprachgruppe oder Stamm (v.a. in den amerikanischen "
        "Tagebüchern). (z.B. 'Chaymas', 'Caribe', 'Guayqueri', 'Cumanagoto', 'Tamanac', "
        "'Maypure'). Auch 'Indios de...' wenn ethnisch spezifisch. "
        "Tritt in den europäischen/englischen Tagebüchern kaum auf."
    ),
    "Instrument": (
        "Wissenschaftliches Instrument oder Messgerät. "
        "(z.B. 'Sextant', 'Chronometer', 'Barometer', 'Thermometer', 'Cercle de Borda', "
        "'Lunette de Dollond', 'Pistor-Kreis', 'Libelle', 'Magnetnadel'). "
        "Auch mit Herstellernamen (z.B. 'Bird', 'Dollond', 'Pistor')."
    ),
    "Species": (
        "Biologische Art/Gattung (Pflanze, Tier) ODER Mineral/Gestein: Latein oder Volksname. "
        "(z.B. 'Croton', 'Hevea', 'Jaguar', 'Cacao', 'Manatí' [Amerika]; "
        "'Schaf', 'Wolle' [England]; 'Basalt', 'Schiefer', 'Toadstone', 'Röthel' "
        "[Mineralogie/Geologie]). "
        "NICHT allgemeine Begriffe wie 'Baum' oder 'Vogel' ohne Artname."
    ),
    "Publication": (
        "Namentlich genanntes Buch, Karte, Zeitschrift, Traktat oder Atlas. "
        "(z.B. 'Relation historique', 'Flora peruviana' [Amerika]; 'd'Anville' [Karte]; "
        "'report of the Committee of the Highland Society' [England]). "
        "Inkl. Autor + Titel-Kombinationen."
    ),
    "Celestial_Object": (
        "Astronomisches Objekt: Stern, Planet, Sonne, Mond, Sternbild. "
        "(z.B. 'Sonne', 'Mond', 'Jupiter', 'α Orionis', 'Kreuz des Südens'). "
        "Nur im astronomischen/navigatorischen Beobachtungskontext."
    ),
    "Measurement": (
        "Konkrete Messung mit Wert und Einheit: geographische Breite/Länge (Koordinaten), "
        "Temperatur, Luftdruck (Barometerstand), magnetische Deklination/Inklination, "
        "Höhe/Meereshöhe. "
        "(z.B. '10° 27' N', '51° 2' 54'' Breite', '23° Réaumur', '780 Toisen', '338 t.'). "
        "Nur markante, identifizierbare Werte, nicht jede beliebige Zahl."
    ),
}

# ---------------------------------------------------------------------------
# Entity colours – scholarly, muted palette
# ---------------------------------------------------------------------------

ENTITY_COLORS: dict[str, str] = {
    "Person":           "#6a1b9a",
    "Location":         "#1565c0",
    "Indigenous_Group": "#00695c",
    "Instrument":       "#e65100",
    "Species":          "#2e7d32",
    "Publication":      "#4e342e",
    "Celestial_Object": "#283593",
    "Measurement":      "#ad1457",
}

ENTITY_LABELS: dict[str, str] = {
    "Person":           "Persons",
    "Location":         "Locations",
    "Indigenous_Group": "Indigenous Groups",
    "Instrument":       "Instruments",
    "Species":          "Species",
    "Publication":      "Publications",
    "Celestial_Object": "Celestial Objects",
    "Measurement":      "Measurements",
}

# ---------------------------------------------------------------------------
# Region type display config
# ---------------------------------------------------------------------------

REGION_COLORS: dict[str, str] = {
    "entry_heading":     "#1a237e",
    "main_text":         "#37474f",
    "marginal_note":     "#7b1fa2",
    "pasted_slip":       "#f57f17",
    "calculation":       "#00695c",
    "observation_table": "#006064",
    "sketch":            "#4e342e",
    "crossed_out":       "#b71c1c",
    "instrument_list":   "#bf360c",
    "page_number":       "#78909c",
}

REGION_LABELS: dict[str, str] = {
    "entry_heading":     "Entry Heading",
    "main_text":         "Main Text",
    "marginal_note":     "Marginal Note",
    "pasted_slip":       "Pasted Slip",
    "calculation":       "Calculation",
    "observation_table": "Observation Table",
    "sketch":            "Sketch",
    "crossed_out":       "Crossed Out",
    "instrument_list":   "Instrument List",
    "page_number":       "Page No.",
}
