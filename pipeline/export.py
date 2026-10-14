"""Static site exporter and broadsheet / account-book ledger designer for Loretta's Ledger.

Information architecture:
- Nav: Home | Briefs | Who's in the Room | Principles | About
- Home (index.html):
  (a) One sentence on what the Ledger is.
  (b) "Worth your attention": up to 6 Tier 1 upcoming items, each with plain headline, date and days remaining, body, one-line stake (only when supported by documents), and a "Read the brief" button.
  (c) "Just happened": items from the last 7 days.
  (d) "Also on the calendar": collapsed and grouped by meeting (e.g. "Olympia City Council, Oct 13: 2 items"), Tier 2 only.
- Briefs (briefs.html, with matters.html alias):
  One row per matter (merged duplicates, no Tier 3 procedural rows).
  Shows plain headline, body, date, and plain status ("Hearing", "Vote", "Recommendation stage", "Brief ready", "Outcome not yet posted").
  Hides internal identifiers. Filters: Upcoming, Past, Test cases.
  The brief and its timeline are on the same page (`briefs/brief-<item_id>.html`).
"""

import argparse
import html
import json
import logging
import re
import shutil
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from pipeline.calendar import build_and_export_calendars
from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.titles import (
    classify_item_tier,
    clean_agenda_title,
    determine_exact_body_name,
    generate_display_title,
)

logger = logging.getLogger(__name__)

DEFAULT_TODAY_STR = "2026-10-12"


def get_whats_at_stake(item: Dict[str, Any]) -> Optional[str]:
    """Returns a concise, one-line summary of the core public stake ONLY when supported by documents.

    Returns None when the documents do not support a specific stake (never returns generic filler).
    """
    title = html.unescape(item.get("title") or "").lower()
    if "conservation district" in title and ("rate" in title or "ordinance" in title):
        return "Proposed multi-year assessment adjustment on real property parcels under RCW 89.08.400 to fund conservation district operations."
    if "drinking water" in title or "article iii" in title:
        return "Revisions to Sanitary Code Article III governing Group B private and shared residential drinking water wells."
    if ("wetland" in title or "24.30" in title) and "fwhca" not in title:
        return "Updates to Critical Areas Ordinance Chapter 24.30 wetland buffer widths, rating manuals, and reasonable-use rules."
    if "fwhca" in title or "24.25" in title or "fish and wildlife" in title or "fish & wildlife" in title:
        return "Updates to Chapter 24.25 stream riparian management zones, prairie soil rules, and residential yard allowances."
    if any(k in title for k in ("cdbg", "hud", "housing and urban development")):
        return "Acceptance of $376,415.00 in federal Community Development Block Grant funds with federal compliance conditions."
    if "development fees" in title or ("impact fees" in title and "utility rates" in title):
        return "Proposed 2027 rate and fee adjustments for municipal water, sewer, stormwater, and development impact charges."
    if "2027 utility operating budget" in title:
        return "Draft 2027 utility operating budget, capital facilities plan, and general facility connection charges."
    if "2027 special (dedicated) revenues" in title:
        return "Proposed 2027 allocations for Lodging Tax, Inspire Olympia, PBIA, and housing/homeless response funds."
    if "west bay marina" in title:
        return "Shoreline substantial development permit and environmental mitigation conditions for replacing West Bay Marina."
    if "springwood garden" in title:
        return "Preliminary plat approval for a 37-lot single-family subdivision on 7.2 acres with dedicated wetland buffers."
    if "quince street" in title:
        return "Intergovernmental contract amendments with Thurston County funding operations at Quince Street Tiny Home Village."
    if "franz anderson" in title and "tiny home" in title:
        return "Funding agreement amendment supporting operations at the Franz Anderson Tiny Home Village."
    if "timberland regional library" in title and "levy" in title:
        return "Council endorsement of Timberland Regional Library District Proposition No. 1 property tax levy lid lift."
    if "yelm-rainier tenino trail" in title:
        return "Proposed intergovernmental transfer of county-owned portions of the Yelm-Rainier-Tenino Trail to the City of Yelm."
    if "speed limit schedule" in title:
        return "Proposed ordinance revising maximum vehicle speed limits across designated unincorporated county roads."
    if "six-year transportation improvement program" in title:
        return "Six-year county road, bridge, and trail capital improvement priority list for 2027–2032."
    if "conduct and solicitation" in title or "chapter 2.03" in title:
        return "New county code chapter regulating public conduct and solicitation inside Thurston County facilities."
    if "minimum parking requirements" in title:
        return "Proposed municipal zoning code amendments reducing or removing minimum off-street parking mandates."
    if "critical area ordinance update" in title:
        return "City of Olympia updates to critical area buffer regulations and environmental review rules."
    if "home based businesses" in title:
        return "Proposed municipal code revisions governing residential home-based business permits and neighborhood limits."
    return None


def format_short_date(date_str: str) -> str:
    """Formats '2026-10-13' as 'Oct 13, 2026' or 'Oct 13'."""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return date_str or "Date not specified"


def format_month_day(date_str: str) -> str:
    """Formats '2026-10-13' as 'Oct 13'."""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.strftime("%b %-d")
    except Exception:
        return date_str or ""


def format_days_remaining(meeting_date: str, today_str: str) -> str:
    """Returns plain days-remaining label relative to today_str."""
    try:
        m_dt = datetime.strptime(meeting_date[:10], "%Y-%m-%d").date()
        t_dt = datetime.strptime(today_str[:10], "%Y-%m-%d").date()
        diff = (m_dt - t_dt).days
        if diff == 0:
            return "Today"
        if diff == 1:
            return "Tomorrow (1 day left)"
        if diff > 1:
            return f"In {diff} days"
        if diff == -1:
            return "Yesterday"
        return f"{abs(diff)} days ago"
    except Exception:
        return ""


def determine_plain_status(
    item: Dict[str, Any],
    body_name: str,
    has_brief: bool,
    today_str: str,
) -> str:
    """Returns one of the five plain status labels:
    'Hearing', 'Vote', 'Recommendation stage', 'Brief ready', 'Outcome not yet posted'.
    Never returns 'Deliberation'.
    """
    meeting_date = (item.get("meeting_date") or "")[:10]
    title_lower = (item.get("title") or "").lower()
    is_past = bool(meeting_date and meeting_date < today_str[:10])
    is_advisory = any(
        k in body_name.lower()
        for k in ("commission", "committee", "advisory", "board")
    ) and "county commissioners" not in body_name.lower() and "board of health" not in body_name.lower()

    if is_past:
        if has_brief:
            return "Brief ready"
        return "Outcome not yet posted"

    if "hearing" in title_lower:
        return "Hearing"
    if is_advisory:
        return "Recommendation stage"
    if has_brief:
        return "Brief ready"
    return "Vote"


def normalize_dedupe_key(item: Dict[str, Any], display_title: str) -> str:
    """Builds a canonical key to merge duplicate matters/items across IDs and stages."""
    raw = html.unescape(item.get("title") or "")
    raw_lower = raw.lower()
    jur = (item.get("jurisdiction") or "").lower()
    # Check for explicit Case number (e.g. 25-1692, 25-0980, 26-5942)
    m_case = re.search(r"\b(?:case|permit)[:\s#]*([0-9]{2}-[0-9]{4})\b", raw, re.IGNORECASE)
    if m_case:
        return f"case-{m_case.group(1)}"
    # Check for TCC chapter or Thurston CAO subject (e.g. 24.30 Wetlands, 24.25 FWHCA, 24.20 Flood)
    m_chap = re.search(r"\bchapter\s+(24\.[0-9]+)\b", raw, re.IGNORECASE)
    if m_chap:
        return f"tcc-{m_chap.group(1)}"
    if jur == "thurston" or "cao" in raw_lower:
        if "fwhca" in raw_lower or "fish and wildlife" in raw_lower or "fish & wildlife" in raw_lower:
            return "tcc-24.25"
        if "wetland" in raw_lower:
            return "tcc-24.30"
        if "frequently flooded" in raw_lower:
            return "tcc-24.20"
    # Multi-stage Olympia matters
    if "critical area ordinance update" in raw_lower:
        return "olympia-cao-update"
    if "development fees" in raw_lower and "utility rates" in raw_lower:
        return "olympia-2027-utility-rates-fees"
    if "2027-2032 capital facilities plan" in raw_lower or "capital facilities plan 2027-2032" in raw_lower:
        return "olympia-2027-2032-cfp"
    if "2027 budget update" in raw_lower or "2027 proposed budget update" in raw_lower:
        return "olympia-2027-budget-update"
    # Otherwise normalize display_title
    norm_disp = re.sub(r"[^a-z0-9]+", "-", display_title.lower()).strip("-")
    return f"title-{norm_disp}"


ROOT_DIR = Path(__file__).resolve().parent.parent
SITE_SRC_DIR = ROOT_DIR / "site" / "src"
SITE_OUT_DIR = ROOT_DIR / "site" / "_site"
DOCS_DIR = ROOT_DIR / "docs"
BRIEFS_DIR = ROOT_DIR / "briefs"
CONFIG_DIR = ROOT_DIR / "config"


def ensure_directories():
    for base in (SITE_SRC_DIR, SITE_OUT_DIR, DOCS_DIR):
        for legacy_sub in ("matters", "archive", "test-cases", "influences", "action-pages"):
            legacy_path = base / legacy_sub
            if legacy_path.exists():
                shutil.rmtree(legacy_path, ignore_errors=True)
    for d in (
        SITE_SRC_DIR / "briefs",
        SITE_SRC_DIR / "actors",
        SITE_SRC_DIR / "reading",
        SITE_OUT_DIR / "briefs",
        SITE_OUT_DIR / "actors",
        SITE_OUT_DIR / "reading",
        DOCS_DIR / "briefs",
        DOCS_DIR / "actors",
        DOCS_DIR / "reading",
        BRIEFS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def markdown_to_html(md_text: str) -> str:
    """Broadsheet-styled Markdown to HTML converter."""
    lines = md_text.split("\n")
    html_lines = []
    in_code = False
    in_ul = False
    in_table = False

    for line in lines:
        if line.startswith("```"):
            if in_code:
                html_lines.append("</code></pre>")
                in_code = False
            else:
                html_lines.append("<pre><code>")
                in_code = True
            continue

        if in_code:
            html_lines.append(html.escape(line))
            continue

        stripped = line.strip()

        # Markdown Table
        if stripped.startswith("|") and stripped.endswith("|"):
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.match(r"^:?-+:?$", c) for c in cells if c):
                continue
            if not in_table:
                html_lines.append('<table class="ledger-table">')
                html_lines.append("<thead><tr>")
                for c in cells:
                    cell_html = re_links(re_italic(re_bold(html.escape(c))))
                    html_lines.append(f"<th>{cell_html}</th>")
                html_lines.append("</tr></thead><tbody>")
                in_table = True
            else:
                html_lines.append("<tr>")
                for c in cells:
                    cell_html = re_links(re_italic(re_bold(html.escape(c))))
                    html_lines.append(f"<td>{cell_html}</td>")
                html_lines.append("</tr>")
            continue
        else:
            if in_table:
                html_lines.append("</tbody></table>")
                in_table = False

        # Unordered list
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_ul:
                html_lines.append('<ul class="ledger-list">')
                in_ul = True
            raw_content = stripped[2:]
            verdict_attr = ""
            m_verdict = re.search(r"<!--\s*verdict:\s*(.*?)\s*-->", raw_content)
            if m_verdict:
                verdict_val = html.escape(m_verdict.group(1).strip())
                verdict_attr = f' data-verdict="{verdict_val}"'
                raw_content = re.sub(r"\s*<!--\s*verdict:\s*.*?\s*-->", "", raw_content)

            content = html.escape(raw_content)
            content = re_bold(content)
            content = re_italic(content)
            content = re_links(content)
            html_lines.append(f"<li{verdict_attr}>{content}</li>")
            continue
        else:
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False

        if not stripped:
            html_lines.append('<div class="spacer"></div>')
            continue

        # Headings
        if stripped.startswith("# "):
            html_lines.append(f'<h1 class="headline-title">{html.escape(stripped[2:])}</h1>')
        elif stripped.startswith("## "):
            html_lines.append(f'<h2 class="section-label">{html.escape(stripped[3:])}</h2>')
        elif stripped.startswith("### "):
            html_lines.append(f'<h3 class="subsection-label">{html.escape(stripped[4:])}</h3>')
        elif stripped.startswith("> "):
            html_lines.append(f'<blockquote class="ledger-quote">{html.escape(stripped[2:])}</blockquote>')
        else:
            content = html.escape(stripped)
            content = re_bold(content)
            content = re_italic(content)
            content = re_links(content)
            html_lines.append(f"<p>{content}</p>")

    if in_ul:
        html_lines.append("</ul>")
    if in_table:
        html_lines.append("</tbody></table>")
    if in_code:
        html_lines.append("</code></pre>")

    return "\n".join(html_lines)


def re_bold(text: str) -> str:
    return re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)


def re_italic(text: str) -> str:
    return re.sub(r"(?<!\*)\*([^\*\n]+)\*(?!\*)", r"<em>\1</em>", text)


def re_links(text: str) -> str:
    def _replace_link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if url.startswith(("http://", "https://")):
            return f'<a href="{url}" target="_blank" rel="noopener">{label}</a>'
        return f'<a href="{url}">{label}</a>'

    return re.sub(r"\[(.*?)\]\((.*?)\)", _replace_link, text)


