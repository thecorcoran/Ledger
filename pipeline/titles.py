"""Plain display title generator and readability checker for Loretta's Ledger.

Generates concise, plain-language headlines under ~90 characters in the form:
"[Body] [does what] [to what]"
Enforces Flesch-Kincaid Grade Level <= 8.0.
"""

import re
from typing import Any, Dict, List, Optional


def count_syllables(word: str) -> int:
    """Estimates the syllable count of an English word."""
    w = word.lower().strip(".:,;!?()\"'-")
    if not w:
        return 0
    # Digits e.g. "2027" -> treat as 2 syllables
    if w.isdigit():
        return 2
    if len(w) <= 3:
        return 1
    # Handle common suffixes
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


# Plain synonyms for agenda, legal, and bureaucratic jargon
JARGON_REPLACEMENTS = [
    (r"\bordinance\b", "new law"),
    (r"\bresolution\b", "measure"),
    (r"\bauthorizing\b", "approving"),
    (r"\badjustment[s]?\b", "changes"),
    (r"\binterlocal agreement\b", "joint agreement"),
    (r"\bintergovernmental\b", "joint"),
    (r"\bprocurement\b", "buying"),
    (r"\bappropriation[s]?\b", "funding"),
    (r"\bimplementation\b", "plan"),
    (r"\brevisions\b", "updates"),
    (r"\bamendments\b", "changes"),
    (r"\bcompensation\b", "pay"),
    (r"\bpreliminary\b", "draft"),
    (r"\bdeliberation[s]?\b", "review"),
    (r"\bconsideration\b", "review"),
    (r"\bconsiders\b", "weighs"),
    (r"\bdeliberates on\b", "weighs"),
    (r"\bauthorizes\b", "approves"),
    (r"\bdevelopment\b", "building"),
    (r"\boperating budget\b", "budget"),
    (r"\butility rates\b", "service rates"),
    (r"\butility\b", "water and sewer"),
    (r"\bcdbg\b", "housing grant"),
    (r"\bhud\b", "federal housing agency"),
    (r"\bcao\b", "land rules"),
    (r"\bfwhca[s]?\b", "wildlife habitat"),
    (r"\bsepa\b", "environmental review"),
    (r"\bgma\b", "growth law"),
    (r"\bgeneral facility charges\b", "facility fees"),
]


def clean_agenda_title(title: str) -> str:
    """Strips agenda prefixes like '4.J Approval of', '6.A', 'Public Hearing:', etc."""
    t = title.strip()
    t = re.sub(r"^(?:item\s+)?[0-9]+(?:\.[0-9a-zA-Z]+)*\.?\s*(?:[-–:]\s*)?", "", t, flags=re.IGNORECASE).strip()
    prefixes = [
        r"^public hearing\s*:\s*",
        r"^boh public hearing\s*:\s*",
        r"^draft\s*\([^)]*\)\s*-\s*",
        r"^draft\s*-\s*",
        r"^approval of a resolution authorizing\s*",
        r"^approval of a resolution\s*",
        r"^approval of the recommended\s*",
        r"^approval of recommended\s*",
        r"^approval of\s*",
        r"^review of the proposed\s*",
        r"^review of proposed\s*",
        r"^review of\s*",
        r"^public hearing on\s*",
        r"^resolution authorizing\s*",
    ]
    for p in prefixes:
        t = re.sub(p, "", t, flags=re.IGNORECASE).strip()
    return t


def determine_body_name(item: Dict[str, Any], raw_title: str) -> str:
    """Determines the short plain body name: 'County', 'City Council', 'Health Board', 'Planners'."""
    jur = item.get("jurisdiction", "").lower()
    t_lower = raw_title.lower()
    
    if "board of health" in t_lower or "boh" in t_lower:
        return "Health Board"
    if "planning commission" in t_lower or item.get("source_id", "") == "thurston-planning-commission":
        return "Planners"
    if "hearing examiner" in t_lower:
        return "Examiner"
    
    if jur == "olympia":
        return "City Council"
    elif jur == "thurston":
        return "County"
    return "Council"


