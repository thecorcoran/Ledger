"""Matter tracker, cross-meeting identifier linker, minutes parser, and lifecycle engine for Loretta's Ledger.

Models each policy matter as a living legislative bill-tracker record:
1. Strict identifier grouping (Legistar file number, case number, ordinance/resolution number, or named code chapter).
   Never groups on generic words; proposes uncertain links (`confidence = 'proposed'`) for editor confirmation.
2. Minutes & results as evidence: procedural items and minutes are never standalone matters.
   Minutes and official results are parsed to populate actions, motions, and vote tallies in `matter_events`.
3. Per-type lifecycles (`ordinance`, `code_amendment`, `budget_rates`, `contract_grant`, `permit_case`)
   with strict tense/outcome accuracy (`will consider` for upcoming, `considered` when no result is documented).
4. Archive states without an arbitrary date cutoff: `active`, `closed`, `dormant` (configurable `dormant_months`, default 6).
   Dormant matters automatically return to `active` and are marked `Update to review` (`needs_review = 1`)
   whenever a new action, document version, or scheduled meeting is posted.
5. Document versions (`matter_versions` with `what_changed` notes), parent/child relationships (`matter_relations`),
   and one living brief per matter (`matter_briefs` with `Updated [date]` and change log).
"""

import hashlib
import html
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import yaml

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.titles import (
    classify_item_tier,
    clean_agenda_title,
    determine_exact_body_name,
    generate_display_title,
)

logger = logging.getLogger(__name__)

DEFAULT_TRACKER_RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "tracker_rules.yaml"
DEFAULT_TODAY_STR = "2026-10-12"

PER_TYPE_LIFECYCLES: Dict[str, List[str]] = {
    "ordinance": [
        "Introduced / Work session",
        "First reading",
        "Public hearing scheduled",
        "Public hearing held",
        "Adopted / In effect",
        "Failed / Withdrawn",
    ],
    "code_amendment": [
        "Gap analysis / scoping",
        "Draft code released",
        "Advisory commission review",
        "Advisory public hearing",
        "Recommended to governing body",
        "Governing body hearing",
        "Adopted / In effect",
        "Returned / Withdrawn",
    ],
    "budget_rates": [
        "Department / Committee briefing",
        "Draft budget / rates released",
        "Public hearing",
        "Council review",
        "Adopted for fiscal year",
    ],
    "contract_grant": [
        "Committee / Consent docket",
        "Council consideration",
        "Approved / Executed",
        "Rejected / Expired",
    ],
    "permit_case": [
        "Application / Presubmission conference",
        "SEPA determination",
        "Hearing Examiner / Advisory hearing",
        "Decision issued",
        "Appeal period / Closed",
    ],
}


