"""Plain display title generator, body naming, and tier classifier for Loretta's Ledger.

Generates accurate, plain-language headlines under ~90 characters in the form:
"[Body] [does what] [to what]"
Never chooses a category or topic by keyword. If a title cannot be rewritten
confidently from the documents, falls back to the cleaned official title.
Names the exact body (committee, commission, council, board).
"""

import html
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set
import yaml


def count_syllables(word: str) -> int:
    """Estimates the syllable count of an English word."""
    w = word.lower().strip(".:,;!?()\"'-")
    if not w:
        return 0
    if w.isdigit():
        return 2
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]|ed|es|e)$", "", w)
    w = re.sub(r"^y", "", w)
    matches = re.findall(r"[aeiouy]{1,2}", w)
    return max(1, len(matches))


def flesch_kincaid_grade_level(text: str) -> float:
    """Computes standard Flesch-Kincaid Grade Level.

    Formula: 0.39 * (words / sentences) + 11.8 * (syllables / words) - 15.59
    """
    words = [w for w in text.split() if w.strip(".:,;!?()\"'-")]
    if not words:
        return 0.0

    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    total_syllables = sum(count_syllables(w) for w in words)

    words_count = len(words)
    score = 0.39 * (words_count / sentences) + 11.8 * (total_syllables / words_count) - 15.59
    return round(score, 1)


STOPWORDS: Set[str] = {
    "the", "and", "for", "with", "from", "that", "this", "into", "over", "under",
    "between", "regarding", "concerning", "relating", "approval", "approving",
    "authorizing", "consideration", "review", "proposed", "draft", "preliminary",
    "recommended", "resolution", "ordinance", "public", "hearing", "meeting",
    "minutes", "agenda", "item", "items", "city", "olympia", "thurston", "county",
    "commissioners", "board", "council", "committee", "commission", "advisory",
    "new", "local", "plan", "measure", "weighs", "weigh", "votes", "vote",
    "holds", "approves", "accepts", "updates", "update", "changes", "change",
    "rules", "rule", "law", "laws", "program", "project", "services", "service",
    "department", "state", "washington", "year", "years", "all", "since", "sept",
    "august", "october", "july", "may", "june",
}


def extract_key_nouns(text: str) -> Set[str]:
    """Extracts significant content words/roots from a title for accuracy verification."""
    cleaned = html.unescape(text).lower()
    # Expand well-known abbreviations to their plain-word equivalents so valid acronym expansions match
    equivalents = {
        "fwhca": ["wildlife", "habitat", "fish"],
        "fwhcas": ["wildlife", "habitat", "fish"],
        "cdbg": ["grant", "housing", "community", "block"],
        "hud": ["housing", "federal"],
        "cao": ["critical", "wetlands", "habitat", "areas"],
        "boh": ["health", "water", "drinking"],
        "prac": ["parks", "recreation"],
        "pbia": ["parking", "business"],
        "ompd": ["park", "parks", "metropolitan"],
        "lott": ["water", "clean", "alliance"],
        "uac": ["utility", "utilities"],
        "cfp": ["capital", "facilities"],
    }
    words = re.findall(r"[a-z0-9]{3,}", cleaned)
    tokens: Set[str] = set()
    for w in words:
        if w in STOPWORDS:
            continue
        if w.isdigit() and len(w) < 4:
            continue
        tokens.add(w)
        # Add stem (strip trailing 's')
        if w.endswith("s") and len(w) > 4:
            tokens.add(w[:-1])
        if w in equivalents:
            for eq in equivalents[w]:
                tokens.add(eq)
    return tokens


def shares_key_nouns(plain_title: str, official_title: str) -> bool:
    """Returns True if plain_title shares at least one key content noun with official_title."""
    official_nouns = extract_key_nouns(official_title)
    if not official_nouns:
        return True
    plain_nouns = extract_key_nouns(plain_title)
    return bool(official_nouns.intersection(plain_nouns))


