"""Static site exporter and broadsheet / account-book ledger designer for Loretta's Ledger.

Information architecture:
- Nav: This Week | Matters | Who's in the Room | Archive | Method
- Matters: unified pages per matter with origin, timeline/events, current stage, likely next steps.
  Test case status is a flag & filter on Matters (not a separate tab).
- This Week: upcoming meetings and hearings with comment deadlines.
- Who's in the Room: per-actor pages (consultants, advisory bodies, funders, repeat applicants, outside groups, state/regional bodies)
  listing documented facts only with source links.
- Policy Briefs: 3 labeled sections: "For Staff", "For Residents", and "Trajectory".
- Design: broadsheet / account-book aesthetic. Cream background (#fbf8f1), near-black ink (#1c1c1c),
  muted oxblood accent (#7a2828), classical serif typography, ruled lines, ledger-style tables.
  Single source of truth in Eleventy / static build directory (docs/ and site/_site/).
"""

import argparse
import html
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pipeline.db import DEFAULT_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
SITE_SRC_DIR = ROOT_DIR / "site" / "src"
SITE_OUT_DIR = ROOT_DIR / "site" / "_site"
DOCS_DIR = ROOT_DIR / "docs"
BRIEFS_DIR = ROOT_DIR / "briefs"


def ensure_directories():
    for d in (
        SITE_SRC_DIR / "briefs",
        SITE_SRC_DIR / "matters",
        SITE_SRC_DIR / "actors",
        SITE_SRC_DIR / "archive",
        SITE_OUT_DIR / "briefs",
        SITE_OUT_DIR / "matters",
        SITE_OUT_DIR / "actors",
        SITE_OUT_DIR / "archive",
        DOCS_DIR / "briefs",
        DOCS_DIR / "matters",
        DOCS_DIR / "actors",
        DOCS_DIR / "archive",
        BRIEFS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def markdown_to_html(md_text: str) -> str:
    """Broadsheet-styled Markdown to HTML converter."""
    lines = md_text.split("\n")
    html_lines = []
    in_code = False
    in_ul = False

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

        # Unordered list
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_ul:
                html_lines.append('<ul class="ledger-list">')
                in_ul = True
            content = stripped[2:]
            content = html.escape(content)
            content = re_bold(content)
            content = re_links(content)
            html_lines.append(f"<li>{content}</li>")
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
            content = re_links(content)
            html_lines.append(f"<p>{content}</p>")

    if in_ul:
        html_lines.append("</ul>")
    if in_code:
        html_lines.append("</code></pre>")

    return "\n".join(html_lines)


def re_bold(text: str) -> str:
    return re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)


def re_links(text: str) -> str:
    return re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)


def get_base_html_page(title: str, content: str, active_nav: str = "this-week", root_prefix: str = "") -> str:
    """Account-book and broadsheet layout template."""
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

        a:hover {{
            color: #4a1715;
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
            padding: 18px 0;
        }}

        .ledger-entry:first-child {{
            padding-top: 0;
        }}

        .ledger-meta {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--ink-faint);
            margin-bottom: 6px;
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
                <span>Independent Public Account &bull; Olympia & Thurston County</span>
                <span class="meta-date">Vol. I &bull; Record Current to October 2026</span>
            </div>
            <div class="masthead-title">
                <a href="{root_prefix}index.html">Loretta's Ledger</a>
            </div>
            <div class="masthead-subtitle">
                An Account of Local Decisions, Upstream Influences, and Public Trajectory
            </div>
            <nav class="ledger-nav">
                <a href="{root_prefix}index.html" class="{'active' if active_nav == 'this-week' else ''}">This Week</a>
                <a href="{root_prefix}matters.html" class="{'active' if active_nav == 'matters' else ''}">Matters</a>
                <a href="{root_prefix}actors.html" class="{'active' if active_nav == 'actors' else ''}">Who's in the Room</a>
                <a href="{root_prefix}archive.html" class="{'active' if active_nav == 'archive' else ''}">Archive</a>
                <a href="{root_prefix}principles.html" class="{'active' if active_nav == 'principles' else ''}">Principles</a>
                <a href="{root_prefix}about.html" class="{'active' if active_nav == 'about' else ''}">About</a>
                <a href="{root_prefix}method.html" class="{'active' if active_nav == 'method' else ''}">Method</a>
            </nav>
        </div>
    </header>
    <div class="container">
        {content}
    </div>
    <footer class="broadsheet-footer">
        Loretta's Ledger &bull; Documented Public Record for Thurston County and City of Olympia &bull; Strictly Factual
    </footer>