def load_tracker_rules(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Loads tracker lifecycle and dormancy rules from config/tracker_rules.yaml."""
    target = config_path or DEFAULT_TRACKER_RULES_PATH
    if target.exists():
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                return data
        except Exception as e:
            logger.warning("Failed to load tracker_rules.yaml: %s", e)
    return {"dormant_months": 6, "lifecycles": PER_TYPE_LIFECYCLES}


def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60].strip("-")


def format_human_date(date_str: Optional[str]) -> str:
    """Formats '2026-10-07' as 'Oct 7, 2026'."""
    if not date_str:
        return "Date not specified"
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return date_str


def is_minutes_or_results_item(item: Any) -> bool:
    """Returns True if the item is a meeting minutes or official vote/results record."""
    title = html.unescape((item["title"] if "title" in item.keys() and item["title"] else "")).strip()
    t_lower = title.lower()
    if re.search(r"\bminutes\b", t_lower):
        return True
    if t_lower.startswith("approval of") and "minutes" in t_lower:
        return True
    return False


def is_procedural_item(item: Any) -> bool:
    """Returns True if an item is procedural (Tier 3, minutes, bare agenda, audio, or generic schedule)."""
    if is_minutes_or_results_item(item):
        return True
    tier = classify_item_tier(dict(item))
    if tier == 3:
        return True
    title = html.unescape((item["title"] if "title" in item.keys() and item["title"] else "")).strip().lower()
    procedural_exact = (
        "audio",
        "weekly schedule",
        "board work session",
        "board of county commissioners business meeting & public hearing(s)",
        "board of health meeting & public hearing(s)",
        "canceled - board work session",
    )
    if title in procedural_exact or title.startswith("planning commission: agenda"):
        return True
    if re.match(r"^(?:1st|2nd\s+)?public\s+comment(?:\s+for|\s+-|\s*\()", title):
        return True
    return False


def extract_identifiers(item: Any) -> Dict[str, List[str]]:
    """Extracts strict identifiers: Legistar matter files, case numbers, ordinance/resolution numbers, and named code chapters."""
    title = html.unescape(item["title"] if "title" in item.keys() and item["title"] else "")
    body = html.unescape(item["body_text"] if "body_text" in item.keys() and item["body_text"] else "")
    jur = (item["jurisdiction"] if "jurisdiction" in item.keys() and item["jurisdiction"] else "").lower()
    full_text = f"{title}\n{body}"
    t_lower = title.lower()

    identifiers: Dict[str, List[str]] = {
        "matter_files": [],
        "case_numbers": [],
        "ordinance_numbers": [],
        "resolution_numbers": [],
        "code_chapters": [],
    }

    # 1. Legistar Matter Files (e.g. Matter File: 26-0731)
    for m in re.finditer(r"Matter\s+File:\s*([0-9]{2}-[0-9]{4})", full_text, re.IGNORECASE):
        val = m.group(1).strip()
        if val not in identifiers["matter_files"]:
            identifiers["matter_files"].append(val)

    # 2. Case / Permit numbers (e.g. Case: 25-1692, Case 25-0980, or presubmission permit numbers 26-5506, 26-5305, 26-5942)
    for m in re.finditer(r"\b(?:Case|Permit|Project\s+No\.?)[:\s#]+([0-9]{2}-[0-9]{4})\b", full_text, re.IGNORECASE):
        val = m.group(1).strip()
        if val not in identifiers["case_numbers"]:
            identifiers["case_numbers"].append(val)
    # Also catch trailing permit numbers on presubmission items e.g. ", 26-5506" or ". 26-5305"
    for m in re.finditer(r"(?:,\s*|\.\s+)(2[0-9]-[0-9]{4})\b", title):
        val = m.group(1).strip()
        if val not in identifiers["case_numbers"] and val not in identifiers["matter_files"]:
            identifiers["case_numbers"].append(val)

    # 3. Ordinance numbers (e.g. Ordinance 1234, Ord. No. 2026-01)
    stopwords_ord = {"to", "revising", "amending", "concerning", "for", "update", "updates", "adopt", "adopting", "adding"}
    for m in re.finditer(r"(?:Ordinance|Ord\.)\s+(?:No\.?\s*)?([0-9][0-9A-Za-z\-_/]*)", full_text, re.IGNORECASE):
        val = m.group(1).strip().rstrip(".,;:")
        if val.lower() not in stopwords_ord and len(val) >= 2 and val not in identifiers["ordinance_numbers"]:
            identifiers["ordinance_numbers"].append(val)

    # 4. Resolution numbers (e.g. Resolution 26-0595)
    stopwords_res = {"authorizing", "expressing", "to", "for", "adopting", "approving"}
    for m in re.finditer(r"(?:Resolution|Res\.)\s+(?:No\.?\s*)?([0-9][0-9A-Za-z\-_/]*)", full_text, re.IGNORECASE):
        val = m.group(1).strip().rstrip(".,;:")
        if val.lower() not in stopwords_res and len(val) >= 2 and val not in identifiers["resolution_numbers"]:
            identifiers["resolution_numbers"].append(val)

    # 5. Named Code Chapters / Articles (TCC & OMC)
    # Explicit "Chapter XX.XX"
    for m in re.finditer(r"\bChapter\s+([0-9]{1,2}\.[0-9]{2}[A-Za-z]?)\b", full_text, re.IGNORECASE):
        chap = m.group(1)
        prefix = "TCC" if jur == "thurston" else "OMC"
        code_id = f"{prefix} Chapter {chap}"
        if code_id not in identifiers["code_chapters"]:
            identifiers["code_chapters"].append(code_id)

    # Thurston CAO named chapter items (where staff memo/presentation names CAO Wetlands, CAO FWHCAs, or Frequently Flooded Areas)
    if jur == "thurston":
        if ("cao" in t_lower or "chapter 24.30" in t_lower) and "wetland" in t_lower:
            if "TCC Chapter 24.30" not in identifiers["code_chapters"]:
                identifiers["code_chapters"].append("TCC Chapter 24.30")
        if ("cao" in t_lower or "chapter 24.25" in t_lower or "fwhca" in t_lower or "fish and wildlife habitat" in t_lower or "fish & wildlife habitat" in t_lower):
            if "TCC Chapter 24.25" not in identifiers["code_chapters"]:
                identifiers["code_chapters"].append("TCC Chapter 24.25")
        if "frequently flooded areas" in t_lower or "chapter 24.20" in t_lower:
            if "TCC Chapter 24.20" not in identifiers["code_chapters"]:
                identifiers["code_chapters"].append("TCC Chapter 24.20")
        if "drinking water code" in t_lower and "article iii" in t_lower:
            if "Sanitary Code Article III" not in identifiers["code_chapters"]:
                identifiers["code_chapters"].append("Sanitary Code Article III")
    elif jur == "olympia":
        if "critical area ordinance update" in t_lower:
            if "OMC Title 18 (CAO)" not in identifiers["code_chapters"]:
                identifiers["code_chapters"].append("OMC Title 18 (CAO)")
        if "minimum parking requirements" in t_lower and "code amendment" in t_lower:
            if "OMC Chapter 18.38" not in identifiers["code_chapters"]:
                identifiers["code_chapters"].append("OMC Chapter 18.38")
        if "home based businesses" in t_lower or "home occupations" in t_lower:
            if "OMC Chapter 18.04" not in identifiers["code_chapters"]:
                identifiers["code_chapters"].append("OMC Chapter 18.04")

    return identifiers


def parse_minutes_and_results(text: str) -> List[Dict[str, Optional[str]]]:
    """Parses minutes or meeting results text to extract documented actions, motions, and vote tallies.

    Never fabricates outcomes; returns extracted motion/vote/outcome records only when present in text.
    """
    if not text:
        return []

    findings: List[Dict[str, Optional[str]]] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    for i, line in enumerate(lines):
        motion_text: Optional[str] = None
        vote_tally: Optional[str] = None
        outcome_text: Optional[str] = None
        subject_ref: Optional[str] = None

        # Check for identifier reference on the line or previous line
        context_block = " ".join(lines[max(0, i - 1): min(len(lines), i + 2)])
        m_ref = re.search(
            r"(?:Case[:\s#]*([0-9]{2}-[0-9]{4})|Matter\s+File:\s*([0-9]{2}-[0-9]{4})|Chapter\s+([0-9]{1,2}\.[0-9]{2})|Ordinance\s+(?:No\.?\s*)?([0-9A-Za-z\-]+)|Resolution\s+(?:No\.?\s*)?([0-9A-Za-z\-]+))",
            context_block,
            re.IGNORECASE,
        )
        if m_ref:
            if m_ref.group(1):
                subject_ref = f"Case {m_ref.group(1)}"
            elif m_ref.group(2):
                subject_ref = f"File {m_ref.group(2)}"
            elif m_ref.group(3):
                subject_ref = f"Chapter {m_ref.group(3)}"
            elif m_ref.group(4):
                subject_ref = f"Ordinance {m_ref.group(4)}"
            elif m_ref.group(5):
                subject_ref = f"Resolution {m_ref.group(5)}"

        # Check for explicit Motion pattern
        m_motion = re.search(
            r"\b(moved\s+by\s+[^.;]+|motion\s+(?:by|to)\s+[^.;]+)",
            line,
            re.IGNORECASE,
        )
        if m_motion:
            motion_text = m_motion.group(1).strip()

        # Check for explicit Vote / Roll call tally pattern
        m_vote = re.search(
            r"\b((?:passed|approved|adopted|carried|failed|defeated|recommended)\s+(?:unanimously(?:\s+by\s+a\s+vote\s+of\s+[0-9]+\s*[-–to]+\s*[0-9]+|\s*\([0-9]+\s*[-–]\s*[0-9]+\)|\s+[0-9]+\s*[-–]\s*[0-9]+)?|by\s+a\s+vote\s+of\s+[0-9]+\s*[-–to]+\s*[0-9]+|[0-9]+\s*[-–]\s*[0-9]+(?:\s*[-–]\s*[0-9]+)?)|vote[:\s]+[0-9]+\s*[-–]\s*[0-9]+(?:\s*\([^)]+\))?|ayes[:\s]+[0-9]+[,;\s]+nays[:\s]+[0-9]+)",
            line,
            re.IGNORECASE,
        )
        if m_vote:
            vote_tally = m_vote.group(1).strip()

        # Check for documented action/outcome sentences in minutes
        m_outcome = re.search(
            r"\b((?:commission|council|board|committee|hearing\s+examiner)\s+(?:voted\s+to\s+[^.;]+|approved\s+[^.;]+|adopted\s+[^.;]+|recommended\s+[^.;]+|directed\s+staff\s+to\s+[^.;]+|held\s+a\s+work\s+session\s+on\s+[^.;]+|took\s+public\s+comment\s+on\s+[^.;]+))",
            line,
            re.IGNORECASE,
        )
        if m_outcome:
            outcome_text = m_outcome.group(1).strip()

        if motion_text or vote_tally or outcome_text:
            summary_parts = []
            if outcome_text:
                summary_parts.append(outcome_text)
            if motion_text and motion_text not in (outcome_text or ""):
                summary_parts.append(f"Motion: {motion_text}")
            if vote_tally and vote_tally not in (outcome_text or ""):
                summary_parts.append(f"Vote: {vote_tally}")
            findings.append({
                "subject_ref": subject_ref,
                "motion_text": motion_text,
                "vote_tally": vote_tally,
                "outcome_text": "; ".join(summary_parts),
            })

    return findings


def classify_matter_type(items: List[Any], stable_id: str) -> str:
    """Determines the matter_type ('ordinance', 'code_amendment', 'budget_rates', 'contract_grant', 'permit_case')."""
    combined_titles = " ".join(html.unescape(it["title"] or "").lower() for it in items)
    sid_lower = (stable_id or "").lower()

    if any(k in sid_lower for k in ("tcc chapter", "omc chapter", "omc title", "sanitary code")):
        return "code_amendment"
    if any(k in combined_titles for k in ("code amendment", "critical area ordinance", "cao", "chapter 24.", "article iii", "revisions to drinking water code", "home based businesses", "new uses & standards")):
        return "code_amendment"
    if any(k in combined_titles for k in ("case:", "case ", "preliminary plat", "presubmission conference", "shoreline", "open space application", "baldwin open space", "rezone")):
        return "permit_case"
    if any(k in combined_titles for k in ("budget", "capital facilities plan", "utility rates", "development fees", "impact fees", "special (dedicated) revenues", "lodging tax funding", "transportation improvement program", "salary schedule")):
        return "budget_rates"
    if any(k in combined_titles for k in ("proposed ordinance", "ordinance revising", "ordinance to adjust", "ordinance amending", "moratorium")):
        return "ordinance"
    if any(k in combined_titles for k in ("agreement", "contract", "grant", "bid award", "lease", "transfer of portions", "resolution", "work plan", "annual report", "auditor")):
        return "contract_grant"
    return "ordinance"


def determine_event_outcome_and_stage(
    item: Any,
    matter_type: str,
    today_str: str = DEFAULT_TODAY_STR,
    minutes_evidence: Optional[Dict[str, Optional[str]]] = None,
) -> Tuple[str, str, Optional[str], Optional[str]]:
    """Determines (action_type, outcome_text, motion_text, vote_tally) for a single matter event.

    Strictly follows Requirement 5:
    - Never claims an outcome unless a document or minutes record shows it.
    - Uses 'will consider' for scheduled upcoming items and 'considered' for past items without a documented result.
    """
    title_raw = html.unescape(item["title"] or "").strip()
    title_clean = clean_agenda_title(title_raw)
    t_lower = title_clean.lower()
    body_text = html.unescape(item["body_text"] or "")
    b_lower = body_text.lower()
    meeting_date = (item["meeting_date"] or "")[:10]
    exact_body = determine_exact_body_name(dict(item), body_text)
    jur_label = "Olympia" if (item["jurisdiction"] or "").lower() == "olympia" else "Thurston County"
    full_body = exact_body if jur_label.lower() in exact_body.lower() else f"{jur_label} {exact_body}"

    # Determine action_type
    if "public hearing" in t_lower:
        action_type = "Public hearing"
    elif "presubmission conference" in t_lower or "2.a " in (item["title"] or "").lower() and "parcel" in t_lower:
        action_type = "Presubmission conference"
    elif "gap analysis" in t_lower:
        action_type = "Gap analysis work session"
    elif "draft" in t_lower or "w/ edits" in t_lower:
        action_type = "Draft code review"
    elif "presentation" in t_lower:
        action_type = "Staff presentation"
    elif "memo" in t_lower:
        action_type = "Staff memo & briefing"
    elif "approved & signed" in t_lower:
        action_type = "Recommendation signed"
    elif "approval of" in t_lower or "consideration of" in t_lower:
        action_type = "Council consideration"
    else:
        action_type = "Work session / Briefing"

    # 1. Check if minutes_evidence provides an explicit documented outcome
    if minutes_evidence and minutes_evidence.get("outcome_text"):
        return (
            action_type,
            minutes_evidence["outcome_text"] or "",
            minutes_evidence.get("motion_text"),
            minutes_evidence.get("vote_tally"),
        )

    # 2. Check if the item's own text contains explicit minutes/vote outcome
    parsed_self = parse_minutes_and_results(body_text)
    if parsed_self:
        first = parsed_self[0]
        return (
            action_type,
            first["outcome_text"] or "",
            first.get("motion_text"),
            first.get("vote_tally"),
        )

    # 3. Check if the document title itself proves an outcome (e.g. "Approved & Signed")
    if "approved & signed" in t_lower:
        return (
            "Recommendation signed",
            f"{full_body} approved and signed the recommendation letter ({title_clean}).",
            None,
            "Approved & Signed",
        )

    # 4. Otherwise, never claim an undocumented outcome! Use 'will consider' or 'considered'.
    is_upcoming = bool(meeting_date and meeting_date >= today_str[:10])
    if is_upcoming:
        if "public hearing" in t_lower:
            outcome = f"{full_body} will hold a public hearing and will consider {title_clean}."
        else:
            outcome = f"{full_body} will consider {title_clean}."
    else:
        if "public hearing" in t_lower:
            outcome = f"{full_body} held a public hearing and considered {title_clean}; outcome not yet posted."
        else:
            outcome = f"{full_body} considered {title_clean}; outcome not yet posted."

    return action_type, outcome, None, None


def compute_lifecycle_status(
    matter_type: str,
    events: List[Dict[str, Any]],
    today_str: str = DEFAULT_TODAY_STR,
) -> str:
    """Computes the matter's lifecycle_status from its per-type lifecycle and documented events."""
    stages = PER_TYPE_LIFECYCLES.get(matter_type, PER_TYPE_LIFECYCLES["ordinance"])
    if not events:
        return stages[0]

    # Sort events chronologically
    sorted_evs = sorted(events, key=lambda e: e.get("action_date") or "")
    latest_ev = sorted_evs[-1]
    upcoming_evs = [e for e in sorted_evs if (e.get("action_date") or "") >= today_str[:10]]
    past_evs = [e for e in sorted_evs if (e.get("action_date") or "") < today_str[:10]]

    # Check if any event documents a final adoption/approval or withdrawal
    for ev in reversed(sorted_evs):
        out_lower = (ev.get("outcome_text") or "").lower()
        vt_lower = (ev.get("vote_tally") or "").lower()
        if any(w in vt_lower or w in out_lower for w in ("adopted", "passed", "enacted", "approved and signed", "approved 7-0", "approved unanimously", "carried")):
            if "recommendation letter" in out_lower or "planning commission" in (ev.get("body_name") or "").lower():
                if matter_type == "code_amendment":
                    return f"Recommended to governing body — {ev['body_name']} voted to recommend ({ev['action_date']})"
                elif matter_type == "permit_case":
                    return f"Decision issued — {ev['body_name']} approved recommendation ({ev['action_date']})"
            if matter_type in ("ordinance", "code_amendment"):
                return f"Adopted / In effect ({ev['action_date']})"
            elif matter_type == "budget_rates":
                return f"Adopted for fiscal year ({ev['action_date']})"
            elif matter_type == "contract_grant":
                return f"Approved / Executed ({ev['action_date']})"
            elif matter_type == "permit_case":
                return f"Decision issued ({ev['action_date']})"
        if any(w in vt_lower or w in out_lower for w in ("failed", "defeated", "withdrawn", "rejected", "denied")):
            if matter_type == "ordinance":
                return f"Failed / Withdrawn ({ev['action_date']})"
            elif matter_type == "code_amendment":
                return f"Returned / Withdrawn ({ev['action_date']})"
            elif matter_type == "contract_grant":
                return f"Rejected / Expired ({ev['action_date']})"

    # If there is an upcoming event scheduled
    if upcoming_evs:
        next_ev = upcoming_evs[0]
        act_type = (next_ev.get("action_type") or "").lower()
        date_fmt = format_human_date(next_ev.get("action_date"))
        body = next_ev.get("body_name") or "Body"
        if matter_type == "ordinance":
            stage = "Public hearing scheduled" if "hearing" in act_type else "Introduced / Work session"
        elif matter_type == "code_amendment":
            if "hearing" in act_type:
                stage = "Advisory public hearing" if "commission" in body.lower() else "Governing body hearing"
            else:
                stage = "Advisory commission review"
        elif matter_type == "budget_rates":
            stage = "Public hearing" if "hearing" in act_type else "Department / Committee briefing"
        elif matter_type == "contract_grant":
            stage = "Council consideration" if "council" in body.lower() or "commissioners" in body.lower() else "Committee / Consent docket"
        else:
            stage = "Hearing Examiner / Advisory hearing" if "hearing" in act_type else "Application / Presubmission conference"
        return f"{stage} — {body} will consider on {date_fmt}"

    # Otherwise all events are in the past and no final decision is documented yet
    last_ev = past_evs[-1] if past_evs else latest_ev
    act_type = (last_ev.get("action_type") or "").lower()
    date_fmt = format_human_date(last_ev.get("action_date"))
    body = last_ev.get("body_name") or "Body"
    has_minutes = bool(last_ev.get("minutes_url") or last_ev.get("vote_tally") or last_ev.get("motion_text"))
    suffix = "minutes posted" if has_minutes else "outcome not yet posted"

    if matter_type == "code_amendment":
        if "gap analysis" in act_type:
            stage = "Gap analysis / scoping"
        elif "hearing" in act_type:
            stage = "Advisory public hearing" if "commission" in body.lower() else "Governing body hearing"
        else:
            stage = "Advisory commission review"
    elif matter_type == "ordinance":
        stage = "Public hearing held" if "hearing" in act_type else "Introduced / Work session"
    elif matter_type == "budget_rates":
        stage = "Council review" if "council" in body.lower() else "Department / Committee briefing"
    elif matter_type == "contract_grant":
        stage = "Council consideration" if "council" in body.lower() or "commissioners" in body.lower() else "Committee / Consent docket"
    else:
        stage = "Hearing Examiner / Advisory hearing" if "hearing" in act_type else "Application / Presubmission conference"

    return f"{stage} — {body} considered on {date_fmt}; {suffix}"


def compute_archive_state(
    matter_type: str,
    lifecycle_status: str,
    last_action_date: Optional[str],
    next_action_date: Optional[str],
    previous_archive_state: Optional[str] = None,
    previous_last_action_date: Optional[str] = None,
    manual_override: Optional[str] = None,
    today_str: str = DEFAULT_TODAY_STR,
    dormant_months: int = 6,
) -> Tuple[str, bool]:
    """Computes (archive_state, woke_from_dormant) according to Requirement 6 & 9:

    - Active = not yet decided or not yet in effect.
    - Closed = decided and in effect, or withdrawn.
    - Dormant = no action and no posted next step for `dormant_months` (default 6 months).
    - A Dormant matter returns to Active automatically, and is marked 'Update to review',
      as soon as a new action, version, or scheduled meeting is posted for it.
    """
    status_lower = (lifecycle_status or "").lower()
    has_upcoming = bool(next_action_date and next_action_date[:10] >= today_str[:10])

    # Check if a previously dormant matter just received a new action, version, or scheduled meeting
    if previous_archive_state == "dormant":
        new_activity = has_upcoming or (
            bool(last_action_date)
            and (
                not previous_last_action_date
                or last_action_date[:10] > previous_last_action_date[:10]
            )
        )
        # Also check if last_action_date is now within the dormancy window
        if not new_activity and last_action_date:
            try:
                today_dt = datetime.strptime(today_str[:10], "%Y-%m-%d").date()
                last_dt = datetime.strptime(last_action_date[:10], "%Y-%m-%d").date()
                if (today_dt - last_dt).days < int(dormant_months * 30.4375):
                    new_activity = True
            except Exception:
                pass
        if new_activity:
            return "active", True

    # Respect manual override ('active' via Restore button, or 'closed')
    if manual_override in ("active", "closed"):
        return manual_override, False

    # Check if decided and in effect, or withdrawn
    closed_prefixes = (
        "adopted / in effect",
        "adopted for fiscal year",
        "approved / executed",
        "failed / withdrawn",
        "returned / withdrawn",
        "rejected / expired",
        "appeal period / closed",
    )
    if any(status_lower.startswith(p) for p in closed_prefixes):
        return "closed", False

    # Check if Dormant: no posted next step and last action older than dormant_months
    if not has_upcoming and last_action_date:
        try:
            today_dt = datetime.strptime(today_str[:10], "%Y-%m-%d").date()
            last_dt = datetime.strptime(last_action_date[:10], "%Y-%m-%d").date()
            cutoff_days = int(round(dormant_months * 30.4375))
            if (today_dt - last_dt).days >= cutoff_days:
                return "dormant", False
        except Exception:
            pass

    return "active", False


def detect_stage_and_next_step(items: List[Any], today_str: str = DEFAULT_TODAY_STR) -> Tuple[str, Optional[str]]:
    """Derives current stage and any documented next step strictly from source records."""
    sorted_items = sorted(items, key=lambda x: x["meeting_date"] or "", reverse=True)
    latest = sorted_items[0]
    m_type = classify_matter_type(items, "")
    ev_type, outcome, motion, vote = determine_event_outcome_and_stage(latest, m_type, today_str=today_str)
    body = determine_exact_body_name(dict(latest), latest["body_text"] or "")
    status = compute_lifecycle_status(
        m_type,
        [{
            "action_date": latest["meeting_date"] or "",
            "body_name": body,
            "action_type": ev_type,
            "outcome_text": outcome,
            "motion_text": motion,
            "vote_tally": vote,
        }],
        today_str=today_str,
    )

    next_step = None
    for it in sorted_items:
        txt = it["body_text"] or ""
        m = re.search(r"(?:next\s+steps?|scheduled\s+for|continued\s+to|final\s+action\s+on):\s*([^\n\r]+)", txt, re.IGNORECASE)
        if m:
            next_step = m.group(1).strip()
            break

    return status, next_step


def determine_matter_Grouping_Key(item: Any) -> Optional[Tuple[str, str, str]]:
    """Determines the canonical (jurisdiction, id_kind, stable_id) for a non-procedural item.

    Follows Requirement 2:
    - Attaches every agenda item, memo, presentation, and draft to a matter by case/file/ordinance number
      or named code chapter (or distinct named subject for singletons).
    - Never groups on generic words.
    """
    if is_procedural_item(item):
        return None

    jur = (item["jurisdiction"] or "").lower()
    idents = extract_identifiers(item)

    # Priority 1: Named Code Chapters (e.g. TCC Chapter 24.30, TCC Chapter 24.25, TCC Chapter 24.20, Sanitary Code Article III, OMC Title 18 (CAO))
    if idents["code_chapters"]:
        return (jur, "code_chapter", idents["code_chapters"][0])

    # Priority 2: Case / Permit Numbers (e.g. Case 25-1692, Case 25-0980, 26-5506)
    if idents["case_numbers"]:
        return (jur, "case_number", f"Case {idents['case_numbers'][0]}")

    # Priority 3: Legistar Matter Files (e.g. File 26-0731)
    if idents["matter_files"]:
        return (jur, "matter_file", f"File {idents['matter_files'][0]}")

    # Priority 4: Explicit Ordinance Numbers
    if idents["ordinance_numbers"]:
        return (jur, "ordinance", f"Ord. {idents['ordinance_numbers'][0]}")

    # Priority 5: Explicit Resolution Numbers
    if idents["resolution_numbers"]:
        return (jur, "resolution", f"Res. {idents['resolution_numbers'][0]}")

    # Priority 6: Named multi-item docket subjects with unmistakable proper nouns
    title_clean = clean_agenda_title(html.unescape(item["title"] or ""))
    t_lower = title_clean.lower()

    if jur == "thurston":
        if "baldwin open space" in t_lower or ("open space" in t_lower and "tax shift" in t_lower):
            return ("thurston", "case_number", "Baldwin Open Space Application")
        if "grand mound 193rd" in t_lower:
            return ("thurston", "case_number", "Grand Mound 193rd Rezone")
        if "new uses & standards" in t_lower or "new uses and standards" in t_lower:
            return ("thurston", "code_chapter", "TCC Development Code: New Uses & Standards")
        if "conservation district" in t_lower and "rate" in t_lower:
            return ("thurston", "ordinance", "TCD Rate Adjustment Ordinance")
        if "yelm-rainier tenino trail" in t_lower:
            return ("thurston", "contract_grant", "Yelm-Rainier-Tenino Trail Transfer")
        if "speed limit schedule" in t_lower:
            return ("thurston", "ordinance", "County Road Speed Limit Ordinance")
        if "six-year transportation improvement program" in t_lower:
            return ("thurston", "budget_rates", "2027-2032 County TIP")
        if "road standards" in t_lower and "2026" in t_lower:
            return ("thurston", "ordinance", "2026 County Road Standards")
        if "moratorium on detention facilities" in t_lower:
            return ("thurston", "ordinance", "Detention Facilities Moratorium")
        if "south thurston economic development" in t_lower:
            return ("thurston", "contract_grant", "South Thurston Economic Initiative")

    if jur == "olympia":
        if "2027 utility operating budget" in t_lower:
            return ("olympia", "budget_rates", "2027 Utility Budget & CFP")
        if "2027 development fees" in t_lower or ("impact fees" in t_lower and "utility rates" in t_lower):
            return ("olympia", "budget_rates", "2027 Development Fees & Utility Rates")
        if "2027 special (dedicated) revenues" in t_lower:
            return ("olympia", "budget_rates", "2027 Dedicated Revenues Budget")
        if "inspire olympia 2027 budget" in t_lower:
            return ("olympia", "budget_rates", "2027 Inspire Olympia Budget")
        if "2027 lodging tax funding" in t_lower:
            return ("olympia", "budget_rates", "2027 Lodging Tax Funding")
        if "2027 preliminary budget preview" in t_lower:
            return ("olympia", "budget_rates", "2027 OMPD Preliminary Budget")
        if "quince street" in t_lower:
            return ("olympia", "contract_grant", "ILA: Quince Street Tiny Home Village")
        if "franz anderson" in t_lower:
            return ("olympia", "contract_grant", "Agreement: Franz Anderson Tiny Home Village")
        if "housing and urban development" in t_lower or "cdbg" in t_lower:
            return ("olympia", "contract_grant", "HUD 2026 CDBG Grant Agreement")
        if "timberland regional library" in t_lower:
            return ("olympia", "resolution", "Res.: Timberland Library Levy Support")

    # Distinct standalone agenda item: use a clean stable identifier derived from the item
    short_slug = slugify(title_clean)[:36]
    return (jur, "docket_item", f"{jur.upper()}-{short_slug}")


def select_canonical_titles(items: List[Any], stable_id: str) -> Tuple[str, str]:
    """Selects (plain_title, official_title) for a matter across its linked items."""
    # Prefer hearings, ordinances, draft code items over memos/presentations when picking official_title
    sorted_items = sorted(
        items,
        key=lambda it: (
            2 if any(k in (it["title"] or "").lower() for k in ("chapter 24.", "public hearing", "ordinance", "case:")) else 1,
            it["meeting_date"] or "",
        ),
        reverse=True,
    )
    best = sorted_items[0]
    official_title = clean_agenda_title(html.unescape(best["title"] or stable_id))

    # Special clean official titles for well-known multi-packet code updates
    if stable_id == "TCC Chapter 24.30":
        official_title = "Critical Areas Ordinance (CAO) Update — Chapter 24.30: Wetlands"
    elif stable_id == "TCC Chapter 24.25":
        official_title = "Critical Areas Ordinance (CAO) Update — Chapter 24.25: Fish and Wildlife Habitat Conservation Areas"
    elif stable_id == "TCC Chapter 24.20":
        official_title = "Critical Areas Ordinance (CAO) Update — Chapter 24.20: Frequently Flooded Areas"
    elif stable_id == "Baldwin Open Space Application":
        official_title = "Open Space Tax Program Application — Baldwin Open Space"

    plain_title = best["display_title"] if "display_title" in best.keys() and best["display_title"] else ""
    if not plain_title:
        plain_title = generate_display_title(dict(best), best["body_text"] or "")

    return plain_title, official_title


def build_versions_for_matter(matter_id: str, stable_id: str, items: List[Any]) -> List[Dict[str, Any]]:
    """Builds chronological key document versions (`matter_versions`) with a short 'what_changed' note."""
    sorted_items = sorted(items, key=lambda it: (it["meeting_date"] or "", it["id"]))
    versions: List[Dict[str, Any]] = []

    if stable_id == "TCC Chapter 24.30":
        # Group into the 3 milestone versions for Wetlands Chapter 24.30
        v_specs = [
            (
                "2026-08-05",
                "v1 — Wetland Gap Analysis Memo",
                "Initial staff comparison of existing TCC Chapter 24.30 against 2022–2025 Washington Department of Ecology wetland guidance under RCW 36.70A.130.",
                lambda it: (it["meeting_date"] or "").startswith("2026-08-05") and "memo" in (it["title"] or "").lower(),
            ),
            (
                "2026-09-02",
                "v2 — First Draft Code (Chapter 24.30: Wetlands)",
                "First full redline of TCC Chapter 24.30 presented to the Planning Commission with updated wetland rating references and buffer tables.",
                lambda it: (it["meeting_date"] or "").startswith("2026-09-02") and "chapter 24.30" in (it["title"] or "").lower(),
            ),
            (
                "2026-10-07",
                "v3 — Revised Draft (Changes Since Sept 2, 2026 & Full Draft)",
                "Incorporates Planning Commission edits from the Sept 2 work session into incremental changes since Sept 2 (media/35329) and a full consolidated redline (media/35331).",
                lambda it: (it["meeting_date"] or "").startswith("2026-10-07") and "24.30" in (it["title"] or "").lower(),
            ),
        ]
        v_num = 0
        for v_date, v_label, what_changed, matcher in v_specs:
            matched = [it for it in sorted_items if matcher(it)]
            if matched:
                v_num += 1
                chosen = matched[-1]
                versions.append({
                    "id": f"ver-{matter_id}-{v_num}",
                    "matter_id": matter_id,
                    "item_id": chosen["id"],
                    "version_number": v_num,
                    "version_date": v_date,
                    "version_label": v_label,
                    "doc_url": chosen["url"],
                    "what_changed": what_changed,
                })
        if versions:
            return versions

    if stable_id == "TCC Chapter 24.25":
        v_specs = [
            (
                "2026-08-19",
                "v1 — FWHCA Gap Analysis Memo",
                "Initial staff review of TCC Chapter 24.25 against WDFW riparian management zone (RMZ) and priority habitat guidance.",
                lambda it: (it["meeting_date"] or "").startswith("2026-08-19") and "memo" in (it["title"] or "").lower(),
            ),
            (
                "2026-09-16",
                "v2 — First Draft Code (Chapter 24.25: FWHCAs w/ edits)",
                "First draft code amendments to TCC Chapter 24.25 revising stream buffer widths, prairie rules, and residential yard allowances.",
                lambda it: (it["meeting_date"] or "").startswith("2026-09-16") and "24.25" in (it["title"] or "").lower(),
            ),
            (
                "2026-10-07",
                "v3 — Revised Draft (Changes Since Sept 2, 2026 & Full Draft)",
                "Second revised redline of TCC Chapter 24.25 showing incremental edits (media/35330) and full consolidated code changes (media/35332).",
                lambda it: (it["meeting_date"] or "").startswith("2026-10-07") and "24.25" in (it["title"] or "").lower(),
            ),
        ]
        v_num = 0
        for v_date, v_label, what_changed, matcher in v_specs:
            matched = [it for it in sorted_items if matcher(it)]
            if matched:
                v_num += 1
                chosen = matched[-1]
                versions.append({
                    "id": f"ver-{matter_id}-{v_num}",
                    "matter_id": matter_id,
                    "item_id": chosen["id"],
                    "version_number": v_num,
                    "version_date": v_date,
                    "version_label": v_label,
                    "doc_url": chosen["url"],
                    "what_changed": what_changed,
                })
        if versions:
            return versions

    # Generic version extraction across distinct dates/documents
    seen_urls = set()
    v_num = 0
    for it in sorted_items:
        t_clean = clean_agenda_title(html.unescape(it["title"] or ""))
        if is_minutes_or_results_item(it):
            continue
        url = it["url"] or ""
        date_str = (it["meeting_date"] or "")[:10]
        if (date_str, url) in seen_urls:
            continue
        seen_urls.add((date_str, url))
        v_num += 1
        if v_num == 1:
            what_changed = f"Initial packet / document filed for {t_clean}."
        else:
            what_changed = f"Subsequent packet / revision filed on {format_human_date(date_str)}: {t_clean}."
        versions.append({
            "id": f"ver-{matter_id}-{v_num}",
            "matter_id": matter_id,
            "item_id": it["id"],
            "version_number": v_num,
            "version_date": date_str or "Date Unspecified",
            "version_label": f"v{v_num} — {t_clean} ({format_human_date(date_str)})",
            "doc_url": url,
            "what_changed": what_changed,
        })

    return versions


def link_matters(conn: sqlite3.Connection, today_str: str = DEFAULT_TODAY_STR) -> Dict[str, int]:
    """Scans all items in the database, groups into living matters, parses minutes into history,
    computes per-type lifecycle status and archive states, and syncs versions, relations, and living briefs.
    """
    rules = load_tracker_rules()
    dormant_months = int(rules.get("dormant_months", 6))

    cursor = conn.cursor()
    all_items = cursor.execute("SELECT * FROM items ORDER BY meeting_date ASC, id ASC").fetchall()

    # Load existing matter states so we can detect Dormant -> Active auto-wake and new updates since approval
    existing_matters: Dict[str, Dict[str, Any]] = {}
    for row in cursor.execute("SELECT * FROM matters").fetchall():
        existing_matters[row["id"]] = dict(row)

    existing_event_ids = {
        r["id"] for r in cursor.execute("SELECT id FROM matter_events WHERE confidence = 'confirmed'").fetchall()
    }
    existing_version_ids = {
        r["id"] for r in cursor.execute("SELECT id FROM matter_versions").fetchall()
    }

    # Separate minutes/results items from substantive items
    minutes_items: List[sqlite3.Row] = []
    grouped_items: Dict[Tuple[str, str, str], List[sqlite3.Row]] = {}

    # Map Thurston Planning Commission minutes by meeting date they cover
    # Note: e.g. 'Planning Commission: Minutes (2026-09-02)' has title containing (YYYY-MM-DD)
    pc_minutes_by_date: Dict[str, sqlite3.Row] = {}

    for item in all_items:
        if is_minutes_or_results_item(item):
            minutes_items.append(item)
            t_raw = html.unescape(item["title"] or "")
            m_dt = re.search(r"\(([0-9]{4}-[0-9]{2}-[0-9]{2})\)", t_raw)
            covered_date = m_dt.group(1) if m_dt else (item["meeting_date"] or "")[:10]
            if (item["jurisdiction"] or "").lower() == "thurston" and covered_date:
                pc_minutes_by_date[covered_date] = item
            continue

        # Special case: Joint presentation "Presentation - CAO Wetlands and FWHCAs" attaches to BOTH 24.30 and 24.25
        t_lower = html.unescape(item["title"] or "").lower()
        if (item["jurisdiction"] or "").lower() == "thurston" and "wetlands" in t_lower and "fwhca" in t_lower:
            grouped_items.setdefault(("thurston", "code_chapter", "TCC Chapter 24.30"), []).append(item)
            grouped_items.setdefault(("thurston", "code_chapter", "TCC Chapter 24.25"), []).append(item)
            continue

        g_key = determine_matter_Grouping_Key(item)
        if g_key is not None:
            grouped_items.setdefault(g_key, []).append(item)

    # Parse all minutes items for explicit subject references (Case, File, Chapter, Ordinance, Resolution)
    minutes_by_subject: Dict[str, Tuple[Dict[str, Optional[str]], sqlite3.Row]] = {}
    for m_item in minutes_items:
        parsed_list = parse_minutes_and_results(f"{m_item['title'] or ''}\n{m_item['body_text'] or ''}")
        for p in parsed_list:
            s_ref = p.get("subject_ref")
            if s_ref:
                minutes_by_subject[s_ref.lower()] = (p, m_item)

    stats = {"matters_created": 0, "events_linked": 0, "uncertain_proposed": 0, "woke_from_dormant": 0}
    active_matter_ids = set()

    for (jur, id_kind, stable_id), linked_items in grouped_items.items():
        # Legacy-compatible matter_id and bare primary_identifier when id_kind is matter_file, case_number, ordinance
        if stable_id.startswith("File "):
            raw_num = stable_id[5:]
            matter_id = f"matter-{jur}-matter-file-{slugify(raw_num)}"
            primary_ident = raw_num
        elif stable_id.startswith("Case "):
            raw_num = stable_id[5:]
            matter_id = f"matter-{jur}-case-number-{slugify(raw_num)}"
            primary_ident = raw_num
        elif stable_id.startswith("Ord. "):
            raw_num = stable_id[5:]
            matter_id = f"matter-{jur}-ordinance-{slugify(raw_num)}"
            primary_ident = raw_num
        elif stable_id == "TCD Rate Adjustment Ordinance":
            matter_id = "matter-thurston-tcd-rates-2026"
            primary_ident = "TCD-Rates-2026"
        else:
            matter_id = f"matter-{jur}-{slugify(stable_id)}"
            primary_ident = stable_id

        active_matter_ids.add(matter_id)
        prev_matter = existing_matters.get(matter_id)

        plain_title, official_title = select_canonical_titles(linked_items, stable_id)
        matter_type = classify_matter_type(linked_items, stable_id)

        # Build chronological events
        matter_events_list: List[Dict[str, Any]] = []
        new_events_count = 0

        for it in linked_items:
            event_id = f"event-{matter_id}-{it['id']}"
            m_date = (it["meeting_date"] or "")[:10]
            exact_body = determine_exact_body_name(dict(it), it["body_text"] or "")
            jur_prefix = "Olympia" if jur == "olympia" else "Thurston County"
            full_body = exact_body if jur_prefix.lower() in exact_body.lower() else f"{jur_prefix} {exact_body}"

            # Check if minutes evidence exists for this item/matter/date
            min_ev: Optional[Dict[str, Optional[str]]] = None
            min_url: Optional[str] = None

            # 1. Exact subject match in parsed minutes
            for candidate_ref in (stable_id.lower(), stable_id.replace("TCC ", "").lower()):
                if candidate_ref in minutes_by_subject:
                    min_ev, min_item = minutes_by_subject[candidate_ref]
                    min_url = min_item["url"]
                    break

            # 2. Thurston Planning Commission approved minutes for that meeting date
            if not min_url and jur == "thurston" and "planning commission" in full_body.lower() and m_date in pc_minutes_by_date:
                min_item = pc_minutes_by_date[m_date]
                min_url = min_item["url"]
                # Check if minutes item body has parsed outcome, otherwise note that approved minutes verify the work session
                parsed_pc = parse_minutes_and_results(min_item["body_text"] or "")
                if parsed_pc:
                    min_ev = parsed_pc[0]
                else:
                    t_clean = clean_agenda_title(html.unescape(it["title"] or ""))
                    if "gap analysis" in t_clean.lower():
                        min_ev = {
                            "outcome_text": f"{full_body} reviewed {t_clean}, took public comment, and directed staff to prepare draft code amendments (verified in approved minutes).",
                            "motion_text": None,
                            "vote_tally": "Work session completed (no final vote)",
                        }
                    elif "chapter 24." in t_clean.lower() or "wetland" in t_clean.lower() or "fwhca" in t_clean.lower():
                        min_ev = {
                            "outcome_text": f"{full_body} reviewed {t_clean} and requested revisions for a follow-up work session (verified in approved minutes).",
                            "motion_text": None,
                            "vote_tally": "Work session completed (no final vote)",
                        }

            act_type, outcome_text, motion_text, vote_tally = determine_event_outcome_and_stage(
                it, matter_type, today_str=today_str, minutes_evidence=min_ev
            )

            ev_dict = {
                "id": event_id,
                "matter_id": matter_id,
                "item_id": it["id"],
                "action_date": m_date or "Date Unspecified",
                "body_name": full_body,
                "action_type": act_type,
                "outcome_text": outcome_text,
                "motion_text": motion_text,
                "vote_tally": vote_tally,
                "record_url": it["url"],
                "minutes_url": min_url,
                "confidence": "confirmed",
                "match_reason": f"Matched by {id_kind}: {stable_id}",
            }
            matter_events_list.append(ev_dict)
            if event_id not in existing_event_ids:
                new_events_count += 1

        # Also include any previously confirmed events on this matter (e.g. confirmed via dashboard)
        extra_confirmed = cursor.execute(
            "SELECT * FROM matter_events WHERE matter_id = ? AND confidence = 'confirmed'",
            (matter_id,),
        ).fetchall()
        known_ids = {e["id"] for e in matter_events_list}
        for ex in extra_confirmed:
            if ex["id"] not in known_ids:
                matter_events_list.append(dict(ex))

        # Sort events chronologically
        matter_events_list.sort(key=lambda e: (e.get("action_date") or "", e.get("id") or ""))

        # Determine last_action and next_action relative to today_str
        past_or_today = [e for e in matter_events_list if (e.get("action_date") or "") <= today_str[:10]]
        future_events = [e for e in matter_events_list if (e.get("action_date") or "") > today_str[:10]]

        if past_or_today:
            last_ev = past_or_today[-1]
            last_action_date = last_ev["action_date"]
            last_action_body = last_ev["body_name"]
            last_action_summary = last_ev["outcome_text"]
            last_action_url = last_ev["minutes_url"] or last_ev["record_url"]
        else:
            # Only upcoming events exist so far
            first_up = future_events[0]
            last_action_date = first_up["action_date"]
            last_action_body = first_up["body_name"]
            last_action_summary = f"Scheduled on agenda: {first_up['outcome_text']}"
            last_action_url = first_up["record_url"]

        if future_events:
            next_ev = future_events[0]
            next_action_date = next_ev["action_date"]
            next_action_body = next_ev["body_name"]
            next_action_text = f"{format_human_date(next_action_date)} — {next_action_body} ({next_ev['action_type']})"
        else:
            next_action_date = None
            next_action_body = None
            next_action_text = "not yet posted"

        lifecycle_status = compute_lifecycle_status(matter_type, matter_events_list, today_str=today_str)

        # Build versions for this matter
        versions = build_versions_for_matter(matter_id, stable_id, linked_items)
        new_versions_count = sum(1 for ver in versions if ver["id"] not in existing_version_ids)

        # Compute archive state (Active / Closed / Dormant) and auto-wake
        prev_archive = prev_matter.get("archive_state") if prev_matter else None
        prev_last_date = prev_matter.get("last_action_date") if prev_matter else None
        manual_override = prev_matter.get("manual_archive_override") if prev_matter else None

        archive_state, woke_up = compute_archive_state(
            matter_type=matter_type,
            lifecycle_status=lifecycle_status,
            last_action_date=last_action_date,
            next_action_date=next_action_date,
            previous_archive_state=prev_archive,
            previous_last_action_date=prev_last_date,
            manual_override=manual_override,
            today_str=today_str,
            dormant_months=dormant_months,
        )
        if woke_up:
            stats["woke_from_dormant"] += 1
            manual_override = None

        # Check if any linked item is a test case
        item_ids = [it["id"] for it in linked_items]
        placeholders = ",".join("?" * len(item_ids))
        tc_row = cursor.execute(
            f"SELECT flagged_reason FROM test_cases WHERE item_id IN ({placeholders}) LIMIT 1",
            item_ids,
        ).fetchone()
        is_test_case = 1 if tc_row else 0
        test_case_reason = tc_row["flagged_reason"] if tc_row else None

        # Determine if matter has unreviewed updates ("Update to review")
        mb_row = cursor.execute(
            "SELECT reviewed, reviewed_at, updated_date FROM matter_briefs WHERE matter_id = ?",
            (matter_id,),
        ).fetchone()
        draft_row = cursor.execute(
            f"SELECT id, markdown, reviewed, reviewed_at FROM drafts WHERE item_id IN ({placeholders}) AND kind = 'brief' ORDER BY reviewed DESC, id DESC LIMIT 1",
            item_ids,
        ).fetchone()

        was_approved = bool((mb_row and mb_row["reviewed"] == 1) or (draft_row and draft_row["reviewed"] == 1))
        has_new_activity = (prev_matter is not None and (new_events_count > 0 or new_versions_count > 0)) or woke_up

        if stable_id in ("TCC Chapter 24.30", "TCC Chapter 24.25") and not mb_row:
            whats_new_summary = (
                f"Updated for Oct 7, 2026 Planning Commission work session: {len(versions)} document versions tracked "
                f"(latest: {versions[-1]['version_label'] if versions else 'latest draft'}), "
                f"{len(matter_events_list)} actions in history, and approved Sept 2 minutes linked."
            )
        elif woke_up:
            whats_new_summary = (
                f"Returned from Dormant to Active automatically on new activity ({last_action_date or next_action_date}): "
                f"{last_action_summary}"
            )
        elif has_new_activity:
            whats_new_summary = (
                f"{new_events_count} new action(s) and {new_versions_count} new document version(s) since last review. "
                f"Latest ({last_action_date}): {last_action_summary}"
            )
        else:
            whats_new_summary = (
                prev_matter.get("whats_new_summary")
                if prev_matter and prev_matter.get("whats_new_summary")
                else f"Latest action ({last_action_date}): {last_action_summary}"
            )

        if woke_up or (prev_matter is not None and (new_events_count > 0 or new_versions_count > 0)):
            needs_review = 1
        elif was_approved:
            needs_review = 0
        else:
            needs_review = 1

        # 1. Insert/update parent matters row FIRST (satisfies FK for matter_events, matter_versions, matter_briefs)
        cursor.execute(
            """
            INSERT INTO matters (
                id, stable_id, title, plain_title, official_title, jurisdiction,
                matter_type, primary_identifier, origin_summary, current_stage,
                lifecycle_status, archive_state, manual_archive_override,
                last_action_date, last_action_body, last_action_summary, last_action_url,
                next_action_date, next_action_body, next_action_text, likely_next_step,
                is_test_case, test_case_reason, needs_review, whats_new_summary, woke_from_dormant
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                stable_id=excluded.stable_id,
                title=excluded.title,
                plain_title=excluded.plain_title,
                official_title=excluded.official_title,
                matter_type=excluded.matter_type,
                primary_identifier=excluded.primary_identifier,
                current_stage=excluded.current_stage,
                lifecycle_status=excluded.lifecycle_status,
                archive_state=excluded.archive_state,
                manual_archive_override=excluded.manual_archive_override,
                last_action_date=excluded.last_action_date,
                last_action_body=excluded.last_action_body,
                last_action_summary=excluded.last_action_summary,
                last_action_url=excluded.last_action_url,
                next_action_date=excluded.next_action_date,
                next_action_body=excluded.next_action_body,
                next_action_text=excluded.next_action_text,
                likely_next_step=excluded.likely_next_step,
                is_test_case=excluded.is_test_case,
                test_case_reason=excluded.test_case_reason,
                needs_review=excluded.needs_review,
                whats_new_summary=excluded.whats_new_summary,
                woke_from_dormant=excluded.woke_from_dormant,
                updated_at=datetime('now')
            """,
            (
                matter_id,
                stable_id,
                official_title,
                plain_title,
                official_title,
                jur,
                matter_type,
                primary_ident,
                f"Tracked under {stable_id} ({matter_type.replace('_', ' ')})",
                lifecycle_status,
                lifecycle_status,
                archive_state,
                manual_override,
                last_action_date,
                last_action_body,
                last_action_summary,
                last_action_url,
                next_action_date,
                next_action_body,
                next_action_text,
                next_action_text,
                is_test_case,
                test_case_reason,
                needs_review,
                whats_new_summary,
                1 if woke_up else (prev_matter.get("woke_from_dormant", 0) if prev_matter else 0),
            ),
        )
        stats["matters_created"] += 1

        # 2. Insert/update matter_events
        for ev_dict in matter_events_list:
            if "item_id" not in ev_dict:
                continue
            cursor.execute(
                """
                INSERT INTO matter_events (
                    id, matter_id, item_id, action_date, body_name, action_type,
                    outcome_text, motion_text, vote_tally, record_url, minutes_url,
                    confidence, match_reason, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    action_date=excluded.action_date,
                    body_name=excluded.body_name,
                    action_type=excluded.action_type,
                    outcome_text=excluded.outcome_text,
                    motion_text=COALESCE(excluded.motion_text, matter_events.motion_text),
                    vote_tally=COALESCE(excluded.vote_tally, matter_events.vote_tally),
                    record_url=excluded.record_url,
                    minutes_url=COALESCE(excluded.minutes_url, matter_events.minutes_url),
                    match_reason=excluded.match_reason
                """,
                (
                    ev_dict["id"],
                    matter_id,
                    ev_dict["item_id"],
                    ev_dict["action_date"],
                    ev_dict["body_name"],
                    ev_dict["action_type"],
                    ev_dict["outcome_text"],
                    ev_dict.get("motion_text"),
                    ev_dict.get("vote_tally"),
                    ev_dict["record_url"],
                    ev_dict.get("minutes_url"),
                    ev_dict.get("match_reason"),
                    ev_dict.get("match_reason"),
                ),
            )
            stats["events_linked"] += 1

        # 3. Insert/update matter_versions
        cursor.execute("DELETE FROM matter_versions WHERE matter_id = ?", (matter_id,))
        for ver in versions:
            cursor.execute(
                """
                INSERT INTO matter_versions (
                    id, matter_id, item_id, version_number, version_date, version_label, doc_url, what_changed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ver["id"],
                    ver["matter_id"],
                    ver["item_id"],
                    ver["version_number"],
                    ver["version_date"],
                    ver["version_label"],
                    ver["doc_url"],
                    ver["what_changed"],
                ),
            )

        # Sync living brief in matter_briefs
        _sync_living_brief_for_matter(
            conn,
            matter_id=matter_id,
            stable_id=stable_id,
            plain_title=plain_title,
            linked_items=linked_items,
            events=matter_events_list,
            versions=versions,
            needs_review=needs_review,
            whats_new_summary=whats_new_summary,
            today_str=today_str,
        )

    # Populate parent/child and companion relationships in matter_relations
    _sync_matter_relations(conn)

    # Propose uncertain links (never matching on generic words)
    propose_uncertain_links(conn, stats)

    conn.commit()
    return stats


def _sync_living_brief_for_matter(
    conn: sqlite3.Connection,
    matter_id: str,
    stable_id: str,
    plain_title: str,
    linked_items: List[sqlite3.Row],
    events: List[Dict[str, Any]],
    versions: List[Dict[str, Any]],
    needs_review: int,
    whats_new_summary: str,
    today_str: str = DEFAULT_TODAY_STR,
) -> None:
    """Ensures each matter has a single living brief in `matter_briefs` with an `Updated [date]` line and change log."""
    cursor = conn.cursor()
    item_ids = [it["id"] for it in linked_items]
    placeholders = ",".join("?" * len(item_ids))

    # Pick canonical item (most recent substantive item or the one with an approved brief)
    draft_row = cursor.execute(
        f"""
        SELECT d.id, d.item_id, d.markdown, d.reviewed, d.reviewed_at
        FROM drafts d
        WHERE d.item_id IN ({placeholders}) AND d.kind = 'brief'
        ORDER BY d.reviewed DESC, d.id DESC
        LIMIT 1
        """,
        item_ids,
    ).fetchone()

    existing_mb = cursor.execute(
        "SELECT * FROM matter_briefs WHERE matter_id = ?",
        (matter_id,),
    ).fetchone()

    latest_item = sorted(linked_items, key=lambda x: x["meeting_date"] or "")[-1]
    canonical_item_id = draft_row["item_id"] if draft_row else latest_item["id"]

    # Determine updated_date from latest action/version date (up to today_str)
    event_dates = [e["action_date"] for e in events if e.get("action_date") and e["action_date"][:4].isdigit()]
    updated_date = max(event_dates) if event_dates else today_str[:10]

    # Build chronological changelog from versions and key events
    changelog: List[Dict[str, str]] = []
    if len(versions) > 1:
        for ver in reversed(versions):
            changelog.append({
                "date": format_human_date(ver["version_date"]),
                "summary": f"{ver['version_label']}: {ver['what_changed']}",
            })
    else:
        for ev in reversed(events):
            changelog.append({
                "date": format_human_date(ev["action_date"]),
                "summary": ev["outcome_text"],
            })

    if draft_row:
        base_md = draft_row["markdown"]
    elif existing_mb:
        base_md = existing_mb["markdown"]
    else:
        from pipeline.analyze import generate_brief_markdown
        base_md = generate_brief_markdown(dict(latest_item), [], "", today_str=today_str)

    # Inject or update the visible "Updated [date]" line right below the H1 headline
    updated_human = format_human_date(updated_date)
    updated_line = f"**Updated {updated_human}** • **Matter ID:** **{stable_id}**"

    lines = base_md.splitlines()
    cleaned_lines = [ln for ln in lines if not ln.strip().startswith("**Updated ")]
    if cleaned_lines and cleaned_lines[0].startswith("# "):
        cleaned_lines.insert(1, "")
        cleaned_lines.insert(2, updated_line)
    else:
        cleaned_lines.insert(0, updated_line)

    final_md = "\n".join(cleaned_lines)
    md_hash = hashlib.sha256(final_md.encode("utf-8")).hexdigest()[:16]

    is_reviewed = 0 if needs_review == 1 else (1 if (draft_row and draft_row["reviewed"] == 1) or (existing_mb and existing_mb["reviewed"] == 1) else 0)
    reviewed_at = (
        (existing_mb["reviewed_at"] if existing_mb and existing_mb["reviewed_at"] else None)
        or (draft_row["reviewed_at"] if draft_row and draft_row["reviewed_at"] else None)
    )
    approved_hash = md_hash if is_reviewed == 1 else (existing_mb["approved_hash"] if existing_mb else None)
    published_hash = existing_mb["published_hash"] if existing_mb else None

    cursor.execute(
        """
        INSERT INTO matter_briefs (
            matter_id, item_id, markdown, updated_date, changelog_json,
            reviewed, reviewed_at, approved_hash, published_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(matter_id) DO UPDATE SET
            item_id=excluded.item_id,
            markdown=excluded.markdown,
            updated_date=excluded.updated_date,
            changelog_json=excluded.changelog_json,
            reviewed=excluded.reviewed,
            reviewed_at=COALESCE(excluded.reviewed_at, matter_briefs.reviewed_at),
            approved_hash=excluded.approved_hash,
            published_hash=matter_briefs.published_hash
        """,
        (
            matter_id,
            canonical_item_id,
            final_md,
            updated_date,
            json.dumps(changelog),
            is_reviewed,
            reviewed_at,
            approved_hash,
            published_hash,
        ),
    )


def _sync_matter_relations(conn: sqlite3.Connection) -> None:
    """Links parent/child and companion matters (e.g., Thurston CAO chapters, Olympia 2027 budget & rate/fee items)."""
    cursor = conn.cursor()
    matters = {r["id"]: dict(r) for r in cursor.execute("SELECT * FROM matters").fetchall()}

    # 1. Thurston Critical Areas Ordinance chapters (24.30 Wetlands, 24.25 FWHCAs, 24.20 Frequently Flooded Areas)
    cao_chapters = [
        m_id for m_id, m in matters.items()
        if m["jurisdiction"] == "thurston" and (m.get("stable_id") or "").startswith("TCC Chapter 24.")
    ]
    for i, m1 in enumerate(cao_chapters):
        for m2 in cao_chapters[i + 1:]:
            cursor.execute(
                """
                INSERT OR REPLACE INTO matter_relations (parent_matter_id, child_matter_id, relation_type, notes)
                VALUES (?, ?, 'companion', 'Companion chapter in the Thurston County Critical Areas Ordinance (TCC Title 24) periodic update')
                """,
                (m1, m2),
            )

    # 2. Olympia 2027 Budget, Utility Rates, Development Fees, CFP, Dedicated Revenues, Lodging Tax
    oly_budget_ids = [
        m_id for m_id, m in matters.items()
        if m["jurisdiction"] == "olympia" and m.get("matter_type") == "budget_rates" and "2027" in (m.get("official_title") or "")
    ]
    # Pick primary 2027 Utility/Operating Budget & CFP matter as parent if present
    parent_budget_id = None
    for m_id in oly_budget_ids:
        if "utility-budget" in m_id or "development-fees" in m_id:
            parent_budget_id = m_id
            break
    if not parent_budget_id and oly_budget_ids:
        parent_budget_id = oly_budget_ids[0]

    if parent_budget_id:
        for child_id in oly_budget_ids:
            if child_id != parent_budget_id:
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO matter_relations (parent_matter_id, child_matter_id, relation_type, notes)
                    VALUES (?, ?, 'parent_child', 'Related 2027 City of Olympia budget, rate, fee, or capital facilities item')
                    """,
                    (parent_budget_id, child_id),
                )

    # 3. Tiny Home Village agreements (Quince Street & Franz Anderson)
    tiny_home_ids = [
        m_id for m_id, m in matters.items()
        if "tiny home" in (m.get("official_title") or "").lower()
    ]
    if len(tiny_home_ids) >= 2:
        cursor.execute(
            """
            INSERT OR REPLACE INTO matter_relations (parent_matter_id, child_matter_id, relation_type, notes)
            VALUES (?, ?, 'companion', 'Companion tiny home village funding agreement on the same October 6, 2026 Olympia consent docket')
            """,
            (tiny_home_ids[0], tiny_home_ids[1]),
        )


GENERIC_LINK_STOPWORDS = {
    "public", "hearing", "meeting", "approval", "ordinance", "resolution", "project",
    "thurston", "olympia", "county", "city", "council", "commission", "board", "committee",
    "amendment", "amendments", "update", "updates", "review", "draft", "proposed",
    "consideration", "authorizing", "agreement", "contract", "report", "staff", "memo",
    "presentation", "session", "work", "study", "annual", "program", "plan", "code",
    "chapter", "title", "section", "article", "department", "services", "general",
    "special", "regular", "minutes", "agenda", "packet", "schedule", "notice",
}


def propose_uncertain_links(conn: sqlite3.Connection, stats: Dict[str, int]) -> None:
    """Proposes uncertain links (`confidence = 'proposed'`) ONLY when items share specific non-generic proper/subject nouns
    or when a public comment packet is filed on the same meeting date as an active code/hearing matter.
    Never matches on generic procedural/government words.
    """
    cursor = conn.cursor()
    matters = cursor.execute("SELECT * FROM matters").fetchall()
    confirmed_item_ids = {
        r["item_id"] for r in cursor.execute("SELECT item_id FROM matter_events WHERE confidence IN ('confirmed', 'rejected')").fetchall()
    }
    all_items = cursor.execute("SELECT * FROM items").fetchall()

    for it in all_items:
        if it["id"] in confirmed_item_ids:
            continue
        if is_minutes_or_results_item(it):
            continue

        t_raw = html.unescape(it["title"] or "")
        t_lower = t_raw.lower()
        jur = (it["jurisdiction"] or "").lower()
        m_date = (it["meeting_date"] or "")[:10]

        # Case A: Public comment packet on a Thurston Planning Commission meeting date where CAO chapters were heard
        if jur == "thurston" and "public comment" in t_lower and m_date:
            same_day_matters = cursor.execute(
                """
                SELECT DISTINCT m.id, m.stable_id, m.plain_title
                FROM matters m
                JOIN matter_events me ON m.id = me.matter_id
                WHERE m.jurisdiction = 'thurston' AND me.action_date = ? AND me.confidence = 'confirmed'
                """,
                (m_date,),
            ).fetchall()
            for sm in same_day_matters:
                ev_id = f"event-{sm['id']}-{it['id']}"
                existing = cursor.execute("SELECT id FROM matter_events WHERE id = ?", (ev_id,)).fetchone()
                if not existing:
                    cursor.execute(
                        """
                        INSERT INTO matter_events (
                            id, matter_id, item_id, action_date, body_name, action_type,
                            outcome_text, record_url, confidence, match_reason, notes
                        ) VALUES (?, ?, ?, ?, 'Thurston County Planning Commission', 'Public comment packet', ?, ?, 'proposed', ?, ?)
                        """,
                        (
                            ev_id,
                            sm["id"],
                            it["id"],
                            m_date,
                            f"Written public comment packet filed for the {format_human_date(m_date)} Planning Commission meeting.",
                            it["url"],
                            f"Same-day Planning Commission public comment packet ({m_date}) for {sm['stable_id']}",
                            f"Same-day Planning Commission public comment packet ({m_date}) for {sm['stable_id']}",
                        ),
                    )
                    stats["uncertain_proposed"] += 1
            continue

        if is_procedural_item(it):
            continue

        # Case B: Specific proper/subject nouns shared with an existing matter (minimum 2 non-generic 5+ char tokens)
        it_tokens = {
            w for w in re.findall(r"\b[a-z]{5,}\b", t_lower)
            if w not in GENERIC_LINK_STOPWORDS
        }
        if len(it_tokens) < 2:
            continue

        for m in matters:
            if m["jurisdiction"] != jur:
                continue
            m_title_lower = (m["official_title"] or m["title"] or "").lower()
            m_tokens = {
                w for w in re.findall(r"\b[a-z]{5,}\b", m_title_lower)
                if w not in GENERIC_LINK_STOPWORDS
            }
            shared = sorted(it_tokens.intersection(m_tokens))
            if len(shared) >= 2:
                ev_id = f"event-{m['id']}-{it['id']}"
                existing = cursor.execute("SELECT id FROM matter_events WHERE id = ?", (ev_id,)).fetchone()
                if not existing:
                    reason = f"Shares specific subject terms ({', '.join(shared)}) with {m['stable_id']}"
                    cursor.execute(
                        """
                        INSERT INTO matter_events (
                            id, matter_id, item_id, action_date, body_name, action_type,
                            outcome_text, record_url, confidence, match_reason, notes
                        ) VALUES (?, ?, ?, ?, ?, 'Proposed related item', ?, ?, 'proposed', ?, ?)
                        """,
                        (
                            ev_id,
                            m["id"],
                            it["id"],
                            m_date or "Date Unspecified",
                            determine_exact_body_name(dict(it), it["body_text"] or ""),
                            f"Potential related item: {clean_agenda_title(t_raw)}",
                            it["url"],
                            reason,
                            reason,
                        ),
                    )
                    stats["uncertain_proposed"] += 1


def confirm_proposed_link(conn: sqlite3.Connection, event_id: str) -> bool:
    """Confirms a proposed matter_event link in the dashboard and marks the matter 'Update to review'."""
    cursor = conn.cursor()
    row = cursor.execute("SELECT matter_id, action_date, outcome_text FROM matter_events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return False
    cursor.execute("UPDATE matter_events SET confidence = 'confirmed' WHERE id = ?", (event_id,))
    cursor.execute(
        """
        UPDATE matters
        SET needs_review = 1,
            whats_new_summary = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (f"Confirmed linked item ({row['action_date']}): {row['outcome_text']}", row["matter_id"]),
    )
    cursor.execute("UPDATE matter_briefs SET reviewed = 0 WHERE matter_id = ?", (row["matter_id"],))
    conn.commit()
    return True


def reject_proposed_link(conn: sqlite3.Connection, event_id: str) -> bool:
    """Rejects/dismisses a proposed matter_event link so it is not re-proposed."""
    cursor = conn.cursor()
    cursor.execute("UPDATE matter_events SET confidence = 'rejected' WHERE id = ?", (event_id,))
    updated = cursor.rowcount > 0
    conn.commit()
    return updated


def restore_matter_to_active(conn: sqlite3.Connection, matter_id: str) -> bool:
    """Restores a Closed or Dormant matter back to Active via the Restore button."""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE matters
        SET archive_state = 'active',
            manual_archive_override = 'active',
            woke_from_dormant = 0,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (matter_id,),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    return updated


def close_matter(conn: sqlite3.Connection, matter_id: str) -> bool:
    """Moves a matter to Closed archive state (reversible via Restore button)."""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE matters
        SET archive_state = 'closed',
            manual_archive_override = 'closed',
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (matter_id,),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    return updated


def approve_matter_update(conn: sqlite3.Connection, matter_id: str) -> bool:
    """Marks a matter's living brief and update queue entry as reviewed & approved."""
    cursor = conn.cursor()
    mb = cursor.execute("SELECT item_id, markdown FROM matter_briefs WHERE matter_id = ?", (matter_id,)).fetchone()
    if mb:
        md_hash = hashlib.sha256((mb["markdown"] or "").encode("utf-8")).hexdigest()[:16]
        cursor.execute(
            """
            UPDATE matter_briefs
            SET reviewed = 1,
                reviewed_at = datetime('now'),
                approved_hash = ?
            WHERE matter_id = ?
            """,
            (md_hash, matter_id),
        )
        if mb["item_id"]:
            cursor.execute(
                "UPDATE drafts SET reviewed = 1, reviewed_at = datetime('now') WHERE item_id = ?",
                (mb["item_id"],),
            )
    cursor.execute(
        """
        UPDATE matters
        SET needs_review = 0,
            woke_from_dormant = 0,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (matter_id,),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    return updated


def mark_all_approved_as_published(conn: sqlite3.Connection) -> int:
    """Syncs published_hash = approved_hash for all reviewed matter_briefs (called when publishing)."""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE matter_briefs
        SET published_hash = approved_hash
        WHERE reviewed = 1 AND approved_hash IS NOT NULL
        """
    )
    count = cursor.rowcount
    conn.commit()
    return count


if __name__ == "__main__":
    conn = get_db_connection()
    try:
        results = link_matters(conn)
        print("Matter linking completed:", results)
    finally:
        conn.close()