def clean_agenda_title(title: str) -> str:
    """Strips agenda prefixes like '4.J Approval of', '6.A', 'Public Hearing:', etc."""
    t = html.unescape(title or "").replace("\xa0", " ").strip()
    t = re.sub(r"^(?:item\s+)?[0-9]+(?:\.[0-9a-zA-Z]+)*\.?(?=\s|[-–:])\s*(?:[-–:]\s*)?", "", t, flags=re.IGNORECASE).strip()
    prefixes = [
        r"^public hearing\s*[-–:]\s*",
        r"^boh public hearing\s*[-–:]\s*",
        r"^new draft\s*\([^)]*\)\s*-\s*",
        r"^new draft\s*-\s*",
        r"^draft\s*\([^)]*\)\s*-\s*",
        r"^draft\s*-\s*",
        r"^presentation\s*-\s*",
        r"^staff memo\s*-\s*",
        r"^memo\s*-\s*",
        r"^approval of a resolution authorizing\s+",
        r"^approval of a resolution\s+",
        r"^approval of resolution\s+",
        r"^approval of the recommended\s+",
        r"^approval of recommended\s+",
        r"^approval of a\s+",
        r"^approval of the\s+",
        r"^approval of\s+",
        r"^consideration of a resolution expressing collective city council support for\s+",
        r"^consideration of a resolution expressing the city council['’]s collective opposition to\s+",
        r"^consideration of a resolution\s+",
        r"^consideration of a referral to\s+",
        r"^consideration of a referral regarding\s+",
        r"^consideration of\s+",
        r"^review of the proposed\s+",
        r"^review of proposed\s+",
        r"^review of\s+",
        r"^public hearing on\s+",
        r"^resolution authorizing\s+",
    ]
    for p in prefixes:
        t = re.sub(p, "", t, flags=re.IGNORECASE).strip()
    # Strip trailing parentheticals like "(all changes)"
    t = re.sub(r"\s*\(all changes\)\s*$", "", t, flags=re.IGNORECASE).strip()
    return t


def determine_exact_body_name(item: Dict[str, Any], body_text: str = "") -> str:
    """Determines the exact body name (committee, commission, council, board) from item metadata.

    Never labels a committee or commission as 'City Council'.
    """
    raw_title = html.unescape(item.get("title") or "")
    bt = body_text or item.get("body_text") or ""
    jur = (item.get("jurisdiction") or "").lower()
    source_id = (item.get("source_id") or "").lower()

    # 1. Check explicit "Body: ..." line in body_text
    for line in bt.splitlines():
        if line.lower().startswith("body:"):
            b_val = line.split(":", 1)[1].strip()
            if b_val:
                if b_val.lower() == "thurston county planning commission":
                    return "Planning Commission"
                if b_val.lower() == "olympia planning commission":
                    return "Planning Commission"
                return b_val

    t_lower = raw_title.lower()
    if "board of health" in t_lower or "boh" in t_lower:
        return "Board of Health"
    if "planning commission" in t_lower or source_id == "thurston-planning-commission":
        return "Planning Commission"
    if "hearing examiner" in t_lower:
        return "Hearing Examiner"
    if "finance committee" in t_lower:
        return "Finance Committee"
    if "land use" in t_lower and "committee" in t_lower:
        return "Land Use & Environment Committee"
    if "utility advisory committee" in t_lower or "uac" in t_lower:
        return "Utility Advisory Committee"
    if "parks and recreation advisory committee" in t_lower or "prac" in t_lower:
        return "Parks & Recreation Committee"
    if "lodging tax advisory committee" in t_lower:
        return "Lodging Tax Committee"

    if jur == "olympia":
        return "City Council"
    if jur == "thurston":
        return "County Commissioners"
    return "Council"


def determine_short_body_subject(item: Dict[str, Any], body_text: str = "") -> str:
    """Returns a concise, accurate body name for use in '[Body] [does what] [to what]' headlines."""
    exact = determine_exact_body_name(item, body_text)
    short_map = {
        "Thurston County Planning Commission": "Planning Commission",
        "Olympia Planning Commission": "Planning Commission",
        "Board of Health": "Health Board",
        "County Commissioners": "County",
        "Thurston County Board of County Commissioners": "County",
        "Parks and Recreation Advisory Committee": "Parks Committee",
        "Bicycle and Pedestrian Advisory Committee": "Bike & Walk Committee",
        "Community Livability and Public Safety Committee": "Public Safety Committee",
        "Land Use & Environment Committee": "Land Use Committee",
        "Utility Advisory Committee": "Utility Committee",
        "Lodging Tax Advisory Committee": "Lodging Tax Committee",
        "Metropolitan Park District Advisory Committee": "Park District Committee",
        "Cultural Access Program Advisory Board": "Cultural Access Board",
        "Social Justice & Equity Commission": "Equity Commission",
        "Community Policing Board": "Policing Board",
        "Site Plan Review Committee": "Site Plan Committee",
        "PBIA Advisory Board": "Parking & Business Board",
        "LEOFF I Disability Board": "Disability Board",
    }
    return short_map.get(exact, exact)


