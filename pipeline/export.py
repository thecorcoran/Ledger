"""Static site exporter and PDF brief generator for Loretta's Ledger."""

import argparse
import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.upstream.chains import get_item_lineage, format_lineage_display

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
SITE_SRC_DIR = ROOT_DIR / "site" / "src"
SITE_OUT_DIR = ROOT_DIR / "site" / "_site"
DOCS_DIR = ROOT_DIR / "docs"
BRIEFS_DIR = ROOT_DIR / "briefs"


def ensure_directories():
    for d in (
        SITE_SRC_DIR / "briefs",
        SITE_SRC_DIR / "action-pages",
        SITE_SRC_DIR / "test-cases",
        SITE_SRC_DIR / "influences",
        SITE_OUT_DIR / "briefs",
        SITE_OUT_DIR / "action-pages",
        SITE_OUT_DIR / "test-cases",
        SITE_OUT_DIR / "influences",
        DOCS_DIR / "briefs",
        DOCS_DIR / "action-pages",
        DOCS_DIR / "test-cases",
        DOCS_DIR / "influences",
        BRIEFS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def markdown_to_html(md_text: str) -> str:
    """Lightweight converter for markdown headings, lists, bold text, links, and code blocks."""
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
                html_lines.append("<ul>")
                in_ul = True
            content = stripped[2:]
            content = html.escape(content)
            # Bold
            content = re_bold(content)
            # Links
            content = re_links(content)
            html_lines.append(f"<li>{content}</li>")
            continue
        else:
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False

        if not stripped:
            html_lines.append("<br>")
            continue

        # Headings
        if stripped.startswith("# "):
            html_lines.append(f"<h1>{html.escape(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            html_lines.append(f"<h2>{html.escape(stripped[3:])}</h2>")
        elif stripped.startswith("### "):
            html_lines.append(f"<h3>{html.escape(stripped[4:])}</h3>")
        elif stripped.startswith("> "):
            html_lines.append(f"<blockquote>{html.escape(stripped[2:])}</blockquote>")
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
    import re
    return re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)


def re_links(text: str) -> str:
    import re
    return re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2" target="_blank">\1</a>', text)


def get_base_html_page(title: str, content: str, active_nav: str = "home", root_prefix: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(title)} — Loretta's Ledger</title>
    <style>
        :root {{
            --bg: #0b1120;
            --surface: #1e293b;
            --surface-hover: #24344d;
            --border: #334155;
            --text: #f8fafc;
            --muted: #94a3b8;
            --accent: #38bdf8;
            --accent-glow: rgba(56, 189, 248, 0.15);
            --gold: #f59e0b;
            --green: #10b981;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 0;
            line-height: 1.6;
        }}
        header {{
            background: rgba(15, 23, 42, 0.95);
            border-bottom: 1px solid var(--border);
            padding: 18px 24px;
            position: sticky;
            top: 0;
            z-index: 100;
            backdrop-filter: blur(8px);
        }}
        .header-inner {{
            max-width: 1100px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .logo {{
            font-size: 20px;
            font-weight: 700;
            color: #fff;
            text-decoration: none;
            letter-spacing: -0.02em;
        }}
        .logo span {{ color: var(--accent); }}
        nav a {{
            color: var(--muted);
            text-decoration: none;
            font-size: 14px;
            margin-left: 20px;
            font-weight: 500;
            transition: color 0.15s ease;
        }}
        nav a:hover, nav a.active {{ color: var(--accent); }}
        .container {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 32px 20px 60px 20px;
        }}
        .hero {{
            border-bottom: 1px solid var(--border);
            padding-bottom: 28px;
            margin-bottom: 32px;
        }}
        .hero h1 {{
            font-size: 32px;
            margin: 0 0 10px 0;
            font-weight: 800;
            line-height: 1.2;
        }}
        .hero p {{
            font-size: 16px;
            color: var(--muted);
            margin: 0;
            max-width: 800px;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-jur {{ background: var(--accent-glow); color: var(--accent); }}
        .badge-testcase {{ background: rgba(245, 158, 11, 0.15); color: var(--gold); border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge-topic {{ background: #1e3a5f; color: #7dd3fc; margin-right: 4px; }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            transition: transform 0.15s ease, border-color 0.15s ease;
        }}
        .card:hover {{ border-color: #475569; }}
        .card-title {{
            font-size: 18px;
            font-weight: 700;
            margin: 0 0 8px 0;
        }}
        .card-title a {{
            color: #fff;
            text-decoration: none;
        }}
        .card-title a:hover {{ color: var(--accent); }}
        .card-meta {{
            font-size: 13px;
            color: var(--muted);
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .btn {{
            display: inline-block;
            background: var(--accent);
            color: #0b1120;
            font-weight: 600;
            font-size: 13px;
            padding: 8px 14px;
            border-radius: 6px;
            text-decoration: none;
            margin-top: 10px;
        }}
        .btn:hover {{ opacity: 0.9; }}
        pre {{
            background: #060913;
            border: 1px solid var(--border);
            padding: 16px;
            border-radius: 6px;
            overflow-x: auto;
            color: #cbd5e1;
            font-size: 13px;
        }}
        blockquote {{
            border-left: 3px solid var(--accent);
            margin: 16px 0;
            padding-left: 16px;
            color: #cbd5e1;
            font-style: italic;
        }}
        footer {{
            border-top: 1px solid var(--border);
            padding: 24px;
            text-align: center;
            color: var(--muted);
            font-size: 13px;
        }}
        @media print {{
            body {{ background: #fff; color: #000; }}
            header, footer, nav, .btn {{ display: none; }}
            .container {{ max-width: 100%; padding: 0; }}
            .card {{ border: none; padding: 0; }}
        }}
    </style>
</head>
<body>
    <header>
        <div class="header-inner">
            <a href="{root_prefix}index.html" class="logo">Loretta's <span>Ledger</span></a>
            <nav>
                <a href="{root_prefix}index.html" class="{'active' if active_nav == 'home' else ''}">Home</a>
                <a href="{root_prefix}briefs.html" class="{'active' if active_nav == 'briefs' else ''}">Policy Briefs</a>
                <a href="{root_prefix}action.html" class="{'active' if active_nav == 'action' else ''}">Citizen Action</a>
                <a href="{root_prefix}test-cases.html" class="{'active' if active_nav == 'test-cases' else ''}">Test Cases</a>
                <a href="{root_prefix}influences.html" class="{'active' if active_nav == 'influences' else ''}">Who's Behind This</a>
            </nav>
        </div>
    </header>
    <div class="container">
        {content}
    </div>
    <footer>
        Loretta's Ledger &bull; Independent local policy publication for Olympia and Thurston County &bull; People-First Policy Framework
    </footer>
</body>
</html>
"""


def export_site_content(conn, out_dirs: Optional[List[Path]] = None) -> Dict[str, int]:
    """Exports reviewed drafts, test cases, and upstream dossier pages."""
    target_dirs = out_dirs if out_dirs is not None else [SITE_OUT_DIR, DOCS_DIR]
    for d in target_dirs:
        for sub in ("briefs", "action-pages", "test-cases", "influences"):
            (d / sub).mkdir(parents=True, exist_ok=True)
    stats = {"briefs": 0, "actions": 0, "test_cases": 0, "influences": 0}

    # 1. Export Reviewed Briefs
    briefs = conn.execute(
        """
        SELECT d.*, i.title as item_title, i.jurisdiction, i.meeting_date, i.url as item_url
        FROM drafts d
        JOIN items i ON d.item_id = i.id
        WHERE d.kind = 'brief' AND d.reviewed = 1
        ORDER BY i.meeting_date DESC
        """
    ).fetchall()

    brief_cards = []
    for b in briefs:
        html_body = markdown_to_html(b["markdown"])
        page_html = get_base_html_page(b["item_title"], html_body, active_nav="briefs", root_prefix="../")

        file_slug = f"brief-{b['item_id']}.html"
        for out_base in target_dirs:
            with open(out_base / "briefs" / file_slug, "w", encoding="utf-8") as f:
                f.write(page_html)

        brief_cards.append(f"""
        <div class="card">
            <div class="card-meta">
                <span class="badge badge-jur">{b['jurisdiction'].upper()}</span>
                <span>Meeting: {b['meeting_date']}</span>
            </div>
            <h2 class="card-title"><a href="briefs/{file_slug}">{html.escape(b['item_title'])}</a></h2>
            <a href="briefs/{file_slug}" class="btn">Read Policy Brief &rarr;</a>
        </div>
        """)
        stats["briefs"] += 1

    # 2. Export Reviewed Action Pages
    actions = conn.execute(
        """
        SELECT d.*, i.title as item_title, i.jurisdiction, i.meeting_date, i.comment_deadline, i.url as item_url
        FROM drafts d
        JOIN items i ON d.item_id = i.id
        WHERE d.kind = 'action_page' AND d.reviewed = 1
        ORDER BY i.meeting_date DESC
        """
    ).fetchall()

    action_cards = []
    for a in actions:
        html_body = markdown_to_html(a["markdown"])
        page_html = get_base_html_page(a["item_title"], html_body, active_nav="action", root_prefix="../")

        file_slug = f"action-{a['item_id']}.html"
        for out_base in target_dirs:
            with open(out_base / "action-pages" / file_slug, "w", encoding="utf-8") as f:
                f.write(page_html)

        action_cards.append(f"""
        <div class="card">
            <div class="card-meta">
                <span class="badge badge-jur">{a['jurisdiction'].upper()}</span>
                <span>Deadline: {a['comment_deadline'] or a['meeting_date']}</span>
            </div>
            <h2 class="card-title"><a href="action-pages/{file_slug}">{html.escape(a['item_title'])}</a></h2>
            <a href="action-pages/{file_slug}" class="btn">View Action Page &rarr;</a>
        </div>
        """)
        stats["actions"] += 1

    # 3. Export Test Cases (Only for items that have an approved/reviewed brief)
    test_cases = conn.execute(
        """
        SELECT t.*, i.title as item_title, i.jurisdiction, i.meeting_date, i.url as item_url, i.body_text
        FROM test_cases t
        JOIN items i ON t.item_id = i.id
        JOIN drafts d ON d.item_id = i.id
        WHERE d.kind = 'brief' AND d.reviewed = 1
        ORDER BY i.meeting_date DESC
        """
    ).fetchall()

    tc_cards = []
    for tc in test_cases:
        tc_cards.append(f"""
        <div class="card">
            <div class="card-meta">
                <span class="badge badge-jur">{tc['jurisdiction'].upper()}</span>
                <span class="badge badge-testcase">&#9888; Status: {tc['status'].upper()}</span>
                <span>Date: {tc['meeting_date']}</span>
            </div>
            <h2 class="card-title">{html.escape(tc['item_title'])}</h2>
            <p><strong>Precedent Reason:</strong> {html.escape(tc['flagged_reason'])}</p>
            <p><small>{html.escape(tc['notes'] or '')}</small></p>
            <a href="{html.escape(tc['item_url'])}" target="_blank" class="btn">Official Packet &rarr;</a>
        </div>
        """)
        stats["test_cases"] += 1

    # 4. Export Upstream Influence Dossier Pages (Only linking items that have approved/reviewed drafts)
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

    inf_cards = []
    for act in actors:
        refs = conn.execute(
            """
            SELECT r.*, i.title as item_title, i.meeting_date, i.jurisdiction
            FROM upstream_refs r
            JOIN items i ON r.item_id = i.id
            JOIN drafts d ON d.item_id = r.item_id
            WHERE r.actor_id = ? AND d.reviewed = 1
            ORDER BY i.meeting_date DESC
            """,
            (act["id"],),
        ).fetchall()

        ref_items_html = ""
        if refs:
            ref_items_html = "<h4>Touched Local Items:</h4><ul>" + "".join([
                f"<li><strong>{r['meeting_date']}</strong> ({r['jurisdiction'].upper()}): {html.escape(r['item_title'])} <br><em>Mechanism: {r['mechanism']}</em></li>"
                for r in refs
            ]) + "</ul>"

        inf_cards.append(f"""
        <div class="card">
            <div class="card-meta">
                <span class="badge badge-jur">{html.escape(act['upstream_type'].upper())}</span>
                <span>Local Items Touched: {act['items_touched']}</span>
            </div>
            <h2 class="card-title">{html.escape(act['name'])}</h2>
            <p>{html.escape(act['notes'] or '')}</p>
            {ref_items_html}
            {f'<p><a href="{act["home_url"]}" target="_blank" class="btn">Official Site &rarr;</a></p>' if act["home_url"] else ''}
        </div>
        """)
        stats["influences"] += 1

    # Generate Index, Briefs, Action, Test Cases, and Influences Pages
    index_content = f"""
    <div class="hero">
        <h1>Loretta's Ledger</h1>
        <p>A local policy publication providing consistent, evidence-based policy briefs for <strong>Olympia</strong> and <strong>Thurston County</strong>, analyzed through a people-first policy lens (local ownership, self-determination, affordability, and skepticism of concentrated power).</p>
    </div>

    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 40px;">
        <div class="card">
            <h3 style="margin-top:0;">&#128269; Precedent Watch</h3>
            <p>Active test cases setting land use, water, and property precedents in our region.</p>
            <p><strong>{len(test_cases)} Active Watch Items</strong></p>
            <a href="test-cases.html" class="btn">View Test Cases &rarr;</a>
        </div>
        <div class="card">
            <h3 style="margin-top:0;">&#128392; Who's Behind This?</h3>
            <p>Tracking state statutes, agency mandates, and funding strings shaping local decisions.</p>
            <p><strong>{len(actors)} Upstream Bodies Monitored</strong></p>
            <a href="influences.html" class="btn">View Influence Tracker &rarr;</a>
        </div>
        <div class="card">
            <h3 style="margin-top:0;">&#128227; Citizen Action</h3>
            <p>Plain-language deadlines and instructions on how to submit public comments before decisions pass.</p>
            <a href="action.html" class="btn">View Action Pages &rarr;</a>
        </div>
    </div>

    <h2>Published Policy Briefs ({len(briefs)})</h2>
    {''.join(brief_cards) if brief_cards else '<p>No reviewed policy briefs published yet. Run <code>python3 -m pipeline.review --approve &lt;id&gt;</code> to approve drafts.</p>'}
    """

    briefs_content = f"""
    <div class="hero">
        <h1>Policy Briefs Archive</h1>
        <p>In-depth, 8-question Litmus Test evaluations of Olympia and Thurston County decisions.</p>
    </div>
    {''.join(brief_cards) if brief_cards else '<p>No reviewed policy briefs published yet.</p>'}
    """

    action_content = f"""
    <div class="hero">
        <h1>Citizen Action Pages</h1>
        <p>What is happening, when and where it will be decided, and how to have your say before hearings conclude.</p>
    </div>
    {''.join(action_cards) if action_cards else '<p>No action pages published yet.</p>'}
    """

    tc_content = f"""
    <div class="hero">
        <h1>Test-Case Watch</h1>
        <p>Precedent-setting land use, well, water, and regulatory decisions affecting property owners and local independence.</p>
    </div>
    {''.join(tc_cards)}
    """

    inf_content = f"""
    <div class="hero">
        <h1>Who's Behind This? (Upstream Influence Tracker)</h1>
        <p>Documented multi-step lineage showing where local Olympia and Thurston County policies originate.</p>
    </div>
    {''.join(inf_cards)}
    """

    all_out_bases = list(target_dirs)
    if ROOT_DIR not in all_out_bases and out_dirs is None:
        all_out_bases.append(ROOT_DIR)

    for out_base in all_out_bases:
        with open(out_base / "index.html", "w", encoding="utf-8") as f:
            f.write(get_base_html_page("Home", index_content, active_nav="home"))
        with open(out_base / "briefs.html", "w", encoding="utf-8") as f:
            f.write(get_base_html_page("Policy Briefs Archive", briefs_content, active_nav="briefs"))
        with open(out_base / "action.html", "w", encoding="utf-8") as f:
            f.write(get_base_html_page("Citizen Action Pages", action_content, active_nav="action"))
        with open(out_base / "test-cases.html", "w", encoding="utf-8") as f:
            f.write(get_base_html_page("Test-Case Watch", tc_content, active_nav="test-cases"))
        with open(out_base / "influences.html", "w", encoding="utf-8") as f:
            f.write(get_base_html_page("Who's Behind This", inf_content, active_nav="influences"))

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
    parser = argparse.ArgumentParser(description="Export Loretta's Ledger site and PDF briefs")
    parser.add_argument("--all", action="store_true", help="Export complete static site to site/_site and docs/")
    parser.add_argument("--pdf", type=int, default=None, help="Export a specific approved brief to print/PDF HTML")

    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.pdf:
            out = export_pdf_html(conn, args.pdf)
            print(f"Exported printable brief to: {out}")
        else:
            stats = export_site_content(conn)
            print(f"Site export complete:")
            print(f"  Briefs published:     {stats['briefs']}")
            print(f"  Action pages:         {stats['actions']}")
            print(f"  Test cases:           {stats['test_cases']}")
            print(f"  Influence dossiers:   {stats['influences']}")
            print(f"Site written to: docs/ and site/_site/")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
