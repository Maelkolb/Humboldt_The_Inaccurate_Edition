"""Shared logging configuration for the CLI entry points."""

from __future__ import annotations

import logging

# Third-party loggers that emit one line per HTTP call; quieted to keep run logs
# readable (the SDK logs every generate_content request at INFO).
_NOISY = ("google_genai", "google.genai", "httpx", "httpcore", "urllib3")


def configure_logging(level: str | int = "INFO") -> None:
    """Configure root logging for a CLI run and silence noisy HTTP loggers."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