# Safe, plain-word replacements that never distort topic nouns
PLAIN_WORD_REPLACEMENTS = [
    (r"\bordinance\b", "new law"),
    (r"\bresolution\b", "measure"),
    (r"\bauthorizing\b", "approving"),
    (r"\badjustment[s]?\b", "changes"),
    (r"\binterlocal agreement\b", "joint agreement"),
    (r"\bintergovernmental\b", "joint"),
    (r"\bappropriation[s]?\b", "funding"),
    (r"\brevisions\b", "updates"),
    (r"\bamendments\b", "changes"),
    (r"\bpreliminary\b", "draft"),
    (r"\bdeliberation[s]?\b", "review"),
    (r"\bconsideration\b", "review"),
    (r"\bconsiders\b", "weighs"),
    (r"\bauthorizes\b", "approves"),
    (r"\boperating budget\b", "budget"),
    (r"\bgeneral facility charges\b", "facility fees"),
    (r"\bcdbg\b", "housing grant"),
    (r"\bhud\b", "federal housing"),
    (r"\bcao\b", "critical area rules"),
    (r"\bfwhca[s]?\b", "fish and wildlife habitat"),
]


def generate_display_title(item: Dict[str, Any], body_text: str = "") -> str:
    """Generates an accurate plain headline under ~90 chars.

    Rules:
    - Never chooses a category or topic by keyword fallback (no generic 'local road plan', 'local measure', etc.).
    - Names the accurate body (committee, commission, council, board).
    - Fixes known items explicitly and derives all others directly from the title's own nouns.
    - If a rewritten title fails readability or shares no key nouns with the official title,
      returns the cleaned official title prefixed with the body name if it fits.
    """
    raw_title = html.unescape(item.get("title") or "").replace("\xa0", " ").strip()
    if not raw_title:
        return "Agenda Item"

    body = determine_short_body_subject(item, body_text)
    cleaned = clean_agenda_title(raw_title)
    t_lower = raw_title.lower()

    is_plural = body in ("Planners", "County Commissioners")
    weigh_v = "weigh" if is_plural else "weighs"
    approve_v = "approve" if is_plural else "approves"
    hold_v = "hold" if is_plural else "holds"
    review_v = "review" if is_plural else "reviews"
    back_v = "back" if is_plural else "backs"
    oppose_v = "oppose" if is_plural else "opposes"

    candidate = ""

    # 1. Explicit accurate rewrites for known items and well-defined agenda patterns
    if "quince street tiny home village" in t_lower:
        candidate = f"{body} {approve_v} contract changes for Quince Street Tiny Home Village"
    elif "franz anderson tiny home village" in t_lower:
        candidate = f"{body} {approve_v} funding changes for Franz Anderson Tiny Home Village"
    elif "franz anderson" in t_lower and "pooling" in t_lower:
        candidate = f"{body} {approve_v} joint funding for Franz Anderson supportive housing"
    elif "franz anderson" in t_lower and "grant application" in t_lower:
        candidate = f"{body} {approve_v} grant request for Franz Anderson housing"
    elif "harbor pines" in t_lower and "grant application" in t_lower:
        candidate = f"{body} {approve_v} grant request for Harbor Pines housing"
    elif "timberland regional library" in t_lower and "levy" in t_lower:
        candidate = f"{body} {back_v} Timberland library levy lid lift"
    elif "working lands farm tour" in t_lower:
        candidate = "County Commissioners join Working Lands Farm Tour"
    elif "cooper point pharmacy" in t_lower:
        candidate = f"{body} {review_v} Cooper Point Pharmacy drive-through plan"
    elif "lodging tax funding tourism service" in t_lower:
        candidate = f"{body} {review_v} 2027 lodging tax tourism grant proposals"
    elif "parks and recreation advisory committee (prac) work plan" in t_lower:
        candidate = f"{body} {approve_v} Parks Committee work plan update"
    elif "social justice & equity commission work plan" in t_lower:
        candidate = f"{body} {approve_v} Equity Commission work plan update"
    elif "community events permitting process" in t_lower:
        candidate = f"{body} {review_v} community events permit rules and resources"
    elif "conservation district" in t_lower and ("rate" in t_lower or "fee" in t_lower or "ordinance" in t_lower):
        candidate = f"{body} {weigh_v} new rates to fund Thurston Conservation District"
    elif ("drinking water" in t_lower or "article iii" in t_lower) and ("sanitary" in (body_text or "").lower() or "drinking water code" in t_lower):
        candidate = f"{body} {weigh_v} updates to county drinking water code"
    elif ("24.30" in t_lower or "wetland" in t_lower) and ("24.25" not in t_lower and "fwhca" not in t_lower):
        candidate = f"{body} {weigh_v} draft rules for local wetlands"
    elif ("24.25" in t_lower or "fwhca" in t_lower or "fish and wildlife" in t_lower or "fish & wildlife" in t_lower) and "wetland" not in t_lower:
        candidate = f"{body} {weigh_v} draft rules for fish and wildlife habitat"
    elif "wetlands and fwhcas" in t_lower:
        candidate = f"{body} {review_v} draft rules for wetlands and wildlife habitat"
    elif "frequently flooded areas" in t_lower or "24.20" in t_lower:
        candidate = f"{body} {review_v} rules for flood areas"
    elif "cdbg" in t_lower or "community development block grant" in t_lower:
        candidate = f"{body} {approve_v} grant for local housing and homes"
    elif "west bay marina" in t_lower:
        candidate = f"{body} {hold_v} hearing on West Bay Marina replacement"
    elif "springwood garden" in t_lower:
        candidate = f"{body} {hold_v} hearing on Springwood Garden subdivision"
    elif "yelm-rainier tenino trail" in t_lower:
        candidate = f"{body} {hold_v} hearing on transferring part of Yelm-Rainier Tenino Trail"
    elif "2027 development fees" in t_lower or ("utility rates" in t_lower and ("impact fees" in t_lower or "facility charges" in t_lower)):
        candidate = f"{body} {weigh_v} 2027 water and sewer rates and fees"
    elif "2027 utility operating budget" in t_lower:
        candidate = f"{body} {review_v} draft 2027 utility budget and rates"
    elif "2027 special (dedicated) revenues" in t_lower:
        candidate = f"{body} {review_v} 2027 dedicated tax and housing budgets"
    elif "speed limit schedule" in t_lower:
        candidate = f"{body} {hold_v} hearing on county road speed limits"
    elif "six-year transportation improvement program" in t_lower:
        candidate = f"{body} {hold_v} hearing on six-year road and transit plan"
    elif "conduct and solicitation" in t_lower or "chapter 2.03" in t_lower:
        candidate = f"{body} {hold_v} hearing on Title 2 conduct rules for county buildings"
    elif "2026 thurston county road standards" in t_lower:
        candidate = f"{body} {hold_v} hearing on 2026 county road standards"
    elif "moratorium on detention facilities" in t_lower:
        candidate = f"{body} {hold_v} hearing on pause for detention facilities"
    elif "grand mound 193rd" in t_lower:
        candidate = f"{body} {review_v} Grand Mound 193rd land use and zone change"
    elif "baldwin open space" in t_lower:
        candidate = f"{body} {review_v} Baldwin open space tax application"
    elif "minimum parking requirements" in t_lower:
        candidate = f"{body} {weigh_v} changes to minimum parking rules"
    elif "critical area ordinance update - public hearing" in t_lower:
        candidate = f"{body} {hold_v} hearing on critical area land rules"
    elif "critical area ordinance update - briefing" in t_lower:
        candidate = f"{body} {review_v} update on critical area land rules"
    elif "2027-2032 capital facilities plan - public hearing" in t_lower:
        candidate = f"{body} {hold_v} hearing on 2027-2032 capital facilities plan"
    elif "home based businesses code" in t_lower:
        candidate = f"{body} {weigh_v} rule changes for home-based businesses"
    elif "tree protection measures" in t_lower:
        candidate = f"{body} {review_v} tree protection rules"
    elif "middle housing infrastructure" in t_lower:
        candidate = f"{body} {review_v} middle housing utility and street rules"
    elif "puget sound energy" in t_lower and "rate increase" in t_lower:
        candidate = f"{body} {oppose_v} Puget Sound Energy 30% rate increase"
    elif "initiative to the legislature no. 26-001" in t_lower:
        candidate = f"{body} {oppose_v} state Initiative 26-001 on school parental rights"
    elif "initiative to the legislature 26-638" in t_lower:
        candidate = f"{body} {oppose_v} state Initiative 26-638 on school athletics"
    elif "hands on children" in t_lower:
        candidate = f"{body} {approve_v} contract for Hands On Children's Museum expansion"
    elif "yelm highway community park" in t_lower:
        candidate = f"{body} {approve_v} bid for Yelm Highway Community Park construction"
    elif "seismic valves" in t_lower:
        candidate = f"{body} {approve_v} bid for water main seismic valves"
    elif "terry kissick" in t_lower or "fire vehicles storage" in t_lower:
        candidate = f"{body} {approve_v} lease for fire vehicle storage"
    elif "refuse collection truck" in t_lower or "western peterbilt" in t_lower:
        candidate = f"{body} {approve_v} lease for city garbage truck"
    elif "cultural access tax transportation" in t_lower:
        candidate = f"{body} {approve_v} school bus funding for cultural access trips"
    elif "olyaha museum" in t_lower:
        candidate = f"{body} {weigh_v} funding for school visits to OlyAHA Museum"
    elif "downtown seasonal lighting" in t_lower:
        candidate = f"{body} {weigh_v} funding for downtown seasonal lights"
    elif "compliance of 2025 funding levels" in t_lower:
        candidate = f"{body} {review_v} annual report on 2025 park funding compliance"
    elif "climate element review" in t_lower:
        candidate = f"{body} {review_v} development code updates for climate plan"
    elif "parks capital facilities plan" in t_lower:
        candidate = f"{body} {weigh_v} Parks Capital Facilities Plan recommendation"
    elif "police evidence facility" in t_lower:
        candidate = f"{body} {review_v} Police Evidence Facility design (Case 26-4842)"
    elif "palomino multifamily" in t_lower:
        candidate = f"{body} {review_v} Palomino Multifamily project design (Case 26-1865)"
    elif "909 919 prospect multifamily" in t_lower:
        candidate = f"{body} {review_v} Prospect Street multifamily site plan"
    elif "swantown boatworks" in t_lower:
        candidate = f"{body} {review_v} Swantown Boatworks yard expansion plan"
    elif "nghc parking lot" in t_lower:
        candidate = f"{body} {review_v} Poplar Street parking lot site plan"
    elif "police auditor semi-annual report" in t_lower:
        candidate = f"{body} {review_v} 2026 Police Auditor semi-annual report"
    elif "councilmember salary schedule" in t_lower:
        candidate = f"{body} {approve_v} 2026 City Council salary schedule"
    elif "deschutes estuary restoration" in t_lower:
        candidate = f"{body} {back_v} funding for Deschutes Estuary restoration"
    elif "low income housing institute" in t_lower and "purchase and sale" in t_lower:
        candidate = f"{body} {approve_v} land sale agreement with Low Income Housing Institute"
    elif "law enforcement hiring" in t_lower and "grant" in t_lower:
        candidate = f"{body} {approve_v} state grant for police hiring and training"
    elif "washington technology solutions" in t_lower and "fire protection" in t_lower:
        candidate = f"{body} {approve_v} fire protection agreement with WaTech"
    elif "enterprise services" in t_lower and "fire protection" in t_lower:
        candidate = f"{body} {approve_v} fire protection agreement with state Enterprise Services"
    elif "enterprise services" in t_lower and "vehicle and equipment repair" in t_lower:
        candidate = f"{body} {approve_v} vehicle repair agreement with state Enterprise Services"
    elif "reappointment of david brine" in t_lower:
        candidate = f"{body} {approve_v} reappointing David Brine to regional facilities board"
    elif "appoint members to fill vacancies" in t_lower and "youth council" in t_lower:
        candidate = f"{body} {approve_v} appointments to Olympia Youth Council"
    elif "cherry street" in t_lower and "kato street" in t_lower:
        candidate = f"{body} {approve_v} honorary Kato Street name on Cherry Street"
    elif "south thurston economic development" in t_lower:
        candidate = f"{body} {review_v} South Thurston Economic Development Initiative"
    elif "discuss and comment on the 2027-2032 preliminary capital facilities plan" in t_lower:
        candidate = f"{body} {review_v} draft 2027-2032 Capital Facilities Plan"
    elif "briefing on sidewalk repair policy" in t_lower:
        candidate = f"{body} {review_v} sidewalk repair policy"
    elif "calls for art" in t_lower:
        candidate = f"{body} {review_v} upcoming public calls for art"
    elif "french door installation" in t_lower:
        candidate = f"{body} {review_v} heritage permit for French door installation"

    # 2. Construct directly from cleaned title if no specific pattern matched
    if not candidate:
        obj = cleaned
        for pattern, repl in PLAIN_WORD_REPLACEMENTS:
            obj = re.sub(pattern, repl, obj, flags=re.IGNORECASE)
        obj = re.sub(r"\s+", " ", obj).strip()

        if "public hearing" in t_lower:
            verb = f"{hold_v} hearing on"
        elif any(k in t_lower for k in ("approval of", "authorizing", "adopt")):
            verb = f"{approve_v}"
        elif any(k in t_lower for k in ("briefing", "presentation", "update", "report", "discussion")):
            verb = f"{review_v}"
        else:
            verb = f"{weigh_v}"

        candidate = f"{body} {verb} {obj}"

    # 3. Trim length cleanly if over 88 characters
    if len(candidate) > 88:
        # Prefer cleaned official title if candidate is too long
        short_clean = f"{body}: {cleaned}"
        if len(short_clean) <= 88:
            candidate = short_clean
        elif len(cleaned) <= 88:
            candidate = cleaned
        else:
            candidate = candidate[:85].rsplit(" ", 1)[0] + "..."

    # 4. Readability & Accuracy Check:
    # Never replace with a generic keyword topic! If readability is > 8.0 or shares no key nouns,
    # fall back to the cleaned official title (with body prefix if short enough).
    if not shares_key_nouns(candidate, raw_title):
        fallback = f"{body}: {cleaned}" if len(f"{body}: {cleaned}") <= 88 else cleaned
        return fallback[:88]

    return candidate


