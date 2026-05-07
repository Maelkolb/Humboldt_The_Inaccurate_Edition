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

# Per-stage model overrides (optional).
# Set to a Gemini model ID (e.g. "gemini-3.1-pro-preview",
# "gemini-3.1-flash-lite-preview") to use a different model for that stage.
# Leave as None to fall back to MODEL_ID.
# Each can also be set via an environment variable of the same name.
MODEL_ID_LAYOUT:        str | None = os.environ.get("MODEL_ID_LAYOUT")        or None
MODEL_ID_TRANSCRIPTION: str | None = os.environ.get("MODEL_ID_TRANSCRIPTION") or None
MODEL_ID_CONSISTENCY:   str | None = os.environ.get("MODEL_ID_CONSISTENCY")   or None
MODEL_ID_NER:           str | None = os.environ.get("MODEL_ID_NER")           or None

THINKING_LEVEL: str = "medium"  # default fallback
THINKING_LEVEL_LAYOUT: str = "high"  # complex layouts need deeper reasoning
THINKING_LEVEL_TRANSCRIPTION: str = "low"  # transcription is more straightforward

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
IMAGE_FOLDER: Path = Path(os.environ.get("IMAGE_FOLDER", BASE_DIR / "images"))
OUTPUT_FOLDER: Path = Path(os.environ.get("OUTPUT_FOLDER", BASE_DIR / "output"))

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
    "bibliographic_ref",    # citation of a publication, atlas, or other work
    "coordinates",          # geographic coordinate notations
    "instrument_list",      # lists of scientific instruments (often with prices)
    "page_number",          # folio number (usually top corner)
    "catch_phrase",         # catchword at page bottom for continuation
]

# ---------------------------------------------------------------------------
# Entity types – adapted for Humboldt's American travel journals
# Context: Venezuelan and South American field research (1799–1804)
# ---------------------------------------------------------------------------

ENTITY_TYPES: dict[str, str] = {
    "Person": (
        "Namentlich genannte Person: Wissenschaftler, Missionare, Gouverneure, "
        "Conquistadoren, Entdecker, Reisegefährten, lokale Informanten. "
        "(z.B. 'Bonpland', 'Bello', 'Depons', 'Fray', 'Pater'). "
        "Auch mit Titel (Fr., P., Don, Sr.) und abgekürzten Vornamen. "
        "KEINE generischen Bezeichnungen wie 'los Indios' oder 'los Misioneros'."
    ),
    "Location": (
        "Konkret benannter Ort: Städte, Dörfer, Missionen, Häfen, Provinzen, "
        "Landschaften, Küstenabschnitte. Spanische und indigene Ortsnamen. "
        "(z.B. 'Cumaná', 'Caracas', 'Villa de Cura', 'Nueva Barcelona', "
        "'Cerro de Ávila', 'Llanos'). "
        "KEINE relativen Angaben wie 'im Süden' oder generische Bezeichnungen."
    ),
    "Indigenous_Group": (
        "Indigenes Volk, Ethnie, Sprachgruppe oder Stamm in Venezuela/Südamerika. "
        "(z.B. 'Chaymas', 'Caribe', 'Guayqueri', 'Cumanagoto', 'Tamanac', "
        "'Maypure', 'Atures'). "
        "Auch Bezeichnungen wie 'Indios de...' wenn ethnisch spezifisch."
    ),
    "Instrument": (
        "Wissenschaftliches Instrument oder Messgerät. "
        "(z.B. 'Sextant', 'Chronometer', 'Barometer', 'Dipping needle', "
        "'Cercle de Borda', 'Lunette de Dollond', 'Magnetometer'). "
        "Auch mit Herstellernamen kombiniert."
    ),
    "Species": (
        "Biologische Art oder Gattung (Pflanze, Tier, Mineral): Latein oder Volksname. "
        "(z.B. 'Croton', 'Hevea', 'Jaguar', 'Caiman', 'Elektrischer Aal', "
        "'Manatí', 'Cacao', 'Vanilla', 'Cassia', 'Loxia'). "
        "Auch neu beschriebene Arten und lokale Namen mit botanischem Kontext. "
        "NICHT allgemeine Begriffe wie 'Baum', 'Vogel' ohne Artname."
    ),
    "Publication": (
        "Namentlich genanntes Buch, Karte, Zeitschrift, Traktat oder Atlas. "
        "(z.B. 'Reise in die Äquinoktial-Gegenden', 'Relation historique', "
        "'Flora peruviana', 'Humboldt und Bonpland'). "
        "Inkl. Autor + Titel-Kombinationen."
    ),
    "Celestial_Object": (
        "Astronomisches Objekt: Stern, Planet, Sonne, Mond, Sternbild. "
        "(z.B. 'Sonne', 'Mond', 'Jupiter', 'α Orionis', 'Kreuz des Südens'). "
        "Nur im astronomischem/navigatorischen Beobachtungskontext."
    ),
    "Measurement": (
        "Konkrete Messung mit Wert und Einheit: geographische Breite/Länge, "
        "Temperatur, Luftdruck, magnetische Deklination/Inklination, Meereshöhe. "
        "(z.B. '10° 27' N', '23° Réaumur', '780 Toisen', '4° 21' östl.'). "
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
    "bibliographic_ref": "#3e2723",
    "coordinates":       "#0d47a1",
    "instrument_list":   "#bf360c",
    "page_number":       "#78909c",
    "catch_phrase":      "#546e7a",
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
    "bibliographic_ref": "Bibliographic Ref.",
    "coordinates":       "Coordinates",
    "instrument_list":   "Instrument List",
    "page_number":       "Page No.",
    "catch_phrase":      "Catchword",
}
