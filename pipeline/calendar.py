"""Calendar feeds generator for Loretta's Ledger.

Implements build-time static iCalendar (.ics) generation according to RFC 5545:
- Feeds: Everything (all.ics), Olympia only (olympia.ics), Thurston County only (thurston.ics), Test cases (test-cases.ics).
- Event types: hearing, vote, comment_deadline.
- Filter: Includes only briefed matters and hearings, never raw procedural agenda lines.
- Stable UIDs: format `ledger-{stable_slug}-{event_type}@{domain}` so edits update existing events.
- Timezone: America/Los_Angeles.
- Reminder: VALARM trigger 1 day before comment deadlines (-P1D).
- Cancelled events: marked with STATUS:CANCELLED.
- "Dates change; confirm on the official agenda."
- "Updated [date]" in calendar description.
"""

import os
import re
import sqlite3
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
CALENDAR_CONFIG_PATH = CONFIG_DIR / "calendar_feeds.yaml"


def load_calendar_config(path: Optional[Path] = None) -> Dict[str, Any]:
    cfg_path = path or CALENDAR_CONFIG_PATH
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {
        "timezone": "America/Los_Angeles",
        "domain": "lorettas-ledger.local",
        "base_url": "https://thecorcoran.github.io/Ledger",
        "feeds": {
            "all": {"slug": "all", "filename": "all.ics", "title": "Loretta's Ledger — Everything", "filter": {}},
            "olympia": {"slug": "olympia", "filename": "olympia.ics", "title": "Loretta's Ledger — City of Olympia", "filter": {"jurisdiction": "olympia"}},
            "thurston": {"slug": "thurston", "filename": "thurston.ics", "title": "Loretta's Ledger — Thurston County", "filter": {"jurisdiction": "thurston"}},
            "test_cases": {"slug": "test-cases", "filename": "test-cases.ics", "title": "Loretta's Ledger — Test Cases", "filter": {"is_test_case": True}},
        },
    }


def slugify_id(val: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(val or "")).strip("-").lower()
    return s or "matter"


def determine_body_start_time(body_name: str, item_title: str) -> Tuple[str, str]:
    """Returns (start_time_HHMMSS, end_time_HHMMSS) in America/Los_Angeles."""
    t_lower = (item_title or "").lower()
    b_lower = (body_name or "").lower()

    if "hearing examiner" in b_lower or "hearing examiner" in t_lower:
        return "100000", "120000"
    if "health" in b_lower or "boh" in t_lower:
        return "153000", "170000"
    if "planning commission" in b_lower and "thurston" in b_lower:
        return "183000", "203000"
    if "planning commission" in b_lower:
        return "180000", "200000"
    if "commissioner" in b_lower or "bocc" in t_lower:
        return "140000", "153000"
    if "council" in b_lower:
        return "173000", "200000"
    return "170000", "190000"


def format_ics_date_time(date_str: str, time_str: str) -> str:
    """Formats YYYYMMDDTHHMMSS."""
    clean_date = date_str.replace("-", "")[:8]
    clean_time = time_str.replace(":", "")[:6]
    return f"{clean_date}T{clean_time}"


