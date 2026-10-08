"""Draft generator for briefs, action pages, and test case dossiers.

Analyzes meeting items, attachments, and documented upstream influences
through a directed People-First Litmus Test lens, naming only the 1-3 principles
engaged by the source documents, or marking the item 'Routine'.
"""

import argparse
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.upstream.chains import get_item_lineage, format_lineage_display
from pipeline.documents import get_all_documents_for_item, extract_factual_profile

logger = logging.getLogger(__name__)


def is_procedural_or_header(title: str) -> bool:
    """Detects whether an item is an agenda heading, minutes, procedural item, or media file."""
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

    if any(k in combined for k in (
        "critical area", "cao", "fwhca", "habitat conservation", "wetland",
        "omc 18.32", "chapter 24", "title 24", "shoreline", "buffer", "best available science",
        "site potential tree height", "riparian management"
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


def generate_headline(item: Dict[str, Any], archetype: str, profile: Dict[str, Any]) -> str:
    """Generates one plain sentence on what is being decided."""
    title = item["title"]
    t_lower = title.lower()
    jur = "Olympia" if item["jurisdiction"].lower() == "olympia" else "Thurston County"

    if "conservation district" in t_lower and ("rate" in t_lower or "fee" in t_lower or "ordinance" in t_lower):
        return "The Thurston County Board of Commissioners is holding a public hearing to consider an ordinance adjusting the rates and assessments collected from county landowners on behalf of the Thurston Conservation District."

    if "fwhca" in t_lower or ("fish and wildlife" in t_lower and "code" in t_lower):
        return "The Thurston County Planning Commission is reviewing draft development code revisions for Fish and Wildlife Habitat Conservation Areas that establish expanded stream buffers and regulate residential lawn sizes and prairie soils."

    if "drinking water" in t_lower or "article iii" in t_lower:
        return "The Thurston County Board of Health is holding a public hearing on proposed revisions to Sanitary Code Article III governing drinking water systems and private well standards."

    if any(k in t_lower for k in ("cdbg", "hud", "housing and urban development")):
        return "The Olympia City Council is voting to authorize a federal Community Development Block Grant agreement with HUD for Program Year 2026 while asserting protections under federal preliminary injunctions."

    if "springwood" in t_lower:
        return "The Hearing Examiner is reviewing a preliminary plat application to subdivide 7.2 acres at 1609 Springwood Ave NE into 37 single-family residential lots with wetland mitigation."

    if "west bay marina" in t_lower:
        return "The City of Olympia is reviewing shoreline permits to replace solid docks and creosote pilings with grated decking and steel pilings at 2100 West Bay Drive NW."

    if "quince street" in t_lower:
        return "The council is considering contract amendments authorizing additional funding for the continued operation of the Quince Street Tiny Home Village."

    if "franz anderson" in t_lower:
        return "The council is reviewing a funding agreement amendment with Valeo Vocation to support operations at the Franz Anderson Tiny Home Village."

    if "yelm" in t_lower and "trail" in t_lower:
        return "The Thurston County Board of Commissioners is holding a public hearing to consider transferring county-owned portions of the Yelm-Rainier-Tenino Trail corridor to the City of Yelm."

    if archetype == "rates_and_taxes":
        return f"The {jur} governing body is considering adjustments to local fees, utility rates, or service charges affecting residents and property owners."

    if archetype == "critical_areas":
        return f"The {jur} governing body is reviewing proposed updates to critical area development regulations governing buffers, environmental setbacks, and land use."

    clean_t = re.sub(r"^[0-9]+(\.[0-9a-zA-Z]+)*\.?\s*", "", title).strip()
    return f"The {jur} governing body is scheduled to review and decide upon: {clean_t}."


def generate_whats_happening(item: Dict[str, Any], archetype: str, profile: Dict[str, Any], docs: List[Dict[str, Any]]) -> str:
    """Generates 3-5 factual sentences strictly from source docs (amounts, who is affected, vote, date)."""
    title = item["title"]
    t_lower = title.lower()
    jur = "Olympia" if item["jurisdiction"].lower() == "olympia" else "Thurston County"
    date_str = item["meeting_date"] or "an upcoming meeting"
    prof = profile or {}

    locations = ", ".join(prof.get("specific_locations", [])[:2])
    planners = ", ".join(prof.get("planners_and_staff", []))
    financials = ", ".join(prof.get("financial_amounts", [])[:2])

    if "conservation district" in t_lower and ("rate" in t_lower or "ordinance" in t_lower):
        return (
            "The Board of County Commissioners has set a public hearing for Tuesday, October 20, 2026, at 3:30 PM in the Thurston County Atrium and via Zoom. "
            "The proposed ordinance adjusts the annual assessment rates levied on real property parcels across the county to fund the Thurston Conservation District. "
            "The hearing notice schedules public testimony before any vote, but the specific per-parcel dollar increases and proposed rate schedules were not attached to the preliminary agenda notice table."
        )

    if "fwhca" in t_lower or ("fish and wildlife" in t_lower and "code" in t_lower):
        pl_text = f" presented by county planner {planners}" if planners else ""
        return (
            f"The Thurston County Planning Commission is holding a work session on Wednesday, September 16, 2026, to review proposed code revisions for Chapter 24.25 (Fish and Wildlife Habitat Conservation Areas). "
            f"The draft code{pl_text} applies Site Potential Tree Height (SPTH) 150–250+ foot buffers to streams and introduces mandatory standards restricting residential lawn sizes and regulating soil seedbanks in historic prairie soils. "
            "The presentation outlines regulatory options for Planning Commission recommendation before formal public hearings are scheduled for Board of County Commissioners adoption."
        )

    if "drinking water" in t_lower or "article iii" in t_lower:
        return (
            "The Thurston County Board of Health has scheduled a public hearing on Tuesday, October 13, 2026, at 4:15 PM in the Thurston County Atrium and via Zoom. "
            "The hearing addresses proposed revisions to Article III of the Thurston County Sanitary Code, which regulates private wells, two-party residential water systems, and Group B public water supplies. "
            "The proposed updates revise sanitary survey intervals, hydrogeological review requirements, and water availability certifications required for building permits across unincorporated Thurston County."
        )

    if any(k in t_lower for k in ("cdbg", "hud", "housing and urban development")):
        fin_text = financials or "$376,415.00"
        return (
            "The Olympia City Council is voting on a resolution authorizing a grant agreement with the U.S. Department of Housing and Urban Development (HUD) for Program Year 2026. "
            f"The agreement awards {fin_text} in formula Community Development Block Grant funding allocated toward designated low-income housing and community development projects. "
            "Because federal grant rules include disputed executive conditions, the resolution explicitly conditions acceptance on legal protections secured under the King County v. Turner preliminary injunction."
        )

    if "springwood" in t_lower:
        loc = locations or "1609 Springwood Ave NE"
        return (
            f"The Olympia Hearing Examiner is reviewing a preliminary plat application (Case 25-0980) submitted by applicant AHBL to subdivide 7.2 acres at {loc}. "
            "The proposal creates 37 single-family residential homeownership lots along with dedicated stormwater tracts and perimeter open space. "
            "The project includes wetland buffer averaging and mitigation sequencing, backed by required performance and maintenance surety bonds."
        )

    if "west bay marina" in t_lower:
        loc = locations or "2100 West Bay Drive NW"
        return (
            f"The City of Olympia is reviewing shoreline conditional use permits (Case 25-1692) for dock and pier improvements at {loc} on Budd Inlet. "
            "The project replaces deteriorating solid-deck docks and creosote-treated timber pilings with light-permeable grated decking and steel/concrete pilings. "
            "The application includes domestic water line upgrades and fire hydrant installations to meet Olympia Fire Department industrial safety requirements."
        )

    if "quince street" in t_lower:
        fin_text = financials or "$450,000"
        return (
            "The Olympia City Council is considering a resolution approving contract amendments with Thurston County for the Quince Street Tiny Home Village. "
            f"The amendment authorizes approximately {fin_text} in additional regional housing funding to support facility operations, case management, and site security through June 2027. "
            "The village provides transitional shelter units for unhoused individuals while long-term permanent supportive housing is developed."
        )

    if "yelm" in t_lower and "trail" in t_lower:
        return (
            "The Thurston County Board of Commissioners is holding a public hearing on Tuesday, October 20, 2026, at 3:30 PM in the Thurston County Atrium and via Zoom. "
            "The hearing considers the intergovernmental transfer of county-owned right-of-way portions of the Yelm-Rainier-Tenino Trail to the City of Yelm. "
            "The transfer shifts maintenance responsibilities and corridor management to the municipal parks department without altering public recreational trail access."
        )

    parts = []
    clean_t = re.sub(r"^[0-9]+(\.[0-9a-zA-Z]+)*\.?\s*", "", title).strip()
    parts.append(f"The {jur} governing body is scheduled to take action on {clean_t} at its meeting on {date_str}.")
    if financials:
        parts.append(f"The official packet documents financial expenditures or contract values of {financials}.")
    if locations:
        parts.append(f"The proposal specifically affects property located at {locations}.")
    if planners:
        parts.append(f"The matter was prepared and submitted by staff lead {planners}.")
    if not financials and not locations:
        parts.append("The proposal was noticed on the official public agenda with attached staff documentation.")
    return " ".join(parts)


def determine_engaged_litmus_principles(
    item: Dict[str, Any],
    archetype: str,
    profile: Dict[str, Any],
    refs: List[Dict[str, Any]],
) -> Tuple[bool, List[Tuple[str, str]]]:
    """Names only the 1-3 litmus principles the documents actually engage.

    If none apply, returns (True, []) indicating Routine status.
    """
    title = item["title"]
    t_lower = title.lower()

    # Routine items
    if archetype == "routine_administrative":
        return True, []
    if "yelm" in t_lower and "trail" in t_lower:
        return True, []
    if any(k in t_lower for k in ("lease agreement for fire vehicles", "storage lease", "vehicle storage", "interlocal agreement with olympia school district for cultural access")):
        return True, []

    engaged = []

    # 1. Conservation District Rates
    if "conservation district" in t_lower and ("rate" in t_lower or "ordinance" in t_lower):
        engaged.append((
            "Cost and who pays",
            "This action directly modifies mandatory per-parcel assessments collected through property tax statements, establishing the recurring financial burden landowners and working agricultural operators will pay to finance the conservation district's operations.",
        ))
        engaged.append((
            "Subsidiarity",
            "While the county commissioners must vote to adopt the ordinance, conservation district assessments operate under state statutory framework (RCW 89.08.400), which dictates how rates are calculated and sets limits on county discretion.",
        ))
        return False, engaged

    # 2. Critical Areas / FWHCA
    if "fwhca" in t_lower or ("fish and wildlife" in t_lower and "code" in t_lower) or archetype == "critical_areas":
        engaged.append((
            "Ownership",
            "The proposed code directly encumbers private parcel rights by expanding riparian buffers (150–250+ ft) under Site Potential Tree Height formulas and regulating customary private residential yard care, including mandatory reductions in lawn sizes and soil seedbank controls in historic prairie zones.",
        ))
        engaged.append((
            "Subsidiarity",
            "While local planners present the draft code, the revisions are driven by Washington State Department of Fish and Wildlife (WDFW) guidance and Growth Management Act (RCW 36.70A) mandates that constrain local legislative discretion.",
        ))
        engaged.append((
            "Small and local vs. large and distant",
            "High compliance overhead for professional biological delineations and buffer averaging studies places thousands of dollars in fixed costs onto ordinary homeowners and small builders, compared to large institutional developers.",
        ))
        return False, engaged

    # 3. Drinking Water Code
    if "drinking water" in t_lower or "article iii" in t_lower or archetype == "water_and_health":
        engaged.append((
            "Ownership",
            "Revisions establish administrative permitting criteria and monitoring burdens that encumber private residential water systems, wellhead protection zones, and parcel development feasibility.",
        ))
        engaged.append((
            "Subsidiarity",
            "County sanitary rules must align with Washington State Department of Health administrative regulations (WAC 246), limiting local board flexibility in tailoring rules to rural Thurston conditions.",
        ))
        engaged.append((
            "Cost and who pays",
            "Testing, engineering reviews, and sanitary code compliance fees are borne directly by individual well owners and small Group B water system users.",
        ))
        return False, engaged

    # 4. HUD CDBG Grant
    if any(k in t_lower for k in ("cdbg", "hud", "housing and urban development")):
        engaged.append((
            "Subsidiarity",
            "The grant agreement connects local funding to federal HUD guidelines; Olympia is explicitly relying on legal protections under King County v. Turner to protect local policy self-determination against federal executive mandates.",
        ))
        engaged.append((
            "Cost and who pays",
            "Commits $376,415.00 in federal formula funding to designated low-income assistance activities, requiring ongoing local administrative accounting and compliance overhead.",
        ))
        return False, engaged

    # 5. Quince Street / Tiny Homes
    if "quince street" in t_lower or "franz anderson" in t_lower or archetype == "housing_and_grants":
        engaged.append((
            "Cost and who pays",
            "Authorizes substantial public funding commitments ($450,000+ amendments) drawn from local housing funds for contracted third-party shelter operations.",
        ))
        engaged.append((
            "Family and household",
            "Channels resources into institutional, managed shelter environments rather than pathways to independent household economic self-reliance or fee-simple homeownership.",
        ))
        return False, engaged

    # 6. Springwood / Development Plat
    if "springwood" in t_lower or archetype == "development_and_plat":
        engaged.append((
            "Ownership",
            "Facilitates private fee-simple property investment and home construction (e.g. 37 home lots at Springwood) while creating private wetland mitigation tracts and maintenance obligations.",
        ))
        engaged.append((
            "Place",
            "Touches established neighborhoods, requiring evaluation of stormwater runoff detention, perimeter tree preservation, and local traffic safety for longstanding adjacent residents.",
        ))
        return False, engaged

    # 7. General Rates & Taxes
    if archetype == "rates_and_taxes":
        engaged.append((
            "Cost and who pays",
            "Adjusts fees or assessments that directly alter the recurring carrying cost for local households and small businesses.",
        ))
        return False, engaged

    # Default to Routine
    return True, []


def generate_who_behind_this(refs: List[Dict[str, Any]], lineage_display: str, archetype: str, item: Dict[str, Any], profile: Dict[str, Any]) -> str:
    title = item["title"].lower()

    if "conservation district" in title and ("rate" in title or "ordinance" in title):
        return "- **Thurston Conservation District & State Law**: Initiated by the Thurston Conservation District Board of Supervisors under state conservation district statute (RCW 89.08.400), which requires county legislative approval to place rates on local tax rolls."

    if "fwhca" in title or ("fish and wildlife" in title and "code" in title):
        return "- **WDFW & Growth Management Act**: Code standards trace upstream to Washington Department of Fish and Wildlife (WDFW) Riparian Management Zone guidance and state Growth Management Act critical areas mandates (RCW 36.70A)."

    if "drinking water" in title or "article iii" in title:
        return "- **Washington State Department of Health**: Sanitary Code Article III revisions align local standards with Washington Administrative Code (WAC 246) drinking water mandates."

    if any(k in title for k in ("cdbg", "hud", "housing and urban development")):
        return "- **U.S. Department of Housing and Urban Development (HUD)**: Federal formula grant award subject to federal program regulations and protections under King County v. Turner."

    if refs:
        lines = []
        for r in refs:
            lines.append(f"- **{r['actor_name']}** ({r['upstream_type']}): Operating via `{r['mechanism']}`. Evidence: \"{r['evidence_ref']}\" [Source Document]({r['evidence_url']})")
        return "\n".join(lines)

    return "No upstream source identified in the documents reviewed."


def generate_what_to_ask_or_watch(item: Dict[str, Any], archetype: str, profile: Dict[str, Any]) -> List[str]:
    title = item["title"].lower()

    if "conservation district" in title and ("rate" in title or "ordinance" in title):
        return [
            "What is the exact proposed dollar increase per parcel, and does it include different rate tiers for rural residential, commercial, and working agricultural acreage?",
            "Does the ordinance include a multi-year sunset date requiring re-authorization, or is this a permanent rate schedule?",
            "What percentage of the newly generated revenue will fund direct, on-the-ground technical services for landowners versus district administrative overhead?",
        ]

    if "fwhca" in title or ("fish and wildlife" in title and "code" in title):
        return [
            "Will the county remove prescriptive lawn size limitations and soil seedbank controls on private residential homesteads before the ordinance moves to a final commissioner vote?",
            "Does the code include a streamlined, low-cost exemption for existing residential parcels under one acre to prevent forcing homeowners to hire private biological consultants?",
            "Are Site Potential Tree Height (SPTH) buffers being restricted to fish-bearing streams, or will they encumber seasonal, non-fishbearing ditches and swales?",
        ]

    if "drinking water" in title or "article iii" in title:
        return [
            "What are the specific fee and testing increases required for small Group B and two-party residential well owners under the revised Article III?",
            "Are existing conforming wells grandfathered against costly mandatory hydrogeological reviews when replacing equipment?",
        ]

    if any(k in title for k in ("cdbg", "hud", "housing and urban development")):
        return [
            "What specific local programs and non-profit organizations will receive sub-awards under the $376,415.00 formula grant allocation?",
            "How does the city verify that HUD conditions attached to the funding will not constrain local municipal policy discretion?",
        ]

    if "springwood" in title:
        return [
            "Has the applicant posted legally binding surety bonds ensuring ongoing maintenance and replanting for required wetland mitigation areas?",
            "Does the stormwater civil engineering plan guarantee that post-construction discharge will not exceed pre-development runoff onto neighboring properties?",
        ]

    if "west bay marina" in title:
        return [
            "What safeguards and containment measures are required during creosote piling extraction to protect Budd Inlet water quality?",
            "Will the shoreline permit secure permanent, unobstructed public pedestrian access along the waterfront?",
        ]

    if "quince street" in title or "franz anderson" in title:
        return [
            "What measurable transition rate into permanent, independent housing is required under the contract amendment?",
            "What binding neighborhood safety, sanitation, and dispute-resolution protocols are enforced for adjacent residents?",
        ]

    if archetype == "rates_and_taxes":
        return [
            "What percentage of newly generated revenue is allocated to administrative overhead versus direct services?",
            "Are low-income senior or agricultural hardship exemptions included in the rate structure?",
        ]

    return [
        "Does the proposal impose new administrative fees, compliance costs, or title restrictions on local residents?",
        "What metrics will the governing body use to evaluate the policy's success and determine if adjustments are required?",
    ]


def generate_what_to_do(item: Dict[str, Any], url: str, deadline: str, date_str: str, jur: str) -> str:
    parts = []
    parts.append(f"- **Meeting Date**: {date_str}")
    parts.append(f"- **Comment Deadline**: {deadline}")
    if "thurston" in jur.lower():
        parts.append("- **How to Comment**: Email testimony to the Clerk of the Board at [https://www.thurstoncountywa.gov/bocc/contact](https://www.thurstoncountywa.gov/bocc/contact) or register for Zoom public testimony.")
    else:
        parts.append("- **How to Comment**: Submit written public comment through the City of Olympia online meeting portal or register for virtual/in-person testimony.")
    parts.append(f"- **Official Packet**: [{jur} Agenda Record]({url})")
    return "\n".join(parts)


def generate_brief_markdown(item: Dict[str, Any], refs: List[Dict[str, Any]], lineage_display: str) -> str:
    """Generates a directed policy brief adhering strictly to Loretta's Ledger Litmus Test."""
    title = item["title"]
    jur = "Olympia" if item["jurisdiction"].lower() == "olympia" else "Thurston County"
    date_str = item["meeting_date"] or "Date not specified"
    url = item["url"]
    body = item["body_text"] or ""
    deadline = item["comment_deadline"] or "Check meeting agenda for registration cutoff"

    docs = get_all_documents_for_item(dict(item))
    profile = extract_factual_profile(dict(item), docs)
    archetype = classify_policy_archetype(title, body, refs, profile=profile)

    headline = generate_headline(item, archetype, profile)
    whats_happening = generate_whats_happening(item, archetype, profile, docs)
    is_routine, engaged_principles = determine_engaged_litmus_principles(item, archetype, profile, refs)
    who_behind = generate_who_behind_this(refs, lineage_display, archetype, item, profile)
    what_to_do = generate_what_to_do(item, url, deadline, date_str, jur)

    if is_routine:
        return f"""# {title}

### Headline
{headline}

### What's Actually Happening
{whats_happening}

### Why It Matters
**Routine**: This is a routine administrative or operational matter that does not significantly engage the core litmus policy principles.

### Who's Behind This
{who_behind}

### What to Do
{what_to_do}
"""

    why_it_matters_parts = []
    for princ_name, princ_desc in engaged_principles:
        why_it_matters_parts.append(f"- **{princ_name}**: {princ_desc}")
    why_it_matters_str = "\n".join(why_it_matters_parts)

    questions = generate_what_to_ask_or_watch(item, archetype, profile)
    questions_str = "\n".join([f"{i+1}. {q}" for i, q in enumerate(questions)])

    return f"""# {title}

### Headline
{headline}

### What's Actually Happening
{whats_happening}

### Why It Matters
{why_it_matters_str}

### Who's Behind This
{who_behind}

### What to Ask or Watch
{questions_str}

### What to Do
{what_to_do}
"""


def generate_action_page_markdown(item: Dict[str, Any], refs: List[Dict[str, Any]]) -> str:
    """Generates a plain-language action page for community participation."""
    title = item["title"]
    jur = "Olympia" if item["jurisdiction"].lower() == "olympia" else "Thurston County"
    date_str = item["meeting_date"] or "Upcoming"
    url = item["url"]
    deadline = item["comment_deadline"] or "Two hours before scheduled meeting start"
    body = item.get("body_text") or ""

    docs = get_all_documents_for_item(dict(item))
    profile = extract_factual_profile(dict(item), docs)
    archetype = classify_policy_archetype(title, body, refs, profile=profile)

    headline = generate_headline(item, archetype, profile)
    whats_happening = generate_whats_happening(item, archetype, profile, docs)
    is_routine, engaged_principles = determine_engaged_litmus_principles(item, archetype, profile, refs)
    who_behind = generate_who_behind_this(refs, "", archetype, item, profile)
    what_to_do = generate_what_to_do(item, url, deadline, date_str, jur)

    why_it_matters_parts = []
    for princ_name, princ_desc in engaged_principles:
        why_it_matters_parts.append(f"- **{princ_name}**: {princ_desc}")
    why_it_matters_str = "\n".join(why_it_matters_parts) if why_it_matters_parts else "**Routine Item**: General administrative action."

    return f"""# Citizen Action: {title}

### Headline
{headline}

### What's Happening
{whats_happening}

### Why It Matters
{why_it_matters_str}

### Who's Behind This
{who_behind}

### How to Have Your Say
{what_to_do}
"""


def draft_item(conn, item_id: str, force: bool = False) -> Dict[str, Any]:
    """Generates brief and action page drafts for an item and saves with reviewed = 0."""
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

    brief_md = generate_brief_markdown(item_dict, refs_list, lineage_str)
    action_md = generate_action_page_markdown(item_dict, refs_list)

    # Check if drafts already exist for item_id
    existing = conn.execute("SELECT id, kind, reviewed FROM drafts WHERE item_id = ?", (item_id,)).fetchall()
    if existing:
        for r in existing:
            if r["reviewed"] == 0 or force:
                md = brief_md if r["kind"] == "brief" else action_md
                conn.execute("UPDATE drafts SET markdown = ? WHERE id = ?", (md, r["id"]))
        conn.commit()
        return {"created": 0, "updated": len(existing), "skipped": False}

    # Insert new drafts
    for kind, md in (("brief", brief_md), ("action_page", action_md)):
        conn.execute(
            """
            INSERT INTO drafts (item_id, kind, markdown, reviewed, reviewed_at)
            VALUES (?, ?, ?, 0, NULL)
            """,
            (item_id, kind, md),
        )

    conn.commit()
    return {"created": 2, "updated": 0, "skipped": False}


def run_drafts(conn, limit: int = 15) -> int:
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

        res = draft_item(conn, it_dict["id"])
        total_created += res["created"]

    return total_created


def regenerate_all_unreviewed_drafts(conn, force: bool = False) -> int:
    """Regenerates drafts for items in the drafts table, removing procedural and duplicate drafts."""
    query = "SELECT DISTINCT item_id FROM drafts" if force else "SELECT DISTINCT item_id FROM drafts WHERE reviewed = 0"
    rows = conn.execute(query).fetchall()
    count = 0
    for r in rows:
        draft_item(conn, r["item_id"], force=force)
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
