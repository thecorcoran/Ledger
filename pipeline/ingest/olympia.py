"""Ingester for City of Olympia legislative meetings and agenda items via Granicus Legistar Web API."""

import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from pipeline.normalize import clean_text, compute_item_hash

logger = logging.getLogger(__name__)

BASE_API_URL = "https://webapi.legistar.com/v1/olympia"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "olympia"

# Procedural items that do not contain substantive policy decisions
PROCEDURAL_TITLES = {
    "roll call",
    "call to order",
    "approval of agenda",
    "approval of the agenda",
    "approval of the consent agenda",
    "pledge of allegiance",
    "adjournment",
    "executive session",
    "council response to public comment",
    "council response to public comment (optional)",
    "public comment",
    "continued public comment",
    "consent calendar",
    "announcements",
    "other business",
    "city manager's report and referrals",
    "council intergovernmental/committee reports and referrals",
}


class OlympiaIngester:
    def __init__(self, base_url: str = BASE_API_URL, cache_dir: Optional[Path] = None):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _http_get_json(self, url: str, cache_file: Optional[Path] = None, use_cache: bool = True) -> Any:
        if use_cache and cache_file and cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed to read cache file %s: %s", cache_file, e)

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "LorettaLedger/1.0 (local policy researcher; https://github.com/thecorcoran/Ledger)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw_data = resp.read().decode("utf-8")
            data = json.loads(raw_data)

        if cache_file:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                logger.warning("Failed to write cache file %s: %s", cache_file, e)

        return data

    def fetch_recent_events(
        self,
        days_back: int = 30,
        days_ahead: int = 30,
        limit: int = 50,
        bodies: Optional[Set[str]] = None,
        use_cache: bool = True,
    ) -> List[Dict[str, Any]]:
        """Fetches events from Legistar API within the specified date range."""
        now = datetime.now(timezone.utc)
        start_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        filter_clause = f"EventDate ge datetime'{start_date}' and EventDate le datetime'{end_date}'"
        params = {
            "$filter": filter_clause,
            "$orderby": "EventDate desc",
            "$top": str(limit),
        }
        url = f"{self.base_url}/Events?{urllib.parse.urlencode(params)}"
        cache_file = self.cache_dir / f"events_{start_date}_{end_date}.json"

        events = self._http_get_json(url, cache_file=cache_file, use_cache=use_cache)
        if not isinstance(events, list):
            return []

        if bodies:
            bodies_lower = {b.lower() for b in bodies}
            events = [e for e in events if e.get("EventBodyName", "").lower() in bodies_lower]

        return events

    def fetch_event_items(self, event_id: int, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Fetches items for a specific event."""
        url = f"{self.base_url}/Events/{event_id}/EventItems"
        cache_file = self.cache_dir / f"event_{event_id}_items.json"
        items = self._http_get_json(url, cache_file=cache_file, use_cache=use_cache)
        return items if isinstance(items, list) else []

    def fetch_matter_attachments(self, matter_id: int, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Fetches attachments for a legislative matter."""
        url = f"{self.base_url}/Matters/{matter_id}/Attachments"
        cache_file = self.cache_dir / f"matter_{matter_id}_attachments.json"
        try:
            attachments = self._http_get_json(url, cache_file=cache_file, use_cache=use_cache)
            return attachments if isinstance(attachments, list) else []
        except Exception as e:
            logger.debug("Could not fetch attachments for matter %s: %s", matter_id, e)
            return []

    @staticmethod
    def is_procedural_item(title: Optional[str], matter_id: Optional[int] = None) -> bool:
        """Determines if an agenda item is purely ceremonial, procedural, or a boilerplate notice."""
        if not title:
            return True
        normalized = clean_text(title).strip().lower()
        if not normalized:
            return True

        # Check for boilerplate public hearing notices with asterisks
        if "****" in normalized or "opportunity to speak at this" in normalized:
            return True

        # Items ending with "- none" (e.g. "SECOND READINGS (Ordinances) - None")
        if normalized.endswith("- none") or normalized.endswith("(none)"):
            return True

        if normalized in PROCEDURAL_TITLES:
            return True

        for p in PROCEDURAL_TITLES:
            if normalized == p or normalized.startswith(f"{p}:") or normalized.startswith(f"{p} -"):
                return True

        # If no matter_id is associated and title matches category uppercase headers
        if matter_id is None and (
            normalized.startswith("consideration of a resolution")
            or normalized.startswith("special recognition")
        ):
            return True

        return False

    def build_item_record(
        self,
        event: Dict[str, Any],
        event_item: Dict[str, Any],
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Constructs a normalized item record suitable for insertion into the items table."""
        title = event_item.get("EventItemTitle")
        matter_id = event_item.get("EventItemMatterId")
        if not title or self.is_procedural_item(title, matter_id=matter_id):
            return None

        event_id = event.get("EventId")
        event_item_id = event_item.get("EventItemId")
        item_id = f"olympia-event-{event_id}-item-{event_item_id}"

        agenda_num = event_item.get("EventItemAgendaNumber")
        display_title = f"{agenda_num} {title}".strip() if agenda_num else title

        # Meeting date
        event_date_raw = event.get("EventDate", "")
        meeting_date = event_date_raw[:10] if event_date_raw else None

        # Published timestamp
        published_at = event.get("EventAgendaLastPublishedUTC") or event_date_raw

        # URL preference: Event InSite URL or Granicus agenda PDF
        url = (
            event.get("EventInSiteURL")
            or event.get("EventAgendaFile")
            or f"https://olympia.legistar.com/MeetingDetail.aspx?LEGID={event_id}"
        )

        # Assemble body text
        body_parts = []
        body_name = event.get("EventBodyName", "City of Olympia")
        body_parts.append(f"Body: {body_name}")

        if event.get("EventComment"):
            body_parts.append(f"Meeting Details: {clean_text(event.get('EventComment'))}")

        if event_item.get("EventItemActionName"):
            body_parts.append(f"Action: {event_item.get('EventItemActionName')}")

        matter_file = event_item.get("EventItemMatterFile")
        matter_type = event_item.get("EventItemMatterType")
        if matter_file:
            matter_str = f"Matter File: {matter_file}"
            if matter_type:
                matter_str += f" ({matter_type})"
            body_parts.append(matter_str)

        if event_item.get("EventItemAgendaNote"):
            body_parts.append(f"Agenda Note: {event_item.get('EventItemAgendaNote')}")

        if event_item.get("EventItemMinutesNote"):
            body_parts.append(f"Minutes Note: {event_item.get('EventItemMinutesNote')}")

        if event.get("EventAgendaFile"):
            body_parts.append(f"Full Agenda PDF: {event.get('EventAgendaFile')}")

        if attachments:
            attach_lines = []
            for a in attachments:
                name = a.get("MatterAttachmentName") or "Attachment"
                link = a.get("MatterAttachmentHyperlink") or ""
                if link:
                    attach_lines.append(f"- [{name}]({link})")
            if attach_lines:
                body_parts.append("Supporting Attachments:\n" + "\n".join(attach_lines))

        body_text = clean_text("\n\n".join(body_parts))
        item_hash = compute_item_hash(
            jurisdiction="olympia",
            title=display_title,
            body_text=body_text,
            meeting_date=meeting_date,
            url=url,
        )

        return {
            "id": item_id,
            "source_id": "olympia-legistar-api",
            "jurisdiction": "olympia",
            "title": display_title,
            "url": url,
            "published_at": published_at,
            "body_text": body_text,
            "meeting_date": meeting_date,
            "comment_deadline": None,
            "status": "active",
            "hash": item_hash,
        }

    def ingest(
        self,
        conn,
        days_back: int = 30,
        days_ahead: int = 30,
        limit: int = 50,
        bodies: Optional[Set[str]] = None,
        use_cache: bool = True,
        fetch_attachments: bool = True,
    ) -> Dict[str, int]:
        """Runs the Olympia ingestion and inserts or updates records in the database."""
        events = self.fetch_recent_events(
            days_back=days_back,
            days_ahead=days_ahead,
            limit=limit,
            bodies=bodies,
            use_cache=use_cache,
        )

        counts = {"events_processed": 0, "items_created": 0, "items_updated": 0, "items_skipped": 0}

        for event in events:
            counts["events_processed"] += 1
            event_id = event.get("EventId")
            if not event_id:
                continue

            event_items = self.fetch_event_items(event_id, use_cache=use_cache)
            for event_item in event_items:
                matter_id = event_item.get("EventItemMatterId")
                attachments = None
                if fetch_attachments and matter_id:
                    attachments = self.fetch_matter_attachments(matter_id, use_cache=use_cache)

                record = self.build_item_record(event, event_item, attachments=attachments)
                if not record:
                    counts["items_skipped"] += 1
                    continue

                # Check existing item
                cursor = conn.execute("SELECT id, hash FROM items WHERE id = ?", (record["id"],))
                existing = cursor.fetchone()

                if existing:
                    if existing["hash"] == record["hash"]:
                        counts["items_skipped"] += 1
                        continue
                    # Update changed item
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
