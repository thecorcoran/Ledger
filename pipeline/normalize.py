"""Normalization and hashing utilities for Loretta's Ledger."""

import hashlib
import re
from typing import Optional


def clean_text(text: Optional[str]) -> str:
    """Normalizes whitespace and removes control characters."""
    if not text:
        return ""
    # Normalize Windows CRLF to LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Replace multiple spaces while preserving paragraphs
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    # Collapse multiple blank lines to at most one
    cleaned = []
    prev_blank = False
    for line in lines:
        if not line:
            if not prev_blank:
                cleaned.append("")
                prev_blank = True
        else:
            cleaned.append(line)
            prev_blank = False
    return "\n".join(cleaned).strip()


def compute_item_hash(
    jurisdiction: str,
    title: str,
    body_text: Optional[str],
    meeting_date: Optional[str],
    url: str,
) -> str:
    """Computes a deterministic SHA-256 hash for deduplication and change detection."""
    content = "|".join([
        jurisdiction.strip().lower(),
        title.strip(),
        (body_text or "").strip(),
        (meeting_date or "").strip(),
        url.strip(),
    ])
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