def get_base_html_page(
    title: str,
    content: str,
    active_nav: str = "home",
    root_prefix: str = "",
    og_description: Optional[str] = None,
    og_url: Optional[str] = None,
) -> str:
    """Account-book and broadsheet layout template with unified 5-item nav:
    Home | Briefs | Who's in the Room | Principles | About
    """
    desc = og_description or "Independent public account of local decisions, upstream influences, and policy trajectories in Olympia and Thurston County."
    og_url_tag = f'\n    <meta property="og:url" content="{html.escape(og_url)}">' if og_url else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(title)} — Loretta's Ledger</title>
    <meta property="og:site_name" content="Loretta's Ledger">
    <meta property="og:title" content="{html.escape(title)} — Loretta's Ledger">
    <meta property="og:description" content="{html.escape(desc)}">
    <meta property="og:type" content="{'article' if active_nav in ('briefs', 'matters') else 'website'}">{og_url_tag}
    <meta name="description" content="{html.escape(desc)}">
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="{html.escape(title)} — Loretta's Ledger">
    <meta name="twitter:description" content="{html.escape(desc)}">
    <style>
        :root {{
            --bg: #fcfaf5;
            --surface: #ffffff;
            --ink: #1c1a17;
            --ink-muted: #575249;
            --ink-faint: #807a70;
            --rule: #ded8cb;
            --rule-dark: #2c2824;
            --oxblood: #7b2a26;
            --oxblood-tint: #f5ebea;
            --font-serif: "Iowan Old Style", "Palatino Linotype", "URW Palladio L", Palatino, Georgia, serif;
            --font-mono: "SF Mono", "Consolas", "Liberation Mono", Courier, monospace;
        }}

        * {{ box-sizing: border-box; }}

        body {{
            font-family: var(--font-serif);
            background-color: var(--bg);
            color: var(--ink);
            margin: 0;
            padding: 0;
            line-height: 1.55;
            font-size: 16px;
            -webkit-font-smoothing: antialiased;
        }}

        header.broadsheet-masthead {{
            background: var(--bg);
            border-bottom: 2px solid var(--rule-dark);
            padding: 24px 20px 14px 20px;
        }}

        .masthead-inner {{
            max-width: 1040px;
            margin: 0 auto;
        }}

        .top-row {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            border-bottom: 1px solid var(--rule);
            padding-bottom: 8px;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--ink-muted);
        }}

        .top-row span.meta-date {{
            font-variant-numeric: tabular-nums;
        }}

        .masthead-title {{
            text-align: center;
            margin: 18px 0 6px 0;
        }}

        .masthead-title a {{
            color: var(--ink);
            text-decoration: none;
            font-size: 38px;
            font-weight: 700;
            letter-spacing: -0.01em;
            text-transform: uppercase;
        }}

        .masthead-subtitle {{
            text-align: center;
            font-size: 14px;
            font-style: italic;
            color: var(--ink-muted);
            margin: 0 0 16px 0;
        }}

        nav.ledger-nav {{
            border-top: 1px solid var(--rule-dark);
            border-bottom: 1px solid var(--rule-dark);
            padding: 8px 0;
            text-align: center;
        }}

        nav.ledger-nav a {{
            color: var(--ink);
            text-decoration: none;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin: 0 16px;
            padding: 4px 2px;
            font-weight: 600;
        }}

        nav.ledger-nav a:hover, nav.ledger-nav a.active {{
            color: var(--oxblood);
            border-bottom: 2px solid var(--oxblood);
        }}

        .container {{
            max-width: 1040px;
            margin: 0 auto;
            padding: 32px 20px 70px 20px;
        }}

        h1.headline-title {{
            font-size: 28px;
            font-weight: 700;
            line-height: 1.25;
            margin: 0 0 16px 0;
            color: var(--ink);
            border-bottom: 2px solid var(--rule-dark);
            padding-bottom: 10px;
        }}

        h2.section-label {{
            font-size: 17px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--oxblood);
            border-bottom: 1px solid var(--rule);
            padding-bottom: 4px;
            margin: 32px 0 14px 0;
        }}

        h3.subsection-label {{
            font-size: 16px;
            font-weight: 700;
            margin: 20px 0 8px 0;
            color: var(--ink);
        }}

        p {{
            margin: 0 0 14px 0;
        }}

        a {{
            color: var(--oxblood);
            text-decoration: underline;
            text-underline-offset: 2px;
        }}

        .btn-brief {{
            display: inline-block;
            background: var(--oxblood);
            color: #ffffff !important;
            text-decoration: none !important;
            padding: 6px 14px;
            font-size: 13px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            border: 1px solid var(--oxblood);
            transition: background 0.15s ease;
        }}

        .btn-brief:hover {{
            background: #521a17;
        }}

        .btn-secondary {{
            display: inline-block;
            background: transparent;
            color: var(--oxblood) !important;
            text-decoration: none !important;
            padding: 5px 12px;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            border: 1px solid var(--oxblood);
            font-family: inherit;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .btn-secondary:hover {{
            background: var(--oxblood-tint);
        }}

        button.btn-brief, button.btn-secondary {{
            font-family: inherit;
            cursor: pointer;
        }}

        .share-box {{
            background: #ffffff;
            border: 1px solid var(--rule);
            border-left: 4px solid var(--oxblood);
        }}

        .share-actions {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
        }}

        .share-post-preview summary {{
            outline: none;
            user-select: none;
        }}

        .share-post-preview summary:hover {{
            color: var(--oxblood);
        }}

        .share-bottom-box {{
            background: #fdfbf7;
            border: 1px solid var(--rule);
            border-left: 4px solid var(--oxblood);
            padding: 16px 20px;
            margin-top: 28px;
            margin-bottom: 24px;
        }}

        .attention-card {{
            background: var(--surface);
            border: 1px solid var(--rule);
            border-left: 4px solid var(--oxblood);
            padding: 18px 20px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 20px;
        }}

        .attention-card-main {{
            flex: 1;
        }}

        .attention-card-action {{
            flex-shrink: 0;
            padding-top: 4px;
        }}

        details.calendar-group {{
            border-bottom: 1px solid var(--rule);
            padding: 10px 0;
        }}

        details.calendar-group summary {{
            cursor: pointer;
            font-weight: 600;
            font-size: 15px;
            color: var(--ink);
            list-style: none;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        details.calendar-group summary::-webkit-details-marker {{
            display: none;
        }}

        details.calendar-group summary::after {{
            content: "+ Show items";
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--oxblood);
            font-weight: 600;
        }}

        details.calendar-group[open] summary::after {{
            content: "− Hide items";
        }}

        /* Ledger table styles */
        table.ledger-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
            margin: 20px 0 32px 0;
            border-top: 2px solid var(--rule-dark);
            border-bottom: 2px solid var(--rule-dark);
        }}

        table.ledger-table th {{
            text-align: left;
            padding: 9px 12px;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            background: #f4eee2;
            border-bottom: 1px solid var(--rule-dark);
            color: var(--ink);
        }}

        table.ledger-table td {{
            padding: 10px 12px;
            border-bottom: 1px solid var(--rule);
            vertical-align: top;
            font-variant-numeric: tabular-nums;
        }}

        table.ledger-table tr:last-child td {{
            border-bottom: none;
        }}

        /* Ruled boxes and entries */
        .ledger-entry {{
            border-bottom: 1px solid var(--rule);
            padding: 16px 0;
        }}

        .ledger-entry:first-child {{
            padding-top: 0;
        }}

        .ledger-meta {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--ink-faint);
            margin-bottom: 5px;
        }}

        .tag-oxblood {{
            color: var(--oxblood);
            font-weight: 700;
        }}

        .tag-testcase {{
            background: var(--oxblood-tint);
            color: var(--oxblood);
            border: 1px solid var(--oxblood);
            padding: 2px 6px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-right: 6px;
            display: inline-block;
        }}

        .status-badge {{
            display: inline-block;
            font-size: 12px;
            font-weight: 600;
            padding: 2px 8px;
            border: 1px solid var(--rule-dark);
            background: #f4eee2;
            color: var(--ink);
        }}

        .status-badge.ready {{
            background: var(--oxblood-tint);
            border-color: var(--oxblood);
            color: var(--oxblood);
        }}

        .timeline-block {{
            border-left: 2px solid var(--oxblood);
            padding-left: 18px;
            margin: 16px 0;
        }}

        .timeline-item {{
            margin-bottom: 14px;
        }}

        .memo-box {{
            background: #fff;
            border: 1px solid var(--rule);
            border-left: 4px solid var(--oxblood);
            padding: 18px 20px;
            margin: 20px 0;
        }}

        ul.ledger-list {{
            margin: 8px 0 16px 20px;
            padding: 0;
        }}

        ul.ledger-list li {{
            margin-bottom: 6px;
        }}

        .spacer {{ height: 10px; }}

        footer.broadsheet-footer {{
            border-top: 2px solid var(--rule-dark);
            padding: 26px 20px;
            text-align: center;
            font-size: 13px;
            color: var(--ink-muted);
            background: var(--bg);
        }}

        @media (max-width: 768px) {{
            .top-row {{ flex-direction: column; gap: 4px; }}
            nav.ledger-nav a {{ display: inline-block; margin: 4px 8px; font-size: 13px; }}
            .masthead-title a {{ font-size: 28px; }}
            .attention-card {{ flex-direction: column; gap: 12px; }}
            table.ledger-table {{ display: block; overflow-x: auto; }}
        }}

        @media print {{
            body {{ background: #fff; color: #000; font-size: 11pt; }}
            header.broadsheet-masthead, footer.broadsheet-footer, nav.ledger-nav {{ display: none; }}
            .share-box, .share-bottom-box {{ display: none !important; }}
            .container {{ max-width: 100%; padding: 0; }}
            a {{ text-decoration: none; color: #000; }}
            .memo-box {{ border: 1px solid #ccc; }}
        }}
    </style>
</head>
<body>
    <header class="broadsheet-masthead">
        <div class="masthead-inner">
            <div class="top-row">
                <span>Independent Public Account &bull; Olympia &amp; Thurston County</span>
                <span class="meta-date">Vol. I &bull; Record Current to October 2026</span>
            </div>
            <div class="masthead-title">
                <a href="{root_prefix}index.html">Loretta's Ledger</a>
            </div>
            <div class="masthead-subtitle">
                An Account of Local Decisions, Upstream Influences, and Public Trajectory
            </div>
            <nav class="ledger-nav">
                <a href="{root_prefix}index.html" class="{'active' if active_nav in ('home', 'this-week') else ''}">Home</a>
                <a href="{root_prefix}briefs.html" class="{'active' if active_nav in ('briefs', 'matters') else ''}">Briefs</a>
                <a href="{root_prefix}actors.html" class="{'active' if active_nav == 'actors' else ''}">Who's in the Room</a>
                <a href="{root_prefix}principles.html" class="{'active' if active_nav == 'principles' else ''}">Principles</a>
                <a href="{root_prefix}about.html" class="{'active' if active_nav == 'about' else ''}">About</a>
            </nav>
        </div>
    </header>
    <div class="container">
        {content}
    </div>
    <footer class="broadsheet-footer">
        Loretta's Ledger &bull; Documented Public Record for Thurston County and City of Olympia &bull; Strictly Factual &bull; <a href="{root_prefix}how-to-take-part.html">How to Take Part</a> &bull; <a href="{root_prefix}calendar.html">Calendar</a> &bull; <a href="{root_prefix}glossary.html">Glossary</a> &bull; <a href="{root_prefix}reading/">Further Reading</a>
    </footer>
    <script>
    function getBriefShareUrl(btn) {{
        if (btn && btn.getAttribute("data-url")) {{
            var u = btn.getAttribute("data-url");
            if (window.location.protocol === "http:" || window.location.protocol === "https:") {{
                if (window.location.hostname !== "localhost" && window.location.hostname !== "127.0.0.1") {{
                    return window.location.href.split("#")[0].split("?")[0];
                }}
            }}
            return u;
        }}
        return window.location.href.split("#")[0].split("?")[0];
    }}

    function copyBriefLink(btn) {{
        var url = getBriefShareUrl(btn);
        navigator.clipboard.writeText(url).then(function() {{
            var old = btn.innerHTML;
            btn.innerHTML = "✓ Copied Link!";
            setTimeout(function() {{ btn.innerHTML = old; }}, 2200);
        }}).catch(function() {{
            prompt("Copy brief link:", url);
        }});
    }}

    function copyBriefPost(btn) {{
        var pre = document.getElementById("share-post-content");
        var txt = pre ? pre.innerText : "";
        if (!txt && btn && btn.getAttribute("data-url")) {{
            txt = getBriefShareUrl(btn);
        }}
        navigator.clipboard.writeText(txt).then(function() {{
            var old = btn.innerHTML;
            btn.innerHTML = "✓ Copied Post!";
            setTimeout(function() {{ btn.innerHTML = old; }}, 2200);
        }}).catch(function() {{
            prompt("Copy post text:", txt);
        }});
    }}

    function nativeShareBrief(btn) {{
        if (navigator.share) {{
            var url = getBriefShareUrl(btn);
            var title = document.title ? document.title.replace(" — Loretta's Ledger", "") : "Policy Brief";
            navigator.share({{
                title: title,
                text: title + " — Loretta's Ledger",
                url: url
            }}).catch(function(e) {{}});
        }}
    }}

    function copyContactAddress(btn, addr) {{
        navigator.clipboard.writeText(addr).then(function() {{
            var old = btn.innerHTML;
            btn.innerHTML = "✓ Copied Address!";
            setTimeout(function() {{ btn.innerHTML = old; }}, 2200);
        }}).catch(function() {{
            prompt("Copy address:", addr);
        }});
    }}

    function copyDraftText(btn) {{
        var draft = btn.getAttribute("data-draft") || "";
        navigator.clipboard.writeText(draft).then(function() {{
            var old = btn.innerHTML;
            btn.innerHTML = "✓ Copied Draft!";
            setTimeout(function() {{ btn.innerHTML = old; }}, 2200);
        }}).catch(function() {{
            prompt("Copy draft text:", draft);
        }});
    }}

    function copyCalendarLink(btn) {{
        var url = btn.getAttribute("data-url") || "";
        navigator.clipboard.writeText(url).then(function() {{
            var old = btn.innerHTML;
            btn.innerHTML = "✓ Copied Calendar Link!";
            setTimeout(function() {{ btn.innerHTML = old; }}, 2200);
        }}).catch(function() {{
            prompt("Copy calendar link:", url);
        }});
    }}

    document.addEventListener("DOMContentLoaded", function() {{
        if (navigator.share) {{
            var nativeBtns = document.querySelectorAll(".btn-share-native");
            nativeBtns.forEach(function(b) {{ b.style.display = "inline-block"; }});
        }}
    }});
    </script>
</body>
</html>
"""


def build_item_timeline_html(conn, item: Dict[str, Any], dedupe_key: str, matter: Optional[Dict[str, Any]] = None) -> str:
    """Builds the Status, History, Key Document Versions, and Related Matters sections at the bottom of a Matter/Brief page."""
    matter_id = matter["id"] if matter else None
    if not matter_id:
        m_row = conn.execute(
            "SELECT m.* FROM matters m JOIN matter_events me ON m.id = me.matter_id WHERE me.item_id = ? AND me.confidence = 'confirmed' LIMIT 1",
            (item["id"],),
        ).fetchone()
        if m_row:
            matter = dict(m_row)
            matter_id = matter["id"]

    # 1. Brief Change Log (from matter_briefs)
    changelog_html = ""
    if matter_id:
        mb_row = conn.execute("SELECT changelog_json, updated_date FROM matter_briefs WHERE matter_id = ?", (matter_id,)).fetchone()
        if mb_row and mb_row["changelog_json"]:
            try:
                cl_list = json.loads(mb_row["changelog_json"])
            except Exception:
                cl_list = []
            if cl_list:
                cl_items = "".join(
                    f'<li><strong>{html.escape(entry.get("date", ""))}:</strong> {html.escape(entry.get("summary", ""))}</li>'
                    for entry in cl_list
                )
                changelog_html = f"""
                <div class="memo-box" style="margin-top: 24px;">
                    <div class="ledger-meta" style="margin-bottom: 6px;">Living Brief Revision Record</div>
                    <h3 style="margin: 0 0 8px 0; font-size: 16px;">Brief Change Log</h3>
                    <ul class="ledger-list" style="margin: 0;">
                        {cl_items}
                    </ul>
                </div>
                """

    # 2. Matter Status Summary Box
    status_box_html = ""
    if matter:
        m_type_label = (matter.get("matter_type") or "ordinance").replace("_", " ").title()
        stable_id = matter.get("stable_id") or matter.get("primary_identifier") or matter["id"]
        lifecycle_st = matter.get("lifecycle_status") or matter.get("current_stage") or "Active"
        archive_st = (matter.get("archive_state") or "active").capitalize()
        last_dt = format_short_date(matter.get("last_action_date") or "")
        last_body = matter.get("last_action_body") or ""
        last_sum = matter.get("last_action_summary") or ""
        last_url = matter.get("last_action_url") or item.get("url") or ""
        next_txt = matter.get("next_action_text") or "not yet posted"

        status_box_html = f"""
        <h2 class="section-label">Matter Status &amp; Lifecycle</h2>
        <table class="ledger-table">
            <tbody>
                <tr>
                    <th style="width: 180px;">Matter ID &amp; Type</th>
                    <td><strong>{html.escape(stable_id)}</strong> &bull; {html.escape(m_type_label)} &bull; <span class="status-badge">{html.escape(archive_st)}</span></td>
                </tr>
                <tr>
                    <th>Lifecycle Status</th>
                    <td><strong>{html.escape(lifecycle_st)}</strong></td>
                </tr>
                <tr>
                    <th>Last Action</th>
                    <td><strong>{html.escape(last_dt)}</strong> ({html.escape(last_body)}): {html.escape(last_sum)} &bull; <a href="{html.escape(last_url)}" target="_blank" rel="noopener">Source record &rarr;</a></td>
                </tr>
                <tr>
                    <th>Next Action</th>
                    <td>{html.escape(next_txt)}</td>
                </tr>
            </tbody>
        </table>
        """

    # 3. Key Document Versions (`matter_versions`)
    versions_html = ""
    if matter_id:
        ver_rows = conn.execute(
            "SELECT * FROM matter_versions WHERE matter_id = ? ORDER BY version_number ASC",
            (matter_id,),
        ).fetchall()
        if ver_rows:
            ver_tr = []
            for vr in ver_rows:
                v_date_fmt = format_short_date(vr["version_date"])
                ver_tr.append(f"""
                <tr>
                    <td style="white-space: nowrap;"><strong>v{vr['version_number']}</strong> ({html.escape(v_date_fmt)})</td>
                    <td><a href="{html.escape(vr['doc_url'])}" target="_blank" rel="noopener"><strong>{html.escape(vr['version_label'])}</strong> &rarr;</a></td>
                    <td>{html.escape(vr['what_changed'])}</td>
                </tr>
                """)
            versions_html = f"""
            <h2 class="section-label">Key Document Versions</h2>
            <table class="ledger-table">
                <thead>
                    <tr>
                        <th style="width: 150px;">Version &amp; Date</th>
                        <th style="width: 280px;">Document</th>
                        <th>What Changed</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(ver_tr)}
                </tbody>
            </table>
            """

    # 4. Matter History (`matter_events`, including parsed minutes outcomes/votes)
    history_rows_html = []
    if matter_id:
        ev_rows = conn.execute(
            """
            SELECT me.*, i.title as item_title
            FROM matter_events me
            LEFT JOIN items i ON me.item_id = i.id
            WHERE me.matter_id = ? AND me.confidence = 'confirmed'
            ORDER BY me.action_date DESC, me.id DESC
            """,
            (matter_id,),
        ).fetchall()
        seen_hist_keys = set()
        for ev in ev_rows:
            d_ev = dict(ev)
            sig = (d_ev.get("action_date") or "", (d_ev.get("outcome_text") or "").lower(), d_ev.get("record_url") or "")
            if sig in seen_hist_keys:
                continue
            seen_hist_keys.add(sig)
            date_label = format_short_date(d_ev.get("action_date") or "")
            body_name = d_ev.get("body_name") or determine_exact_body_name(item, item.get("body_text") or "")
            outcome = d_ev.get("outcome_text") or clean_agenda_title(d_ev.get("item_title") or "")
            motion_bit = f'<div style="font-size: 13px; color: var(--ink-muted); margin-top: 2px;"><strong>Motion:</strong> {html.escape(d_ev["motion_text"])}</div>' if d_ev.get("motion_text") else ""
            vote_bit = f'<div style="font-size: 13px; color: var(--ink-muted); margin-top: 2px;"><strong>Vote / Result:</strong> {html.escape(d_ev["vote_tally"])}</div>' if d_ev.get("vote_tally") else ""
            min_link = (
                f' &bull; <a href="{html.escape(d_ev["minutes_url"])}" target="_blank" rel="noopener">Verified in Approved Minutes &rarr;</a>'
                if d_ev.get("minutes_url")
                else ""
            )
            history_rows_html.append(f"""
            <div class="timeline-item">
                <div class="ledger-meta">{html.escape(date_label)} &bull; {html.escape(body_name)} &bull; {html.escape(d_ev.get('action_type') or 'Action')}</div>
                <div><strong>{html.escape(outcome)}</strong></div>
                {motion_bit}
                {vote_bit}
                <div style="font-size: 13px; color: var(--ink-muted); margin-top: 2px;">
                    <a href="{html.escape(d_ev.get('record_url') or '')}" target="_blank" rel="noopener">Official Packet / Record &rarr;</a>{min_link}
                </div>
            </div>
            """)

    if not history_rows_html:
        body_name = determine_exact_body_name(item, item.get("body_text") or "")
        date_label = format_short_date(item.get("meeting_date") or "")
        cleaned_t = clean_agenda_title(item.get("title") or "")
        history_rows_html.append(f"""
        <div class="timeline-item">
            <div class="ledger-meta">{html.escape(date_label)} &bull; {html.escape(body_name)}</div>
            <div><strong>{html.escape(cleaned_t)}</strong></div>
            <div style="font-size: 13px; color: var(--ink-muted); margin-top: 2px;">
                <a href="{html.escape(item.get('url') or '')}" target="_blank" rel="noopener">Official Meeting Packet &amp; Record &rarr;</a>
            </div>
        </div>
        """)

    # 5. Related Matters (`matter_relations`)
    related_html = ""
    if matter_id:
        rel_rows = conn.execute(
            """
            SELECT r.relation_type, r.notes,
                   m.id as rel_id, m.stable_id as rel_stable_id, m.plain_title as rel_plain, m.official_title as rel_official, m.lifecycle_status as rel_status,
                   mb.item_id as rel_item_id
            FROM matter_relations r
            JOIN matters m ON (CASE WHEN r.parent_matter_id = ? THEN r.child_matter_id ELSE r.parent_matter_id END) = m.id
            LEFT JOIN matter_briefs mb ON mb.matter_id = m.id
            WHERE r.parent_matter_id = ? OR r.child_matter_id = ?
            """,
            (matter_id, matter_id, matter_id),
        ).fetchall()
        if rel_rows:
            rel_li = []
            for rr in rel_rows:
                rel_href = f"brief-{rr['rel_item_id']}.html" if rr["rel_item_id"] else f"{rr['rel_id']}.html"
                rel_title = rr["rel_plain"] or rr["rel_official"]
                rel_sid = rr["rel_stable_id"] or rr["rel_id"]
                rel_li.append(
                    f'<li><strong><a href="{html.escape(rel_href)}">{html.escape(rel_title)}</a></strong> '
                    f'(<code>{html.escape(rel_sid)}</code>) &mdash; {html.escape(rr["rel_status"] or "")} '
                    f'<span style="font-size: 12px; color: var(--ink-muted);">[{html.escape(rr["notes"] or rr["relation_type"])}]</span></li>'
                )
            related_html = f"""
            <h2 class="section-label">Related Matters</h2>
            <ul class="ledger-list">
                {''.join(rel_li)}
            </ul>
            """

    return f"""
    {changelog_html}
    {status_box_html}
    {versions_html}
    <h2 class="section-label">Matter History &amp; Documented Record</h2>
    <div class="timeline-block">
        {''.join(history_rows_html)}
    </div>
    {related_html}
    """


def build_brief_share_html(it: Dict[str, Any], canonical_url: str) -> Tuple[str, str]:
    """Generates the simplified one-line 'Share · Ask a question' bar for a brief page.

    Provides:
    - Quick Copy Link button with live visual feedback
    - Email this brief link with subject 'Loretta's Ledger: [plain title]' and body containing only URL and 1-sentence summary
    - Ask a question link with pre-filled email to thecorcoran@gmail.com with subject 'Question: [brief title]'
    """
    title = it.get("display_title") or "Policy Brief"
    stake = it.get("stake") or "Documented policy brief from Loretta's Ledger."
    encoded_url = canonical_url

    email_subject = urllib.parse.quote(f"Loretta's Ledger: {title}", safe="")
    email_body = urllib.parse.quote(f"{encoded_url}\n\n{stake}\n", safe="")
    email_share_url = f"mailto:?subject={email_subject}&body={email_body}"

    question_subject = urllib.parse.quote(f"Question: {title}", safe="")
    question_url = f"mailto:thecorcoran@gmail.com?subject={question_subject}"

    one_line_html = f"""
    <div class="brief-share-bar" style="margin: 12px 0 20px 0; padding: 8px 14px; background: #faf7f0; border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); font-size: 14px; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;">
        <span style="font-weight: 700; color: var(--oxblood); text-transform: uppercase; letter-spacing: 0.05em; font-size: 12px;">Share &middot; Ask a question</span>
        <span style="color: var(--rule-dark);">&mdash;</span>
        <button type="button" class="btn-share-link" onclick="copyBriefLink(this)" data-url="{html.escape(canonical_url)}" style="background: none; border: none; padding: 0; color: var(--oxblood); text-decoration: underline; cursor: pointer; font-family: inherit; font-size: 14px;">Copy link</button>
        <span style="color: var(--ink-faint);">&bull;</span>
        <a href="{email_share_url}" style="color: var(--oxblood); text-decoration: underline;">Email</a>
        <span style="color: var(--ink-faint);">&bull;</span>
        <a href="{question_url}" style="color: var(--oxblood); text-decoration: underline;">Ask a question</a>
    </div>
    """

    return one_line_html, ""


def load_matter_qa_html(matter_id: Optional[str], item_id: str, brief_title: str) -> str:
    """Loads and renders the 'Questions We've Been Asked' section for a brief.

    Stored per matter in data/qa/<matter>.md (or data/qa/<item_id>.md).
    Shown ONLY after marked approved (frontmatter 'approved: true').
    Hidden when empty or unapproved.
    Adds a 'Have a question about this item?' link to thecorcoran@gmail.com.
    """
    qa_dir = ROOT_DIR / "data" / "qa"
    if not qa_dir.exists():
        return ""

    candidates = []
    if matter_id:
        candidates.append(qa_dir / f"{matter_id}.md")
    candidates.append(qa_dir / f"{item_id}.md")
    candidates.append(qa_dir / f"brief-{item_id}.md")

    qa_file = None
    for c in candidates:
        if c.exists():
            qa_file = c
            break

    if not qa_file:
        return ""

    try:
        content = qa_file.read_text(encoding="utf-8").strip()
    except Exception:
        return ""

    is_approved = False
    qa_body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            qa_body = parts[2].strip()
            for line in fm_text.splitlines():
                k, sep, v = line.partition(":")
                if sep and k.strip().lower() == "approved" and v.strip().lower() in ("true", "yes", "1"):
                    is_approved = True
                    break

    if not is_approved or not qa_body:
        return ""

    qa_html = markdown_to_html(qa_body)
    q_subject = urllib.parse.quote(f"Question: {brief_title}", safe="")
    ask_link = f'<p style="margin-top: 14px;"><a href="mailto:thecorcoran@gmail.com?subject={q_subject}" style="color: var(--oxblood); text-decoration: underline; font-weight: 600;">Have a question about this item? &rarr;</a></p>'

    return f"""
    <div class="qa-section memo-box" style="margin-top: 28px; margin-bottom: 24px; padding: 18px 22px; background: #fffcf8; border-left: 4px solid var(--oxblood);">
        <h2 class="section-label" style="margin-top: 0; font-size: 18px; color: var(--oxblood);">Questions We&#x27;ve Been Asked</h2>
        <div class="qa-content" style="font-size: 15px; line-height: 1.6;">
            {qa_html}
        </div>
        {ask_link}
    </div>
    """


def load_contacts_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Loads verified contacts directory from config/contacts.yaml."""
    cfg_path = path or (CONFIG_DIR / "contacts.yaml")
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load contacts config from {cfg_path}: {e}")
    return {}


def resolve_contact_entity(item: Dict[str, Any], contacts_data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Resolves the verified government body and contacts entry for a given agenda item or matter."""
    contacts_map = contacts_data.get("contacts", {})
    raw_title = (item.get("title") or item.get("display_title") or "").lower()
    body_label = (item.get("full_body_label") or item.get("body") or "").lower()
    jur = (item.get("jurisdiction") or "").lower()
    source_id = (item.get("source_id") or "").lower()

    combined = f"{raw_title} {body_label} {source_id}"

    if "board of health" in combined or "boh" in combined or "article iii" in combined or "drinking water" in combined:
        return "thurston_boh", contacts_map.get("thurston_boh", {})
    if "conservation district" in combined:
        return "thurston_cd", contacts_map.get("thurston_cd", {})
    if "planning commission" in combined:
        if jur == "olympia" or "olympia" in combined:
            return "olympia_planning_commission", contacts_map.get("olympia_planning_commission", {})
        return "thurston_planning_commission", contacts_map.get("thurston_planning_commission", {})
    if ("committee" in combined or "board" in combined) and (jur == "olympia" or "olympia" in combined):
        return "olympia_committees", contacts_map.get("olympia_committees", {})
    if jur == "olympia" or "olympia" in combined:
        return "olympia_council", contacts_map.get("olympia_council", {})
    return "thurston_bocc", contacts_map.get("thurston_bocc", {})


def extract_brief_gap_questions(md_text: str) -> List[str]:
    """Extracts questions and documented gap items from ## For Staff and ## Questions sections."""
    if not md_text:
        return []
    questions: List[str] = []

    def clean_md_inline(s: str) -> str:
        s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)
        s = re.sub(r"[*_`]", "", s)
        return s.strip()

    m_staff = re.search(r"## For Staff\n(.*?)(?=\n## |\Z)", md_text, re.DOTALL)
    if m_staff:
        for line in m_staff.group(1).splitlines():
            line = line.strip()
            if line.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.")):
                val = re.sub(r"^[-*\d\.]+\s*", "", line)
                cleaned = clean_md_inline(val)
                if cleaned and cleaned not in questions:
                    questions.append(cleaned)

    m_q = re.search(r"## Questions\n(.*?)(?=\n## |\Z)", md_text, re.DOTALL)
    if m_q:
        for line in m_q.group(1).splitlines():
            line = line.strip()
            if line.startswith(("-", "*", "1.", "2.", "3.", "4.", "5.")):
                val = re.sub(r"^[-*\d\.]+\s*", "", line)
                cleaned = clean_md_inline(val)
                if cleaned and cleaned not in questions:
                    questions.append(cleaned)

    return questions


def extract_brief_possible_asks(md_text: str) -> List[str]:
    """Extracts possible asks from ## What Would Make It Better or ## What to Do."""
    if not md_text:
        return []
    asks: List[str] = []

    def clean_md_inline(s: str) -> str:
        s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)
        s = re.sub(r"[*_`]", "", s)
        return s.strip()

    m_better = re.search(r"## What Would Make It Better\n(.*?)(?=\n## |\Z)", md_text, re.DOTALL)
    if m_better:
        for line in m_better.group(1).splitlines():
            line = line.strip()
            if line.startswith(("-", "*", "1.", "2.", "3.")):
                val = re.sub(r"^[-*\d\.]+\s*", "", line)
                cleaned = clean_md_inline(val)
                if cleaned and cleaned not in asks:
                    asks.append(cleaned)

    if not asks:
        m_todo = re.search(r"## What to Do\n(.*?)(?=\n## |\Z)", md_text, re.DOTALL)
        if m_todo:
            for line in m_todo.group(1).splitlines():
                line = line.strip()
                if "possible ask" in line.lower() or "suggested ask" in line.lower():
                    val = re.sub(r"^[-*\d\.]+\s*", "", line)
                    cleaned = clean_md_inline(val)
                    if cleaned and cleaned not in asks:
                        asks.append(cleaned)

    return asks


def build_brief_contacts_html(it: Dict[str, Any], contacts_data: Dict[str, Any], md_source: str = "", today_str: str = "2026-10-13") -> str:
    """Builds two-group verified contact component for briefs:
    Group A: 'Ask staff a question' (for facts and process)
    Group B: 'Tell officials what you think' (for opinions and requests)
    Note C: Official record submission with clerk and portal link.
    """
    plain_title = it.get("display_title") or clean_agenda_title(it.get("title") or "Policy Matter")
    meeting_date_raw = (it.get("meeting_date") or "")[:10]
    jur = (it.get("jurisdiction") or "thurston").lower()
    area_name = "an Olympia" if jur == "olympia" else "a Thurston County"

    if meeting_date_raw == today_str[:10]:
        dt_display = f"today ({format_short_date(meeting_date_raw)})"
    elif meeting_date_raw:
        dt_display = format_short_date(meeting_date_raw)
    else:
        dt_display = "the upcoming meeting"

    _, entity = resolve_contact_entity(it, contacts_data)
    if not entity:
        return ""

    entity_name = entity.get("entity_name") or "Local Deciding Body"
    portal_url = entity.get("portal_url") or "#"
    source_url = entity.get("source_url") or entity.get("portal_url") or "#"
    last_verified = format_short_date(entity.get("last_verified") or "2026-10-13")

    # Documented deadline
    raw_deadline = it.get("comment_deadline")
    if raw_deadline and str(raw_deadline).strip():
        deadline_text = html.escape(str(raw_deadline).strip())
    else:
        deadline_text = "Check the agenda"

    # --- Group A: Ask staff a question ---
    staff_contacts = []
    
    staff_name = entity.get("staff_name") or entity.get("contact_name")
    staff_dept = entity.get("staff_department") or entity.get("department") or entity_name
    staff_email = entity.get("staff_email") or entity.get("email")
    staff_phone = entity.get("staff_phone") or entity.get("phone")
    if staff_name or staff_email:
        staff_contacts.append({
            "name": staff_name or staff_dept,
            "role": f"Staff Liaison, {staff_dept}" if staff_name else staff_dept,
            "email": staff_email,
            "phone": staff_phone,
        })

    clerk_name = entity.get("clerk_name") or entity.get("contact_name")
    clerk_title = entity.get("clerk_title") or entity.get("contact_title") or "Clerk"
    clerk_email = entity.get("clerk_email") or entity.get("email")
    clerk_phone = entity.get("clerk_phone") or entity.get("phone")
    if clerk_name and (not staff_contacts or clerk_name != staff_contacts[0].get("name")):
        staff_contacts.append({
            "name": clerk_name,
            "role": clerk_title,
            "email": clerk_email,
            "phone": clerk_phone,
        })

    staff_cards_html = []
    for c in staff_contacts:
        c_name = c["name"]
        c_role = c["role"]
        c_email = c["email"]
        c_phone = c["phone"]

        subj_str = f"Question on {plain_title} ({dt_display})"
        body_str = (
            f"Hello {c_name},\n\n"
            f"I'm a resident of ___ reading the packet for {plain_title} on {dt_display}. "
            f"I couldn't find where it says ___.\n\n"
            f"Could you point me to the section?\n\n"
            f"Thank you,\n"
        )
        draft_str = (
            f"Subject: {subj_str}\n"
            f"To: {c_email or '(Use official portal)'}\n\n"
            f"Hello {c_name},\n\n"
            f"I'm a resident of ___ reading the packet for {plain_title} on {dt_display}. "
            f"I couldn't find where it says ___.\n\n"
            f"Could you point me to the section?\n\n"
            f"Thank you,\n"
            f"[Your Name]\n"
            f"[Your phone or address, optional]\n"
        )

        mailto_link = f"mailto:{c_email}?subject={urllib.parse.quote(subj_str)}&body={urllib.parse.quote(body_str)}" if c_email else portal_url
        write_btn = f'<a href="{mailto_link}" class="btn-brief" style="font-size: 12px; padding: 4px 10px;">Write</a>' if c_email else f'<a href="{portal_url}" target="_blank" rel="noopener" class="btn-brief" style="font-size: 12px; padding: 4px 10px;">Official Page &rarr;</a>'
        copy_addr_btn = f'<button type="button" class="btn-secondary" onclick="copyContactAddress(this, \'{html.escape(c_email)}\')" style="font-size: 12px; padding: 4px 10px;">Copy address</button>' if c_email else ""
        copy_draft_btn = f'<button type="button" class="btn-secondary" onclick="copyDraftText(this)" data-draft="{html.escape(draft_str)}" style="font-size: 12px; padding: 4px 10px;">Copy a starting draft</button>'

        phone_info = f" &bull; {c_phone}" if c_phone else ""
        email_info = f" &bull; <code>{html.escape(c_email)}</code>" if c_email else ""

        staff_cards_html.append(f"""
        <div style="background: var(--surface); border: 1px solid var(--rule); padding: 12px 14px; margin-bottom: 10px;">
            <div style="font-size: 14px; font-weight: 700; color: var(--ink);">{html.escape(c_name)} <span style="font-weight: 400; font-size: 13px; color: var(--ink-muted);">({html.escape(c_role)})</span></div>
            <div style="font-size: 12px; color: var(--ink-muted); margin: 2px 0 8px 0;">{phone_info}{email_info}</div>
            <div style="display: flex; gap: 6px; flex-wrap: wrap; align-items: center;">
                {write_btn}
                {copy_addr_btn}
                {copy_draft_btn}
            </div>
        </div>
        """)

    gap_questions = extract_brief_gap_questions(md_source)
    gap_box_html = ""
    if gap_questions:
        items_li = "".join(f"<li style='margin-bottom: 6px;'>{html.escape(q)}</li>" for q in gap_questions)
        gap_box_html = f"""
        <div style="margin-top: 12px; padding: 12px 14px; background: #fffcf8; border: 1px solid var(--rule); border-left: 3px solid var(--oxblood); font-size: 13px;">
            <div style="font-weight: 700; color: var(--oxblood); margin-bottom: 6px;">Questions from this brief to ask staff <span style="font-weight: 400; color: var(--ink-muted);">(optional &mdash; keep, change, or delete; not inserted into email automatically)</span>:</div>
            <ul style="margin: 0 0 0 18px; padding: 0;">
                {items_li}
            </ul>
        </div>
        """

    # --- Group B: Tell officials what you think ---
    members = entity.get("members") or []
    possible_asks = extract_brief_possible_asks(md_source)
    asks_box_html = ""
    if possible_asks:
        asks_li = "".join(f"<li style='margin-bottom: 6px;'>{html.escape(a)}</li>" for a in possible_asks)
        asks_box_html = f"""
        <div style="margin-bottom: 14px; padding: 12px 14px; background: #fffcf8; border: 1px solid var(--rule); border-left: 3px solid var(--oxblood); font-size: 13px;">
            <div style="font-weight: 700; color: var(--oxblood); margin-bottom: 6px;">Possible asks from this brief <span style="font-weight: 400; color: var(--ink-muted);">(for reference only &mdash; not inserted into email automatically)</span>:</div>
            <ul style="margin: 0 0 0 18px; padding: 0;">
                {asks_li}
            </ul>
        </div>
        """

    officials_cards_html = []
    if members:
        for m in members:
            m_name = m.get("name") or "Deciding Member"
            m_role = m.get("role") or "Elected / Appointed Member"
            m_email = m.get("email") or entity.get("email")

            subj_str = f"{plain_title}: from {area_name} resident"
            body_str = (
                f"Hello {m_name},\n\n"
                f"I'm writing about {plain_title} on the {dt_display} agenda.\n\n"
                f"I'm concerned about ___ because ___.\n\n"
                f"I'd ask that ___.\n\n"
                f"Thank you for your service.\n"
            )
            draft_str = (
                f"Subject: {subj_str}\n"
                f"To: {m_email or '(Use official portal)'}\n\n"
                f"Hello {m_name},\n\n"
                f"I'm writing about {plain_title} on the {dt_display} agenda.\n\n"
                f"I'm concerned about ___ because ___.\n\n"
                f"I'd ask that ___.\n\n"
                f"Thank you for your service.\n\n"
                f"[Your Name]\n"
                f"[Your address or neighborhood]\n"
            )

            mailto_link = f"mailto:{m_email}?subject={urllib.parse.quote(subj_str)}&body={urllib.parse.quote(body_str)}" if m_email else portal_url
            write_btn = f'<a href="{mailto_link}" class="btn-brief" style="font-size: 12px; padding: 4px 10px;">Write</a>' if m_email else f'<a href="{portal_url}" target="_blank" rel="noopener" class="btn-brief" style="font-size: 12px; padding: 4px 10px;">Member Directory &rarr;</a>'
            copy_addr_btn = f'<button type="button" class="btn-secondary" onclick="copyContactAddress(this, \'{html.escape(m_email)}\')" style="font-size: 12px; padding: 4px 10px;">Copy address</button>' if m_email else ""
            copy_draft_btn = f'<button type="button" class="btn-secondary" onclick="copyDraftText(this)" data-draft="{html.escape(draft_str)}" style="font-size: 12px; padding: 4px 10px;">Copy a starting draft</button>'

            officials_cards_html.append(f"""
            <div style="background: var(--surface); border: 1px solid var(--rule); padding: 10px 14px; margin-bottom: 8px;">
                <div style="font-size: 14px; font-weight: 700; color: var(--ink);">{html.escape(m_name)} <span style="font-weight: 400; font-size: 13px; color: var(--ink-muted);">({html.escape(m_role)})</span></div>
                <div style="display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 6px;">
                    {write_btn}
                    {copy_addr_btn}
                    {copy_draft_btn}
                </div>
            </div>
            """)
    else:
        officials_cards_html.append(f"""
        <div style="background: var(--surface); border: 1px solid var(--rule); padding: 12px 14px;">
            <p style="margin: 0 0 8px 0; font-size: 14px;">Deciding body member roster is available on the official directory:</p>
            <a href="{portal_url}" target="_blank" rel="noopener" class="btn-brief" style="font-size: 12px;">Visit Official Member Directory &rarr;</a>
        </div>
        """)

    # --- Note C: Clerk record submission & Comment Deadline ---
    clerk_info_str = f"{clerk_name}" if clerk_name else "Clerk of the Board"
    if clerk_email:
        clerk_info_str += f" ({clerk_email})"
    elif clerk_phone:
        clerk_info_str += f" ({clerk_phone})"

    note_c_html = f"""
    <div style="margin-top: 14px; padding: 12px 14px; background: #f7f5ed; border: 1px solid var(--rule); border-left: 3px solid var(--oxblood); font-size: 13px; line-height: 1.5;">
        <div><strong>To put your comments in the official record:</strong> Also send them to the clerk ({html.escape(clerk_info_str)}) or use the <a href="{portal_url}" target="_blank" rel="noopener">official comment portal</a>.</div>
        <div style="margin-top: 6px;"><strong>Comment deadline:</strong> {deadline_text}</div>
    </div>
    """

    verification_html = f"""
    <div style="margin-top: 14px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-faint);">
        Contacts verified {last_verified} from official public records &bull; <a href="{source_url}" target="_blank" rel="noopener">Official Source</a>
    </div>
    """

    return f"""
    <div class="brief-contacts-container memo-box" style="margin-top: 28px; margin-bottom: 24px; padding: 20px 22px; background: #fdfbf7;">
        <h2 class="section-label" style="margin-top: 0; font-size: 18px; color: var(--oxblood);">Who Decides &amp; How to Contact Them</h2>
        
        <!-- Group A: Ask staff a question -->
        <div style="margin-bottom: 24px;">
            <h3 style="margin: 0 0 4px 0; font-size: 16px; font-weight: 700; color: var(--ink);">A. Ask staff a question <span style="font-weight: 400; font-size: 13px; color: var(--ink-muted);">(for facts and process)</span></h3>
            <p style="margin: 0 0 12px 0; font-size: 13px; color: var(--ink-muted);">Staff wrote and manage the packet documents. They can clarify sections, explain calculations, or point you to attachments.</p>
            {''.join(staff_cards_html)}
            {gap_box_html}
        </div>

        <!-- Group B: Tell officials what you think -->
        <div style="margin-bottom: 12px;">
            <h3 style="margin: 0 0 4px 0; font-size: 16px; font-weight: 700; color: var(--ink);">B. Tell officials what you think <span style="font-weight: 400; font-size: 13px; color: var(--ink-muted);">(for opinions and requests)</span></h3>
            <p style="margin: 0 0 12px 0; font-size: 13px; color: var(--ink-muted);">Elected and appointed officials make the final decision. Tell them what outcome or amendments you want.</p>
            {asks_box_html}
            {''.join(officials_cards_html)}
            {note_c_html}
        </div>

        {verification_html}
    </div>
    """


def build_calendar_picker_widget_html(today_str: str = "2026-10-13", root_prefix: str = "") -> str:
    """Builds interactive calendar feed subscription widget (.ics) with webcal:// links and Google Calendar copy."""
    return f"""
    <div class="calendar-picker-widget" style="background: #ffffff; border: 1px solid var(--rule); border-left: 4px solid var(--oxblood); padding: 20px 22px; margin: 24px 0;">
        <div class="ledger-meta" style="margin-bottom: 6px;">Calendar Feeds (.ics)</div>
        <h3 style="margin: 0 0 10px 0; font-size: 18px; font-weight: 700; color: var(--ink);">Subscribe to Hearing &amp; Meeting Calendars</h3>
        
        <p style="margin: 0 0 16px 0; font-size: 14px; color: var(--ink-muted); line-height: 1.5;">
            These calendar feeds update automatically whenever a new edition of Loretta's Ledger is published. Event dates and comment deadlines come strictly from official agenda packets and hearing notices; if a deadline is not documented, please confirm on the official agenda.
        </p>

        <div style="margin-bottom: 16px;">
            <label for="cal-feed-select" style="font-weight: 600; font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em; display: block; margin-bottom: 6px;">Select Jurisdiction or Feed:</label>
            <select id="cal-feed-select" onchange="updateCalendarFeedWidget()" style="font-family: var(--font-serif); font-size: 14px; padding: 6px 10px; border: 1px solid var(--rule-dark); background: var(--bg); max-width: 100%;">
                <option value="all">Everything (All Matters, Hearings, Deadlines)</option>
                <option value="thurston">Thurston County (Board of Health, BOCC, Planning Commission)</option>
                <option value="olympia">City of Olympia (City Council, Planning Commission, Committees)</option>
                <option value="test-cases">Test Cases Only</option>
            </select>
        </div>

        <div id="cal-feed-details" style="background: #faf7f0; border: 1px solid var(--rule); padding: 16px 18px; margin-bottom: 12px;">
            <div id="cal-feed-title" style="font-weight: 700; font-size: 15px; margin-bottom: 4px; color: var(--ink);">Loretta's Ledger — Everything</div>
            <div id="cal-feed-desc" style="font-size: 13px; color: var(--ink-muted); margin-bottom: 14px;">All public hearings, votes, and documented comment deadlines across Thurston County and the City of Olympia.</div>
            
            <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
                <a id="cal-webcal-link" href="webcal://thecorcoran.github.io/Ledger/calendars/all.ics" class="btn-brief" style="font-size: 12px; padding: 6px 12px;">Subscribe in Calendar</a>
                <button id="cal-copy-btn" type="button" class="btn-secondary" onclick="copyCalendarLink(this)" data-url="https://thecorcoran.github.io/Ledger/calendars/all.ics" style="font-size: 12px; padding: 6px 12px;">Copy Link for Google Calendar</button>
                <a id="cal-download-link" href="{root_prefix}calendars/all.ics" download style="font-size: 12px; color: var(--oxblood); text-decoration: underline; margin-left: 6px;">Download .ics file</a>
            </div>
        </div>
    </div>

    <script>
    var CAL_FEEDS = {{
        "all": {{
            "title": "Loretta's Ledger — Everything",
            "desc": "All public hearings, votes, and documented comment deadlines across Thurston County and the City of Olympia.",
            "slug": "all"
        }},
        "thurston": {{
            "title": "Loretta's Ledger — Thurston County",
            "desc": "Public hearings and deadlines for Thurston County Board of Health, Board of County Commissioners, and Planning Commission.",
            "slug": "thurston"
        }},
        "olympia": {{
            "title": "Loretta's Ledger — City of Olympia",
            "desc": "Public hearings and deadlines for Olympia City Council, Planning Commission, and advisory committees.",
            "slug": "olympia"
        }},
        "test-cases": {{
            "title": "Loretta's Ledger — Test Cases",
            "desc": "Tracked policy matters designated as test cases for reader verification and public interest.",
            "slug": "test-cases"
        }}
    }};

    function updateCalendarFeedWidget() {{
        var sel = document.getElementById("cal-feed-select");
        var key = sel ? sel.value : "all";
        var feed = CAL_FEEDS[key] || CAL_FEEDS["all"];
        var titleEl = document.getElementById("cal-feed-title");
        var descEl = document.getElementById("cal-feed-desc");
        var webcalEl = document.getElementById("cal-webcal-link");
        var copyBtn = document.getElementById("cal-copy-btn");
        var dlLink = document.getElementById("cal-download-link");

        if (titleEl) titleEl.innerText = feed.title;
        if (descEl) descEl.innerText = feed.desc;
        var httpUrl = "https://thecorcoran.github.io/Ledger/calendars/" + feed.slug + ".ics";
        var webcalUrl = "webcal://thecorcoran.github.io/Ledger/calendars/" + feed.slug + ".ics";
        if (webcalEl) webcalEl.href = webcalUrl;
        if (copyBtn) copyBtn.setAttribute("data-url", httpUrl);
        if (dlLink) dlLink.href = "{root_prefix}calendars/" + feed.slug + ".ics";
    }}
    </script>
    """


def build_calendar_page_html(today_str: str = "2026-10-13") -> str:
    """Builds content for calendar.html."""
    picker_html = build_calendar_picker_widget_html(today_str=today_str, root_prefix="")
    return f"""
    <div class="ledger-meta" style="margin-bottom: 12px;">
        <a href="index.html">&larr; Home</a> &bull; Community Calendar &bull; Updated {html.escape(format_short_date(today_str))}
    </div>

    <h1 class="headline-title">Meeting &amp; Hearing Calendar Feeds</h1>
    
    <p style="font-size: 17px; line-height: 1.5; color: var(--ink); margin-bottom: 20px;">
        Track local public hearings, formal votes, and comment deadlines in your personal calendar. Loretta's Ledger publishes static, standards-compliant iCalendar (<code style="font-size: 15px;">.ics</code>) feeds according to RFC 5545 with <code style="font-size: 15px;">America/Los_Angeles</code> time zones.
    </p>

    {picker_html}

    <h2 class="section-label">Feed Standards &amp; Inclusions</h2>
    <div style="font-size: 15px; line-height: 1.6; margin-bottom: 24px;">
        <p><strong>What is included:</strong> Feeds include only briefed matters, scheduled public hearings, and documented written comment deadlines. Routine procedural agenda lines are filtered out so your calendar remains uncluttered.</p>
        <p><strong>Deadline Reminders:</strong> Comment deadline entries feature an automatic 1-day advance notification reminder (<code style="font-size: 13px;">VALARM</code>).</p>
        <p><strong>Stable Event IDs:</strong> Each policy matter has a permanent UID. If a meeting time changes or a matter is postponed, your calendar app updates the existing event rather than creating a duplicate.</p>
        <p><strong>Strict Verification:</strong> All dates and deadlines are taken strictly from published agenda packets and hearing notices. If a deadline is not documented, we do not guess; please confirm on the official agenda.</p>
    </div>

    <h2 class="section-label">How to Subscribe</h2>
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 32px;">
        <div style="background: var(--surface); border: 1px solid var(--rule); padding: 16px;">
            <h3 style="margin: 0 0 6px 0; font-size: 15px; font-weight: 700;">Apple Calendar (Mac / iOS)</h3>
            <p style="font-size: 13px; color: var(--ink-muted); margin: 0;">Click the "Subscribe in Calendar" button above or open Calendar &rarr; File &rarr; New Calendar Subscription and paste the calendar address.</p>
        </div>
        <div style="background: var(--surface); border: 1px solid var(--rule); padding: 16px;">
            <h3 style="margin: 0 0 6px 0; font-size: 15px; font-weight: 700;">Google Calendar</h3>
            <p style="font-size: 13px; color: var(--ink-muted); margin: 0;">Click "Copy Link for Google Calendar" above, then open Google Calendar on the web &rarr; Other calendars (+) &rarr; From URL, and paste the link.</p>
        </div>
        <div style="background: var(--surface); border: 1px solid var(--rule); padding: 16px;">
            <h3 style="margin: 0 0 6px 0; font-size: 15px; font-weight: 700;">Microsoft Outlook</h3>
            <p style="font-size: 13px; color: var(--ink-muted); margin: 0;">In Outlook Calendar, select Add Calendar &rarr; Subscribe from web, paste the link, and choose your preferred refresh interval.</p>
        </div>
    </div>

    <div class="memo-box" style="margin-top: 24px; background: #faf7f0;">
        <div style="font-weight: 700; margin-bottom: 4px;">Looking for advice on testifying or submitting written comment?</div>
        <div style="font-size: 14px; color: var(--ink-muted); margin-bottom: 8px;">Review our step-by-step participation guide covering comment cutoffs, agenda requests, and email drafting.</div>
        <a href="how-to-take-part.html" class="btn-secondary">Read How to Take Part &rarr;</a>
    </div>
    """


def build_how_to_take_part_html(contacts_data: Dict[str, Any], today_str: str = "2026-10-13") -> str:
    """Builds content for how-to-take-part.html based on user guidance and verified contacts."""
    contacts_map = contacts_data.get("contacts", {})
    bodies_order = [
        ("olympia_council", "Olympia City Council"),
        ("olympia_planning_commission", "Olympia Planning Commission"),
        ("olympia_committees", "Olympia advisory committees and boards"),
        ("thurston_bocc", "Thurston County Board of County Commissioners"),
        ("thurston_planning_commission", "Thurston County Planning Commission"),
        ("thurston_boh", "Thurston County Board of Health"),
    ]

    body_cards_html = []
    for key, fallback_title in bodies_order:
        b = contacts_map.get(key, {})
        b_name = b.get("entity_name") or fallback_title
        b_role = b.get("role") or "Local Deciding Body"
        b_schedule = b.get("meeting_schedule") or "Check official calendar"
        b_comment = b.get("comment_rules") or b.get("comment_info") or "See official agenda packet for comment guidelines."
        b_agenda = b.get("agenda_request_rules") or "Contact the clerk or department staff to request agenda placement."
        
        # Contact string
        c_name = b.get("contact_name") or b.get("clerk_name") or "Clerk's Office"
        c_title = b.get("contact_title") or b.get("clerk_title") or ""
        c_email = b.get("email") or b.get("clerk_email")
        c_phone = b.get("phone") or b.get("clerk_phone")
        
        parts = [c_name]
        if c_title:
            parts.append(f"({c_title})")
        contact_str = " ".join(parts)
        if c_email:
            contact_str += f" &bull; <a href='mailto:{c_email}'>{c_email}</a>"
        if c_phone:
            contact_str += f" &bull; {c_phone}"
            
        b_source = b.get("source_url") or b.get("portal_url") or "#"
        b_verified = format_short_date(b.get("last_verified") or "2026-10-13")

        body_cards_html.append(f"""
        <div class="memo-box" style="margin-bottom: 16px; background: var(--surface); padding: 18px 20px; border-left: 4px solid var(--oxblood);">
            <h3 style="margin: 0 0 2px 0; font-size: 17px; font-weight: 700; color: var(--ink);">{html.escape(b_name)}</h3>
            <div style="font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-muted); margin-bottom: 12px;">{html.escape(b_role)}</div>
            
            <div style="font-size: 14px; margin-bottom: 8px;"><strong>When it meets:</strong> {html.escape(b_schedule)}</div>
            <div style="font-size: 14px; margin-bottom: 8px;"><strong>How it takes comment:</strong> {html.escape(b_comment)}</div>
            <div style="font-size: 14px; margin-bottom: 8px;"><strong>How to request an agenda item:</strong> {html.escape(b_agenda)}</div>
            <div style="font-size: 14px; margin-bottom: 10px;"><strong>Contact:</strong> {contact_str}</div>
            
            <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-faint); border-top: 1px solid var(--rule); padding-top: 8px;">
                Last checked: {b_verified} &bull; <a href="{b_source}" target="_blank" rel="noopener">Official Source</a>
            </div>
        </div>
        """)

    picker_html = build_calendar_picker_widget_html(today_str=today_str, root_prefix="")

    return f"""
    <div class="ledger-meta" style="margin-bottom: 12px;">
        <a href="index.html">&larr; Home</a> &bull; Public Participation Guide &bull; Updated {html.escape(format_short_date(today_str))}
    </div>

    <h1 class="headline-title">How to Take Part</h1>
    
    <p style="font-size: 18px; line-height: 1.55; color: var(--ink); margin-bottom: 28px; border-bottom: 1px solid var(--rule); padding-bottom: 16px;">
        You don't need to be an expert to speak up about a local decision. This page covers four things: how to comment on something already scheduled, how to ask for something to be added to an agenda, how to write to staff and to officials, and how to keep track of the dates.
    </p>

    <!-- Section 1 -->
    <h2 class="section-label">1. Commenting on an item that is already scheduled</h2>
    <div style="font-size: 15px; line-height: 1.6; margin-bottom: 24px;">
        <p>Every brief on this site lists who decides, the meeting date, and the comment deadline when the documents give one. Most bodies take comment in one or more of these ways:</p>
        <ul class="ledger-list">
            <li><strong>In writing</strong>, by email or an online form, usually due before the meeting. Some cutoffs are as short as two hours before it starts.</li>
            <li><strong>In person</strong> at the meeting.</li>
            <li><strong>By video call</strong>, often with a sign-up that closes before the meeting.</li>
        </ul>
        <p>Written comments sent to the clerk or through the official portal go into the record the decision-makers receive. An email sent only to one official may not. When it matters, send it both ways.</p>
        <p>Deadlines and rules change. Each brief links to the official agenda, and that is the final word.</p>
    </div>

    <!-- Section 2 -->
    <h2 class="section-label">2. Asking for something to be added to an agenda</h2>
    <div style="font-size: 15px; line-height: 1.6; margin-bottom: 20px;">
        <p>Commenting on a scheduled item is different from asking a body to take something up. Agenda requests usually go to a chair, a mayor, a council member, a commissioner, or a clerk, and often need to be made weeks ahead. The rules differ by body.</p>
        <p>Below is a short guide for each body we follow. Each entry shows when it meets, how it takes comment, how to ask for an agenda item, who to contact, and the date we last checked.</p>
    </div>

    {''.join(body_cards_html)}

    <p style="font-size: 13px; color: var(--ink-muted); margin-top: -6px; margin-bottom: 28px;">If a body's rules aren't posted anywhere we can find, we say so and point you to its clerk.</p>

    <!-- Section 3 -->
    <h2 class="section-label">3. Writing to staff and writing to officials</h2>
    <div style="font-size: 15px; line-height: 1.6; margin-bottom: 24px;">
        <p>These are two different kinds of email, and mixing them up is the most common reason a message doesn't land.</p>
        <p><strong>Staff answer questions.</strong> They wrote or manage the documents, and they can tell you what a section means, where something is, or how a process works. They don't decide the matter, and it isn't fair to ask them to take a side.</p>
        <p><strong>Officials make decisions.</strong> Council members, commissioners, and board members are the people to tell what you think and what you'd like them to do.</p>
        
        <h3 class="subsection-label" style="margin-top: 24px; font-size: 17px;">Writing to staff: asking a question</h3>
        <ul class="ledger-list">
            <li>Look in the packet first. If you can't find your answer, say so: <em>"I looked in the packet and couldn't find&hellip;"</em></li>
            <li>Put the item and date in the subject line.</li>
            <li>In the first two sentences, say who you are (a resident, and where), which item, and what you're asking.</li>
            <li>Ask one specific question, ideally one that can be answered in a few lines or with a section number.</li>
            <li>Keep it to about 100 to 150 words.</li>
            <li>Write early, so an answer can arrive before the hearing.</li>
        </ul>

        <div style="background: #ffffff; border: 1px solid var(--rule); border-left: 4px solid var(--oxblood); padding: 16px 20px; margin: 14px 0 24px 0; font-size: 14px; font-style: italic;">
            <div style="font-style: normal; font-weight: 700; color: var(--oxblood); margin-bottom: 8px;">Example starting draft for staff:</div>
            <strong>Subject:</strong> Question about [item] ([meeting date])<br><br>
            Hello [name],<br><br>
            I'm a resident of [area] reading the packet for [item] on [date]. I couldn't find where it says [what you're looking for]. Could you point me to the section?<br><br>
            Thank you,<br>
            [Your name]<br>
            [Your phone or address, optional]
        </div>

        <h3 class="subsection-label" style="margin-top: 24px; font-size: 17px;">Writing to officials: telling them what you think</h3>
        <ul class="ledger-list">
            <li>Put the item, date, and your position in the subject line: <em>"[Item]: support,"</em> <em>"[Item]: oppose,"</em> or <em>"[Item]: from a resident of [area]."</em></li>
            <li>State your conclusion in the first sentence: what you're asking them to do (vote yes, vote no, amend, hold a hearing, take more time).</li>
            <li>Give two or three reasons, grounded in your experience or in the packet. Numbers and specific sections help; general accusations don't.</li>
            <li>If you have an alternative, say what it is: <em>"Instead of X, please consider Y."</em></li>
            <li>Thank them for their service, even if you disagree with them.</li>
            <li>Keep it to about 200 to 300 words.</li>
        </ul>

        <div style="background: #ffffff; border: 1px solid var(--rule); border-left: 4px solid var(--oxblood); padding: 16px 20px; margin: 14px 0 24px 0; font-size: 14px; font-style: italic;">
            <div style="font-style: normal; font-weight: 700; color: var(--oxblood); margin-bottom: 8px;">Example starting draft for officials:</div>
            <strong>Subject:</strong> [Item]: from a [city or area] resident<br><br>
            Hello [name],<br><br>
            I'm writing about [item] on the [date] agenda.<br><br>
            I'm concerned about [specific provision] because [one clear reason].<br><br>
            I'd ask that you [specific request, such as: amend section X to&hellip;, ask staff for&hellip;, or vote no on&hellip;].<br><br>
            Thank you for your service to our community.<br><br>
            [Your name]<br>
            [Your address or neighborhood &mdash; officials pay closest attention to their own constituents]
        </div>
    </div>

    <!-- Section 4 -->
    <h2 class="section-label">4. Keeping track of the dates</h2>
    <div style="font-size: 15px; line-height: 1.6;">
        <p>Subscribe to our static calendar feeds to stay informed about upcoming public hearings, votes, and comment deadlines in your personal calendar.</p>
    </div>

    {picker_html}
    """


def export_site_content(
    conn,
    out_dirs: Optional[List[Path]] = None,
    today_str: str = DEFAULT_TODAY_STR,
) -> Dict[str, int]:
    """Exports the broadsheet site structure to docs/ and site/_site/."""
    import json as _json
    from pipeline.matters import link_matters

    ensure_directories()
    link_matters(conn, today_str=today_str)

    target_dirs = out_dirs if out_dirs is not None else [DOCS_DIR, SITE_OUT_DIR]
    for d in target_dirs:
        for legacy_sub in ("matters", "archive", "test-cases", "influences", "action-pages"):
            legacy_path = d / legacy_sub
            if legacy_path.exists():
                shutil.rmtree(legacy_path, ignore_errors=True)
        for legacy_file in ("matters.html", "archive.html", "method.html"):
            lf = d / legacy_file
            if lf.exists():
                lf.unlink()
        for sub in ("briefs", "actors", "reading"):
            sub_dir = d / sub
            sub_dir.mkdir(parents=True, exist_ok=True)
            for old_html in sub_dir.glob("*.html"):
                old_html.unlink()

    stats = {"matters": 0, "briefs": 0, "actors": 0, "test_cases": 0}
    contacts_data = load_contacts_config()

    today_dt = datetime.strptime(today_str[:10], "%Y-%m-%d").date()
    seven_days_ago_str = (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    # Fetch all items with their latest approved brief (if any) and test_case status
    all_rows = conn.execute(
        """
        SELECT i.*,
               t.flagged_reason as test_case_reason,
               (SELECT d.id FROM drafts d WHERE d.item_id = i.id AND d.kind = 'brief' AND d.reviewed = 1 ORDER BY d.id DESC LIMIT 1) as brief_id,
               (SELECT d.markdown FROM drafts d WHERE d.item_id = i.id AND d.kind = 'brief' AND d.reviewed = 1 ORDER BY d.id DESC LIMIT 1) as brief_markdown,
               (SELECT d.markdown FROM drafts d WHERE d.item_id = i.id AND d.kind = 'brief' ORDER BY d.reviewed DESC, d.id DESC LIMIT 1) as any_brief_markdown
        FROM items i
        LEFT JOIN test_cases t ON i.id = t.item_id
        ORDER BY i.meeting_date DESC, i.id ASC
        """
    ).fetchall()

    # Map item_id -> matter record
    item_to_matter: Dict[str, Dict[str, Any]] = {}
    matter_rows = conn.execute("SELECT * FROM matters").fetchall()
    matters_by_id = {r["id"]: dict(r) for r in matter_rows}
    for me in conn.execute("SELECT matter_id, item_id FROM matter_events WHERE confidence = 'confirmed'").fetchall():
        if me["matter_id"] in matters_by_id:
            item_to_matter[me["item_id"]] = matters_by_id[me["matter_id"]]

    mb_by_matter_id = {
        r["matter_id"]: dict(r)
        for r in conn.execute("SELECT * FROM matter_briefs").fetchall()
    }

    # Process and classify all items, merging duplicates by matter_id / dedupe_key
    processed_items: List[Dict[str, Any]] = []
    for r in all_rows:
        d = dict(r)
        bt = d.get("body_text") or ""
        disp_title = generate_display_title(d, bt)
        body_name = determine_exact_body_name(d, bt)
        has_brief = bool(d.get("brief_id"))
        is_tc = bool(d.get("test_case_reason"))
        tier = classify_item_tier(d, has_brief=has_brief, is_test_case=is_tc)
        if tier == 3:
            continue

        jur_label = "Olympia" if (d.get("jurisdiction") or "").lower() == "olympia" else "Thurston County"
        if jur_label.lower() in body_name.lower():
            full_body_label = body_name
        elif jur_label == "Thurston County" and body_name.lower().startswith("county"):
            full_body_label = f"Thurston {body_name}"
        else:
            full_body_label = f"{jur_label} {body_name}"
        stake = get_whats_at_stake(d)
        status_label = determine_plain_status(d, body_name, has_brief=has_brief, today_str=today_str)
        linked_matter = item_to_matter.get(d["id"])
        dedupe_key = linked_matter["id"] if linked_matter else normalize_dedupe_key(d, disp_title)

        d.update({
            "display_title": linked_matter["plain_title"] if (linked_matter and linked_matter.get("plain_title")) else disp_title,
            "body_name": body_name,
            "full_body_label": full_body_label,
            "has_brief": has_brief,
            "is_test_case": is_tc or bool(linked_matter and linked_matter.get("is_test_case")),
            "tier": tier,
            "stake": stake,
            "status_label": status_label,
            "dedupe_key": dedupe_key,
            "matter": linked_matter,
        })
        processed_items.append(d)

    # Deduplicate Tier 1 / brief / test-case items into canonical matters first
    briefs_page_candidates = [
        it for it in processed_items
        if it["tier"] == 1 or it["has_brief"] or it["is_test_case"]
    ]
    briefs_page_candidates.sort(
        key=lambda x: (1 if x["has_brief"] else 0, 1 if x["is_test_case"] else 0, x.get("meeting_date") or ""),
        reverse=True,
    )

    merged_matters: List[Dict[str, Any]] = []
    seen_matter_keys = set()
    all_item_ids_by_key: Dict[str, List[str]] = {}
    for it in briefs_page_candidates:
        all_item_ids_by_key.setdefault(it["dedupe_key"], []).append(it["id"])
        if it["dedupe_key"] in seen_matter_keys:
            continue
        seen_matter_keys.add(it["dedupe_key"])
        merged_matters.append(it)

    # 1. Export Unified Living Matter + Brief pages (`briefs/brief-<item_id>.html` and `briefs/<matter_id>.html`)
    exported_brief_ids = set()
    for it in merged_matters:
        m_rec = it.get("matter")
        mb_rec = mb_by_matter_id.get(m_rec["id"]) if m_rec else None

        md_source = (
            (mb_rec["markdown"] if mb_rec and mb_rec.get("markdown") else None)
            or it.get("brief_markdown")
            or it.get("any_brief_markdown")
        )
        if not md_source and it["tier"] == 1:
            from pipeline.analyze import generate_brief_markdown
            md_source = generate_brief_markdown(it, [], "", today_str=today_str)

        if md_source:
            html_body = markdown_to_html(md_source)
            canonical_url = f"https://thecorcoran.github.io/Ledger/briefs/brief-{it['id']}.html"
            top_share_html, bottom_share_html = build_brief_share_html(it, canonical_url)

            # Insert top_share_html right below the docket metadata header and before the first section
            if '<h2 class="section-label">' in html_body:
                parts = html_body.split('<h2 class="section-label">', 1)
                html_body_with_share = parts[0] + top_share_html + '<h2 class="section-label">' + parts[1]
            else:
                html_body_with_share = top_share_html + html_body

            # Insert two-group verified contact component into brief
            contacts_section_html = build_brief_contacts_html(it, contacts_data, md_source=md_source, today_str=today_str)
            if "<p><strong>Verified Contacts</strong>" in html_body_with_share:
                html_body_with_contacts = html_body_with_share.split("<p><strong>Verified Contacts</strong>")[0] + contacts_section_html
            elif "<p><strong>Verified Contacts:" in html_body_with_share:
                html_body_with_contacts = html_body_with_share.split("<p><strong>Verified Contacts:")[0] + contacts_section_html
            else:
                html_body_with_contacts = html_body_with_share + contacts_section_html

            timeline_html = build_item_timeline_html(conn, it, it["dedupe_key"], matter=m_rec)
            back_bar = f"""
            <div class="ledger-meta" style="margin-bottom: 12px;">
                <a href="../briefs.html">&larr; All Briefs</a> &bull; {html.escape(it['full_body_label'])} &bull; {html.escape(format_short_date(it.get('meeting_date') or ''))}
            </div>
            """
            qa_section_html = load_matter_qa_html(m_rec["id"] if m_rec else None, it["id"], it["display_title"])
            full_brief_html = back_bar + html_body_with_contacts + bottom_share_html + qa_section_html + timeline_html
            page_html = get_base_html_page(
                it["display_title"],
                full_brief_html,
                active_nav="briefs",
                root_prefix="../",
                og_description=it.get("stake") or f"Documented policy brief for {it['full_body_label']} on {format_short_date(it.get('meeting_date') or '')}.",
                og_url=canonical_url,
            )

            # Write canonical brief-<item_id>.html plus aliases for all merged item IDs and matter_id so permanent links never break
            slugs_to_write = {f"brief-{it['id']}.html"}
            for alias_item_id in all_item_ids_by_key.get(it["dedupe_key"], []):
                slugs_to_write.add(f"brief-{alias_item_id}.html")
                exported_brief_ids.add(alias_item_id)
            if m_rec:
                slugs_to_write.add(f"{m_rec['id']}.html")

            for out_base in target_dirs:
                for file_slug in slugs_to_write:
                    with open(out_base / "briefs" / file_slug, "w", encoding="utf-8") as f:
                        f.write(page_html)
            exported_brief_ids.add(it["id"])
            if it["has_brief"]:
                stats["briefs"] += 1

    # 2. Build Home Page (`index.html`)
    upcoming_t1_raw = [
        it for it in merged_matters
        if it["tier"] == 1
        and (it.get("meeting_date") or "") >= today_str
        and ((it.get("matter") or {}).get("archive_state", "active") == "active")
    ]
    upcoming_t1_raw.sort(key=lambda x: (x.get("meeting_date") or "9999-99-99", 0 if x["has_brief"] else 1))
    upcoming_t1 = upcoming_t1_raw[:6]

    worth_attention_cards = []
    for it in upcoming_t1:
        days_rem = format_days_remaining(it["meeting_date"], today_str)
        date_fmt = format_short_date(it["meeting_date"])
        tc_badge = '<span class="tag-testcase">Test Case</span>' if it["is_test_case"] else ""
        stake_html = f'<p style="margin: 6px 0 0 0; font-size: 15px; color: var(--ink);"><strong>What&#x27;s at stake:</strong> {html.escape(it["stake"])}</p>' if it["stake"] else ""
        brief_href = f"briefs/brief-{it['id']}.html"

        worth_attention_cards.append(f"""
        <div class="attention-card">
            <div class="attention-card-main">
                <div class="ledger-meta">
                    {tc_badge}<strong>{html.escape(it['full_body_label'])}</strong> &bull; {html.escape(date_fmt)} ({html.escape(days_rem)})
                </div>
                <h3 style="margin: 4px 0 4px 0; font-size: 20px;">
                    <a href="{brief_href}" style="text-decoration: none; color: var(--ink);">{html.escape(it['display_title'])}</a>
                </h3>
                <div style="font-size: 13px; color: var(--ink-muted);">Official title: {html.escape(it['title'])}</div>
                {stake_html}
            </div>
            <div class="attention-card-action">
                <a href="{brief_href}" class="btn-brief">Read the brief &rarr;</a>
            </div>
        </div>
        """)

    just_happened_list = [
        it for it in merged_matters
        if it["tier"] == 1 and seven_days_ago_str <= (it.get("meeting_date") or "") < today_str
    ]
    just_happened_list.sort(key=lambda x: (x.get("meeting_date") or "", 1 if x["has_brief"] else 0), reverse=True)

    just_happened_rows = []
    for it in just_happened_list:
        date_fmt = format_short_date(it["meeting_date"])
        days_ago = format_days_remaining(it["meeting_date"], today_str)
        stake_line = f'<div style="font-size: 14px; color: var(--ink); margin-top: 4px;">{html.escape(it["stake"])}</div>' if it["stake"] else ""
        if it["id"] in exported_brief_ids:
            title_html = f'<a href="briefs/brief-{it["id"]}.html"><strong>{html.escape(it["display_title"])}</strong></a>'
            action_html = f'<a href="briefs/brief-{it["id"]}.html" class="btn-secondary">Read the brief &rarr;</a>'
        else:
            title_html = f'<strong>{html.escape(it["display_title"])}</strong>'
            action_html = f'<a href="{html.escape(it["url"])}" target="_blank" rel="noopener" style="font-size: 13px;">Official record &rarr;</a>'

        just_happened_rows.append(f"""
        <div class="ledger-entry" style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px;">
            <div>
                <div class="ledger-meta"><strong>{html.escape(it['full_body_label'])}</strong> &bull; {html.escape(date_fmt)} ({html.escape(days_ago)}) &bull; {html.escape(it['status_label'])}</div>
                <div style="font-size: 17px;">{title_html}</div>
                <div style="font-size: 12px; color: var(--ink-muted); margin-top: 2px;">Official: {html.escape(it['title'])}</div>
                {stake_line}
            </div>
            <div style="flex-shrink: 0; padding-top: 4px;">
                {action_html}
            </div>
        </div>
        """)

    tier2_calendar_items = [
        it for it in processed_items
        if it["tier"] == 2 and (it.get("meeting_date") or "") >= seven_days_ago_str
    ]
    meeting_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    seen_t2_per_group: Dict[Tuple[str, str], set] = {}
    for it in tier2_calendar_items:
        g_key = (it["full_body_label"], it.get("meeting_date") or "")
        seen_set = seen_t2_per_group.setdefault(g_key, set())
        if it["dedupe_key"] in seen_set:
            continue
        seen_set.add(it["dedupe_key"])
        meeting_groups.setdefault(g_key, []).append(it)

    sorted_group_keys = sorted(meeting_groups.keys(), key=lambda k: k[1], reverse=True)

    calendar_groups_html = []
    for body_lbl, m_date in sorted_group_keys:
        g_items = meeting_groups[(body_lbl, m_date)]
        count = len(g_items)
        item_word = "item" if count == 1 else "items"
        short_md = format_month_day(m_date)
        summary_label = f"{body_lbl}, {short_md}: {count} {item_word}"

        li_rows = []
        for git in g_items:
            li_rows.append(
                f'<li><strong>{html.escape(git["display_title"])}</strong> '
                f'<span style="font-size: 12px; color: var(--ink-muted);">({html.escape(clean_agenda_title(git["title"]))})</span> '
                f'&bull; <a href="{html.escape(git["url"])}" target="_blank" rel="noopener" style="font-size: 12px;">Agenda record &rarr;</a></li>'
            )

        calendar_groups_html.append(f"""
        <details class="calendar-group">
            <summary><span>{html.escape(summary_label)}</span></summary>
            <ul class="ledger-list" style="margin-top: 10px;">
                {''.join(li_rows)}
            </ul>
        </details>
        """)

    home_content = f"""
    <p style="font-size: 17px; color: var(--ink); margin-bottom: 24px; border-bottom: 1px solid var(--rule); padding-bottom: 14px;">
        Loretta's Ledger reads Olympia and Thurston County public meeting packets to state plainly what is being decided, who is shaping it, and how residents can weigh in.
    </p>

    <div class="memo-box" style="margin: 0 0 28px 0; background: #faf7f0; border-left: 4px solid var(--oxblood); padding: 14px 18px;">
        <div class="ledger-meta" style="margin-bottom: 4px;">Public Participation Guide</div>
        <div style="font-weight: 700; font-size: 16px; margin-bottom: 4px; color: var(--ink);">How to Take Part in Local Decisions</div>
        <p style="margin: 0 0 10px 0; font-size: 14px; color: var(--ink-muted);">
            You don't need to be an expert to speak up. Learn how to comment on items already scheduled, how to request a topic on a future agenda, how to write to staff versus elected officials, and subscribe to meeting calendars.
        </p>
        <div style="display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;">
            <a href="how-to-take-part.html" class="btn-secondary" style="font-size: 12px;">How to Take Part Guide &rarr;</a>
            <a href="calendar.html" style="font-size: 13px; color: var(--oxblood); text-decoration: underline;">Subscribe to Calendar Feeds (.ics) &rarr;</a>
        </div>
    </div>

    <h2 class="section-label" style="margin-top: 0;">Worth Your Attention</h2>
    <div>
        {''.join(worth_attention_cards) if worth_attention_cards else '<p>No major hearings or ordinance votes scheduled this week.</p>'}
    </div>

    <h2 class="section-label">Just Happened (Last 7 Days)</h2>
    <div>
        {''.join(just_happened_rows) if just_happened_rows else '<p>No major items concluded in the past 7 days.</p>'}
    </div>

    <h2 class="section-label">Also on the Calendar</h2>
    <p style="font-size: 13px; color: var(--ink-muted); margin-bottom: 12px;">Other contracts, grants, and committee briefings grouped by meeting.</p>
    <div>
        {''.join(calendar_groups_html) if calendar_groups_html else '<p>No additional calendar items.</p>'}
    </div>
    """

    # 3. Build Public Briefs Page (`briefs.html`) as a Legislative Bill Tracker:
    # Columns: plain title, jurisdiction, status, last action date, next action date.
    # Filters: Active, Closed, Test cases (+ Search).
    # Sort by next action (soonest upcoming next_action_date first, then most recent last_action_date descending).
    def _tracker_sort_key(it: Dict[str, Any]) -> Tuple[int, str, str]:
        m = it.get("matter") or {}
        next_dt = m.get("next_action_date") or (it.get("meeting_date") if (it.get("meeting_date") or "") >= today_str else None)
        last_dt = m.get("last_action_date") or it.get("meeting_date") or ""
        # Negate last_dt for descending tie-breaker by converting YYYY-MM-DD digits
        inv_last = "".join(chr(ord("9") - ord(c) + ord("0")) if c.isdigit() else c for c in last_dt[:10])
        if next_dt:
            return (0, next_dt[:10], inv_last)
        return (1, "9999-99-99", inv_last)

    merged_matters.sort(key=_tracker_sort_key)

    briefs_table_rows = []
    for it in merged_matters:
        m = it.get("matter") or {}
        m_date = (it.get("meeting_date") or "")[:10]
        last_dt = (m.get("last_action_date") or m_date)[:10]
        next_dt = m.get("next_action_date") or (m_date if m_date >= today_str else None)
        next_label = format_short_date(next_dt) if next_dt else "Not yet posted"
        last_label = format_short_date(last_dt) if last_dt else "—"

        archive_state = (m.get("archive_state") or "active").lower()
        is_active = 1 if archive_state == "active" else 0
        is_closed = 1 if archive_state in ("closed", "dormant") else 0
        is_upcoming = 1 if next_dt else 0
        is_past = 1 if not next_dt else 0
        is_tc = 1 if (it["is_test_case"] and it["has_brief"]) else 0
        if is_tc:
            stats["test_cases"] += 1

        tc_pill = '<span class="tag-testcase">Test Case</span> ' if it["is_test_case"] else ""
        stake_sub = f'<div style="font-size: 13px; color: var(--ink-muted); margin-top: 3px;">{html.escape(it["stake"])}</div>' if it["stake"] else ""
        href = f"briefs/brief-{it['id']}.html" if it["id"] in exported_brief_ids else html.escape(it["url"])
        lifecycle_display = m.get("lifecycle_status") or it["status_label"]
        jur_display = "City of Olympia" if (it.get("jurisdiction") or "").lower() == "olympia" else "Thurston County"
        stable_id_label = m.get("stable_id") or ""
        id_badge = f' &bull; <code>{html.escape(stable_id_label)}</code>' if stable_id_label and not stable_id_label.startswith(("OLYMPIA-", "THURSTON-")) else ""

        briefs_table_rows.append(f"""
        <tr data-active="{is_active}" data-closed="{is_closed}" data-archive="{html.escape(archive_state)}" data-upcoming="{is_upcoming}" data-past="{is_past}" data-testcase="{1 if it['is_test_case'] else 0}">
            <td>
                {tc_pill}<a href="{href}"><strong>{html.escape(it['display_title'])}</strong></a>
                <div style="font-size: 12px; color: var(--ink-faint); margin-top: 2px;">Official: {html.escape(clean_agenda_title(it['title']))}{id_badge}</div>
                {stake_sub}
            </td>
            <td>
                <strong>{html.escape(jur_display)}</strong>
                <div style="font-size: 12px; color: var(--ink-muted);">{html.escape(it['full_body_label'])}</div>
            </td>
            <td><span class="status-badge">{html.escape(lifecycle_display)}</span></td>
            <td style="white-space: nowrap;">{html.escape(last_label)}</td>
            <td style="white-space: nowrap;"><strong>{html.escape(next_label)}</strong></td>
        </tr>
        """)
        stats["matters"] += 1

    search_index_entries = []
    for it in merged_matters:
        m = it.get("matter") or {}
        mb_rec = mb_by_matter_id.get(m.get("id", ""))
        raw_md = (
            (mb_rec["markdown"] if mb_rec and mb_rec.get("markdown") else None)
            or it.get("brief_markdown")
            or it.get("any_brief_markdown")
            or ""
        )
        clean_text = re.sub(r"[#*_`|\[\]\(\)\-]", " ", raw_md)
        clean_text = " ".join(clean_text.split())
        search_index_entries.append({
            "id": it["id"],
            "title": it["display_title"],
            "official": clean_agenda_title(it.get("title", "")),
            "jur": "City of Olympia" if (it.get("jurisdiction") or "").lower() == "olympia" else "Thurston County",
            "body": it.get("full_body_label", ""),
            "date": format_short_date(it.get("meeting_date") or ""),
            "stake": it.get("stake") or "",
            "href": f"briefs/brief-{it['id']}.html" if it["id"] in exported_brief_ids else it.get("url", "#"),
            "has_brief": 1 if it["id"] in exported_brief_ids else 0,
            "text": clean_text[:4000],
        })
    search_index_json = json.dumps(search_index_entries)

    for out_base in target_dirs:
        with open(out_base / "search-index.json", "w", encoding="utf-8") as f:
            f.write(search_index_json)

    briefs_index_content = f"""
    <p style="font-size: 16px; color: var(--ink-muted); margin-bottom: 20px;">
        Living legislative tracker for Olympia and Thurston County ordinances, code amendments, budgets, contracts, and land-use cases. Sorted by next scheduled action.
    </p>

    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 14px;">
        <h1 class="headline-title" style="margin: 0; border: none; padding: 0;">Policy Briefs &amp; Matter Tracker</h1>
        <div style="display: flex; gap: 14px; align-items: center; flex-wrap: wrap; font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em;">
            <label style="cursor: pointer;"><input type="radio" name="brief-filter" value="active" checked onchange="filterBriefs()"> <strong>Active</strong></label>
            <label style="cursor: pointer;"><input type="radio" name="brief-filter" value="closed" onchange="filterBriefs()"> <strong>Closed</strong></label>
            <label style="cursor: pointer;"><input type="radio" name="brief-filter" value="testcase" onchange="filterBriefs()"> <strong>Test cases</strong></label>
            <input type="search" id="brief-search" placeholder="Search brief text, topics, codes..." oninput="filterBriefs()" style="font-family: var(--font-serif); font-size: 13px; padding: 6px 10px; border: 1px solid var(--rule-dark); background: var(--surface); width: 280px;">
        </div>
    </div>

    <!-- Client-side Static Search Results Container -->
    <div id="brief-search-results" style="display: none; margin-bottom: 22px; padding: 14px 18px; background: #fffcf8; border: 1px solid var(--rule); border-left: 4px solid var(--oxblood);">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div id="brief-search-count" style="font-weight: 700; font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--oxblood);"></div>
            <button type="button" onclick="document.getElementById('brief-search').value=''; filterBriefs();" style="background: none; border: none; font-size: 12px; text-decoration: underline; color: var(--ink-muted); cursor: pointer;">Clear search</button>
        </div>
        <div id="brief-search-list" style="display: flex; flex-direction: column; gap: 10px;"></div>
    </div>

    <table class="ledger-table" id="briefs-table">
        <thead>
            <tr>
                <th>Matter</th>
                <th style="width: 170px;">Jurisdiction</th>
                <th style="width: 240px;">Status</th>
                <th style="width: 120px;">Last Action</th>
                <th style="width: 130px;">Next Action</th>
            </tr>
        </thead>
        <tbody>
            {''.join(briefs_table_rows)}
        </tbody>
    </table>

    <script>
        var searchIndexData = {search_index_json};

        function escapeRegExp(string) {{
            return string.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
        }}

        function filterBriefs() {{
            var sel = document.querySelector('input[name="brief-filter"]:checked');
            var val = sel ? sel.value : 'active';
            var rawQ = document.getElementById('brief-search').value || '';
            var q = rawQ.toLowerCase().trim();
            var rows = document.querySelectorAll('#briefs-table tbody tr');

            var resultsBox = document.getElementById('brief-search-results');
            var resultsCount = document.getElementById('brief-search-count');
            var resultsList = document.getElementById('brief-search-list');

            if (q.length >= 2) {{
                var matches = [];
                var terms = q.split(/\\s+/);
                searchIndexData.forEach(function(item) {{
                    var corpus = (item.title + ' ' + item.official + ' ' + item.body + ' ' + item.stake + ' ' + item.text).toLowerCase();
                    var allMatch = terms.every(function(t) {{ return corpus.indexOf(t) !== -1; }});
                    if (allMatch) {{
                        matches.push(item);
                    }}
                }});

                resultsBox.style.display = 'block';
                var mWord = matches.length === 1 ? 'brief' : 'briefs';
                resultsCount.textContent = matches.length + ' ' + mWord + ' matching "' + rawQ.trim() + '" across brief text';

                var htmlItems = [];
                matches.slice(0, 5).forEach(function(m) {{
                    var snippet = '';
                    var idx = m.text.toLowerCase().indexOf(terms[0]);
                    if (idx !== -1) {{
                        var start = Math.max(0, idx - 60);
                        var end = Math.min(m.text.length, idx + 140);
                        var rawSnippet = (start > 0 ? '… ' : '') + m.text.substring(start, end).trim() + (end < m.text.length ? ' …' : '');
                        terms.forEach(function(t) {{
                            var regex = new RegExp('(' + escapeRegExp(t) + ')', 'gi');
                            rawSnippet = rawSnippet.replace(regex, '<mark style="background:#fef08a; padding:1px 3px; font-weight:600;">$1</mark>');
                        }});
                        snippet = '<div style="font-size: 13px; color: var(--ink-muted); margin-top: 4px; line-height: 1.45;">' + rawSnippet + '</div>';
                    }} else if (m.stake) {{
                        snippet = '<div style="font-size: 13px; color: var(--ink-muted); margin-top: 4px;">' + m.stake + '</div>';
                    }}

                    var action = m.has_brief
                        ? '<a href="' + m.href + '" class="btn-brief" style="font-size: 11px; padding: 4px 10px;">Read brief &rarr;</a>'
                        : '<a href="' + m.href + '" target="_blank" rel="noopener" style="font-size: 12px;">Official record &rarr;</a>';

                    htmlItems.push(
                        '<div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; padding-bottom: 8px; border-bottom: 1px dotted var(--rule);">' +
                        '  <div>' +
                        '    <div class="ledger-meta">' + m.jur + ' &bull; ' + m.body + ' &bull; ' + m.date + '</div>' +
                        '    <div style="font-size: 15px;"><a href="' + m.href + '"><strong>' + m.title + '</strong></a></div>' +
                        snippet +
                        '  </div>' +
                        '  <div style="flex-shrink: 0; padding-top: 4px;">' + action + '</div>' +
                        '</div>'
                    );
                }});
                resultsList.innerHTML = htmlItems.join('') || '<div style="font-size: 13px; color: var(--ink-muted);">No text matches found in briefs.</div>';
            }} else {{
                resultsBox.style.display = 'none';
            }}

            rows.forEach(function(r) {{
                var isAct = r.getAttribute('data-active') === '1';
                var isCl = r.getAttribute('data-closed') === '1';
                var isTc = r.getAttribute('data-testcase') === '1';
                var textMatch = !q || r.textContent.toLowerCase().indexOf(q) !== -1;
                var tabMatch = true;
                if (!q) {{
                    if (val === 'active') tabMatch = isAct;
                    else if (val === 'closed') tabMatch = isCl;
                    else if (val === 'testcase') tabMatch = isTc;
                }} else {{
                    if (val === 'closed') tabMatch = isCl;
                    else if (val === 'testcase') tabMatch = isTc;
                }}
                r.style.display = (tabMatch && textMatch) ? '' : 'none';
            }});
        }}
        filterBriefs();
    </script>
    """

    # 4. Export Who's in the Room Actor Pages
    actors = conn.execute(
        """
        SELECT a.*, COUNT(DISTINCT r.item_id) as items_touched
        FROM upstream_actors a
        JOIN upstream_refs r ON a.id = r.actor_id
        JOIN drafts d ON d.item_id = r.item_id
        WHERE d.reviewed = 1
        GROUP BY a.id
        ORDER BY items_touched DESC, a.name ASC
        """
    ).fetchall()

    actor_cards = []
    for act in actors:
        refs = conn.execute(
            """
            SELECT r.*, i.title as item_title, i.display_title, i.meeting_date, i.jurisdiction, i.url as item_url, i.body_text
            FROM upstream_refs r
            JOIN items i ON r.item_id = i.id
            JOIN drafts d ON d.item_id = r.item_id
            WHERE r.actor_id = ? AND d.reviewed = 1
            GROUP BY r.item_id
            ORDER BY i.meeting_date DESC
            """,
            (act["id"],),
        ).fetchall()

        actor_touch_rows = []
        seen_actor_keys = set()
        for r in refs:
            r_dict = dict(r)
            r_dict["title"] = r_dict.get("item_title") or ""
            disp_t = r_dict.get("display_title") or generate_display_title(r_dict, r_dict.get("body_text") or "")
            r_key = normalize_dedupe_key(r_dict, disp_t)
            if r_key in seen_actor_keys:
                continue
            seen_actor_keys.add(r_key)
            rel_brief_url = f"briefs/brief-{r['item_id']}.html"
            target_disp = disp_t
            target_date = r["meeting_date"]
            body_nm = determine_exact_body_name(r_dict, r_dict.get("body_text") or "")
            actor_touch_rows.append(f"""
            <tr>
                <td>{html.escape(format_short_date(target_date))}</td>
                <td>{html.escape(body_nm)}</td>
                <td><a href="../{html.escape(rel_brief_url)}"><strong>{html.escape(target_disp)}</strong></a></td>
                <td>{html.escape(r['mechanism'].replace('_', ' '))}</td>
                <td><a href="{html.escape(r['evidence_url'] or r['item_url'])}" target="_blank" rel="noopener">Official Source Record &rarr;</a></td>
            </tr>
            """)

        actor_page_content = f"""
        <div class="ledger-meta">
            <a href="../actors.html">&larr; Back to Who's in the Room</a> &bull; {html.escape(act['upstream_type'].replace('_', ' ').title())}
        </div>
        <h1 class="headline-title" style="margin-top: 8px;">{html.escape(act['name'])}</h1>

        <div class="memo-box">
            <p><strong>Documented Classification:</strong> {html.escape(act['upstream_type'].replace('_', ' ').title())}</p>
            <p><strong>Factual Record:</strong> {html.escape(act['notes'] or 'Documented participant in municipal agenda items.')}</p>
            {f'<p><a href="{act["home_url"]}" target="_blank" rel="noopener">Official Public Site &rarr;</a></p>' if act["home_url"] else ''}
        </div>

        <h2 class="section-label">Documented Local Briefs Touched ({len(actor_touch_rows)})</h2>
        <p style="font-size: 13px; color: var(--ink-muted);">This table records formal statutory, advisory, financial, or regulatory references in official agenda files.</p>

        <table class="ledger-table">
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Body</th>
                    <th>Brief</th>
                    <th>Documented Role</th>
                    <th>Source Verification</th>
                </tr>
            </thead>
            <tbody>
                {''.join(actor_touch_rows)}
            </tbody>
        </table>
        """

        act_file_slug = f"{act['id']}.html"
        act_page_html = get_base_html_page(act["name"], actor_page_content, active_nav="actors", root_prefix="../")
        for out_base in target_dirs:
            with open(out_base / "actors" / act_file_slug, "w", encoding="utf-8") as f:
                f.write(act_page_html)

        actor_cards.append(f"""
        <div class="ledger-entry">
            <div class="ledger-meta">{html.escape(act['upstream_type'].replace('_', ' ').title())} &bull; Touched {len(actor_touch_rows)} Briefs</div>
            <h3 style="margin: 0 0 6px 0;"><a href="actors/{act_file_slug}">{html.escape(act['name'])}</a></h3>
            <p style="font-size: 14px; margin-bottom: 6px;">{html.escape(act['notes'] or '')}</p>
            <div style="font-size: 13px;"><a href="actors/{act_file_slug}">View Documented Record &rarr;</a></div>
        </div>
        """)
        stats["actors"] += 1

    actors_content = f"""
    <p style="font-size: 16px; color: var(--ink-muted); margin-bottom: 20px;">
        An index of state and federal agencies, consultants, funders, and regional bodies documented inside Olympia and Thurston County meeting packets.
    </p>
    <h1 class="headline-title">Who's in the Room</h1>
    <div style="margin-top: 20px;">
        {''.join(actor_cards) if actor_cards else '<p>No upstream actors linked to approved briefs yet.</p>'}
    </div>
    """

    # 5. About, Principles, and Glossary pages
    about_content = """
    <p style="font-size: 16px; color: var(--ink-muted); margin-bottom: 20px;">
        Why Loretta's Ledger exists, how it reads public records, and what standards it holds itself to.
    </p>
    <h1 class="headline-title">Why This Exists</h1>

    <p>Loretta's Ledger is named for my grandmother, Loretta Corcoran. She ran Loretta's Cafe at 213 North Capitol Way in downtown Olympia for many years, and she made meals for the Olympia jail. The café closed when its building was demolished to make way for the Olympia Center.</p>

    <div class="memo-box" style="font-style: italic; color: var(--ink-muted); margin: 20px 0;">
        <p style="margin-bottom: 6px;">[Photo: Loretta Corcoran at the grill, Loretta's Cafe, 213 North Capitol Way, Olympia, 1959. Corcoran family collection.]</p>
        <p style="margin: 0;">[Image: Newspaper advertisement, Loretta's Cafe, undated.]</p>
    </div>

    <p>The Olympia Center has been a great benefit to the community. It also took the place of an establishment that was part of the city. Both are true, and the Ledger exists to keep both in view. Decisions made in meeting rooms about land have real impacts on real households.</p>

    <p>Most of those decisions are made in public, in documents and meetings that few of us have time to read. The Ledger reads them, says plainly what is being decided and why it might matter, and shows where the decision came from and where it is headed.</p>

    <p>Our city and county governments are good, and the work they do is mundane and necessary. We need them to keep decisions as close to the people they affect as they can, and to count the cost to a household as carefully as the benefit to the city.</p>

    <h2 class="section-label">What we hold ourselves to</h2>

    <p><strong>Start from the record.</strong> Every claim links to the public document it comes from. If the documents don't say, we say so.</p>

    <p><strong>Separate what's known from what's argued.</strong> Facts come first. Analysis is labeled as analysis.</p>

    <p><strong>Count both sides.</strong> We name what a policy does well and what it costs, and who pays.</p>

    <p><strong>Describe relationships, not motives.</strong> We track who is in the room: the consultants, advisory bodies, funders, state and regional agencies, and outside groups that shape local decisions. We report only the relationships the public record shows, and we never claim to know why anyone did anything.</p>

    <p><strong>Stay close to home.</strong> We cover Olympia and Thurston County. Other places appear only when they shape a local decision.</p>

    <p><strong>Use plain language.</strong> We try to write so that any resident can follow what is happening and what they can do about it.</p>

    <p><strong>Leave the conclusions to you.</strong> We offer questions worth asking, not instructions on how to vote or comment.</p>

    <h2 class="section-label">Editorial Process</h2>

    <p>Public agendas and official document packets are collected and analyzed against our core principles. Every item is reviewed and approved by an editor before publication, and nothing unreviewed is published. Source documents and methodology are open to the public: <a href="https://github.com/thecorcoran/Ledger" target="_blank" rel="noopener">https://github.com/thecorcoran/Ledger</a>.</p>

    <h2 class="section-label">Disclosures</h2>

    <p>The Ledger is written by Jon, who lives in Thurston County in the house his mother grew up in. He is not a lawyer, a planner, or a reporter, and he does not own a business. He has no financial ties to the matters the Ledger covers.</p>

    <h2 class="section-label">Mistakes and corrections</h2>

    <p>If something here is wrong, tell us at <a href="mailto:thecorcoran@gmail.com">thecorcoran@gmail.com</a>. We'll fix it and note the change on the page.</p>
    """

    principles_content = """
    <p style="font-size: 16px; color: var(--ink-muted); margin-bottom: 20px;">
        The eight plain-language principles Loretta's Ledger uses to evaluate local ordinances, budgets, and land-use rules.
    </p>
    <h1 class="headline-title">Our Principles</h1>

    <div class="memo-box">
        <h2 style="font-size: 18px; margin: 0 0 10px 0; color: var(--oxblood);">1. Subsidiarity (Who Decides?)</h2>
        <p><strong>The Question:</strong> Is this decision made at the level closest to the people it affects?</p>
        <p>Decisions should be made by local elected officials accountable to neighbors in Olympia and Thurston County, not dictated by distant state or federal bureaucracies, outside lobbying templates, or funding strings.</p>
    </div>

    <div class="memo-box">
        <h2 style="font-size: 18px; margin: 0 0 10px 0; color: var(--oxblood);">2. Ownership (Can Ordinary People Hold Property?)</h2>
        <p><strong>The Question:</strong> Does this help working families and independent businesses own and keep real property?</p>
        <p>A resilient community depends on widely held property. We look at whether rules make it easier to buy, keep, and care for a home, small parcel, or storefront, or whether they lock land behind costly regulations and institutional consolidation.</p>
    </div>

    <div class="memo-box">
        <h2 style="font-size: 18px; margin: 0 0 10px 0; color: var(--oxblood);">3. Small &amp; Local vs. Large &amp; Distant</h2>
        <p><strong>The Question:</strong> Who carries the burden, and who captures the benefit?</p>
        <p>Local government should not favor large institutional corporations over independent local trades, contractors, and shops. We track who gets the contracts and who bears the compliance costs.</p>
    </div>

    <div class="memo-box">
        <h2 style="font-size: 18px; margin: 0 0 10px 0; color: var(--oxblood);">4. Family &amp; Household (Self-Reliance vs. Bureaucracy)</h2>
        <p><strong>The Question:</strong> Does this support what families and neighborhoods do for themselves?</p>
        <p>We favor policies that protect independent household livelihoods and mutual aid over programs that substitute administrative management for family and community responsibility.</p>
    </div>

    <div class="memo-box">
        <h2 style="font-size: 18px; margin: 0 0 10px 0; color: var(--oxblood);">5. Cost and Who Pays (Honest Accounting)</h2>
        <p><strong>The Question:</strong> Who pays the bills, and is the real cost made clear?</p>
        <p>Every public program costs money. We insist on knowing the total dollar amount, how much each household or parcel will pay, and whether costs are transparently shared or quietly loaded onto property tax and utility bills.</p>
    </div>

    <div class="memo-box">
        <h2 style="font-size: 18px; margin: 0 0 10px 0; color: var(--oxblood);">6. Consent &amp; Process (Were Neighbors Heard?)</h2>
        <p><strong>The Question:</strong> Did the people affected have timely notice and a genuine chance to respond?</p>
        <p>Public hearings should not be formalities. We check whether notice was given early enough, in plain language, with full documents attached, and at meeting times ordinary working people can attend.</p>
    </div>

    <div class="memo-box">
        <h2 style="font-size: 18px; margin: 0 0 10px 0; color: var(--oxblood);">7. Reversibility &amp; Accountability (Can Voters Change It?)</h2>
        <p><strong>The Question:</strong> Can voters hold elected officials answerable, and can this be undone if it doesn't work?</p>
        <p>Decisions should have named sponsors, recorded roll-call votes, and expiration dates. We look with skepticism on multi-decade contracts, permanent debt, or powers handed off to unelected boards.</p>
    </div>

    <div class="memo-box">
        <h2 style="font-size: 18px; margin: 0 0 10px 0; color: var(--oxblood);">8. Place (Respecting Our Community's Fabric)</h2>
        <p><strong>The Question:</strong> Does this respect the physical character, history, and people already here?</p>
        <p>Our towns and rural lands have character built across generations. We keep in mind the lesson of Loretta's Cafe: community improvements should not needlessly sweep away the physical and cultural anchors that make our community feel like home.</p>
    </div>

    <p style="margin-top: 28px; padding-top: 14px; border-top: 1px solid var(--rule);">
        <a href="reading/">Further reading: the books and essays behind these questions.</a>
    </p>
    """

    glossary_content = """
    <p style="font-size: 16px; color: var(--ink-muted); margin-bottom: 20px;">
        Plain-language definitions for legal, policy, and planning terms commonly used across Olympia and Thurston County public dockets.
    </p>
    <h1 class="headline-title">Glossary of Local Government Terms</h1>

    <div class="ledger-entry">
        <div class="ledger-meta">Local Legislation</div>
        <h3 style="margin: 0 0 4px 0;">Ordinance</h3>
        <p>A formal local law adopted by a city council or board of county commissioners that has permanent legal force within that city or county.</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Council Action</div>
        <h3 style="margin: 0 0 4px 0;">Resolution</h3>
        <p>A formal statement of policy, administrative approval (such as accepting a grant or setting an advisory goal), or official direction that generally carries less permanent legal effect than an ordinance.</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Meeting Procedure</div>
        <h3 style="margin: 0 0 4px 0;">Consent Agenda</h3>
        <p>A group of routine, non-controversial agenda items passed together in a single collective vote without separate discussion, unless a councilmember or commissioner asks to pull an item for individual debate.</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Public Finance</div>
        <h3 style="margin: 0 0 4px 0;">Assessment</h3>
        <p>A fee or charge placed directly on real property tax statements for a dedicated public purpose or service, such as conservation district operations, diking districts, or stormwater management.</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Environmental Review</div>
        <h3 style="margin: 0 0 4px 0;">SEPA Checklist</h3>
        <p>A standard disclosure form under the Washington State Environmental Policy Act (RCW 43.21C) where project sponsors and local agencies evaluate potential impacts on air, water, land, and traffic before permits are granted.</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Growth Management</div>
        <h3 style="margin: 0 0 4px 0;">Critical Areas Ordinance (CAO)</h3>
        <p>Local land use regulations required by state law (RCW 36.70A) that establish protective buffer zones around wetlands, streams, fish and wildlife habitat conservation areas, and steep slopes.</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Public Health</div>
        <h3 style="margin: 0 0 4px 0;">Group B Water System</h3>
        <p>A small public drinking water system that provides water to 2 through 14 service connections and fewer than 25 people per day, regulated primarily under county health sanitary codes (Article III).</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Inter-Agency Agreement</div>
        <h3 style="margin: 0 0 4px 0;">Interlocal Agreement (ILA)</h3>
        <p>A legally binding contract between two or more local government bodies (such as between the City of Olympia and Thurston County) authorized under chapter 39.34 RCW to coordinate shared services, facilities, or funding.</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Public Participation</div>
        <h3 style="margin: 0 0 4px 0;">Public Hearing</h3>
        <p>A formal meeting required by state statute or city charter where members of the public are legally entitled to give verbal or written testimony on the official record before elected officials vote on an ordinance or major land use decision.</p>
    </div>

    <div class="ledger-entry">
        <div class="ledger-meta">Legal Procedure</div>
        <h3 style="margin: 0 0 4px 0;">Quasi-Judicial Action</h3>
        <p>A proceeding where elected officials or a hearing examiner act like judges applying established legal criteria to a specific property or permit application, with strict rules preventing private discussions outside the formal public record.</p>
    </div>
    """

    reading_md_path = ROOT_DIR / "reading.md"
    reading_md_text = reading_md_path.read_text(encoding="utf-8") if reading_md_path.exists() else "# Further Reading\n"
    reading_content = markdown_to_html(reading_md_text)

    how_to_content = build_how_to_take_part_html(contacts_data, today_str=today_str)
    calendar_content = build_calendar_page_html(today_str=today_str)

    # Write canonical pages (Home | Briefs | Who's in the Room | Principles | About + How to Take Part + Calendar + Glossary + Further Reading)
    pages_to_write = [
        ("index.html", "Home", home_content, "home"),
        ("briefs.html", "Briefs", briefs_index_content, "briefs"),
        ("actors.html", "Who's in the Room", actors_content, "actors"),
        ("principles.html", "Principles", principles_content, "principles"),
        ("about.html", "Why This Exists", about_content, "about"),
        ("how-to-take-part.html", "How to Take Part in Local Decisions", how_to_content, ""),
        ("calendar.html", "Meeting & Hearing Calendar Feeds", calendar_content, ""),
        ("glossary.html", "Glossary", glossary_content, ""),
        ("reading.html", "Further Reading", reading_content, ""),
    ]

    for filename, title, content, nav_key in pages_to_write:
        page_html = get_base_html_page(title, content, active_nav=nav_key)
        for out_base in target_dirs:
            with open(out_base / filename, "w", encoding="utf-8") as f:
                f.write(page_html)

    # Build and export static RFC 5545 iCalendar (.ics) feeds
    cal_stats = build_and_export_calendars(conn, target_dirs, today_str=today_str)
    stats["calendar_events"] = cal_stats.get("events_extracted", 0)
    stats["calendar_feeds"] = cal_stats.get("feeds_generated", 0)

    # Write /reading/index.html for clean /reading/ URL
    reading_dir_html = get_base_html_page("Further Reading", reading_content, active_nav="", root_prefix="../")
    for out_base in target_dirs:
        reading_sub = out_base / "reading"
        reading_sub.mkdir(parents=True, exist_ok=True)
        with open(reading_sub / "index.html", "w", encoding="utf-8") as f:
            f.write(reading_dir_html)

    for out_base in target_dirs:
        nojekyll_file = out_base / ".nojekyll"
        if not nojekyll_file.exists():
            with open(nojekyll_file, "w", encoding="utf-8") as f:
                f.write("# Disable Jekyll processing on GitHub Pages\n")

    return stats


def export_pdf_html(conn, draft_id: int, out_path: Optional[Path] = None) -> Path:
    """Exports a single approved brief into a standalone printable/PDF HTML file with print stylesheet."""
    row = conn.execute(
        """
        SELECT d.*, i.title as item_title, i.jurisdiction, i.meeting_date
        FROM drafts d
        JOIN items i ON d.item_id = i.id
        WHERE d.id = ?
        """,
        (draft_id,),
    ).fetchone()

    if not row:
        raise ValueError(f"Draft not found: {draft_id}")

    target = out_path or (BRIEFS_DIR / f"brief-{row['item_id']}.html")
    html_content = markdown_to_html(row["markdown"])

    full_page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{html.escape(row['item_title'])} — Brief</title>
    <style>
        @page {{
            size: letter;
            margin: 0.8in;
        }}
        body {{
            font-family: Georgia, serif;
            color: #111;
            line-height: 1.5;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
        }}
        h1 {{ font-size: 24px; border-bottom: 2px solid #222; padding-bottom: 6px; }}
        h2 {{ font-size: 18px; margin-top: 24px; border-bottom: 1px solid #ccc; }}
        pre {{ background: #f4f4f4; padding: 10px; font-family: monospace; font-size: 11px; }}
        blockquote {{ border-left: 3px solid #666; margin: 10px 0; padding-left: 12px; font-style: italic; }}
        ul {{ padding-left: 20px; }}
        li {{ margin-bottom: 6px; }}
    </style>
</head>
<body>
    {html_content}
</body>
</html>
"""
    with open(target, "w", encoding="utf-8") as f:
        f.write(full_page)

    return target


def main():
    parser = argparse.ArgumentParser(description="Export Loretta's Ledger broadsheet site")
    parser.add_argument("--all", action="store_true", help="Export complete site")
    parser.add_argument("--pdf", type=int, default=None, help="Export a specific approved brief to print/PDF HTML")
    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.pdf:
            out = export_pdf_html(conn, args.pdf)
            print(f"Exported printable brief to: {out}")
        else:
            stats = export_site_content(conn)
            print("Broadsheet export complete:")
            print(f"  Brief rows indexed: {stats['matters']}")
            print(f"  Approved briefs:    {stats['briefs']}")
            print(f"  Actors indexed:     {stats['actors']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