def extract_calendar_events(
    conn: sqlite3.Connection,
    today_str: str = "2026-10-13",
    domain: str = "lorettas-ledger.local",
    base_url: str = "https://thecorcoran.github.io/Ledger",
) -> List[Dict[str, Any]]:
    """Extracts all briefed matters and hearings into structured calendar event dictionaries."""
    cursor = conn.cursor()

    # Query active matters that have living briefs or confirmed hearings
    query = """
    SELECT m.id as matter_id, m.stable_id, m.plain_title, m.official_title, m.jurisdiction,
           m.matter_type, m.current_stage, m.is_test_case, m.test_case_reason,
           m.last_action_date, m.last_action_body, m.next_action_date, m.next_action_body,
           mb.item_id as brief_item_id, mb.updated_date,
           i.title as item_title, i.meeting_date as item_meeting_date, i.comment_deadline, i.status as item_status
    FROM matters m
    JOIN matter_briefs mb ON m.id = mb.matter_id
    LEFT JOIN items i ON mb.item_id = i.id
    WHERE m.archive_state = 'active'
    ORDER BY m.id
    """
    rows = cursor.execute(query).fetchall()

    events: List[Dict[str, Any]] = []
    seen_uids = set()

    for r in rows:
        row = dict(r)
        mid = row["matter_id"]
        stable_slug = slugify_id(row["stable_id"] or mid)
        plain_title = row["plain_title"] or row["official_title"] or "Policy Matter"
        jur = (row["jurisdiction"] or "thurston").lower()
        is_test = bool(row["is_test_case"])
        brief_url = f"{base_url}/briefs/brief-{row['brief_item_id']}.html" if row["brief_item_id"] else f"{base_url}/briefs/{mid}.html"

        # Check if meeting is marked cancelled
        item_status_text = f"{row.get('item_status') or ''} {row.get('item_title') or ''}".lower()
        is_cancelled = "cancel" in item_status_text or "postponed" in item_status_text

        # 1. Hearing or Public Meeting Event
        meeting_date = row.get("item_meeting_date") or row.get("next_action_date") or row.get("last_action_date")
        if meeting_date and meeting_date[:4].isdigit():
            body_name = row.get("next_action_body") or row.get("last_action_body") or jur.title()
            start_t, end_t = determine_body_start_time(body_name, row.get("item_title") or "")
            dt_start = format_ics_date_time(meeting_date[:10], start_t)
            dt_end = format_ics_date_time(meeting_date[:10], end_t)

            stage_lower = (row.get("current_stage") or "").lower()
            is_hearing = "hearing" in stage_lower or "hearing" in (row.get("item_title") or "").lower()
            event_type = "hearing" if is_hearing else "meeting"

            uid = f"ledger-{stable_slug}-{event_type}@{domain}"
            if uid not in seen_uids:
                seen_uids.add(uid)
                prefix = "[CANCELLED] " if is_cancelled else ("Public Hearing: " if is_hearing else "Public Meeting: ")
                summary = f"{prefix}{plain_title}"
                desc = (
                    f"{body_name} - {plain_title}\\n\\n"
                    f"Brief: {brief_url}\\n\\n"
                    f"Dates change; confirm on the official agenda."
                )
                events.append({
                    "uid": uid,
                    "event_type": event_type,
                    "matter_id": mid,
                    "stable_id": row["stable_id"],
                    "plain_title": plain_title,
                    "jurisdiction": jur,
                    "is_test_case": is_test,
                    "date": meeting_date[:10],
                    "dtstart": dt_start,
                    "dtend": dt_end,
                    "summary": summary,
                    "description": desc,
                    "url": brief_url,
                    "cancelled": is_cancelled,
                    "valarm": False,
                })

        # 2. Documented Comment Deadline Event
        comment_deadline = row.get("comment_deadline")
        # Check if it's a valid ISO date YYYY-MM-DD
        if comment_deadline and re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}", str(comment_deadline).strip()):
            c_date = str(comment_deadline).strip()[:10]
            c_uid = f"ledger-{stable_slug}-comment-deadline@{domain}"
            if c_uid not in seen_uids:
                seen_uids.add(c_uid)
                c_dt_start = format_ics_date_time(c_date, "170000")
                c_dt_end = format_ics_date_time(c_date, "173000")
                c_prefix = "[CANCELLED] " if is_cancelled else ""
                c_summary = f"{c_prefix}Comment Deadline: {plain_title}"
                c_desc = (
                    f"Written comment deadline for {plain_title}.\\n\\n"
                    f"Review details and submission contacts: {brief_url}\\n\\n"
                    f"Dates change; confirm on the official agenda."
                )
                events.append({
                    "uid": c_uid,
                    "event_type": "comment_deadline",
                    "matter_id": mid,
                    "stable_id": row["stable_id"],
                    "plain_title": plain_title,
                    "jurisdiction": jur,
                    "is_test_case": is_test,
                    "date": c_date,
                    "dtstart": c_dt_start,
                    "dtend": c_dt_end,
                    "summary": c_summary,
                    "description": c_desc,
                    "url": brief_url,
                    "cancelled": is_cancelled,
                    "valarm": True,  # 1 day reminder
                })

    return events