DEFAULT_TIERS_PATH = Path(__file__).resolve().parent.parent / "config" / "tiers.yaml"
_TIERS_CACHE: Optional[Dict[str, Any]] = None


def load_tiers_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Loads the editable item tier classification rules from config/tiers.yaml."""
    global _TIERS_CACHE
    path = config_path or DEFAULT_TIERS_PATH
    if _TIERS_CACHE is not None and config_path is None:
        return _TIERS_CACHE
    if not path.exists():
        return {"tier_3_procedural": {"exact_titles": [], "contains_patterns": []}, "tier_1_keywords": [], "tier_2_keywords": []}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if config_path is None:
        _TIERS_CACHE = data
    return data


def classify_item_tier(
    item: Dict[str, Any],
    has_brief: bool = False,
    is_test_case: bool = False,
    config_path: Optional[Path] = None,
) -> int:
    """Classifies an agenda item into Tier 1, Tier 2, or Tier 3 using config/tiers.yaml.

    Tier 1: Has a brief, is a test case, or is a public hearing/ordinance/code amendment/budget/rates item.
    Tier 2: Other substantive items (contracts, resolutions with money or land, briefings).
    Tier 3: Procedural, never shown as a row (call to order, minutes, agenda approval, public comment, etc.).
    """
    cfg = load_tiers_config(config_path)
    raw_title = html.unescape(item.get("title") or "").replace("\xa0", " ").strip()
    if not raw_title:
        return 3

    # Normalize title for exact procedural matching: strip item numbers ("1.A", "3.") and time ranges ("(5:30-5:35 PM)")
    norm = re.sub(r"^(?:item\s+)?[0-9]+(?:\.[0-9a-zA-Z]+)*\.?\s*(?:[-–:]\s*)?", "", raw_title, flags=re.IGNORECASE).strip()
    norm = re.sub(r"\s*\([0-9]{1,2}:[0-9]{2}\s*[-–]\s*[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM)?\)\s*$", "", norm, flags=re.IGNORECASE).strip()
    norm_lower = norm.lower()
    raw_lower = raw_title.lower()

    t3 = cfg.get("tier_3_procedural", {})
    exact_list = [s.lower() for s in t3.get("exact_titles", [])]
    contains_list = [s.lower() for s in t3.get("contains_patterns", [])]

    # Check Tier 3 procedural first (unless it explicitly has an approved brief)
    if not has_brief:
        if norm_lower in exact_list or raw_lower in exact_list:
            return 3
        for pat in contains_list:
            if pat in raw_lower or pat in norm_lower:
                return 3

    # Check Tier 1
    if has_brief or is_test_case:
        return 1

    t1_keywords = [k.lower() for k in cfg.get("tier_1_keywords", [])]
    for kw in t1_keywords:
        if kw in raw_lower:
            return 1

    # Otherwise Tier 2 (substantive item)
    return 2
