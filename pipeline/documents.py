"""Document loader and factual text extractor for Loretta's Ledger.

Loads full extracted document texts from cached attachments, staff reports,
ordinances, and presentations, providing grounded facts for policy briefs.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DOCUMENTS_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "documents"


def get_attachment_text_for_url(url: str) -> Optional[str]:
    """Retrieves extracted text for a given attachment or media URL from cache."""
    if not url:
        return None

    # Granicus attachment: .../attachments/<uuid>.pdf
    m_granicus = re.search(r"attachments/([a-zA-Z0-9\-]+)\.(?:pdf|docx|html)", url)
    if m_granicus:
        uuid_str = m_granicus.group(1)
        txt_path = DOCUMENTS_CACHE_DIR / f"{uuid_str}.txt"
        if txt_path.exists():
            try:
                with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
            except Exception as e:
                logger.warning("Failed to read cached attachment text %s: %s", txt_path, e)

    # Thurston media: .../media/<id>
    m_thurston = re.search(r"media/(\d+)", url)
    if m_thurston:
        media_id = m_thurston.group(1)
        txt_path = DOCUMENTS_CACHE_DIR / f"thurston-{media_id}.txt"
        if txt_path.exists():
            try:
                with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
            except Exception as e:
                logger.warning("Failed to read cached media text %s: %s", txt_path, e)

    # General filename match
    fn = url.rstrip("/").split("/")[-1].replace(".pdf", ".txt").replace(".docx", ".txt")
    direct_path = DOCUMENTS_CACHE_DIR / fn
    if direct_path.exists():
        try:
            with open(direct_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception as e:
            pass

    return None


def get_all_documents_for_item(item: Dict[str, Any]) -> List[Dict[str, str]]:
    """Finds all associated documents and their extracted text for an item."""
    docs = []
    body_text = item.get("body_text") or ""
    item_url = item.get("url") or ""

    # Check main item URL
    main_text = get_attachment_text_for_url(item_url)
    if main_text:
        docs.append({
            "title": item.get("title") or "Main Document",
            "url": item_url,
            "text": main_text,
        })

    # Find all markdown links in body_text: [Title](URL)
    links = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", body_text)
    for title, url in links:
        txt = get_attachment_text_for_url(url)
        if txt and not any(d["url"] == url for d in docs):
            docs.append({
                "title": title,
                "url": url,
                "text": txt,
            })

    return docs


def extract_factual_profile(item: Dict[str, Any], docs: List[Dict[str, str]]) -> Dict[str, Any]:
    """Extracts concrete, item-specific policy facts from the full text of all documents."""
    title = item.get("title") or ""
    body = item.get("body_text") or ""
    all_text = "\n\n".join([body] + [d["text"] for d in docs])

    profile = {
        "title": title,
        "jurisdiction": item.get("jurisdiction"),
        "meeting_date": item.get("meeting_date"),
        "doc_count": len(docs),
        "docs_summary": [d["title"] for d in docs],
        "planners_and_staff": [],
        "applicants": [],
        "financial_amounts": [],
        "case_and_matter_numbers": [],
        "specific_locations": [],
        "key_regulatory_details": [],
        "litigation_and_mandates": [],
    }

    # Extract staff contacts / authors / presenters
    staff_patterns = [
        r"(?:Staff Contact|Planner|Associate Planner|Senior Planner|Presented by|Contact):\s*([A-Za-z\s\.\-]+(?:\s*,\s*[A-Za-z\s]+)?)",
        r"\b(Claire Swearingen|Jackson Ewing|Paula Smith|Summer Miller|Melanie Bruno)\b",
    ]
    for p in staff_patterns:
        for m in re.finditer(p, all_text, re.IGNORECASE):
            name = m.group(1).strip()
            name = re.sub(r"\s+", " ", name)
            if name and 4 <= len(name) < 40 and name not in profile["planners_and_staff"]:
                profile["planners_and_staff"].append(name)

    # Extract applicant / representative
    app_matches = re.finditer(r"(?:Applicant|Representative):\s*([^\n\r]+)", all_text, re.IGNORECASE)
    for m in app_matches:
        app_name = m.group(1).strip()
        app_name = re.sub(r"\s+", " ", app_name)
        if app_name and 4 <= len(app_name) < 50 and app_name not in profile["applicants"]:
            profile["applicants"].append(app_name)

    # Extract financial dollar amounts ($...)
    dollar_matches = re.finditer(r"\$\s?([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?)", all_text)
    for m in dollar_matches:
        amt = f"${m.group(1)}"
        val = float(m.group(1).replace(",", ""))
        if amt not in profile["financial_amounts"] and val >= 1000:
            profile["financial_amounts"].append(amt)

    # Extract case, file, or matter numbers
    case_matches = re.finditer(r"\b(?:Case|File No\.|Matter File|File Number|Contract No\.)[:\s]+([0-9A-Za-z\.\-]+)\b", all_text, re.IGNORECASE)
    for m in case_matches:
        c_num = m.group(1).strip()
        if c_num and 4 <= len(c_num) <= 25 and not c_num.lower() in ("shall", "could", "west") and c_num not in profile["case_and_matter_numbers"]:
            profile["case_and_matter_numbers"].append(c_num)

    # Extract addresses, parcels, and acreages
    loc_matches = re.finditer(r"\b(?:\d{3,5}\s+[A-Za-z0-9\s]+(?:Drive|Street|Avenue|Road|Boulevard|Blvd|Ave|St|Rd|Way|Dr)\s+(?:NW|NE|SE|SW)?|Parcel\s+(?:Number|No\.)?\s*[0-9]+|\b\d+\.?\d*\s+acres\b)", all_text, re.IGNORECASE)
    for m in loc_matches:
        loc = m.group(0).strip()
        loc = re.sub(r"\s+", " ", loc)
        if len(loc) >= 8 and not re.match(r"^\d+\s+[A-Za-z]+$", loc) and loc not in profile["specific_locations"]:
            profile["specific_locations"].append(loc)

    # Extract litigation / court references
    lit_matches = re.finditer(r"\b([A-Z][a-zA-Z\s]+v\.\s+[A-Z][a-zA-Z\s]+|King County v\. Turner|Preliminary Injunction)\b", all_text)
    for m in lit_matches:
        lit = m.group(1).strip()
        if lit and len(lit) < 40 and "ORDER" not in lit and lit not in profile["litigation_and_mandates"]:
            profile["litigation_and_mandates"].append(lit)

    # Extract key regulatory terms
    reg_terms = [
        "Site Potential Tree Height", "Riparian management zone", "WDFW", "PHS",
        "reduce lawn areas", "preserve seedbank", "Oregon White Oak", "subdivision clustering",
        "creosote-treated timber pilings", "solid-decked piers", "Determination of Non-significance",
        "preliminary plat", "wetland mitigation", "Article III", "Sanitary Code", "CDBG formula",
        "Affordable Housing and Homeless Services", "Local Housing Fund", "Inspire Olympia",
    ]
    for term in reg_terms:
        if re.search(r"\b" + re.escape(term) + r"\b", all_text, re.IGNORECASE):
            profile["key_regulatory_details"].append(term)

    return profile

