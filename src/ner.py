"""Named-entity recognition over the transcribed journal text (multilingual
de/fr/la, historical spelling, ``[?]`` markers, scientific terms, abbreviations)."""

from __future__ import annotations

import logging
from typing import List

from google import genai

from .llm import generate_json
from .models import Entity

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
   Achte auf abgekürzte Vornamen und Titel (P., Fr., Hr., Prof.)

2. ORTE: Historische Schreibweisen akzeptieren:
   - "Oedenburg" = Ödenburg/Sopron, "Weißenfels", "Dillingen"
   - Observatorien als Orte: "Kremsmünster", "Bologna"

3. INSTRUMENTE: Oft mit Herstellernamen kombiniert:
   - "Sextant von Troughton" → Instrument (Sextant) + Person (Troughton)
   - "Cercle de Borda", "Lunette de Dollond" → Instrument

4. PUBLIKATIONEN: Oft in Latein oder Französisch:
   - "Mercurii Philosophici firmamentum" → Publication
   - Karten: "Güssfeld'sche Carte des Oestreich" → Publication

5. UNSICHERE LESUNGEN [?]: Wenn ein Wort mit [?] markiert ist, trotzdem als
   Entität erkennen, wenn der Kontext eindeutig ist. Z.B. "Sal[?]burg" ist
   wahrscheinlich "Salzburg" → Location. Gib die Form wie im Text an.

6. MEHRSPRACHIGKEIT: Entitäten können in jeder Sprache erscheinen.

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
    """Run NER on Humboldt's transcribed text.

    Entity placement uses text matching rather than character offsets, since
    LLMs are unreliable at exact character counting.
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

    data = generate_json(
        client, model_id, prompt,
        thinking_level=thinking_level,
        default=[],
        stage="ner",
    )
    if not isinstance(data, list):
        data = []

    entities: List[Entity] = []
    valid_types = set(entity_types.keys())
    seen: set[tuple[str, str]] = set()

    for item in data:
        if not isinstance(item, dict):
            continue
        entity_type = item.get("entity_type", "")
        entity_text = str(item.get("text", "")).strip()
        if entity_type not in valid_types or not entity_text:
            continue
        key = (entity_text, entity_type)
        if key in seen:
            continue
        seen.add(key)
        entities.append(Entity(
            text=entity_text,
            entity_type=entity_type,
            start_char=-1,
            end_char=-1,
            context=item.get("context"),
            normalized_form=item.get("normalized_form"),
            language=item.get("language"),
        ))

    return entities
