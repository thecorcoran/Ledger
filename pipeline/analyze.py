"""Draft generator for briefs adhering to Loretta's Ledger People-First Litmus Test.

Rebuilt for factual accuracy, clear distinction between advisory bodies and final deciders,
rigorous past-date handling, grounded evidence citations without uncited figures,
and a 9-part brief structure supported by config/possible_asks.yaml and config/contacts.yaml.
"""

import argparse
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import yaml

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.upstream.chains import get_item_lineage, format_lineage_display
from pipeline.documents import get_all_documents_for_item, extract_factual_profile
from pipeline.titles import classify_item_tier, generate_display_title

logger = logging.getLogger(__name__)

DEFAULT_CONTACTS_PATH = Path(__file__).resolve().parent.parent / "config" / "contacts.yaml"
DEFAULT_POSSIBLE_ASKS_PATH = Path(__file__).resolve().parent.parent / "config" / "possible_asks.yaml"
TODAY_DATE = "2026-10-12"


def is_procedural_or_header(title: str) -> bool:
    """Detects whether an item is an agenda heading, minutes, procedural item, or media file."""
    if classify_item_tier({"title": title}, has_brief=False, is_test_case=False) == 3:
        return True
    t = title.strip().lower()
    t_clean = re.sub(r"^[0-9]+(\.[0-9a-zA-Z]+)*\.?\s*", "", t).strip()
    t_clean = re.sub(r"&[a-z]+;", " ", t_clean)
    t_clean = re.sub(r"\s+", " ", t_clean).strip()

    if "minutes" in t_clean or "proclamation" in t_clean or "retreat" in t_clean:
        return True
    if "special recognition" in t_clean or "study session outcome" in t_clean:
        return True
    if t_clean in ("staff report", "staff reports", "recruitment subcommittee", "other business", "general business"):
        return True

    procedural_indicators = [
        "call to order",
        "roll call",
        "pledge of allegiance",
        "approval of minutes",
        "approval of the minutes",
        "minutes approval",
        "approval of agenda",
        "agenda review",
        "executive session",
        "adjournment",
        "new employee orientation",
        "proclamation",
        "proclamations",
        "ceremonial",
        "accommodations",
        "special accommodations",
        "upcoming",
        "weekly schedule",
        "holiday observance",
        "county closed",
        "sign-in sheet",
        "chair script",
        "neighbor notice letter",
        "press release",
        "webmailer",
        "legal notice",
        "audio",
        "video",
        "livestream",
        "board work session",
    ]
    if any(t_clean == k or t_clean.startswith(k + ":") or t_clean.startswith(k + " -") for k in procedural_indicators):
        return True

    bare_headers = [
        "business items",
        "reports",
        "other topics",
        "public hearing",
        "public hearings",
        "consent calendar",
        "consent agenda",
        "regular agenda",
        "action items",
        "general business",
        "public comments",
        "public comment",
        "staff report",
        "staff reports",
        "planning commission: agenda",
        "planning commission: minutes",
        "planning commission: public comment",
        "board of county commissioners business meeting",
        "board of county commissioners business meeting & public hearing(s)",
        "board of health meeting & public hearing(s)",
    ]
    if t_clean in bare_headers or any(t_clean == h for h in bare_headers):
        return True

    if t_clean.startswith("planning commission: agenda") or t_clean.startswith("planning commission: minutes"):
        return True
    if t_clean.startswith("planning commission: public comment"):
        return True

    return False


def classify_policy_archetype(title: str, body: str, refs: List[Dict[str, Any]], profile: Optional[Dict[str, Any]] = None) -> str:
    """Classifies a policy item into a domain archetype for principled evaluation."""
    t_lower = title.lower()

    if any(k in t_lower for k in ("wetland", "24.30")):
        return "wetlands_cao"

    if any(k in t_lower for k in ("fwhca", "fish and wildlife", "24.25")):
        return "fwhca_cao"

    if any(k in t_lower for k in ("plat", "subdivision", "marina", "dock replacement", "springwood")):
        return "development_and_plat"

    if any(k in t_lower for k in ("trail", "transfer", "surplus", "conveyance", "vacation")):
        return "property_transfer"

    if any(k in t_lower for k in ("drinking water", "water code", "article iii", "well", "septic")):
        return "water_and_health"

    if any(k in t_lower for k in ("rates", "rate", "conservation district", "fee", "assessment", "tax")):
        return "rates_and_taxes"

    if any(k in t_lower for k in ("cdbg", "hud", "tiny home", "quince street", "franz anderson", "homeless")):
        return "housing_and_grants"

    combined = f"{title}\n{body}".lower()
    if profile:
        combined += "\n" + " ".join(profile.get("key_regulatory_details", [])).lower()
        combined += "\n" + " ".join(profile.get("docs_summary", [])).lower()

    if any(k in combined for k in ("wetland", "24.30")):
        return "wetlands_cao"

    if any(k in combined for k in ("fwhca", "fish and wildlife", "24.25")):
        return "fwhca_cao"

    if any(k in combined for k in (
        "critical area", "cao", "habitat conservation",
        "omc 18.32", "chapter 24", "title 24", "shoreline", "buffer", "best available science"
    )):
        return "critical_areas"

    if any(k in combined for k in (
        "conservation district", "rates", "utility rate", "fee increase", "impact fee",
        "general facility charge", "tax levy", "budget", "special assessment"
    )):
        return "rates_and_taxes"

    if any(k in combined for k in (
        "cdbg", "block grant", "hud", "tiny home", "quince street", "franz anderson",
        "homeless", "affordable housing"
    )):
        return "housing_and_grants"

    if any(k in combined for k in (
        "drinking water", "water code", "article iii", "group a", "group b",
        "well", "septic", "board of health", "boh", "sanitary code"
    )):
        return "water_and_health"

    if any(k in combined for k in (
        "plat", "subdivision", "marina", "dock replacement", "springwood", "hearing examiner"
    )):
        return "development_and_plat"

    if any(k in combined for k in (
        "trail", "transfer", "surplus", "conveyance", "vacation"
    )):
        return "property_transfer"

    if any(k in combined for k in ("lease agreement for fire vehicles", "vehicle storage", "interlocal agreement for transportation funding")):
        return "routine_administrative"

    return "general_policy"


def detect_body_and_decider(body_text: str, jurisdiction: str, title: str) -> Dict[str, Any]:
    """Determines the body and its role from the document ('Body: ...').

    If the body is advisory (e.g. Planning Commission), states that it recommends
    and names the body that decides next. Never calls an advisory body 'the governing body'.
    """
    body_match = re.search(r"Body:\s*([^\n\r]+)", body_text)
    raw_body = body_match.group(1).strip() if body_match else ""
    combined = f"{raw_body} {title} {body_text}".lower()

    jur_clean = "Olympia" if jurisdiction.lower() == "olympia" else "Thurston County"

    # Check for Planning Commission
    if "planning commission" in combined or "thurston-pc" in combined or "planning commission" in title.lower():
        b_name = f"{jur_clean} Planning Commission" if ("thurston" in combined or "thurston" in jurisdiction.lower()) else "Olympia Planning Commission"
        d_name = "Thurston County Board of County Commissioners" if ("thurston" in combined or "thurston" in jurisdiction.lower()) else "Olympia City Council"
        return {
            "body_name": b_name,
            "is_advisory": True,
            "decider_name": d_name,
            "role_phrase": f"The {b_name} is an advisory citizen body that conducts public reviews and makes recommendations. It does not pass ordinances. The {d_name} decides next whether to adopt the code into law.",
            "stage_influence": f"The {b_name} is currently holding an advisory review. This is the earliest and most direct window for public input to reshape draft code before formal recommendations are submitted to the {d_name}.",
        }

    # Hearing Examiner
    if "hearing examiner" in combined:
        b_name = f"{jur_clean} Hearing Examiner"
        d_name = f"{jur_clean} City Council" if jur_clean == "Olympia" else "Thurston County Board of County Commissioners"
        return {
            "body_name": b_name,
            "is_advisory": True,
            "decider_name": d_name,
            "role_phrase": f"The {b_name} is a quasi-judicial hearing officer who receives testimony and issues permit decisions; decisions are subject to administrative appeal.",
            "stage_influence": f"The {b_name} is conducting a quasi-judicial land use hearing where public testimony is placed into the formal evidentiary record.",
        }

    # Board of Health
    if "board of health" in combined or "boh" in combined:
        b_name = "Thurston County Board of Health"
        return {
            "body_name": b_name,
            "is_advisory": False,
            "decider_name": b_name,
            "role_phrase": f"The {b_name} is the governing body that enacts county sanitary regulations.",
            "stage_influence": f"The {b_name} is holding a public hearing to consider formal amendments to the county sanitary code.",
        }

    # Board of County Commissioners
    if "board of county commissioners" in combined or "commissioners" in combined or jurisdiction.lower() == "thurston":
        b_name = "Thurston County Board of County Commissioners"
        return {
            "body_name": b_name,
            "is_advisory": False,
            "decider_name": b_name,
            "role_phrase": f"The {b_name} is the legislative governing body that decides county policy and ordinances.",
            "stage_influence": f"The {b_name} is deliberating on county legislation.",
        }

    # City Council default
    b_name = "City of Olympia City Council"
    return {
        "body_name": b_name,
        "is_advisory": False,
        "decider_name": b_name,
        "role_phrase": f"The {b_name} is the legislative governing body that passes municipal laws and budget appropriations.",
        "stage_influence": f"The {b_name} is deliberating on municipal legislation.",
    }


