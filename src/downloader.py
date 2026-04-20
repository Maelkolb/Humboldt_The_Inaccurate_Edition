"""
IIIF Image Downloader
=====================
Downloads page images from an IIIF Presentation API v2 manifest.

Usage (CLI):
    python -m scripts.download_images

Usage (library):
    from src.downloader import IIIFDownloader
    downloader = IIIFDownloader(book_id="bsb11005578", output_dir="images")
    downloader.download(start_seq=15, end_seq=102)
"""

import os
import time
import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class IIIFDownloader:
    """Downloads images from a IIIF Presentation API v2 manifest."""

    BASE_MANIFEST_URL = (
        "https://api.digitale-sammlungen.de/iiif/presentation/v2/{book_id}/manifest"
    )

    def __init__(
        self,
        book_id: str,
        output_dir: str | Path,
        manifest_url: str | None = None,
        delay: float = 0.5,
    ):
        self.book_id = book_id
        self.output_dir = Path(output_dir)
        self.manifest_url = manifest_url or self.BASE_MANIFEST_URL.format(
            book_id=book_id
        )
        self.delay = delay
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def download(self, start_seq: int = 1, end_seq: int | None = None) -> list[Path]:
        """
        Download images from the manifest in the given sequence range.

        Args:
            start_seq: First canvas to download (1-based, inclusive).
            end_seq:   Last canvas to download (1-based, inclusive).
                       Defaults to the last canvas in the manifest.

        Returns:
            List of paths to downloaded files.
        """
        canvases = self._fetch_canvases()
        if not canvases:
            logger.error("No canvases found in manifest.")
            return []

        logger.info("Manifest has %d canvases total.", len(canvases))

        # Convert 1-based inclusive range to 0-based slice
        start_idx = max(start_seq - 1, 0)
        end_idx = end_seq if end_seq is not None else len(canvases)
        selected = canvases[start_idx:end_idx]

        logger.info(
            "Downloading %d images (seq %d–%d)…",
            len(selected), start_seq, end_seq or len(canvases),
        )

        downloaded: list[Path] = []
        for i, canvas in enumerate(selected):
            seq_num = start_seq + i
            path = self._download_canvas(canvas, seq_num)
            if path:
                downloaded.append(path)
            time.sleep(self.delay)

        logger.info("Download complete. %d files saved to %s", len(downloaded), self.output_dir)
        return downloaded

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_canvases(self) -> list[dict]:
        logger.info("Fetching manifest: %s", self.manifest_url)
        try:
            response = requests.get(self.manifest_url, timeout=30)
            response.raise_for_status()
            manifest = response.json()
        except requests.RequestException as exc:
            logger.error("Failed to fetch manifest: %s", exc)
            return []

        return manifest.get("sequences", [{}])[0].get("canvases", [])

    def _download_canvas(self, canvas: dict, seq_num: int) -> Path | None:
        label = canvas.get("label", "unknown")
        images = canvas.get("images", [])
        if not images:
            logger.warning("Skipping seq %d – no image data.", seq_num)
            return None

        resource = images[0].get("resource", {})
        service = resource.get("service", {})
        service_id = service.get("@id") or resource.get("@id")
        if not service_id:
            logger.warning("Skipping seq %d – cannot determine image URL.", seq_num)
            return None

        image_url = f"{service_id}/full/full/0/default.jpg"
        base_name = (
            f"{self.book_id}_seq_{seq_num:03d}_page_{label}.jpg"
            .replace(" ", "_")
            .replace("/", "-")
        )
        dest = self.output_dir / base_name

        logger.info("  Downloading seq %d (label: %s)…", seq_num, label)
        try:
            resp = requests.get(image_url, stream=True, timeout=60)
            if resp.status_code == 200:
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        fh.write(chunk)
                return dest
            else:
                logger.error("  HTTP %d for seq %d.", resp.status_code, seq_num)
        except requests.RequestException as exc:
            logger.error("  Error for seq %d: %s", seq_num, exc)

        return None
