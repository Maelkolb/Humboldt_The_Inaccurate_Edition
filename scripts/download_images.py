#!/usr/bin/env python3
"""
CLI: Download images from an IIIF manifest.

Usage:
    python scripts/download_images.py --manifest <URL> [--start 1] [--end 50] [--out images/]
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.downloader import IIIFDownloader
from src import config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Download IIIF images.")
    parser.add_argument(
        "--manifest", default=config.IIIF_MANIFEST_URL or None,
        help="Full IIIF manifest URL",
    )
    parser.add_argument(
        "--start", type=int, default=config.DOWNLOAD_START_SEQ,
        help="First sequence number to download (1-based, inclusive)",
    )
    parser.add_argument(
        "--end", type=int, default=config.DOWNLOAD_END_SEQ,
        help="Last sequence number to download (1-based, inclusive)",
    )
    parser.add_argument(
        "--out", default=str(config.IMAGE_FOLDER),
        help="Output directory for downloaded images",
    )
    parser.add_argument(
        "--delay", type=float, default=config.DOWNLOAD_DELAY_SECONDS,
        help="Delay in seconds between requests",
    )
    args = parser.parse_args()

    if not args.manifest:
        print("Error: --manifest URL is required (or set IIIF_MANIFEST_URL in config).")
        sys.exit(1)

    downloader = IIIFDownloader(
        book_id="download",
        output_dir=args.out,
        manifest_url=args.manifest,
        delay=args.delay,
    )
    downloaded = downloader.download(start_seq=args.start, end_seq=args.end)
    print(f"\n{len(downloaded)} images saved to {args.out}")


if __name__ == "__main__":
    main()