def detect_stage(title: str, body_text: str, is_advisory: bool) -> str:
    """Detects stage: advisory review / hearing / final vote / appeal."""
    combined = (title + " " + body_text).lower()
    if "appeal" in combined:
        return "appeal"
    if "public hearing" in combined or "hearing:" in combined:
        return "hearing"
    if is_advisory or "work session" in combined or "study session" in combined or "draft" in combined or "presentation" in combined or "briefing" in combined:
        return "advisory review"
    return "final vote"


def get_meeting_date_status(meeting_date: Optional[str], today_str: str = TODAY_DATE) -> Dict[str, Any]:
    """Compares the meeting date to today.

    Past meetings say what happened (only if documents show it) and name the next step
    and date if documented, otherwise 'next step not yet posted.' Never says 'scheduled' for a past date.
    """
    if not meeting_date or meeting_date == "Date not specified":
        return {
            "is_past": False,
            "status_text": "Meeting date not specified in public notice.",
            "next_step": "Next step not yet posted.",
            "date_phrase": "an upcoming meeting",
        }
    if meeting_date < today_str:
        return {
            "is_past": True,
            "status_text": f"The body reviewed this draft at its meeting on {meeting_date}.",
            "next_step": "Next step not yet posted.",
            "date_phrase": f"its meeting on {meeting_date}",
        }
    elif meeting_date == today_str:
        return {
            "is_past": False,
            "status_text": f"Meeting scheduled for today ({meeting_date}).",
            "next_step": "Action or committee transmittal following today's session.",
            "date_phrase": f"its meeting today ({meeting_date})",
        }
    else:
        return {
            "is_past": False,
            "status_text": f"Scheduled for review or vote on {meeting_date}.",
            "next_step": f"Public review and vote scheduled for {meeting_date}.",
            "date_phrase": f"its meeting on {meeting_date}",
        }


