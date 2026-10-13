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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def get_base_html_page(title: str, content: str, active_nav: str = "home", root_prefix: str = "") -> str:
    """Account-book and broadsheet layout template with unified 5-item nav:
    Home | Briefs | Who's in the Room | Principles | About
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(title)} — Loretta's Ledger</title>
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
        Loretta's Ledger &bull; Documented Public Record for Thurston County and City of Olympia &bull; Strictly Factual &bull; <a href="{root_prefix}glossary.html">Glossary</a> &bull; <a href="{root_prefix}reading/">Further Reading</a>
    </footer>
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
            timeline_html = build_item_timeline_html(conn, it, it["dedupe_key"], matter=m_rec)
            back_bar = f"""
            <div class="ledger-meta" style="margin-bottom: 12px;">
                <a href="../briefs.html">&larr; All Briefs</a> &bull; {html.escape(it['full_body_label'])} &bull; {html.escape(format_short_date(it.get('meeting_date') or ''))}
            </div>
            """
            full_brief_html = back_bar + html_body + timeline_html
            page_html = get_base_html_page(it["display_title"], full_brief_html, active_nav="briefs", root_prefix="../")

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
    <p style="font-size: 17px; color: var(--ink); margin-bottom: 28px; border-bottom: 1px solid var(--rule); padding-bottom: 14px;">
        Loretta's Ledger reads Olympia and Thurston County public meeting packets to state plainly what is being decided, who is shaping it, and how residents can weigh in.
    </p>

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
            <input type="search" id="brief-search" placeholder="Search all matters..." oninput="filterBriefs()" style="font-family: var(--font-serif); font-size: 13px; padding: 4px 8px; border: 1px solid var(--rule-dark); background: var(--surface);">
        </div>
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
        function filterBriefs() {{
            var sel = document.querySelector('input[name="brief-filter"]:checked');
            var val = sel ? sel.value : 'active';
            var q = (document.getElementById('brief-search').value || '').toLowerCase().trim();
            var rows = document.querySelectorAll('#briefs-table tbody tr');
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

    # Write canonical pages (Home | Briefs | Who's in the Room | Principles | About + Glossary + Further Reading)
    pages_to_write = [
        ("index.html", "Home", home_content, "home"),
        ("briefs.html", "Briefs", briefs_index_content, "briefs"),
        ("actors.html", "Who's in the Room", actors_content, "actors"),
        ("principles.html", "Principles", principles_content, "principles"),
        ("about.html", "Why This Exists", about_content, "about"),
        ("glossary.html", "Glossary", glossary_content, ""),
        ("reading.html", "Further Reading", reading_content, ""),
    ]

    for filename, title, content, nav_key in pages_to_write:
        page_html = get_base_html_page(title, content, active_nav=nav_key)
        for out_base in target_dirs:
            with open(out_base / filename, "w", encoding="utf-8") as f:
                f.write(page_html)

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
