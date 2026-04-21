"""
NER Stage (Step 3) – Humboldt Journal Edition
==============================================
Named Entity Recognition on Humboldt's journal text.

Key challenges:
- Imperfect OCR/transcription with [?] markers and uncertain readings
- Multilingual text (German, French, Latin mixed freely)
- Historical spelling variants (Oestreich, Weißenfels, etc.)
- Scientific terminology: instrument names, publication titles
- Abbreviated names and references
- Entities may span language boundaries
"""

import json
import logging
from typing import List

from google import genai
from google.genai import types

from .models import Entity
from .json_utils import parse_json_robust

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt template – Humboldt-specific NER
# ---------------------------------------------------------------------------

NER_PROMPT_TEMPLATE = """\
Du bist Experte für Named Entity Recognition in Alexander von Humboldts
wissenschaftlichen Journalen. Der Text enthält handschriftliche Transkriptionen
mit möglichen Lesefehlern, unsicheren Stellen [?], und mehrsprachigen Passagen
(Deutsch, Französisch, Latein).

ENTITÄTSKATEGORIEN:
{entity_descriptions}

BESONDERE HINWEISE FÜR HUMBOLDTS TEXTE:

1. PERSONEN: Humboldt erwähnt Wissenschaftler, Instrumentenbauer, Kartographen,
   Geistliche, Fürsten. Oft nur Nachnamen oder mit Titel:
   - "P. Audiffredi" = Pater Audiffredi (Person)
   - "Dollond" = Instrumentenbauer (Person, auch Teil von "Lunette de Dollond")
   - "Fixlmillner" = Astronom (Person)
   - "Cassella" = Astronom in Neapel (Person)
   Achte auf abgekürzte Vornamen und Titel (P., Fr., Hr., Prof.)

2. ORTE: Historische Schreibweisen akzeptieren:
   - "Oedenburg" = Ödenburg/Sopron, "Weißenfels", "Dillingen"
   - Observatorien als Orte: "Kremsmünster", "Bologna"
   - "Salzburg", "Wien" auch wenn in anderem Kontext

3. INSTRUMENTE: Oft mit Herstellernamen kombiniert:
   - "Sextant von Troughton" → Instrument (Sextant) + Person (Troughton)
   - "Cercle de Borda" → Instrument
   - "Lunette de Dollond" → Instrument
   - "Horizont v. Carochez" → Instrument

4. PUBLIKATIONEN: Oft in Latein oder Französisch:
   - "Mercurii Philosophici firmamentum" → Publication
   - "Cellarii Speculum orbis terrarum" → Publication
   - Karten: "Güssfeld'sche Carte des Oestreich" → Publication

5. UNSICHERE LESUNGEN [?]: Wenn ein Wort mit [?] markiert ist, trotzdem als
   Entität erkennen, wenn der Kontext eindeutig ist. Z.B. "Sal[?]burg" ist
   wahrscheinlich "Salzburg" → Location. Gib die Form wie im Text an.

6. MEHRSPRACHIGKEIT: Entitäten können in jeder Sprache erscheinen.
   Französisch: "Collège Romain", "Couvent de la Minerve"
   Latein: "Speculum orbis terrarum"
   Deutsch: "Sternwarte zu Kremsmünster"

TEXT ZUR ANALYSE:
```
{text}
```

Antworte NUR mit einem JSON-Array (kein Markdown, kein Kommentar):
[
    {{
        "text": "exakter Text der Entität wie im Originaltext",
        "entity_type": "Kategorie",
        "context": "kurzer Satz/Phrase in dem die Entität vorkommt",
        "normalized_form": "standardisierte/moderne Form falls abweichend, sonst null",
        "language": "de|fr|la"
    }}
]

Gib ein leeres Array [] zurück, wenn keine Entitäten gefunden werden.
"""


# ---------------------------------------------------------------------------
# Core NER function
# ---------------------------------------------------------------------------

def perform_ner(
    client: genai.Client,
    text: str,
    entity_types: dict[str, str],
    model_id: str,
    thinking_level: str = "medium",
) -> List[Entity]:
    """
    Run NER on Humboldt's transcribed text.

    Uses text matching rather than character offsets for entity placement,
    since LLMs are unreliable at exact character counting.
    """
    if not text.strip():
        return []

    entity_descriptions = "\n".join(
        f"- **{etype}**: {desc}" for etype, desc in entity_types.items()
    )
    prompt = NER_PROMPT_TEMPLATE.format(
        entity_descriptions=entity_descriptions,
        text=text,
    )

    max_attempts = 3
    data = []

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
                    response_mime_type="application/json",
                ),
            )
            data = parse_json_robust(response.text)
            break
        except json.JSONDecodeError as exc:
            logger.error("JSON error in NER (attempt %d/%d): %s",
                         attempt, max_attempts, exc)
        except Exception as exc:
            logger.error("NER error (attempt %d/%d): %s",
                         attempt, max_attempts, exc)

    entities: List[Entity] = []
    valid_types = set(entity_types.keys())
    seen_texts: set[tuple[str, str]] = set()

    for item in data:
        if not isinstance(item, dict):
            continue
        entity_type = item.get("entity_type", "")
        entity_text = str(item.get("text", "")).strip()
        if entity_type not in valid_types:
            logger.debug("Skipping unknown entity type: %s", entity_type)
            continue
        if not entity_text:
            continue

        key = (entity_text, entity_type)
        if key in seen_texts:
            continue
        seen_texts.add(key)

        entities.append(
            Entity(
                text=entity_text,
                entity_type=entity_type,
                start_char=-1,
                end_char=-1,
                context=item.get("context"),
                normalized_form=item.get("normalized_form"),
                language=item.get("language"),
            )
        )

    return entities
