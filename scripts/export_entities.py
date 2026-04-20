#!/usr/bin/env python3
"""
CLI: Export all entities from a completed run to CSV for analysis.

Usage:
    python scripts/export_entities.py --json output/digital_edition_complete.json
"""

import argparse
import csv
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export entities to CSV.")
    parser.add_argument(
        "--json", required=True,
        help="Path to digital_edition_complete.json",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output CSV path. Defaults to same directory as JSON.",
    )
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"Error: File not found: {json_path}")
        sys.exit(1)

    csv_path = Path(args.out) if args.out else json_path.with_suffix(".csv")

    with open(json_path, encoding="utf-8") as fh:
        pages = json.load(fh)

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["page", "entity_type", "text", "start_char", "end_char", "context"])
        for page in pages:
            page_num = page.get("page_number", "")
            for ent in page.get("entities", []):
                writer.writerow([
                    page_num,
                    ent.get("entity_type", ""),
                    ent.get("text", ""),
                    ent.get("start_char", ""),
                    ent.get("end_char", ""),
                    ent.get("context", ""),
                ])

    print(f"Entities exported to {csv_path}")

    # Frequency summary
    freq: dict = {}
    for page in pages:
        for ent in page.get("entities", []):
            key = (ent.get("entity_type", ""), ent.get("text", ""))
            freq[key] = freq.get(key, 0) + 1

    print(f"\nTop 20 most frequent entities:")
    for (etype, text), count in sorted(freq.items(), key=lambda x: -x[1])[:20]:
        print(f"  [{etype}] '{text}': {count}x")


if __name__ == "__main__":
    main()
