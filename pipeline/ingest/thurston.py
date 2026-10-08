"""Ingester for Thurston County Board of County Commissioners and Planning Commission."""

import hashlib
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pipeline.normalize import clean_text, compute_item_hash

logger = logging.getLogger(__name__)

THURSTON_BASE_URL = "https://www.thurstoncountywa.gov"
THURSTON_MEETINGS_URL = f"{THURSTON_BASE_URL}/meetings-and-agendas"
THURSTON_PC_URL = (
    f"{THURSTON_BASE_URL}/departments/community-planning-and-economic-development/"
    "community-planning/planning-commission"
)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "thurston"


class ThurstonIngester:
    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _http_get_html(self, url: str, cache_file: Optional[Path] = None, use_cache: bool = True) -> str:
        if use_cache and cache_file and cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning("Failed to read cache file %s: %s", cache_file, e)

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "LorettaLedger/1.0 (local policy researcher; https://github.com/thecorcoran/Ledger)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")

        if cache_file:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                logger.warning("Failed to write cache file %s: %s", cache_file, e)

        return content

    @staticmethod
    def parse_date(date_raw: str) -> Optional[str]:
        """Parses various date strings found on Thurston County web pages into YYYY-MM-DD."""
        date_clean = re.sub(r"\s+", " ", date_raw).strip()
        # Format: "Tuesday, October 20, 2026"
        for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(date_clean, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        # Try regex search: "October 20, 2026"
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", date_clean)
        if m:
            month_str, day_str, year_str = m.groups()
            try:
                dt = datetime.strptime(f"{month_str} {day_str}, {year_str}", "%B %d, %Y")
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        return None

    def parse_meetings_table(self, html_text: str) -> List[Dict[str, Any]]:
        """Parses the meetings and agendas table from the BoCC meetings page."""
        table_match = re.search(r"<table.*?>(.*?)</table>", html_text, re.DOTALL)
        if not table_match:
            return []

        rows = re.findall(r"<tr.*?>(.*?)</tr>", table_match.group(1), re.DOTALL)
        items = []

        for row in rows[1:]:  # Skip header row
            cells = re.findall(r"<t[dh].*?>(.*?)</t[dh]>", row, re.DOTALL)
            if len(cells) < 4:
                continue

            date_raw = re.sub(r"<.*?>", "", cells[0]).strip()
            time_raw = re.sub(r"<.*?>", "", cells[1]).strip()
            loc_cell = cells[2]
            title_raw = re.sub(r"<.*?>", "", cells[3]).strip()
            title_clean = re.sub(r"\s+", " ", title_raw).strip()

            # Skip cancellations and empty titles
            if not title_clean or title_clean.lower().startswith("canceled:") or title_clean.lower().startswith("cancelled:"):
                continue

            # Skip orientation / purely internal notices or generic meeting container rows
            if "new employee orientation" in title_clean.lower():
                continue
            if title_clean.lower() in ("board of county commissioners business meeting &amp; public hearing(s)", "board of county commissioners business meeting & public hearing(s)", "board work session", "agenda review"):
                continue

            meeting_date = self.parse_date(date_raw)
            if not meeting_date:
                continue

            agenda_cell = cells[4] if len(cells) > 4 else ""
            video_cell = cells[5] if len(cells) > 5 else ""

            # Extract links
            loc_clean = re.sub(r"<.*?>", "", loc_cell).strip()
            zoom_links = re.findall(r'href=[\"\'](https?://[^\s\"\']*zoom[^\s\"\']*)[\"\']', loc_cell)
            doc_links = re.findall(r'href=[\"\'](/media/\d+|https?://[^\s\"\']+)[\"\']', agenda_cell)
            vid_links = re.findall(r'href=[\"\'](https?://[^\s\"\']+)[\"\']', video_cell)

            # Build body text
            body_parts = []
            body_parts.append(f"Meeting Time: {time_raw}")
            body_parts.append(f"Location: {loc_clean}")

            if zoom_links:
                body_parts.append(f"Zoom Virtual Attendance: {zoom_links[0]}")

            if vid_links:
                body_parts.append(f"Video / Livestream: {vid_links[0]}")

            if doc_links:
                doc_lines = []
                for d in doc_links:
                    full_link = d if d.startswith("http") else f"{THURSTON_BASE_URL}{d}"
                    doc_lines.append(f"- [Agenda Packet / Supporting Documents]({full_link})")
                body_parts.append("Documents & Packets:\n" + "\n".join(doc_lines))

            body_parts.append(
                "Public Comment: Written comments may be submitted up to 2 hours prior to meeting start "
                "via email to the Clerk of the Board (https://www.thurstoncountywa.gov/bocc/contact)."
            )

            body_text = clean_text("\n\n".join(body_parts))

            # Deterministic ID
            slug_hash = hashlib.sha256(f"{meeting_date}|{title_clean}".encode("utf-8")).hexdigest()[:10]
            item_id = f"thurston-bocc-{meeting_date}-{slug_hash}"

            item_url = THURSTON_MEETINGS_URL
            if doc_links:
                first_doc = doc_links[0]
                item_url = first_doc if first_doc.startswith("http") else f"{THURSTON_BASE_URL}{first_doc}"

            item_hash = compute_item_hash(
                jurisdiction="thurston",
                title=title_clean,
                body_text=body_text,
                meeting_date=meeting_date,
                url=item_url,
            )

            items.append({
                "id": item_id,
                "source_id": "thurston-commissioners",
                "jurisdiction": "thurston",
                "title": title_clean,
                "url": item_url,
                "published_at": meeting_date,
                "body_text": body_text,
                "meeting_date": meeting_date,
                "comment_deadline": None,
                "status": "active",
                "hash": item_hash,
            })

        return items

    def parse_planning_commission_page(self, html_text: str, limit_dates: int = 5) -> List[Dict[str, Any]]:
        """Parses recent Planning Commission agendas, draft code amendments, and memos."""
        # Find date headings
        pattern = re.compile(
            r"(?:<h[234][^>]*>|\b)((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+202\d)(?:</h[234]>|)",
            re.IGNORECASE,
        )
        splits = list(pattern.finditer(html_text))
        if not splits:
            return []

        items = []
        for i, match in enumerate(splits[:limit_dates]):
            date_raw = match.group(1)
            meeting_date = self.parse_date(date_raw)
            if not meeting_date:
                continue

            start_idx = match.end()
            end_idx = splits[i + 1].start() if i + 1 < len(splits) else len(html_text)
            section_html = html_text[start_idx:end_idx]

            # Find media links
            doc_matches = re.findall(
                r'<a\s+[^>]*href=[\"\'](/media/(\d+))[\"\'][^>]*>(.*?)</a>',
                section_html,
                re.DOTALL,
            )

            for rel_url, media_id, anchor_text in doc_matches:
                clean_title = re.sub(r"<.*?>", "", anchor_text).strip()
                clean_title = re.sub(r"\s+", " ", clean_title)

                if not clean_title or clean_title.lower() in ("agenda", "minutes", "public comment"):
                    # Prefix with date/context if just "Agenda"
                    clean_title = f"Planning Commission: {clean_title} ({meeting_date})"

                full_url = f"{THURSTON_BASE_URL}{rel_url}"
                body_parts = [
                    f"Body: Thurston County Planning Commission",
                    f"Meeting Date: {meeting_date}",
                    f"Document URL: {full_url}",
                    "Scope: Land use planning, comprehensive plan amendments, development codes, critical areas.",
                ]
                body_text = clean_text("\n\n".join(body_parts))

                item_id = f"thurston-pc-{meeting_date}-media-{media_id}"
                item_hash = compute_item_hash(
                    jurisdiction="thurston",
                    title=clean_title,
                    body_text=body_text,
                    meeting_date=meeting_date,
                    url=full_url,
                )

                items.append({
                    "id": item_id,
                    "source_id": "thurston-planning-commission",
                    "jurisdiction": "thurston",
                    "title": clean_title,
                    "url": full_url,
                    "published_at": meeting_date,
                    "body_text": body_text,
                    "meeting_date": meeting_date,
                    "comment_deadline": None,
                    "status": "active",
                    "hash": item_hash,
                })

        return items

    def ingest(
        self,
        conn,
        use_cache: bool = True,
        include_planning_commission: bool = True,
    ) -> Dict[str, int]:
        """Ingests Thurston County Board of Commissioners and Planning Commission items."""
        counts = {"events_processed": 0, "items_created": 0, "items_updated": 0, "items_skipped": 0}

        # 1. BoCC & Board of Health meetings
        cache_bocc = self.cache_dir / "meetings_page.html"
        bocc_html = self._http_get_html(THURSTON_MEETINGS_URL, cache_file=cache_bocc, use_cache=use_cache)
        bocc_items = self.parse_meetings_table(bocc_html)
        counts["events_processed"] += len(bocc_items)

        all_items = list(bocc_items)

        # 2. Planning Commission
        if include_planning_commission:
            cache_pc = self.cache_dir / "planning_commission.html"
            pc_html = self._http_get_html(THURSTON_PC_URL, cache_file=cache_pc, use_cache=use_cache)
            pc_items = self.parse_planning_commission_page(pc_html, limit_dates=6)
            all_items.extend(pc_items)

        for record in all_items:
            cursor = conn.execute("SELECT id, hash FROM items WHERE id = ?", (record["id"],))
            existing = cursor.fetchone()

            if existing:
                if existing["hash"] == record["hash"]:
                    counts["items_skipped"] += 1
                    continue
                conn.execute(
                    """
                    UPDATE items SET
                        title = ?,
                        url = ?,
                        published_at = ?,
                        body_text = ?,
                        meeting_date = ?,
                        comment_deadline = ?,
                        status = ?,
                        hash = ?
                    WHERE id = ?
                    """,
                    (
                        record["title"],
                        record["url"],
                        record["published_at"],
                        record["body_text"],
                        record["meeting_date"],
                        record["comment_deadline"],
                        record["status"],
                        record["hash"],
                        record["id"],
                    ),
                )
                counts["items_updated"] += 1
            else:
                conn.execute(
                    """
                    INSERT INTO items (
                        id, source_id, jurisdiction, title, url,
                        published_at, body_text, meeting_date, comment_deadline,
                        status, hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["id"],
                        record["source_id"],
                        record["jurisdiction"],
                        record["title"],
                        record["url"],
                        record["published_at"],
                        record["body_text"],
                        record["meeting_date"],
                        record["comment_deadline"],
                        record["status"],
                        record["hash"],
                    ),
                )
                counts["items_created"] += 1

        conn.commit()
        return counts