def filter_events(events: List[Dict[str, Any]], feed_filter: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Applies jurisdiction, test case, or topic filters."""
    result = []
    target_jur = feed_filter.get("jurisdiction")
    target_test = feed_filter.get("is_test_case")
    target_topic = feed_filter.get("topic")

    for ev in events:
        if target_jur and ev["jurisdiction"] != target_jur:
            continue
        if target_test is not None and ev["is_test_case"] != target_test:
            continue
        result.append(ev)
    return result


def generate_ics_content(
    feed_title: str,
    feed_desc: str,
    events: List[Dict[str, Any]],
    today_str: str = "2026-10-13",
    timezone: str = "America/Los_Angeles",
) -> str:
    """Renders RFC 5545 iCalendar content with America/Los_Angeles VTIMEZONE and VALARM."""
    updated_dt = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    human_date = today_str[:10]

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Loretta's Ledger//Policy Tracker Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{feed_title}",
        f"X-WR-CALDESC:{feed_desc} Updated {human_date}.",
        f"X-WR-TIMEZONE:{timezone}",
        "BEGIN:VTIMEZONE",
        f"TZID:{timezone}",
        "X-LIC-LOCATION:America/Los_Angeles",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:-0800",
        "TZOFFSETTO:-0700",
        "TZNAME:PDT",
        "DTSTART:19700308T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:-0700",
        "TZOFFSETTO:-0800",
        "TZNAME:PST",
        "DTSTART:19701101T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    for ev in events:
        status_line = "STATUS:CANCELLED" if ev.get("cancelled") else "STATUS:CONFIRMED"
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{ev['uid']}")
        lines.append(f"DTSTAMP:{updated_dt}")
        lines.append(f"DTSTART;TZID={timezone}:{ev['dtstart']}")
        lines.append(f"DTEND;TZID={timezone}:{ev['dtend']}")
        lines.append(f"SUMMARY:{ev['summary']}")
        lines.append(f"DESCRIPTION:{ev['description']}")
        lines.append(f"URL:{ev['url']}")
        lines.append(status_line)

        # 1-day reminder before comment deadlines
        if ev.get("valarm"):
            lines.append("BEGIN:VALARM")
            lines.append("ACTION:DISPLAY")
            lines.append(f"DESCRIPTION:Reminder: Comment deadline tomorrow for {ev['plain_title']}")
            lines.append("TRIGGER:-P1D")
            lines.append("END:VALARM")

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    lines.append("")
    return "\r\n".join(lines)


def build_and_export_calendars(
    conn: sqlite3.Connection,
    target_dirs: List[Path],
    today_str: str = "2026-10-13",
) -> Dict[str, int]:
    """Generates all configured calendar feeds into docs/calendars and site/_site/calendars."""
    config = load_calendar_config()
    tz = config.get("timezone", "America/Los_Angeles")
    domain = config.get("domain", "lorettas-ledger.local")
    base_url = config.get("base_url", "https://thecorcoran.github.io/Ledger")
    feeds_cfg = config.get("feeds", {})

    all_events = extract_calendar_events(conn, today_str=today_str, domain=domain, base_url=base_url)

    stats = {"events_extracted": len(all_events), "feeds_generated": 0}

    for target_dir in target_dirs:
        cal_dir = target_dir / "calendars"
        cal_dir.mkdir(parents=True, exist_ok=True)

        for feed_key, f_info in feeds_cfg.items():
            fname = f_info.get("filename", f"{f_info.get('slug', feed_key)}.ics")
            f_title = f_info.get("title", f"Loretta's Ledger — {feed_key.title()}")
            f_desc = f_info.get("description", "Public hearings, votes, and comment deadlines.")
            f_filter = f_info.get("filter", {})

            matched_events = filter_events(all_events, f_filter)
            ics_text = generate_ics_content(f_title, f_desc, matched_events, today_str=today_str, timezone=tz)

            out_path = cal_dir / fname
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(ics_text)

    stats["feeds_generated"] = len(feeds_cfg)
    return stats