def simplify_text_for_grade(text: str) -> str:
    """Progressively simplifies vocabulary to bring grade level <= 8.0."""
    t = text
    for pattern, repl in JARGON_REPLACEMENTS:
        t = re.sub(pattern, repl, t, flags=re.IGNORECASE)
    t = re.sub(r"\bconsiders\b", "weighs", t, flags=re.IGNORECASE)
    t = re.sub(r"\breviews\b", "weighs", t, flags=re.IGNORECASE)
    t = re.sub(r"\bexaminations\b", "tests", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmanagement\b", "rules", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def generate_display_title(item: Dict[str, Any], body_text: str = "") -> str:
    """Generates a plain headline under ~90 chars in the form:
    '[Body] [does what] [to what]'
    with Flesch-Kincaid Grade Level <= 8.0.
    """
    raw_title = item.get("title", "")
    body = determine_body_name(item, raw_title)
    cleaned = clean_agenda_title(raw_title)
    t_lower = raw_title.lower()
    
    candidate = ""
    
    # Specific targeted patterns based on topic/content
    if "conservation district" in t_lower and ("rate" in t_lower or "fee" in t_lower or "ordinance" in t_lower):
        candidate = f"{body} weighs new rates to fund local conservation district"
    
    elif "drinking water" in t_lower or "article iii" in t_lower:
        candidate = f"{body} weighs new rules for local drinking water"
    
    elif "wetlands" in t_lower:
        candidate = f"{body} weighs new land rules for local wetlands"
    
    elif "fwhca" in t_lower or "wildlife" in t_lower or "fish" in t_lower:
        candidate = f"{body} weighs new land rules for fish and wildlife habitats"
    
    elif "cdbg" in t_lower or "community development block grant" in t_lower or "hud" in t_lower:
        candidate = f"{body} approves federal grant for housing and homes"
    
    elif "utility rates" in t_lower or "utility operating budget" in t_lower or "facility charges" in t_lower:
        if "development fees" in t_lower or "impact fees" in t_lower:
            candidate = f"{body} weighs new city fees and service rates"
        else:
            candidate = f"{body} weighs new city budget and service rates"
    
    if not candidate:
        if "public hearing" in t_lower:
            verb = "holds hearing on"
        elif "grant" in t_lower and ("authoriz" in t_lower or "approv" in t_lower or "accept" in t_lower):
            verb = "accepts grant for"
        elif "contract" in t_lower or "agreement" in t_lower:
            verb = "weighs agreement for"
        elif "approv" in t_lower or "adopt" in t_lower:
            verb = "votes on"
        else:
            verb = "weighs"
        
        obj = cleaned
        for pattern, repl in JARGON_REPLACEMENTS:
            obj = re.sub(pattern, repl, obj, flags=re.IGNORECASE)
        obj = re.sub(r"\s+", " ", obj).strip()
        candidate = f"{body} {verb} {obj}"
    
    # Enforce length under 90 chars
    if len(candidate) > 88:
        candidate = candidate[:85].rsplit(" ", 1)[0] + "..."
    
    # Readability check: enforce grade <= 8.0
    grade = flesch_kincaid_grade_level(candidate)
    if grade > 8.0:
        simplified = simplify_text_for_grade(candidate)
        if len(simplified) > 88:
            simplified = simplified[:85].rsplit(" ", 1)[0] + "..."
        simpler_grade = flesch_kincaid_grade_level(simplified)
        if simpler_grade <= 8.0:
            candidate = simplified
        else:
            # Fallback to plain, short words
            short_topic = "local policy plan"
            if "water" in t_lower:
                short_topic = "clean water plan"
            elif "rate" in t_lower or "fee" in t_lower:
                short_topic = "new local fee plan"
            elif "land" in t_lower or "zone" in t_lower:
                short_topic = "new land use plan"
            elif "road" in t_lower or "street" in t_lower:
                short_topic = "local road plan"
            elif "grant" in t_lower:
                short_topic = "new grant funds"
            candidate = f"{body} weighs {short_topic}"
            
    # Final safety check
    final_grade = flesch_kincaid_grade_level(candidate)
    if final_grade > 8.0:
        candidate = f"{body} votes on local measure"
        
    return candidate