</body>
</html>
"""


def export_site_content(conn, out_dirs: Optional[List[Path]] = None) -> Dict[str, int]:
    """Exports the entire broadsheet site structure to docs/ and site/_site/."""
    ensure_directories()
    target_dirs = out_dirs if out_dirs is not None else [DOCS_DIR, SITE_OUT_DIR]
    for d in target_dirs:
        for sub in ("briefs", "matters", "actors", "archive"):
            (d / sub).mkdir(parents=True, exist_ok=True)
    stats = {"matters": 0, "briefs": 0, "actors": 0, "test_cases": 0}
    # Count published test cases (items flagged as test cases that have approved briefs)
    published_tc_count = conn.execute(
        """
        SELECT COUNT(DISTINCT t.item_id)
        FROM test_cases t
        JOIN drafts d ON d.item_id = t.item_id
        WHERE d.kind = 'brief' AND d.reviewed = 1
        """
    ).fetchone()[0]
    stats["test_cases"] = published_tc_count

    # 1. Export Matters and Matter Pages
    matters = conn.execute(
        """
        SELECT m.*
        FROM matters m
        ORDER BY m.is_test_case DESC, m.updated_at DESC
        """
    ).fetchall()

    matters_table_rows = []
    for m in matters:
        events = conn.execute(
            """
            SELECT me.*, i.title as item_title, i.meeting_date, i.comment_deadline, i.url as item_url
            FROM matter_events me
            JOIN items i ON me.item_id = i.id
            WHERE me.matter_id = ? AND me.confidence = 'confirmed'
            ORDER BY me.action_date DESC
            """,
            (m["id"],),
        ).fetchall()

        # Check if there is an approved brief
        brief_row = conn.execute(
            """
            SELECT d.*, i.id as item_id
            FROM drafts d
            JOIN matter_events me ON d.item_id = me.item_id
            JOIN items i ON d.item_id = i.id
            WHERE me.matter_id = ? AND d.kind = 'brief' AND d.reviewed = 1
            LIMIT 1
            """,
            (m["id"],),
        ).fetchone()

        # Generate individual Matter Page
        events_timeline_html = []
        for ev in events:
            events_timeline_html.append(f"""
            <div class="timeline-item">
                <div class="ledger-meta">{ev['action_date']} &bull; {ev['action_type']}</div>
                <div><strong>{html.escape(ev['item_title'])}</strong></div>
                <div style="font-size: 13px; color: var(--ink-muted); margin-top: 3px;">
                    {html.escape(ev['notes'] or '')} &bull; <a href="{html.escape(ev['record_url'])}" target="_blank" rel="noopener">Official Meeting Record &rarr;</a>
                </div>
            </div>
            """)

        next_step_html = f"<p><strong>Likely Next Step:</strong> {html.escape(m['likely_next_step'])}</p>" if m["likely_next_step"] else "<p><strong>Likely Next Step:</strong> No subsequent action stated in public documents reviewed.</p>"
        test_case_badge = f'<span class="tag-testcase">&#9888; Test Case Precedent: {html.escape(m["test_case_reason"] or "")}</span>' if m["is_test_case"] else ''

        brief_link_html = ""
        if brief_row:
            brief_link_html = f"""
            <div class="memo-box" style="margin-top: 24px;">
                <div class="ledger-meta">Approved Publication</div>
                <h3 style="margin: 0 0 6px 0;"><a href="../briefs/brief-{brief_row['item_id']}.html">Read Full Policy Brief: {html.escape(m['title'])} &rarr;</a></h3>
                <p style="font-size: 14px; margin: 0; color: var(--ink-muted);">Includes detailed memos For Staff, For Residents, and complete policy Trajectory.</p>
            </div>
            """

        matter_page_content = f"""
        <div class="ledger-meta">
            <a href="../matters.html">&larr; Back to All Matters</a> &bull; {m['jurisdiction'].upper()} &bull; Primary Identifier: {html.escape(m['primary_identifier'] or 'None')}
        </div>
        <h1 class="headline-title" style="margin-top: 8px;">{html.escape(m['title'])}</h1>
        {f'<div style="margin-bottom: 16px;">{test_case_badge}</div>' if test_case_badge else ''}

        <div class="memo-box">
            <h3 style="margin-top:0;">Matter Summary & Origin</h3>
            <p><strong>Origin:</strong> {html.escape(m['origin_summary'] or 'Local government deliberation.')}</p>
            <p><strong>Current Stage:</strong> {html.escape(m['current_stage'] or 'Scheduled')}</p>
            {next_step_html}
        </div>

        {brief_link_html}

        <h2 class="section-label">Matter Timeline & Documented Events</h2>
        <div class="timeline-block">
            {''.join(events_timeline_html) if events_timeline_html else '<p>No linked timeline items recorded.</p>'}
        </div>
        """

        matter_html = get_base_html_page(m["title"], matter_page_content, active_nav="matters", root_prefix="../")
        file_slug = f"{m['id']}.html"

        for out_base in target_dirs:
            with open(out_base / "matters" / file_slug, "w", encoding="utf-8") as f:
                f.write(matter_html)

        # Build row for Matters Index
        tc_pill = '<span class="tag-testcase">Test Case</span>' if m["is_test_case"] else ""
        if m["is_test_case"]:
            stats["test_cases"] += 1
        brief_indicator = f'<a href="briefs/brief-{brief_row["item_id"]}.html" style="font-size:12px; margin-left:8px;">[Brief &rarr;]</a>' if brief_row else ""

        matters_table_rows.append(f"""
        <tr data-testcase="{1 if m['is_test_case'] else 0}">
            <td><a href="matters/{file_slug}"><strong>{html.escape(m['title'])}</strong></a> {brief_indicator}</td>
            <td>{html.escape(m['jurisdiction'].upper())}</td>
            <td>{tc_pill or html.escape(m['current_stage'] or 'Scheduled')}</td>
            <td>{html.escape(m['primary_identifier'] or '—')}</td>
        </tr>
        """)
        stats["matters"] += 1

    # 2. Export Reviewed Policy Briefs (One page per matter brief with 3 sections)
    briefs = conn.execute(
        """
        SELECT d.*, i.title as item_title, i.jurisdiction, i.meeting_date, i.url as item_url
        FROM drafts d
        JOIN items i ON d.item_id = i.id
        WHERE d.kind = 'brief' AND d.reviewed = 1
        ORDER BY i.meeting_date DESC
        """
    ).fetchall()

    archive_rows = []
    for b in briefs:
        html_body = markdown_to_html(b["markdown"])
        page_html = get_base_html_page(b["item_title"], html_body, active_nav="archive", root_prefix="../")

        file_slug = f"brief-{b['item_id']}.html"
        for out_base in target_dirs:
            with open(out_base / "briefs" / file_slug, "w", encoding="utf-8") as f:
                f.write(page_html)

        archive_rows.append(f"""
        <div class="ledger-entry">
            <div class="ledger-meta">{b['jurisdiction'].upper()} &bull; Meeting Date: {b['meeting_date']}</div>
            <h3 style="margin: 0 0 6px 0;"><a href="briefs/{file_slug}">{html.escape(b['item_title'])}</a></h3>
            <div style="font-size: 13px;"><a href="briefs/{file_slug}">Read Three-Section Policy Brief &rarr;</a></div>
        </div>
        """)
        stats["briefs"] += 1

    # 3. Export Who's in the Room Actor Pages (Consultants, advisory bodies, funders, repeat applicants, state/regional bodies)
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
            SELECT r.*, i.title as item_title, i.meeting_date, i.jurisdiction, i.url as item_url
            FROM upstream_refs r
            JOIN items i ON r.item_id = i.id
            JOIN drafts d ON d.item_id = r.item_id
            WHERE r.actor_id = ? AND d.reviewed = 1
            ORDER BY i.meeting_date DESC
            """,
            (act["id"],),
        ).fetchall()

        # Build individual actor page
        actor_touch_rows = []
        for r in refs:
            actor_touch_rows.append(f"""
            <tr>
                <td>{r['meeting_date']}</td>
                <td>{html.escape(r['jurisdiction'].upper())}</td>
                <td><strong>{html.escape(r['item_title'])}</strong></td>
                <td><code>{html.escape(r['mechanism'])}</code></td>
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

        <h2 class="section-label">Documented Local Matters Touched ({len(refs)})</h2>
        <p style="font-size: 13px; color: var(--ink-muted);">State documented facts only. This table records formal statutory, advisory, financial, or regulatory references in official agenda files.</p>
        
        <table class="ledger-table">
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Jurisdiction</th>
                    <th>Matter / Item</th>
                    <th>Documented Relationship</th>
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
            <div class="ledger-meta">{html.escape(act['upstream_type'].replace('_', ' ').title())} &bull; Touched {act['items_touched']} Approved Matters</div>
            <h3 style="margin: 0 0 6px 0;"><a href="actors/{act_file_slug}">{html.escape(act['name'])}</a></h3>
            <p style="font-size: 14px; margin-bottom: 6px;">{html.escape(act['notes'] or '')}</p>
            <div style="font-size: 13px;"><a href="actors/{act_file_slug}">View Documented Touch Record &rarr;</a></div>
        </div>
        """)
        stats["actors"] += 1

    # 4. Generate Main Top-Level Nav Pages:
    # A. "This Week" (Upcoming meetings, hearings, comment deadlines)
    upcoming_items = conn.execute(
        """
        SELECT i.*, t.flagged_reason as test_case_reason
        FROM items i
        LEFT JOIN test_cases t ON i.id = t.item_id
        WHERE i.meeting_date >= '2026-10-01'
        ORDER BY i.meeting_date ASC
        LIMIT 25
        """
    ).fetchall()

    upcoming_rows = []
    for u in upcoming_items:
        tc_flag = f'<span class="tag-testcase">&#9888; Test Case</span>' if u["test_case_reason"] else ''
        deadline_text = u["comment_deadline"] or "Prior to meeting"
        upcoming_rows.append(f"""
        <tr>
            <td><strong>{u['meeting_date']}</strong></td>
            <td>{u['jurisdiction'].upper()}</td>
            <td>
                {tc_flag}
                <strong>{html.escape(u['title'])}</strong>
            </td>
            <td>{html.escape(deadline_text)}</td>
            <td><a href="{html.escape(u['url'])}" target="_blank" rel="noopener">Official Agenda &rarr;</a></td>
        </tr>
        """)

    this_week_content = f"""
    <h1 class="headline-title">This Week: Public Hearings & Upcoming Decisions</h1>
    <p>A structured docket of active hearings, legislative decisions, and comment submission deadlines across Olympia and Thurston County.</p>

    <table class="ledger-table">
        <thead>
            <tr>
                <th style="width: 110px;">Date</th>
                <th style="width: 90px;">Body</th>
                <th>Subject / Docket Item</th>
                <th style="width: 180px;">Comment Cutoff</th>
                <th style="width: 140px;">Record</th>
            </tr>
        </thead>
        <tbody>
            {''.join(upcoming_rows)}
        </tbody>
    </table>

    <h2 class="section-label">Recently Approved Policy Briefs</h2>
    <div style="margin-top: 14px;">
        {''.join(archive_rows[:5]) if archive_rows else '<p>No briefs approved yet.</p>'}
    </div>
    """

    # B. "Matters" (Unified list with Test-Case filter)
    matters_content = f"""
    <div style="display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 10px; margin-bottom: 12px;">
        <h1 class="headline-title" style="margin: 0; border: none; padding: 0;">Public Matters Register</h1>
        <div style="font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em;">
            <label style="cursor: pointer;">
                <input type="checkbox" id="test-case-filter" onchange="filterTestCases(this.checked)">
                <strong>Show Test Cases Only</strong>
            </label>
        </div>
    </div>
    <p>Tracking matters across sessions from origin through final legislative trajectory. Select any matter to inspect its complete timeline and linked official records.</p>

    <table class="ledger-table" id="matters-table">
        <thead>
            <tr>
                <th>Matter Title</th>
                <th style="width: 110px;">Jurisdiction</th>
                <th style="width: 220px;">Current Stage / Status</th>
                <th style="width: 150px;">Identifier</th>
            </tr>
        </thead>
        <tbody>
            {''.join(matters_table_rows)}
        </tbody>
    </table>

    <script>
        function filterTestCases(checked) {{
            var rows = document.querySelectorAll('#matters-table tbody tr');
            rows.forEach(function(r) {{
                if (checked && r.getAttribute('data-testcase') !== '1') {{
                    r.style.display = 'none';
                }} else {{
                    r.style.display = '';
                }}
            }});
        }}
    </script>
    """

    # C. "Who's in the Room"
    actors_content = f"""
    <h1 class="headline-title">Who's in the Room</h1>
    <p>A factual account of state agencies, consultants, advisory bodies, outside groups, and funders appearing in Olympia and Thurston County policy files. Documents factual relationships and statutory roles only; never asserts motive or coordination.</p>

    <div style="margin-top: 24px;">
        {''.join(actor_cards) if actor_cards else '<p>No upstream actors linked to approved briefs yet.</p>'}
    </div>
    """

    # D. "Archive"
    archive_content = f"""
    <h1 class="headline-title">Policy Briefs Archive</h1>
    <p>Complete record of reviewed and approved policy briefs analyzed through the People-First Policy Framework.</p>

    <div style="margin-top: 20px;">
        {''.join(archive_rows) if archive_rows else '<p>No approved briefs in archive.</p>'}
    </div>
    """

    # E. "About: Why This Exists"
    about_content = f"""
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

    <h2 class="section-label">The questions we ask</h2>

    <p>Of every item, we ask the same few questions:</p>

    <ul class="ledger-list">
        <li>Is this decided at the level closest to the people affected?</li>
        <li>Does it help ordinary families own and keep their homes, land, and shops?</li>
        <li>Who pays, and who benefits?</li>
        <li>Could the people affected find out and respond in time?</li>
        <li>Does it respect the people and places already there?</li>
    </ul>

    <p>These are our questions, and we try to say plainly where a policy does well by them and where it doesn't.</p>

    <h2 class="section-label">What you'll find here</h2>

    <p>Each item gets a short brief: what is being decided, why it might matter, and what to do if you want to weigh in. City and county staff get a separate note on the open questions the documents leave unanswered. Each matter has a timeline showing where it started and every meeting it has passed through.</p>

    <h2 class="section-label">How it's made</h2>

    <p>A program collects the public agendas and documents and drafts the briefs. A person reads and approves every one before it appears here, and nothing unreviewed is published. The software and method are open: <a href="https://github.com/thecorcoran/Ledger" target="_blank" rel="noopener">https://github.com/thecorcoran/Ledger</a>.</p>

    <h2 class="section-label">Disclosures</h2>

    <p>The Ledger is written by Jon, who lives in Thurston County in the house his mother grew up in. He is not a lawyer, a planner, or a reporter, and he does not own a business. He has no financial ties to the matters the Ledger covers.</p>

    <h2 class="section-label">Mistakes and corrections</h2>

    <p>If something here is wrong, tell us at <a href="mailto:thecorcoran@gmail.com">thecorcoran@gmail.com</a>. We'll fix it and note the change on the page.</p>
    """

    # F. "Method"
    method_content = f"""
    <h1 class="headline-title">Editorial Method & Standard of Evidence</h1>
    <div class="memo-box">
        <p><strong>The People-First Standard:</strong> Loretta's Ledger provides independent scrutiny of local government actions in Olympia and Thurston County through the lens of local ownership, subsidiarity, and skepticism of concentrated power.</p>
        <p><strong>Strict Evidentiary Rules:</strong></p>
        <ul class="ledger-list">
            <li><strong>Factual Source Links:</strong> Every factual statement links directly to an official meeting agenda, staff report, or statute.</li>
            <li><strong>Upstream Lineage:</strong> Outside actors, model policies, and state statutes are recorded solely when cited in official records.</li>
            <li><strong>Zero Motive Assertion:</strong> We state documented relationships and statutory gates. We never assert coordination or intent.</li>
            <li><strong>Human Review:</strong> No automated draft publishes without human verification in the review dashboard.</li>
        </ul>
    </div>
    """

    # G. "Principles: Plain-Language Guide"
    principles_content = f"""
    <h1 class="headline-title">Our Principles</h1>
    <p>Loretta's Ledger evaluates every local policy proposal through eight core principles grounded in plain language and everyday household life. These principles help us understand who decides, who pays, and who benefits.</p>

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
    """

    # Write the canonical pages to both docs/ and site/_site/
    pages_to_write = [
        ("index.html", "This Week", this_week_content, "this-week"),
        ("matters.html", "Matters Register", matters_content, "matters"),
        ("actors.html", "Who's in the Room", actors_content, "actors"),
        ("archive.html", "Archive", archive_content, "archive"),
        ("principles.html", "Principles", principles_content, "principles"),
        ("about.html", "Why This Exists", about_content, "about"),
        ("method.html", "Method", method_content, "method"),
    ]

    for filename, title, content, nav_key in pages_to_write:
        page_html = get_base_html_page(title, content, active_nav=nav_key)
        for out_base in target_dirs:
            with open(out_base / filename, "w", encoding="utf-8") as f:
                f.write(page_html)

    # Ensure .nojekyll is present in all target directories
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
            print(f"  Matters created: {stats['matters']}")
            print(f"  Briefs written:  {stats['briefs']}")
            print(f"  Actors indexed:  {stats['actors']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

