"""
JSON Utilities – Robust JSON parsing for LLM responses.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)
_JSON_DECODER = json.JSONDecoder()


def parse_json_robust(text: str) -> Any:
    """
    Parse JSON from an LLM response, tolerating:
    1. Markdown code fences
    2. Extra trailing data
    3. Leading/trailing explanation text
    """
    text = re.sub(r"^```(?:json)?\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for i, ch in enumerate(text):
        if ch in ('{', '['):
            try:
                result, _ = _JSON_DECODER.raw_decode(text, i)
                return result
            except json.JSONDecodeError:
                continue

    for pattern in [r'\{[\s\S]*\}', r'\[[\s\S]*\]']:
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue

    raise json.JSONDecodeError("No valid JSON found in LLM response", text, 0)