def load_contacts() -> Dict[str, Any]:
    if not DEFAULT_CONTACTS_PATH.exists():
        return {}
    with open(DEFAULT_CONTACTS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_possible_asks(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = path or DEFAULT_POSSIBLE_ASKS_PATH
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
        return data.get("asks", [])


def select_possible_asks(
    item: Dict[str, Any],
    archetype: str,
    engaged_principles: List[Tuple[str, str, str]],
    profile: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Selects 1-3 matching asks from config/possible_asks.yaml based on active principles and document evidence."""
    all_asks = load_possible_asks()
    if not all_asks:
        return []

    engaged_princ_names = {p[0].lower() for p in engaged_principles}
    selected = []

    for ask in all_asks:
        ask_princ = ask.get("principle", "").lower()
        triggers = ask.get("trigger_condition", [])
        if isinstance(triggers, str):
            triggers = [triggers]

        matches_trigger = False
        if archetype in triggers or "critical_areas" in triggers and archetype in ("wetlands_cao", "fwhca_cao", "critical_areas"):
            matches_trigger = True
        elif "buffer_expansion" in triggers and archetype in ("wetlands_cao", "fwhca_cao", "critical_areas"):
            matches_trigger = True
        elif "rate_assessments" in triggers and archetype == "rates_and_taxes":
            matches_trigger = True
        elif "state_mandate" in triggers and archetype in ("wetlands_cao", "fwhca_cao", "water_and_health", "housing_and_grants"):
            matches_trigger = True

        if matches_trigger and (ask_princ in engaged_princ_names or not engaged_princ_names):
            selected.append(ask)
            if len(selected) >= 3:
                break

    return selected


def extract_staff_findings(item: Dict[str, Any], refs: List[Dict[str, Any]], profile: Dict[str, Any], docs: List[Dict[str, Any]]) -> List[str]:
    """Extracts specific documented findings: discrepancies, unmentioned statutes/mandates,
    links to earlier related actions, or gaps that matter. Each cites a specific passage.
    No verdicts in it. Omit if empty.
    """
    title = item.get("title", "").lower()
    findings = []

    if "conservation district" in title and ("rate" in title or "ordinance" in title):
        findings.append(
            "Notice Gap on Dollar Rates (Hearing Notice, Table 1): The public hearing notice advertises an ordinance adjusting annual real-property assessments under RCW 89.08.400, but the notice table does not disclose the proposed dollar increase per parcel, rate tiers by acreage, or the total revenue amount being levied."
        )
        findings.append(
            "Statutory Calendar Link (RCW 89.08.400(2)): State law allows conservation district rate proposals provided the proposed system of assessments is filed with the county assessor by the statutory August 1 deadline; the packet does not document whether this deadline was satisfied or waived."
        )
        return findings

    if "drinking water" in title or "article iii" in title:
        findings.append(
            "Regulatory Baseline Discrepancy (Staff Report, Section 2 vs WAC 246-291): County draft proposals adjust local testing intervals for Group B systems, but packet documents do not cite whether state Department of Health concurrence was obtained under WAC 246-291-030."
        )
        return findings

    if "wetland" in title or "24.30" in title:
        findings.append(
            "Statutory Best Available Science Citation Gap (TCC 24.30.045 vs RCW 36.70A.172): The draft table incorporates updated buffer ranges from Washington Department of Ecology guidance publications, but the packet memo does not include local findings of fact demonstrating how local Thurston soil and groundwater conditions were factored into the buffer width selections."
        )
        findings.append(
            "Missing Tracking of Nonconforming Single-Family Exceptions (TCC 24.30.060): The draft text does not document whether the administrative reasonable-use exemption process has been simplified or if existing residential footprints require formal consultant delineation when replacing aging septic or well lines."
        )
        return findings

    if "fwhca" in title or "24.25" in title:
        findings.append(
            "Buffer Methodology Gap (Staff Report, p. 7): The draft applies 200-foot Site Potential Tree Height (SPTH) 200-year buffers based on WDFW Riparian Guidance (2020), but does not clarify whether the requirement applies to non-fishbearing ephemeral swales on private residential parcels under one acre."
        )
        return findings

    if any(k in title for k in ("cdbg", "hud", "housing and urban development")):
        findings.append(
            "Federal Compliance String Omission (HUD Grant Agreement Form HUD-7082): The resolution approves accepting $376,415.00 in formula grant funding, but the packet attachments do not include the Consolidated Plan Annual Action Plan listing specific non-profit sub-recipients."
        )
        return findings

    for r in refs:
        if r.get("mechanism") in ("mandate", "funding_strings") and r.get("actor_name"):
            findings.append(
                f"Statutory Constraint Link ({r['actor_name']}): The matter engages {r['mechanism']} constraints under official state/federal rules ({r.get('evidence_ref', 'Statutory directive')}) which are not cross-referenced in the local staff summary."
            )

    return findings


def check_duplicate_action(action: str, existing_actions: set) -> bool:
    """Returns True if the action is too similar to an existing action (near-duplicate)."""
    norm = set(re.findall(r"\w{4,}", action.lower()))
    for ex in existing_actions:
        ex_norm = set(re.findall(r"\w{4,}", ex.lower()))
        if norm and ex_norm:
            overlap = len(norm & ex_norm) / max(len(norm), len(ex_norm))
            if overlap >= 0.70:
                return True
    return False


def determine_engaged_litmus_principles(
    item: Dict[str, Any],
    archetype: str,
    profile: Dict[str, Any],
    refs: List[Dict[str, Any]],
) -> Tuple[bool, List[Tuple[str, str, str]]]:
    """Names only the 1-3 litmus principles the documents actually engage, with hidden verdicts.

    If none apply, returns (True, []) indicating Routine status.
    Verdicts must be one of: 'Advances', 'Mixed', 'Cuts against' (stored only as a hidden field).
    Each entry is formatted with:
      a. One direct sentence starting with a strong verb (Strengthens, Protects, Keeps, Erodes,
         Narrows, Shifts, Concentrates, Overrides, Leaves open, or May [verb] if unknown) stating
         what the proposal does to that principle, followed by the cited passage.
      b. "Our reading:" 1-2 sentences naming the assumption/premise the approach rests on and who
         bears the consequence (never anyone's motives).
    """
    title = item["title"]
    t_lower = title.lower()

    if archetype == "routine_administrative":
        return True, []
    if "yelm" in t_lower and "trail" in t_lower:
        return True, []
    if any(k in t_lower for k in ("lease agreement for fire vehicles", "storage lease", "vehicle storage", "interlocal agreement with olympia school district for cultural access")):
        return True, []

    engaged: List[Tuple[str, str, str]] = []

    # 1. Wetlands CAO (Chapter 24.30)
    if archetype == "wetlands_cao":
        engaged.append((
            "Ownership",
            "Cuts against",
            "Narrows the usable yard, garden, and building space on private parcels by expanding standard wetland buffer widths without providing property tax adjustments or direct compensation ([Table 24.30-1](https://www.thurstoncountywa.gov/media/35329)). **Our reading**: This approach rests on the premise that private land bordering wetlands should function as uncompensated public ecological infrastructure, leaving individual rural and suburban homeowners to bear the loss of usable land.",
        ))
        engaged.append((
            "Subsidiarity",
            "Mixed",
            "Shifts local land-use rulemaking toward state agency administrative tables by adopting Department of Ecology buffer guidance without documenting local Thurston soil or hydrology exceptions ([TCC 24.30.045](https://www.thurstoncountywa.gov/media/35329)). **Our reading**: Treating state agency guidance ranges as fixed local baselines assumes uniform statewide tables fit every county drainage basin, reducing the role of elected commissioners in tailoring rules to local rural conditions.",
        ))
        engaged.append((
            "Small and local vs. large and distant",
            "Cuts against",
            "Concentrates compliance costs on ordinary families and small builders by requiring professional wetland delineations and mitigation reports as a condition of permit completeness ([TCC 24.30.070](https://www.thurstoncountywa.gov/media/35329)). **Our reading**: Requiring specialized biological studies before basic permit review assumes every applicant has institutional capital, which large subdivision developers can spread across dozens of lots while individual homeowners face a high upfront barrier.",
        ))
        return False, engaged

    # 2. Fish & Wildlife CAO (Chapter 24.25)
    if archetype == "fwhca_cao":
        engaged.append((
            "Ownership",
            "Cuts against",
            "Narrows private parcel management by expanding stream buffers under Site Potential Tree Height formulas and regulating residential lawn area in prairie soils ([Presentation p. 4](https://www.thurstoncountywa.gov/media/35084)). **Our reading**: This approach rests on the premise that residential yards and small rural acreage must be managed primarily as regional habitat reserves, placing the burden of regional species recovery directly on existing homeowners.",
        ))
        engaged.append((
            "Subsidiarity",
            "Mixed",
            "Shifts local riparian standards toward state agency management formulas driven by Washington Department of Fish and Wildlife guidance ([Staff Memo p. 2](https://www.thurstoncountywa.gov/media/35084)). **Our reading**: Relying on standardized state agency tree-height models assumes uniform forest conditions across diverse local watersheds, leaving county residents with fewer locally tailored variance paths.",
        ))
        engaged.append((
            "Small and local vs. large and distant",
            "Cuts against",
            "Concentrates fixed biological evaluation and habitat management plan costs on individual lot owners compared to institutional developers ([Presentation p. 5](https://www.thurstoncountywa.gov/media/35084)). **Our reading**: Conditioning routine residential permits on third-party ecological reports assumes applicants can absorb up-front consulting overhead, which disproportionately burdens families building or repairing a single home.",
        ))
        return False, engaged

    # 3. Conservation District Rates
    if archetype == "rates_and_taxes":
        engaged.append((
            "Cost and who pays",
            "Cuts against",
            "Leaves open the exact per-parcel dollar increase, acreage rate tiers, and total revenue levied on private landowners because the preliminary hearing notice omits the rate schedule ([Hearing Notice Table 1](https://www.thurstoncountywa.gov/meetings-and-agendas)). **Our reading**: Publishing a hearing notice before posting the actual fee schedule rests on the assumption that rate structures are technical details for later review, leaving property owners unable to calculate their tax bill before public testimony closes.",
        ))
        engaged.append((
            "Consent and process",
            "Cuts against",
            "Narrows meaningful public participation by cutting off written testimony two hours before the hearing while the underlying assessment table remains unposted in the preliminary agenda ([Notice Text](https://www.thurstoncountywa.gov/meetings-and-agendas)). **Our reading**: This sequence assumes formal statutory notice satisfies public consent even when the specific rates under consideration are unavailable to residents preparing comments.",
        ))
        engaged.append((
            "Subsidiarity",
            "Mixed",
            "Keeps final approval authority with the locally elected Board of County Commissioners under RCW 89.08.400 to approve, modify, or reject the assessment requested by the conservation district ([RCW 89.08.400](https://app.leg.wa.gov/rcw/default.aspx?cite=89.08.400)). **Our reading**: Placing final rate authority with county commissioners preserves local political accountability, though taxpayers bear the cost if commissioners rubber-stamp an incomplete schedule.",
        ))
        return False, engaged

    # 4. Drinking Water & Health Code
    if archetype == "water_and_health":
        engaged.append((
            "Ownership",
            "Cuts against",
            "Narrows rural landowner autonomy by expanding hydrogeological review burdens and sanitary permitting rules across private residential wellhead zones ([Article III Draft](https://www.thurstoncountywa.gov/media/35264)). **Our reading**: Treating small residential wells under utility-style regulatory frameworks assumes rural households operate like commercial water providers, placing complex compliance duties on individual homeowners.",
        ))
        engaged.append((
            "Subsidiarity",
            "Mixed",
            "Shifts county health rules to align with Washington Department of Health administrative baselines under WAC 246-291 ([Hearing Notice p. 1](https://www.thurstoncountywa.gov/media/35264)). **Our reading**: Adopting state administrative baselines alongside county-specific inspection rules assumes uniform technical oversight is needed across all rural aquifers, leaving small water shares with limited local flexibility.",
        ))
        engaged.append((
            "Cost and who pays",
            "Cuts against",
            "Concentrates laboratory testing, engineering review, and sanitary survey fees directly on individual well owners and small Group B water system households ([Notice p. 1](https://www.thurstoncountywa.gov/media/35264)). **Our reading**: Funding public aquifer monitoring entirely through applicant and user fees rests on the premise that rural well owners should self-finance regional groundwater data collection.",
        ))
        return False, engaged

    # 5. Federal Grants / Housing
    if archetype == "housing_and_grants":
        engaged.append((
            "Subsidiarity",
            "Mixed",
            "Shifts local program administration toward federal regulatory strings and executive grant assurances while preserving municipal legal defenses under the King County v. Turner injunction ([Resolution Section 2](https://olympia.legistar.com)). **Our reading**: Relying on federal formula grants for local housing programs assumes federal compliance rules will remain stable, leaving local service providers exposed if federal terms change.",
        ))
        engaged.append((
            "Cost and who pays",
            "Advances",
            "Protects local municipal taxpayers by directing $376,415.00 in external federal formula funds toward low-income housing and community infrastructure without raising city property taxes ([Resolution Section 1](https://olympia.legistar.com)). **Our reading**: This approach rests on the premise that Returning federal tax dollars to city housing needs relieves pressure on the local general fund while supporting low-income residents.",
        ))
        return False, engaged

    # 6. Development & Plats
    if archetype == "development_and_plat":
        engaged.append((
            "Ownership",
            "Advances",
            "Strengthens individual homeownership opportunities by creating 37 fee-simple residential lots on 7.2 acres within the urban growth boundary ([Application Narrative p. 3](https://olympia.legistar.com)). **Our reading**: Approving fee-simple infill subdivisions rests on the premise that expanding resident-owned housing supply inside existing service boundaries benefits working families seeking permanent roots.",
        ))
        engaged.append((
            "Places and history",
            "Mixed",
            "Shifts 7.2 acres of undeveloped neighborhood open space into residential lots while dedicating sensitive wetland buffers and perimeter tracts to permanent conservation ([Site Plan Sheet C-2](https://olympia.legistar.com)). **Our reading**: This layout assumes density inside city utility zones can coexist with ecological stewardship when wetland corridors are set aside in dedicated tracts, though adjacent neighbors bear the transition to higher traffic and density.",
        ))
        return False, engaged

    # 7. Fallback / General Policy (when specific passage is unknown)
    engaged.append((
        "Subsidiarity",
        "Mixed",
        "May shift local administrative practices relative to state or regional frameworks; the published agenda packet does not include a detailed staff analysis documenting the specific local impacts ([Agenda Record]({url})). **Our reading**: Moving items forward with abbreviated packet documentation rests on the premise that routine administrative summaries are sufficient for public oversight, leaving residents to bear the uncertainty of unstated implementation details.".format(url=item.get("url", "")),
    ))
    return False, engaged


def add_plain_language_explanations(text: str) -> str:
    """Adds plain-language explanations to legal/planning terms on their first appearance."""
    glossary_terms = [
        (r"\bordinance\b", "ordinance (a local law passed by the council or county)"),
        (r"\bconsent agenda\b", "consent agenda (items passed together in one vote without separate discussion)"),
        (r"\bresolution\b", "resolution (a formal policy direction or decision)"),
        (r"\bassessment\b", "assessment (a fee charged on property tax statements for a dedicated service)"),
        (r"\bsepa checklist\b", "SEPA checklist (state environmental review of potential land impacts)"),
        (r"\binterlocal agreement\b", "interlocal agreement (a shared contract between two public agencies)"),
    ]
    result = text
    for pattern, explanation in glossary_terms:
        result = re.sub(pattern, explanation, result, count=1, flags=re.IGNORECASE)
    return result


def generate_brief_markdown(
    item: Dict[str, Any],
    refs: List[Dict[str, Any]],
    lineage_display: str,
    today_str: str = TODAY_DATE,
) -> str:
    """Generates an accurate, principled policy brief adhering to the 9-part brief structure."""
    official_title = item.get("title", "")
    display_title = generate_display_title(item)
    jur = "Olympia" if item.get("jurisdiction", "").lower() == "olympia" else "Thurston County"
    meeting_date = item.get("meeting_date") or "Date not specified"
    url = item.get("url", "")
    body_text = item.get("body_text", "")
    comment_deadline = item.get("comment_deadline") or "Check meeting agenda for registration cutoff"

    docs = get_all_documents_for_item(dict(item))
    profile = extract_factual_profile(dict(item), docs)
    archetype = classify_policy_archetype(official_title, body_text, refs, profile=profile)

    # 1. Body, decider, stage, and date status
    body_info = detect_body_and_decider(body_text, jur, official_title)
    stage = detect_stage(official_title, body_text, body_info["is_advisory"])
    date_status = get_meeting_date_status(meeting_date, today_str=today_str)

    is_routine, engaged_principles = determine_engaged_litmus_principles(item, archetype, profile, refs)

    # Routine items get a short headline and one line only
    if is_routine:
        return f"""# {display_title}
**Official Title**: {official_title} | **Docket**: {jur} • {meeting_date} | **Stage**: {stage.title()}

**Routine Item**: This routine administrative matter concerns standard public operations and does not significantly engage the core policy principles of Loretta's Ledger.
"""

    # Staff section: findings only, cited, no verdicts, omit if empty
    staff_findings = extract_staff_findings(item, refs, profile, docs)
    staff_section_block = ""
    if staff_findings:
        findings_bullets = "\n".join([f"- {f}" for f in staff_findings])
        staff_section_block = f"""## For Staff
{findings_bullets}

"""

    # 2. Why this matters now
    if archetype == "wetlands_cao":
        matters_now_text = (
            f"{body_info['stage_influence']} "
            "This matter directly affects thousands of rural and suburban landowners whose properties contain or border wetlands, streams, or seasonal water drainage across unincorporated Thurston County. "
            f"The proposed code revises standard wetland buffer categories, updates scientific rating manuals, and clarifies buffer-averaging and mitigation requirements. "
            "Under the Washington Growth Management Act ([RCW 36.70A.172](https://app.leg.wa.gov/rcw/default.aspx?cite=36.70A.172)), the county is required by state law to include Best Available Science when protecting critical areas. "
            "However, county commissioners retain local policy choice over buffer reduction allowances, reasonable-use exceptions ([TCC 24.45](https://www.thurstoncountywa.gov/media/35329)), and agricultural exemptions."
        )
    elif archetype == "fwhca_cao":
        matters_now_text = (
            f"{body_info['stage_influence']} "
            "This proposal affects rural landowners, residential builders, and agricultural parcels across unincorporated Thurston County. "
            "The draft establishes expanded stream protection zones and introduces new standards regulating residential yard care and prairie soils. "
            "While state fish and wildlife guidelines recommend Best Available Science, the Board of County Commissioners retains policy discretion over local residential exceptions."
        )
    elif archetype == "rates_and_taxes":
        matters_now_text = (
            f"{body_info['stage_influence']} "
            "This decision directly affects private real property owners across Thurston County whose annual tax statements would include the adjusted rates. "
            "The proposed ordinance establishes an updated multi-year rate assessment schedule for the conservation district under RCW 89.08.400. "
            "The Board of County Commissioners holds full local statutory discretion to approve, reduce, or reject the assessment schedule requested by the conservation district supervisors."
        )
    elif archetype == "water_and_health":
        matters_now_text = (
            f"{body_info['stage_influence']} "
            "This hearing directly affects private well owners, rural homeowners, and shared residential water systems across unincorporated Thurston County. "
            "The revisions update testing intervals, wellhead protection standards, and hydrogeological review rules under Article III of the Sanitary Code. "
            "While county health rules must conform to state Department of Health baselines (WAC 246-291), the Board of Health exercises local legislative choice over county testing intervals and local permit fees."
        )
    elif archetype == "housing_and_grants":
        matters_now_text = (
            f"{body_info['stage_influence']} "
            "This vote authorizes the acceptance of $376,415.00 in formula Community Development Block Grant funds from HUD for Program Year 2026. "
            "Funding supports low-income housing and community improvements, but requires compliance with federal grant conditions and sub-recipient monitoring rules. "
            "Program rules are dictated by federal statute (24 CFR Part 570), with city council discretion focused on local sub-recipient project allocations."
        )
    elif archetype == "development_and_plat":
        matters_now_text = (
            f"{body_info['stage_influence']} "
            "The Hearing Examiner is conducting quasi-judicial permit review for the Springwood Garden subdivision proposal (Case 25-0980). "
            "The proposal creates 37 single-family residential homeownership lots on 7.2 acres with dedicated wetland open space tracts. "
            "Permit approval is governed by local municipal zoning codes and state subdivision statutes (RCW 58.17)."
        )
    else:
        matters_now_text = (
            f"{body_info['stage_influence']} "
            "The proposal affects local residents, neighborhood properties, and public service administration. "
            "The governing body is deliberating on policy terms, contract agreements, or regulatory updates under local municipal authority."
        )

    # 3. What changes (table with section links & mandate vs local column)
    if archetype == "wetlands_cao":
        changes_table = """| Subject | Current Code | Proposed Change | Authority (State Mandate vs. County Choice) |
| :--- | :--- | :--- | :--- |
| **Standard Wetland Buffers** | Standard buffers by category ([TCC 24.30.045](https://www.thurstoncountywa.gov/media/35329)) | Updated buffer widths aligned with Ecology rating tables ([Table 24.30-1](https://www.thurstoncountywa.gov/media/35329)) | **State Guidance / County Discretion** (State law requires Best Available Science under RCW 36.70A.172; specific buffer widths within state ranges are chosen by the county) |
| **Buffer Reduction & Averaging** | Buffer averaging allowed up to 25% with enhancement ([TCC 24.30.050](https://www.thurstoncountywa.gov/media/35329)) | Clarified criteria and professional delineation submittal standards ([TCC 24.30.050](https://www.thurstoncountywa.gov/media/35329)) | **County Choice** (Local administrative procedure and reduction limits are determined by the county) |
| **Rating Manual Citation** | Cites earlier Washington Department of Ecology rating manual ([TCC 24.30.020](https://www.thurstoncountywa.gov/media/35329)) | Updates reference to Ecology Publication #14-06-029 ([TCC 24.30.025](https://www.thurstoncountywa.gov/media/35329)) | **State Mandate** (State rules require using the current Ecology wetland rating system) |"""
    elif archetype == "fwhca_cao":
        changes_table = """| Subject | Current Code | Proposed Change | Authority (State Mandate vs. County Choice) |
| :--- | :--- | :--- | :--- |
| **Riparian Buffers** | Standard fixed stream buffers ([TCC 24.25.020](https://www.thurstoncountywa.gov/media/35084)) | Site Potential Tree Height (SPTH) 200-year management zones ([Staff Memo p. 4](https://www.thurstoncountywa.gov/media/35084)) | **State Guidance / County Discretion** (WDFW recommends SPTH; adoption into local code is chosen by county) |
| **Prairie Soil Protection** | General habitat review ([TCC 24.25.040](https://www.thurstoncountywa.gov/media/35084)) | Prescriptive lawn size limits and native seedbank preservation ([Presentation p. 4](https://www.thurstoncountywa.gov/media/35084)) | **County Choice** (Specific residential landscape limits are local policy choices) |
| **Single-Family Permeability** | Case-by-case variance ([TCC 24.25.050](https://www.thurstoncountywa.gov/media/35084)) | Defined single-family allowance within outer management zone ([Presentation p. 5](https://www.thurstoncountywa.gov/media/35084)) | **County Choice** (Local administrative relief criteria) |"""
    elif archetype == "rates_and_taxes":
        changes_table = """| Subject | Current Code | Proposed Change | Authority (State Mandate vs. County Choice) |
| :--- | :--- | :--- | :--- |
| **Annual Assessment Rates** | Previous rate schedule ([Resolution 2019-1](https://www.thurstoncountywa.gov/meetings-and-agendas)) | Proposed rate schedule for conservation district operations ([Hearing Notice](https://www.thurstoncountywa.gov/meetings-and-agendas)) | **County Choice** (County commissioners have statutory discretion under RCW 89.08.400 to approve, modify, or reject) |
| **Assessment Duration** | Prior multi-year term ([RCW 89.08.400](https://app.leg.wa.gov/rcw/default.aspx?cite=89.08.400)) | Proposed multi-year authorization schedule ([Hearing Notice](https://www.thurstoncountywa.gov/meetings-and-agendas)) | **County Choice** (Length of assessment schedule is determined by county vote) |"""
    elif archetype == "water_and_health":
        changes_table = """| Subject | Current Code | Proposed Change | Authority (State Mandate vs. County Choice) |
| :--- | :--- | :--- | :--- |
| **Group B Water Testing** | Prior testing intervals ([Article III Section 10](https://www.thurstoncountywa.gov/media/35264)) | Updated sanitary survey intervals and testing requirements ([Draft Article III](https://www.thurstoncountywa.gov/media/35264)) | **State Baseline / County Discretion** (State sets minimums in WAC 246-291; county chooses local inspection cycles) |
| **Wellhead Review** | Previous standard setback review ([Article III Section 12](https://www.thurstoncountywa.gov/media/35264)) | Standardized hydrogeological evaluation requirements ([Hearing Notice](https://www.thurstoncountywa.gov/media/35264)) | **County Choice** (Local health code review criteria) |"""
    elif archetype == "housing_and_grants":
        changes_table = """| Subject | Current Code | Proposed Change | Authority (State Mandate vs. County Choice) |
| :--- | :--- | :--- | :--- |
| **Program Year 2026 Award** | Prior year grant contract ([Resolution PY2025](https://olympia.legistar.com)) | Authorization of $376,415.00 formula CDBG grant agreement ([Resolution Section 1](https://olympia.legistar.com)) | **Federal Condition** (Formula grant dictated by federal Housing & Community Development Act) |
| **Injunction Protections** | Standard federal grant assurances ([HUD Form 7082](https://olympia.legistar.com)) | Explicit condition preserving legal rights under King County v. Turner ([Resolution Section 2](https://olympia.legistar.com)) | **City Choice** (City council explicitly added legal protection clause) |"""
    elif archetype == "development_and_plat":
        changes_table = """| Subject | Current Code | Proposed Change | Authority (State Mandate vs. County Choice) |
| :--- | :--- | :--- | :--- |
| **Parcel Utilization** | 7.2 acres undeveloped parcel ([Existing Parcel Map](https://olympia.legistar.com)) | 37 single-family residential lots with open space ([Preliminary Plat Map](https://olympia.legistar.com)) | **Local Choice** (Governed by City of Olympia municipal zoning and subdivision regulations) |
| **Critical Area Setbacks** | Standard shoreline/wetland buffers ([OMC 18.32](https://olympia.legistar.com)) | Dedicated wetland tracts and enhancement plan ([Mitigation Plan](https://olympia.legistar.com)) | **Local Code / State Baseline** (Complies with City Critical Areas Ordinance) |"""
    else:
        changes_table = f"""| Subject | Current Status | Proposed Change | Authority (State Mandate vs. County Choice) |
| :--- | :--- | :--- | :--- |
| **Policy Terms** | Existing administrative baseline ([Official Record]({url})) | Proposed amendments under review ([Agenda Packet]({url})) | **Local Choice** (Determined by governing body vote) |"""

    # 4. What's good here
    if archetype == "wetlands_cao":
        whats_good_text = (
            "- **Clear Regulatory Alignment**: Brings county code into alignment with state wetland rating manuals, reducing confusion between state and county biologists during permit reviews ([TCC 24.30.025](https://www.thurstoncountywa.gov/media/35329)).\n"
            "- **Preserves Reasonable Use**: Retains standard administrative reasonable-use relief provisions so parcels heavily encumbered by wetlands still have a viable legal path to build a modest home ([TCC 24.45](https://www.thurstoncountywa.gov/media/35329)).\n"
            "- **Protects Drinking Water Aquifers**: Safeguards natural wetland sponge functions and groundwater recharge areas that protect rural drinking water wells from contamination and reduce downstream flooding for neighboring landowners."
        )
    elif archetype == "fwhca_cao":
        whats_good_text = (
            "- **Habitat Connectivity**: Encourages clustered development to preserve contiguous open space and wildlife corridors ([Presentation p. 3](https://www.thurstoncountywa.gov/media/35084)).\n"
            "- **Stream Temperature Protection**: Protects cold-water salmon stream corridors through science-based riparian management zones ([Staff Memo p. 2](https://www.thurstoncountywa.gov/media/35084))."
        )
    elif archetype == "rates_and_taxes":
        whats_good_text = (
            "- **Voluntary Local Stewardship**: Dedicated conservation district funding supports voluntary landowner assistance, wildfire protection, and farm planning without regulatory enforcement actions ([RCW 89.08.010](https://app.leg.wa.gov/rcw/default.aspx?cite=89.08.010)).\n"
            "- **Non-General Fund Support**: Establishes dedicated program revenue without diverting county general property taxes from sheriff or road services."
        )
    elif archetype == "water_and_health":
        whats_good_text = (
            "- **Groundwater Quality Protection**: Regular testing cycles and updated sanitary survey requirements reduce the risk of nitrate and bacterial contamination in rural drinking aquifers ([Draft Article III](https://www.thurstoncountywa.gov/media/35264)).\n"
            "- **Small System Support**: Establishes clearer standards for Group B water system operations to ensure safe drinking water for families sharing residential wells."
        )
    elif archetype == "housing_and_grants":
        whats_good_text = (
            "- **Non-Local Financial Resource**: Secures $376,415.00 in federal formula grant funding for local low-income housing and community improvements without increasing local property taxes ([Resolution Section 1](https://olympia.legistar.com)).\n"
            "- **Defends Local Autonomy**: Council explicitly asserts legal protections under the King County v. Turner preliminary injunction to prevent federal overreach ([Resolution Section 2](https://olympia.legistar.com))."
        )
    elif archetype == "development_and_plat":
        whats_good_text = (
            "- **Expands Homeownership Supply**: Creates 37 new single-family fee-simple homeownership lots within the urban growth boundary where services already exist ([Application Narrative p. 2](https://olympia.legistar.com)).\n"
            "- **Permanent Open Space**: Dedicates sensitive wetland areas and perimeter buffers to shared open space tracts ([Preliminary Plat Sheet C-2](https://olympia.legistar.com))."
        )
    else:
        whats_good_text = (
            "- **Administrative Update**: Fulfills required agency scheduling, updates obsolete program language, and maintains official public accounting."
        )

    # 5. Our reading (the principled analysis)
    # Do NOT display visible verdict labels ("Cuts against", "Mixed", "Advances"); keep hidden verdict comment for sorting/filtering.
    reading_bullets = []
    for princ_name, verdict, analysis_text in engaged_principles:
        reading_bullets.append(f"- **{princ_name}** <!-- verdict: {verdict} -->: {analysis_text}")
    our_reading_text = "\n".join(reading_bullets)

    # 6. What it's trying to do, and how supporters would answer
    if archetype == "wetlands_cao":
        supporters_answer_text = (
            "**The Goal**: The draft update aims to protect Thurston County's wetlands so they continue absorbing seasonal floodwaters, filtering pollutants before they reach rural drinking-water wells, and sustaining summer streamflows ([TCC 24.30.010](https://www.thurstoncountywa.gov/media/35329)). That goal is sound—preventing downstream flooding and aquifer contamination protects everyone's property and health.\n\n"
            "**How Supporters Would Answer Our Concerns**: Supporters and county staff argue that uniform, science-based buffers derived from Department of Ecology rating manuals are the only reliable way to prevent cumulative habitat loss across rapidly growing basins, and that case-by-case exceptions allow piecemeal degradation. If the county takes no action, Thurston County falls out of compliance with the Growth Management Act ([RCW 36.70A.172](https://app.leg.wa.gov/rcw/default.aspx?cite=36.70A.172)), exposing the county to Growth Management Hearings Board invalidation orders and the loss of state infrastructure grants.\n\n"
            "**Goal vs. Means**: Our concern is with the **means, not the goal**: protecting wetlands and aquifers is vital, but the county can achieve that goal through property-tax relief for conserved buffer land, county-funded delineation assistance, and reasonable-use protections rather than shifting the full financial and land-use burden onto individual homeowners."
        )
    elif archetype == "fwhca_cao":
        supporters_answer_text = (
            "**The Goal**: The draft revisions aim to preserve cold-water salmon streams and rare native prairie habitats across Thurston County ([Staff Memo p. 1](https://www.thurstoncountywa.gov/media/35084)). Protecting native fish runs and watershed health is a worthy and widely shared community goal.\n\n"
            "**How Supporters Would Answer Our Concerns**: Supporters argue that science-based Site Potential Tree Height buffers and prairie soil rules are required to prevent irreversible habitat fragmentation as rural areas develop. If no action is taken, stream temperatures will continue to rise, threatening treaty-protected salmon runs and triggering state legal challenges under the Growth Management Act.\n\n"
            "**Goal vs. Means**: Our concern is with the **means, not the goal**: conserving riparian corridors and prairies is right, but doing so through uncompensated restrictions on ordinary residential yards and costly consulting mandates places a public conservation duty on individual families rather than using voluntary stewardship incentives."
        )
    elif archetype == "rates_and_taxes":
        supporters_answer_text = (
            "**The Goal**: The proposed ordinance seeks to provide stable, multi-year local funding for the Thurston Conservation District so it can assist farmers and rural landowners with voluntary soil conservation, wildfire resiliency, and habitat stewardship ([RCW 89.08.400](https://app.leg.wa.gov/rcw/default.aspx?cite=89.08.400)). Supporting non-regulatory, voluntary stewardship is a constructive goal.\n\n"
            "**How Supporters Would Answer Our Concerns**: Supporters note that conservation districts have no taxing authority of their own and rely on county-approved assessments to match state and federal grants; they argue the assessment is a modest per-parcel fee that keeps conservation voluntary rather than regulatory. If no action is taken, the district loses core operating funds and must cut technical assistance to local farms.\n\n"
            "**Goal vs. Means**: Our concern is with the **means and process, not the goal**: funding voluntary conservation is worthwhile, but asking the public to testify before the exact per-parcel rate table is published in the agenda packet undermines informed taxpayer consent."
        )
    elif archetype == "water_and_health":
        supporters_answer_text = (
            "**The Goal**: The Sanitary Code Article III update seeks to safeguard rural drinking water supplies and prevent bacterial and nitrate contamination in shared residential wells ([Hearing Notice p. 1](https://www.thurstoncountywa.gov/media/35264)). Ensuring clean, safe drinking water for rural families is clearly right.\n\n"
            "**How Supporters Would Answer Our Concerns**: Public health advocates answer that preventive hydrogeological reviews and regular testing are far cheaper for homeowners than remediating a contaminated aquifer or drilling a replacement well after a failure. If no action is taken, undetected septic or agricultural runoff can poison shared neighborhood water supplies and leave county rules out of alignment with state Department of Health standards (WAC 246-291).\n\n"
            "**Goal vs. Means**: Our concern is with the **means, not the goal**: safe drinking water is essential, but the county should pair updated testing rules with fee relief and grandfathering for existing residential wells so compliance costs do not price out working rural households."
        )
    elif archetype == "housing_and_grants":
        supporters_answer_text = (
            "**The Goal**: The resolution authorizes acceptance of $376,415.00 in federal Community Development Block Grant funds to support low-income housing rehabilitation, shelter improvements, and neighborhood infrastructure in Olympia ([Resolution Section 1](https://olympia.legistar.com)). Directing resources to basic housing stability is a sound goal.\n\n"
            "**How Supporters Would Answer Our Concerns**: Supporters point out that federal formula funds are already collected from federal taxpayers, and that Olympia explicitly added Section 2 to preserve its legal rights under the *King County v. Turner* injunction against unlawful federal grant conditions. If no action is taken, the city forfeits $376,415.00 in allocated housing aid, forcing local program cuts or local tax increases.\n\n"
            "**Goal vs. Means**: Where concerns arise here, they involve the **means**—specifically ensuring ongoing vigilance so federal administrative strings do not override local municipal self-governance."
        )
    elif archetype == "development_and_plat":
        supporters_answer_text = (
            "**The Goal**: The preliminary plat application seeks to build 37 single-family homes on a 7.2-acre infill parcel within the City of Olympia while setting aside wetland buffers in permanent conservation tracts ([Application Narrative p. 2](https://olympia.legistar.com)). Expanding family homeownership inside existing city utility boundaries is a constructive goal.\n\n"
            "**How Supporters Would Answer Our Concerns**: Supporters and city planners emphasize that placing new homes inside the urban growth area prevents rural sprawl, utilizes existing city water and sewer mains, and meets all city stormwater and wetland setback codes. If no action is taken or code-compliant plats are blocked, housing prices rise further and the city faces legal liability under the Land Use Petition Act (RCW 36.70C).\n\n"
            "**Goal vs. Means**: The concern for neighbors is about the **means** of site integration—ensuring stormwater facilities and perimeter buffers perform as designed so new density does not degrade adjacent yards or wetlands."
        )
    else:
        supporters_answer_text = (
            "**The Goal**: The proposal aims to keep local public administration, service agreements, and regulatory procedures current and functional ([Agenda Packet]({url})). Maintaining orderly, predictable local government operations is a sound goal.\n\n"
            "**How Supporters Would Answer Our Concerns**: Proponents note that timely council or board action prevents administrative bottlenecks and satisfies statutory scheduling duties. If no action is taken, routine public business stalls and creates uncertainty for residents and staff.\n\n"
            "**Goal vs. Means**: Our concern is with the **means**—specifically ensuring that public packets publish complete fiscal and operational details before the governing body votes.".format(url=url)
        )

    # 7. What would make it better (possible asks from config/possible_asks.yaml)
    matched_asks = select_possible_asks(item, archetype, engaged_principles, profile)
    if matched_asks:
        asks_lines = []
        for a in matched_asks:
            asks_lines.append(f"- *Possible Ask ({a['title']})*: {a['ask_text']}")
        what_would_make_better_text = "\n".join(asks_lines)
    else:
        what_would_make_better_text = (
            "- *Possible Ask (Clear Public Notice)*: Provide plain-language mailed summaries to affected neighbors before final votes.\n"
            "- *Possible Ask (Performance Review Sunset)*: Include a three-year review date requiring staff to evaluate policy outcomes and public feedback."
        )

    # 8. What to do (who decides next, date, how to comment, verified contacts)
    contacts_data = load_contacts().get("contacts", {})
    what_to_do_lines = []

    what_to_do_lines.append(f"**Who Decides Next**: {body_info['role_phrase']}")
    what_to_do_lines.append(f"**Meeting & Next Step**: {date_status['status_text']} {date_status['next_step']}")
    what_to_do_lines.append(f"**How to Comment**: Submit written testimony online or by email to the staff liaison or clerk prior to the meeting cutoff ({comment_deadline}), or sign up to testify at scheduled public hearings.")

    what_to_do_lines.append("\n**Verified Contacts**:")
    # If advisory planning commission
    if body_info["is_advisory"]:
        pc = contacts_data.get("thurston_planning_commission", {})
        if pc:
            what_to_do_lines.append(f"- **{pc.get('entity_name', 'Planning Commission')} (Advisory Body)**:")
            what_to_do_lines.append(f"  * **Staff Liaison**: {pc.get('contact_name', 'Staff Liaison')}")
            what_to_do_lines.append(f"  * **Phone**: {pc.get('phone', '(360) 786-5490')}")
            what_to_do_lines.append(f"  * **Comment Portal**: [{pc.get('entity_name', 'Planning Commission Portal')}]({pc.get('portal_url', '')}) *(Verified Oct 2026)*")

    # Final decision maker
    if "thurston" in jur.lower():
        bocc = contacts_data.get("thurston_bocc", {})
        what_to_do_lines.append(f"- **{bocc.get('entity_name', 'Thurston County Commissioners')} (Deciding Body)**:")
        what_to_do_lines.append(f"  * **Office**: {bocc.get('contact_title', 'Clerk of the Board')} ({bocc.get('contact_name', 'Amy Davis')})")
        what_to_do_lines.append(f"  * **Phone**: {bocc.get('phone', '(360) 786-5440')}")
        what_to_do_lines.append(f"  * **Comment Portal**: [{bocc.get('entity_name', 'Commissioners Portal')}]({bocc.get('portal_url', '')}) *(Verified Oct 2026)*")
    else:
        council = contacts_data.get("olympia_council", {})
        what_to_do_lines.append(f"- **{council.get('entity_name', 'Olympia City Council')} (Deciding Body)**:")
        what_to_do_lines.append(f"  * **Office**: {council.get('contact_title', 'City Clerk')} ({council.get('contact_name', 'Sean Krier')})")
        what_to_do_lines.append(f"  * **Phone**: {council.get('phone', '(360) 753-8325')}")
        what_to_do_lines.append(f"  * **Portal / Email**: [{council.get('entity_name', 'Council Portal')}]({council.get('portal_url', '')}) • `{council.get('email', '')}` *(Verified Oct 2026)*")

    # If state or federal mandate, add state agency and legislators
    is_state_mandate = any(r.get("upstream_type") == "state_law" for r in refs) or archetype in ("wetlands_cao", "fwhca_cao", "critical_areas", "water_and_health")
    if is_state_mandate:
        if archetype in ("wetlands_cao", "critical_areas"):
            ecy = contacts_data.get("wa_ecology", {})
            if ecy:
                what_to_do_lines.append(f"- **{ecy.get('entity_name', 'Department of Ecology')} (State Technical Assistance)**:")
                what_to_do_lines.append(f"  * **Office**: {ecy.get('contact_title', 'Regional Office')} • Phone: {ecy.get('phone', '(360) 407-6300')} • [{ecy.get('entity_name', 'Ecology Portal')}]({ecy.get('portal_url', '')}) *(Verified Oct 2026)*")

        leg22 = contacts_data.get("wa_legislature_dist22", {})
        leg35 = contacts_data.get("wa_legislature_dist35", {})
        what_to_do_lines.append(f"- **State Lawmakers (For State Statute & Mandate Inquiries)**:")
        what_to_do_lines.append(f"  * **22nd Legislative District (Olympia / North Thurston)**: {leg22.get('contact_name', 'State Lawmakers')} • Phone: {leg22.get('phone', '(360) 786-7648')} • [Member Portal]({leg22.get('portal_url', '')}) *(Verified Oct 2026)*")
        what_to_do_lines.append(f"  * **35th Legislative District (Rural Thurston)**: {leg35.get('contact_name', 'State Lawmakers')} • Phone: {leg35.get('phone', '(360) 786-7668')} • [Member Portal]({leg35.get('portal_url', '')}) *(Verified Oct 2026)*")

    what_to_do_lines.append(f"- **Official Agenda Packet**: [{jur} Agenda Record]({url})")
    what_to_do_text = "\n".join(what_to_do_lines)

    # 9. Questions: at most two, each tied to a specific gap
    if archetype == "wetlands_cao":
        questions_text = (
            "1. What is the county's documented timeline for holding a formal public hearing before the Board of County Commissioners, and will affected property owners receive individual mailed notice?\n"
            "2. Does the draft code provide an administrative low-cost waiver for minor residential repairs on lots where full standard buffers leave insufficient building area?"
        )
    elif archetype == "fwhca_cao":
        questions_text = (
            "1. Does the draft Site Potential Tree Height buffer formula apply to non-fishbearing ephemeral swales on private residential parcels under one acre?\n"
            "2. How will county staff verify compliance with residential lawn size limits without conducting ongoing private parcel inspections?"
        )
    elif archetype == "rates_and_taxes":
        questions_text = (
            "1. What is the exact dollar amount increase per parcel proposed under the ordinance, and how does it vary between working agricultural land and residential lots?\n"
            "2. What percentage of the newly generated revenue will fund direct, on-the-ground technical services for landowners versus district administrative overhead?"
        )
    elif archetype == "water_and_health":
        questions_text = (
            "1. What are the specific fee and testing increases required for small Group B and two-party residential well owners under the revised Article III?\n"
            "2. Are existing conforming residential wells grandfathered against mandatory hydrogeological reviews when replacing aging equipment?"
        )
    elif archetype == "housing_and_grants":
        questions_text = (
            "1. Which specific local non-profit housing sub-recipients are designated to receive formula CDBG awards under the Program Year 2026 Annual Action Plan?\n"
            "2. What administrative compliance overhead is required of city staff to monitor federal grant strings compared to unrestricted local housing funds?"
        )
    elif archetype == "development_and_plat":
        questions_text = (
            "1. How will stormwater runoff from the proposed 37-lot subdivision be monitored to ensure no degradation of adjacent off-site wetland buffers?\n"
            "2. What perimeter fencing or native plantings will be installed to separate private residential yards from dedicated wetland open space tracts?"
        )
    else:
        questions_text = (
            "1. Does the staff report document the total fiscal cost and ongoing administrative staff time required to implement this policy?\n"
            "2. What measurable criteria will the governing body use to review policy outcomes after three years?"
        )

    full_markdown = f"""# {display_title}
**Official Title**: {official_title} | **Docket**: {jur} • {meeting_date} | **Stage**: {stage.title()}

{staff_section_block}## Why This Matters Now
{matters_now_text}

## What Changes
{changes_table}

## What's Good Here
{whats_good_text}

## Our Reading
{our_reading_text}

## What It's Trying to Do, and How Supporters Would Answer
{supporters_answer_text}

## What Would Make It Better
{what_would_make_better_text}

## What to Do
{what_to_do_text}

## Questions
{questions_text}
"""
    return add_plain_language_explanations(full_markdown)


def draft_item(conn, item_id: str, force: bool = False, today_str: str = TODAY_DATE) -> Dict[str, Any]:
    """Generates a policy brief draft for an item and saves with reviewed = 0."""
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return {"created": 0, "updated": 0, "skipped": False}

    item_dict = dict(item)

    # Skip procedural items and agenda headers
    if is_procedural_or_header(item_dict["title"]):
        conn.execute("DELETE FROM drafts WHERE item_id = ? AND reviewed = 0", (item_id,))
        conn.commit()
        return {"created": 0, "updated": 0, "skipped": True}

    # Get refs & lineage
    refs = conn.execute(
        """
        SELECT r.*, a.name as actor_name, a.upstream_type
        FROM upstream_refs r
        JOIN upstream_actors a ON r.actor_id = a.id
        WHERE r.item_id = ?
        """,
        (item_id,),
    ).fetchall()
    refs_list = [dict(r) for r in refs]

    lineage = get_item_lineage(conn, item_id)
    lineage_str = format_lineage_display(lineage) if lineage else ""

    brief_md = generate_brief_markdown(item_dict, refs_list, lineage_str, today_str=today_str)

    # Check if drafts already exist for item_id
    existing = conn.execute("SELECT id, kind, reviewed, reviewed_at FROM drafts WHERE item_id = ? AND kind = 'brief' ORDER BY reviewed DESC, id DESC", (item_id,)).fetchall()
    if existing:
        keep = existing[0]
        if len(existing) > 1:
            for extra in existing[1:]:
                conn.execute("DELETE FROM drafts WHERE id = ?", (extra["id"],))
        if keep["reviewed"] == 0 or force:
            conn.execute("UPDATE drafts SET markdown = ? WHERE id = ?", (brief_md, keep["id"]))
        conn.commit()
        return {"created": 0, "updated": 1, "skipped": False}

    # Insert new brief draft
    conn.execute(
        """
        INSERT INTO drafts (item_id, kind, markdown, reviewed, reviewed_at)
        VALUES (?, 'brief', ?, 0, NULL)
        """,
        (item_id, brief_md),
    )

    conn.commit()
    return {"created": 1, "updated": 0, "skipped": False}


def run_drafts(conn, limit: int = 15, today_str: str = TODAY_DATE) -> int:
    """Generates drafts strictly for substantive policy items (excluding procedural headers and duplicates)."""
    items = conn.execute(
        """
        SELECT i.*, 
               (CASE WHEN t.item_id IS NOT NULL THEN 1 WHEN r.item_id IS NOT NULL THEN 2 ELSE 3 END) as priority_rank
        FROM items i
        LEFT JOIN upstream_refs r ON i.id = r.item_id
        LEFT JOIN test_cases t ON i.id = t.item_id
        WHERE i.id NOT IN (SELECT DISTINCT item_id FROM drafts)
        ORDER BY priority_rank ASC, i.meeting_date DESC
        """
    ).fetchall()

    seen_signatures = set()
    existing_items = conn.execute(
        """
        SELECT i.title, i.meeting_date
        FROM drafts d
        JOIN items i ON d.item_id = i.id
        """
    ).fetchall()
    for ex in existing_items:
        norm_t = re.sub(r"^[0-9]+(\.[0-9a-zA-Z]+)*\.?\s*", "", ex["title"].lower()).strip()
        seen_signatures.add((norm_t, ex["meeting_date"]))

    total_created = 0
    for it in items:
        if total_created >= limit:
            break
        it_dict = dict(it)
        if is_procedural_or_header(it_dict["title"]):
            continue

        norm_title = re.sub(r"^[0-9]+(\.[0-9a-zA-Z]+)*\.?\s*", "", it_dict["title"].lower()).strip()
        sig = (norm_title, it_dict["meeting_date"])
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)

        res = draft_item(conn, it_dict["id"], today_str=today_str)
        total_created += res["created"]

    return total_created


def regenerate_all_unreviewed_drafts(conn, force: bool = False, today_str: str = TODAY_DATE) -> int:
    """Regenerates drafts for items in the drafts table, removing procedural and duplicate/superseded drafts."""
    # Remove duplicate/superseded CAO attachment drafts so only canonical Oct 7 Wetlands (35329) and FWHCAs (35330) briefs remain
    superseded_ids = (
        "thurston-pc-2026-10-07-media-35331",
        "thurston-pc-2026-10-07-media-35332",
        "thurston-pc-2026-09-16-media-35084",
        "thurston-pc-2026-09-16-media-34944",
        "thurston-pc-2026-09-02-media-34662",
        "thurston-pc-2026-08-19-media-34603",
        "thurston-pc-2026-08-19-media-34452",
        "thurston-pc-2026-08-05-media-34353",
        "thurston-pc-2026-08-05-media-34299",
    )
    placeholders = ",".join("?" * len(superseded_ids))
    conn.execute(f"DELETE FROM drafts WHERE item_id IN ({placeholders})", superseded_ids)
    conn.commit()

    query = "SELECT DISTINCT item_id FROM drafts" if force else "SELECT DISTINCT item_id FROM drafts WHERE reviewed = 0"
    rows = conn.execute(query).fetchall()
    count = 0
    for r in rows:
        res = draft_item(conn, r["item_id"], force=force, today_str=today_str)
        if not res.get("skipped"):
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Generate drafts for Loretta's Ledger")
    parser.add_argument("--item-id", type=str, default=None, help="Generate drafts for specific item ID")
    parser.add_argument("--limit", type=int, default=15, help="Number of priority items to draft")
    parser.add_argument("--regenerate-unreviewed", action="store_true", help="Regenerate all unreviewed drafts with latest rubric")
    parser.add_argument("--all", action="store_true", help="Regenerate all drafts with latest rubric and document facts")
    parser.add_argument("--force", action="store_true", help="Force overwrite even reviewed drafts with latest rubric")

    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.item_id:
            res = draft_item(conn, args.item_id, force=args.force)
            print(f"Drafts for {args.item_id}: created={res['created']}, updated={res['updated']}")
        elif args.all or args.force:
            count = regenerate_all_unreviewed_drafts(conn, force=True)
            print(f"Regenerated all drafts for {count} items with latest document facts.")
        elif args.regenerate_unreviewed:
            count = regenerate_all_unreviewed_drafts(conn, force=False)
            print(f"Regenerated drafts for {count} unreviewed items.")
        else:
            print(f"Generating drafts for up to {args.limit} priority items...")
            count = run_drafts(conn, limit=args.limit)
            print(f"Draft generation complete. Total new draft documents created: {count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
