#!/usr/bin/env python3
"""
CLI: Entity linking + consistency check (Step 3.5) as a post-process.

Resolves the Person / Location / Species entities in an existing
``digital_edition_complete.json`` against the *edition humboldt digital*
authority register (person -> VIAF/GND, place -> GeoNames, plant -> GBIF) and
writes:

  * a linked edition JSON  (entities gain ehd_id / authority_uri / ...)
  * an entity_consistency_report.json (normalization & link conflicts,
    ambiguous links, merged spelling variants, per-type coverage)

This runs fully offline — no Gemini, no network — so it is cheap to re-run on a
finished journal.

Usage:
    python scripts/link_entities.py \
        --json output/digital_edition_complete.json \
        --register /path/to/edition-humboldt-digital \
        --out output/digital_edition_linked.json \
        --report output/entity_consistency_report.json
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.entity_register import EntityRegister
from src.entity_linking import link_and_check_json
from src import config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Link entities to the eHD register.")
    p.add_argument("--json", required=True,
                   help="Path to digital_edition_complete.json")
    p.add_argument("--register", default=config.EHD_REGISTER_DIR,
                   help="Local clone of telota/edition-humboldt-digital "
                        "(dir containing data/index/). "
                        "Defaults to $EHD_REGISTER_DIR.")
    p.add_argument("--cache", default=config.EHD_REGISTER_CACHE,
                   help="Compiled register index cache path "
                        "(default: ehd_register_index.json next to --register).")
    p.add_argument("--rebuild", action="store_true",
                   help="Force rebuild of the register index cache.")
    p.add_argument("--out", default=None,
                   help="Linked JSON output path "
                        "(default: <json> with _linked suffix).")
    p.add_argument("--report", default=None,
                   help="Consistency report path "
                        "(default: entity_consistency_report.json next to --json).")
    p.add_argument("--no-fuzzy", action="store_true",
                   help="Disable fuzzy fallback matching.")
    p.add_argument("--fuzzy-cutoff", type=float,
                   default=config.ENTITY_LINK_FUZZY_CUTOFF,
                   help="Minimum similarity for a fuzzy match (default 0.9).")
    args = p.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"Error: file not found: {json_path}")
        sys.exit(1)
    if not args.register:
        print("Error: --register (or $EHD_REGISTER_DIR) is required.")
        sys.exit(1)

    out_json = Path(args.out) if args.out else \
        json_path.with_name(json_path.stem + "_linked.json")
    report_path = Path(args.report) if args.report else \
        json_path.with_name("entity_consistency_report.json")

    register = EntityRegister.load(args.register, args.cache, rebuild=args.rebuild)

    results, report = link_and_check_json(
        json_path, register,
        out_json=out_json, report_path=report_path,
        fuzzy=not args.no_fuzzy, fuzzy_cutoff=args.fuzzy_cutoff,
    )

    # ----- human-readable summary -----
    cov = report["coverage"]
    print("\n=== Entity linking coverage ===")
    for etype, c in cov.items():
        print(f"  {etype:18} {c['linked']:4}/{c['total']:<4} ({c['pct']}%)")

    s = report["summary"]
    print(f"\n=== Consistency issues: {s['total_issues']} ===")
    for k, v in sorted(s["by_issue_type"].items()):
        print(f"  {k:24} {v}")

    print(f"\nLinked JSON: {out_json}")
    print(f"Report:      {report_path}")


if __name__ == "__main__":
    main()
