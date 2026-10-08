"""Back-end Control Center and web dashboard for Loretta's Ledger.

Simplified single-page review dashboard:
1. "Needs your review": list of unreviewed drafts (title, jurisdiction, date, test-case flag).
2. The draft view: brief text, "Who's behind this?" with evidence links, litmus ratings, source link.
   Three buttons: Approve, Reject, Save edits.
3. "Published": list of approved items with links to public pages.
"""

import argparse
import html
import http.server
import re
import socketserver
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.review import approve_draft, reject_draft
from pipeline.export import export_site_content, markdown_to_html


def list_items(db_path: Optional[Path] = None, limit: int = 25, offset: int = 0):
    conn = get_db_connection(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        rows = conn.execute(
            """
            SELECT i.id, i.jurisdiction, i.title, i.meeting_date, i.comment_deadline, i.url,
                   t.flagged_reason as test_case_reason
            FROM items i
            LEFT JOIN test_cases t ON i.id = t.item_id
            ORDER BY i.meeting_date DESC, i.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

        print(f"\nLoretta's Ledger — Ingested Items (Total: {total}, Showing: {len(rows)})")
        print("=" * 95)
        for r in rows:
            t_rows = conn.execute("SELECT topic FROM item_topics WHERE item_id = ?", (r["id"],)).fetchall()
            topics_str = ", ".join([tr["topic"] for tr in t_rows]) if t_rows else "uncategorized"

            print(f"[{r['meeting_date'] or 'No Date'}] ({r['jurisdiction'].upper()}) [{topics_str}] {r['id']}")
            print(f"  Title: {r['title']}")
            if r["comment_deadline"]:
                print(f"  Deadline: {r['comment_deadline']}")
            if r["test_case_reason"]:
                print(f"  * TEST CASE WATCH: {r['test_case_reason']}")
            print(f"  URL:   {r['url']}\n")
    finally:
        conn.close()


def show_item(item_id: str, db_path: Optional[Path] = None):
    conn = get_db_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            print(f"Item not found: {item_id}")
            return

        topics = [r["topic"] for r in conn.execute("SELECT topic FROM item_topics WHERE item_id = ?", (item_id,)).fetchall()]
        test_case = conn.execute("SELECT * FROM test_cases WHERE item_id = ?", (item_id,)).fetchone()
        refs = conn.execute(
            """
            SELECT r.*, a.name as actor_name, a.upstream_type
            FROM upstream_refs r
            JOIN upstream_actors a ON r.actor_id = a.id
            WHERE r.item_id = ?
            """,
            (item_id,),
        ).fetchall()

        print("\n" + "=" * 95)
        print(f"ITEM: {row['id']}")
        print("=" * 95)
        print(f"Jurisdiction: {row['jurisdiction']}")
        print(f"Title:        {row['title']}")
        print(f"Topics:       {', '.join(topics) if topics else 'None'}")
        print(f"Meeting Date: {row['meeting_date']}")
        print(f"Deadline:     {row['comment_deadline'] or 'None'}")
        if test_case:
            print(f"Test Case:    YES - {test_case['flagged_reason']} (Status: {test_case['status']})")
        if refs:
            print("\nWHO'S BEHIND THIS (Upstream Influence):")
            for rf in refs:
                print(f"  - Actor:     {rf['actor_name']} ({rf['upstream_type']})")
                print(f"    Mechanism: {rf['mechanism']} [Confidence: {rf['confidence']}]")
                print(f"    Evidence:  \"{rf['evidence_ref']}\"")
        print(f"\nURL:          {row['url']}")
        print(f"Published:    {row['published_at']}")
        print(f"Status:       {row['status']}")
        print(f"Hash:         {row['hash']}")
        print("\nBODY TEXT:")
        print("-" * 95)
        print(row["body_text"] or "(None)")
        print("=" * 95 + "\n")
    finally:
        conn.close()


def generate_html_dashboard(
    db_path: Optional[Path] = None,
    selected_draft_id: Optional[int] = None,
    approved_draft_id: Optional[int] = None,
) -> str:
    """Generates the simplified, single-page editorial dashboard."""
    conn = get_db_connection(db_path)
    try:
        # 0. Check approved item notice if present
        approved_item = None
        if approved_draft_id:
            approved_item = conn.execute(
                """
                SELECT d.id, d.item_id, d.kind, i.title as item_title
                FROM drafts d
                JOIN items i ON d.item_id = i.id
                WHERE d.id = ?
                """,
                (approved_draft_id,),
            ).fetchone()

        # 1. Unreviewed drafts query (Policy briefs only, triaged by priority: test cases, upstream refs, then meeting date)
        unreviewed = conn.execute(
            """
            SELECT d.id, d.item_id, d.kind, d.markdown, d.reviewed,
                   i.title as item_title, i.jurisdiction, i.meeting_date, i.url as item_url,
                   t.flagged_reason as test_case_reason,
                   (CASE WHEN t.item_id IS NOT NULL THEN 1 WHEN r.item_id IS NOT NULL THEN 2 ELSE 3 END) as priority_rank
            FROM drafts d
            JOIN items i ON d.item_id = i.id
            LEFT JOIN test_cases t ON i.id = t.item_id
            LEFT JOIN upstream_refs r ON i.id = r.item_id
            WHERE d.reviewed = 0 AND d.kind = 'brief'
            GROUP BY d.id
            ORDER BY priority_rank ASC, i.meeting_date DESC, d.id ASC
            """
        ).fetchall()

        # 2. Published drafts query (Policy briefs only to prevent duplicate cards)
        published = conn.execute(
            """
            SELECT d.id, d.item_id, d.kind, d.reviewed, d.reviewed_at,
                   i.title as item_title, i.jurisdiction, i.meeting_date, i.url as item_url
            FROM drafts d
            JOIN items i ON d.item_id = i.id
            WHERE d.reviewed = 1 AND d.kind = 'brief'
            ORDER BY d.reviewed_at DESC, d.id DESC
            """
        ).fetchall()

        waiting_count = len(unreviewed)

        # Determine which draft is actively open for review
        active_draft = None
        if selected_draft_id:
            for d in unreviewed:
                if d["id"] == selected_draft_id:
                    active_draft = d
                    break
            # If not in unreviewed, check all drafts
            if not active_draft:
                active_draft = conn.execute(
                    """
                    SELECT d.id, d.item_id, d.kind, d.markdown, d.reviewed,
                           i.title as item_title, i.jurisdiction, i.meeting_date, i.url as item_url,
                           t.flagged_reason as test_case_reason
                    FROM drafts d
                    JOIN items i ON d.item_id = i.id
                    LEFT JOIN test_cases t ON i.id = t.item_id
                    WHERE d.id = ?
                    """,
                    (selected_draft_id,),
                ).fetchone()
        elif unreviewed:
            active_draft = unreviewed[0]

        # Gather upstream evidence refs for the active draft if selected
        active_refs = []
        if active_draft:
            active_refs = conn.execute(
                """
                SELECT r.*, a.name as actor_name, a.upstream_type, a.home_url
                FROM upstream_refs r
                JOIN upstream_actors a ON r.actor_id = a.id
                WHERE r.item_id = ?
                """,
                (active_draft["item_id"],),
            ).fetchall()

        # Query proposed matter links waiting for confirmation
        proposed_events = conn.execute(
            """
            SELECT me.id as event_id, me.matter_id, me.action_date, me.notes,
                   m.title as matter_title, m.jurisdiction,
                   i.id as item_id, i.title as item_title, i.url as item_url
            FROM matter_events me
            JOIN matters m ON me.matter_id = m.id
            JOIN items i ON me.item_id = i.id
            WHERE me.confidence = 'proposed'
            ORDER BY me.action_date DESC
            LIMIT 15
            """
        ).fetchall()

        # Pattern alerts: outside groups, consultants, or state bodies appearing across 3+ items
        pattern_alerts = conn.execute(
            """
            SELECT a.name as actor_name, a.upstream_type, COUNT(DISTINCT r.item_id) as touch_count
            FROM upstream_actors a
            JOIN upstream_refs r ON a.id = r.actor_id
            GROUP BY a.id
            HAVING touch_count >= 2
            ORDER BY touch_count DESC
            """
        ).fetchall()
    finally:
        conn.close()

    # Section 1: "Needs your review" list
    needs_review_cards = []
    for d in unreviewed:
        is_selected = active_draft and d["id"] == active_draft["id"]
        jur_name = "Olympia" if d["jurisdiction"].lower() == "olympia" else "Thurston County"
        tc_flag = ""
        if d["test_case_reason"]:
            tc_flag = f'<span class="badge badge-testcase">&#9888; Test case: {html.escape(d["test_case_reason"])}</span>'

        # Extract headline and routine status from markdown
        md = d["markdown"] or ""
        m_hl = re.search(r"### Headline\s*\n+([^\n#]+)", md)
        headline_text = m_hl.group(1).strip() if m_hl else ""
        is_routine = "**Routine**" in md or "### Why It Matters\n**Routine**" in md or "### Why It Matters\nRoutine:" in md

        if is_routine:
            status_badge = '<span class="badge badge-routine" style="background:#334155; color:#94a3b8; border:1px solid #475569;">Routine</span>'
        else:
            status_badge = '<span class="badge badge-engaged" style="background:rgba(56, 189, 248, 0.15); color:#38bdf8; border:1px solid rgba(56, 189, 248, 0.3);">Policy Focus</span>'

        headline_html = f'<div class="draft-row-headline" style="font-size:13px; color:#cbd5e1; margin-top:6px; line-height:1.4;">{html.escape(headline_text)}</div>' if headline_text else ''

        needs_review_cards.append(f"""
        <div class="draft-row {'active-row' if is_selected else ''}">
            <div class="draft-row-meta">
                <span class="badge badge-jur">{html.escape(jur_name)}</span>
                {status_badge}
                <span class="date-text">{html.escape(str(d['meeting_date'] or 'Upcoming'))}</span>
                {tc_flag}
            </div>
            <div class="draft-row-title">
                <a href="/?draft_id={d['id']}#draft-view">{html.escape(d['item_title'])}</a>
            </div>
            {headline_html}
        </div>
        """)

    # Section 2: Active Draft View
    draft_view_html = ""
    if active_draft:
        jur_label = "Olympia" if active_draft["jurisdiction"].lower() == "olympia" else "Thurston County"
        tc_notice = ""
        if active_draft["test_case_reason"]:
            tc_notice = f"""
            <div class="testcase-alert">
                <strong>&#9888; Precedent-Setting Test Case:</strong> {html.escape(active_draft['test_case_reason'])}
            </div>
            """

        # Format "Who's behind this?" section
        who_behind_html = ""
        if active_refs:
            ref_items = []
            for r in active_refs:
                link_html = f'<a href="{html.escape(r["evidence_url"])}" target="_blank" class="evidence-link">Evidence Source &rarr;</a>' if r["evidence_url"] else ""
                ref_items.append(f"""
                <div class="influence-item">
                    <div class="influence-title">
                        <strong>{html.escape(r['actor_name'])}</strong> 
                        <span class="badge badge-mech">{html.escape(r['mechanism'])}</span>
                    </div>
                    <div class="influence-evidence">&ldquo;{html.escape(r['evidence_ref'])}&rdquo;</div>
                    {link_html}
                </div>
                """)
            who_behind_html = "".join(ref_items)
        else:
            who_behind_html = "<p class='muted-text'>No upstream influences identified in official documents for this item.</p>"

        # Render preview from markdown
        rendered_draft_body = markdown_to_html(active_draft["markdown"])

        draft_view_html = f"""
        <div id="draft-view" class="active-draft-box">
            <div class="draft-header">
                <div class="draft-meta">
                    <span class="badge badge-jur">{html.escape(jur_label)}</span>
                    <span class="date-text">Meeting: {html.escape(str(active_draft['meeting_date'] or 'Upcoming'))}</span>
                    <a href="{html.escape(active_draft['item_url'])}" target="_blank" class="source-link">View Official Source &rarr;</a>
                </div>
                <h2>{html.escape(active_draft['item_title'])}</h2>
                {tc_notice}
            </div>

            <!-- Action Buttons Bar -->
            <div class="button-bar">
                <form method="POST" action="/action/approve" style="display:inline;">
                    <input type="hidden" name="draft_id" value="{active_draft['id']}">
                    <button type="submit" class="btn btn-green">&#10003; Approve</button>
                </form>

                <form method="POST" action="/action/reject" style="display:inline;" onsubmit="return confirm('Reject and discard this draft?');">
                    <input type="hidden" name="draft_id" value="{active_draft['id']}">
                    <button type="submit" class="btn btn-red">&#10005; Reject</button>
                </form>

                <button type="button" class="btn btn-blue" onclick="toggleEditMode();">&#9998; Edit Draft Text</button>
            </div>

            <!-- Editable Form (Hidden by default, shown when Edit is clicked) -->
            <div id="edit-panel" style="display: none; margin-top: 16px;">
                <form method="POST" action="/action/save-edit">
                    <input type="hidden" name="draft_id" value="{active_draft['id']}">
                    <div style="margin-bottom: 8px;">
                        <label for="markdown-editor" style="font-weight:600; font-size:13px; color:#cbd5e1;">Edit draft text:</label>
                    </div>
                    <textarea id="markdown-editor" name="markdown" rows="18" class="text-editor">{html.escape(active_draft['markdown'])}</textarea>
                    <div style="margin-top: 10px;">
                        <button type="submit" class="btn btn-green">&#128190; Save edits</button>
                        <button type="button" class="btn btn-secondary" onclick="toggleEditMode();">Cancel</button>
                    </div>
                </form>
            </div>

            <!-- Who's Behind This Section -->
            <div class="section-card">
                <h3>Who's behind this?</h3>
                {who_behind_html}
            </div>

            <!-- Draft Content & Litmus Ratings Preview -->
            <div class="section-card">
                <h3>Draft Content & Litmus Ratings</h3>
                <div class="draft-rendered">
                    {rendered_draft_body}
                </div>
            </div>
        </div>
        """
    else:
        draft_view_html = """
        <div class="empty-state">
            <p>No drafts are currently open for review.</p>
        </div>
        """

    # Section 3: "Published" list
    published_items_html = []
    for p in published:
        jur_title = "Olympia" if p["jurisdiction"].lower() == "olympia" else "Thurston County"
        public_url = f"/docs/briefs/brief-{p['item_id']}.html" if p["kind"] == "brief" else f"/docs/action-pages/action-{p['item_id']}.html"

        published_items_html.append(f"""
        <div class="published-row">
            <div class="published-row-meta">
                <span class="badge badge-jur">{html.escape(jur_title)}</span>
                <span class="date-text">Published {html.escape(str(p['reviewed_at'] or ''))[:10]}</span>
            </div>
            <div class="published-row-title" style="flex: 1; margin: 0 16px;">
                <a href="{html.escape(public_url)}" target="_blank">{html.escape(p['item_title'])}</a>
            </div>
            <div>
                <a href="{html.escape(public_url)}" target="_blank" class="btn btn-blue" style="font-size: 12px; padding: 4px 12px; text-decoration: none; white-space: nowrap;">View Public Page &rarr;</a>
            </div>
        </div>
        """)

    published_section_html = "".join(published_items_html) if published_items_html else "<p class='muted-text'>No items have been published yet.</p>"

    # Proposed Matter Links review cards
    proposed_links_html = []
    for pe in proposed_events:
        jur_lbl = "Olympia" if pe["jurisdiction"].lower() == "olympia" else "Thurston County"
        proposed_links_html.append(f"""
        <div class="draft-row" style="display: flex; justify-content: space-between; align-items: center; gap: 16px;">
            <div style="flex: 1;">
                <div class="draft-row-meta">
                    <span class="badge badge-jur">{html.escape(jur_lbl)}</span>
                    <span class="date-text">{html.escape(pe['action_date'])}</span>
                    <span class="badge" style="background: rgba(245, 158, 11, 0.15); color: #fbbf24;">Uncertain Link</span>
                </div>
                <div style="font-size: 14px; font-weight: 600; color: #fff; margin: 4px 0;">
                    Item: {html.escape(pe['item_title'])}
                </div>
                <div style="font-size: 13px; color: #94a3b8;">
                    &rarr; Proposed to Matter: <strong>{html.escape(pe['matter_title'])}</strong>
                </div>
                <div style="font-size: 12px; color: #64748b; margin-top: 4px;">
                    {html.escape(pe['notes'] or '')} &bull; <a href="{html.escape(pe['item_url'])}" target="_blank" style="color: var(--accent);">Official Record &rarr;</a>
                </div>
            </div>
            <div style="display: flex; gap: 8px;">
                <form method="POST" action="/action/confirm-matter-link">
                    <input type="hidden" name="event_id" value="{html.escape(pe['event_id'])}">
                    <button type="submit" class="btn btn-green" style="font-size: 12px; padding: 6px 12px;">Confirm Link</button>
                </form>
                <form method="POST" action="/action/reject-matter-link">
                    <input type="hidden" name="event_id" value="{html.escape(pe['event_id'])}">
                    <button type="submit" class="btn btn-red" style="font-size: 12px; padding: 6px 12px;">Separate</button>
                </form>
            </div>
        </div>
        """)
    proposed_section_html = "".join(proposed_links_html) if proposed_links_html else "<p class='muted-text'>No uncertain matter links pending confirmation.</p>"

    # Pattern Alerts HTML
    patterns_html = []
    for pa in pattern_alerts:
        patterns_html.append(f"""
        <div style="padding: 10px 14px; background: #0f172a; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <strong style="color: #fff;">{html.escape(pa['actor_name'])}</strong>
                <span class="badge badge-jur" style="margin-left: 8px;">{html.escape(pa['upstream_type'])}</span>
                <span style="font-size: 13px; color: #94a3b8; margin-left: 8px;">Detected in <strong>{pa['touch_count']}</strong> separate matters/agenda items</span>
            </div>
            <div>
                <span class="badge" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8;">Requires Confirmation Prior to Public Dossier</span>
            </div>
        </div>
        """)
    patterns_section_html = "".join(patterns_html) if patterns_html else "<p class='muted-text'>No recurring multi-item patterns detected yet.</p>"

    # Banner message
    approved_banner_html = ""
    if approved_item:
        approved_url = f"/docs/briefs/brief-{approved_item['item_id']}.html" if approved_item["kind"] == "brief" else f"/docs/action-pages/action-{approved_item['item_id']}.html"
        approved_banner_html = f"""
        <div class="banner banner-done" style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 20px;">
            <div>
                <strong>&#10003; Policy Brief Approved & Published Locally:</strong> {html.escape(approved_item['item_title'])}
            </div>
            <div style="display: flex; gap: 10px;">
                <a href="{html.escape(approved_url)}" target="_blank" class="btn btn-blue" style="font-size: 12px; padding: 6px 14px; text-decoration: none;">Open Public Policy Brief &rarr;</a>
                <a href="/docs/index.html" target="_blank" class="btn btn-secondary" style="font-size: 12px; padding: 6px 14px; text-decoration: none;">View Public Website &rarr;</a>
            </div>
        </div>
        """

    if waiting_count > 0:
        waiting_banner_html = f"""
        <div class="banner banner-waiting">
            <strong>{waiting_count} {'draft is' if waiting_count == 1 else 'drafts are'} waiting for your review.</strong>
        </div>
        """
    else:
        waiting_banner_html = """
        <div class="banner banner-done">
            <strong>All caught up!</strong> No drafts are waiting for review.
        </div>
        """

    banner_html = approved_banner_html + waiting_banner_html

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Loretta's Ledger — Review</title>
    <style>
        :root {{
            --bg: #0b1120;
            --surface: #1e293b;
            --surface-hover: #26354a;
            --border: #334155;
            --text: #f8fafc;
            --muted: #94a3b8;
            --accent: #38bdf8;
            --gold: #f59e0b;
            --green: #10b981;
            --red: #ef4444;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 0;
            line-height: 1.5;
        }}
        header {{
            background: #0f172a;
            border-bottom: 1px solid var(--border);
            padding: 16px 24px;
        }}
        .header-inner {{
            max-width: 960px;
            margin: 0 auto;
        }}
        .site-title {{
            font-size: 22px;
            font-weight: 700;
            margin: 0 0 4px 0;
            color: #fff;
        }}
        .site-title span {{ color: var(--accent); }}
        .notice-banner {{
            font-size: 13px;
            color: var(--muted);
            margin: 0;
        }}
        .container {{
            max-width: 960px;
            margin: 0 auto;
            padding: 24px 20px 80px 20px;
        }}
        .banner {{
            padding: 14px 20px;
            border-radius: 8px;
            font-size: 15px;
            margin-bottom: 24px;
        }}
        .banner-waiting {{
            background: rgba(245, 158, 11, 0.15);
            border: 1px solid rgba(245, 158, 11, 0.4);
            color: #fbbf24;
        }}
        .banner-done {{
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid rgba(16, 185, 129, 0.4);
            color: #34d399;
        }}
        .section {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 32px;
        }}
        .section h2 {{
            margin-top: 0;
            margin-bottom: 16px;
            font-size: 19px;
            color: #fff;
            border-bottom: 1px solid var(--border);
            padding-bottom: 10px;
        }}
        .draft-row {{
            padding: 12px 14px;
            border-radius: 6px;
            border: 1px solid var(--border);
            margin-bottom: 10px;
            background: #0f172a;
            transition: border-color 0.15s ease;
        }}
        .draft-row:hover {{ border-color: var(--accent); }}
        .draft-row.active-row {{
            border-color: var(--accent);
            background: rgba(56, 189, 248, 0.08);
        }}
        .draft-row-meta {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 4px;
        }}
        .draft-row-title a {{
            color: #fff;
            text-decoration: none;
            font-size: 15px;
            font-weight: 600;
        }}
        .draft-row-title a:hover {{ color: var(--accent); }}
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-jur {{ background: rgba(56, 189, 248, 0.15); color: var(--accent); }}
        .badge-testcase {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }}
        .badge-mech {{ background: #334155; color: #cbd5e1; font-size: 11px; }}
        .date-text {{ font-size: 12px; color: var(--muted); }}
        .btn {{
            display: inline-block;
            font-weight: 600;
            font-size: 14px;
            padding: 8px 18px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            text-decoration: none;
            transition: opacity 0.15s;
        }}
        .btn:hover {{ opacity: 0.9; }}
        .btn-green {{ background: var(--green); color: #0b1120; }}
        .btn-red {{ background: var(--red); color: #fff; }}
        .btn-blue {{ background: #0284c7; color: #fff; }}
        .btn-secondary {{ background: #334155; color: #cbd5e1; }}
        .button-bar {{
            display: flex;
            gap: 10px;
            margin: 20px 0;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }}
        .active-draft-box {{
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 24px;
        }}
        .draft-header h2 {{
            font-size: 20px;
            margin: 8px 0 12px 0;
            color: #fff;
            border: none;
            padding: 0;
        }}
        .draft-meta {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 8px;
        }}
        .source-link {{
            color: var(--accent);
            text-decoration: none;
            font-size: 13px;
        }}
        .source-link:hover {{ text-decoration: underline; }}
        .testcase-alert {{
            background: rgba(245, 158, 11, 0.15);
            border: 1px solid rgba(245, 158, 11, 0.3);
            color: #fbbf24;
            padding: 10px 14px;
            border-radius: 6px;
            font-size: 13px;
            margin: 12px 0;
        }}
        .section-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 16px 20px;
            margin-top: 20px;
        }}
        .section-card h3 {{
            margin-top: 0;
            margin-bottom: 12px;
            font-size: 16px;
            color: var(--accent);
        }}
        .influence-item {{
            padding: 10px 0;
            border-bottom: 1px solid var(--border);
        }}
        .influence-item:last-child {{ border-bottom: none; }}
        .influence-evidence {{
            font-size: 13px;
            color: #cbd5e1;
            font-style: italic;
            margin: 4px 0;
        }}
        .evidence-link {{
            font-size: 12px;
            color: var(--accent);
            text-decoration: none;
        }}
        .text-editor {{
            width: 100%;
            box-sizing: border-box;
            background: #060913;
            color: #f8fafc;
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 12px;
            font-family: inherit;
            font-size: 14px;
            line-height: 1.5;
            resize: vertical;
        }}
        .draft-rendered {{
            font-size: 14px;
            line-height: 1.6;
            color: #e2e8f0;
        }}
        .draft-rendered h1, .draft-rendered h2, .draft-rendered h3 {{
            color: #fff;
            margin-top: 20px;
            margin-bottom: 8px;
        }}
        .draft-rendered a {{ color: var(--accent); }}
        .draft-rendered blockquote {{
            border-left: 3px solid var(--accent);
            margin: 12px 0;
            padding-left: 12px;
            color: #94a3b8;
        }}
        .published-row {{
            padding: 10px 14px;
            border-radius: 6px;
            border: 1px solid var(--border);
            margin-bottom: 8px;
            background: #0f172a;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .published-row-title a {{
            color: #fff;
            text-decoration: none;
            font-size: 14px;
            font-weight: 500;
        }}
        .published-row-title a:hover {{ color: var(--accent); }}
        .published-row-meta {{ display: flex; align-items: center; gap: 10px; }}
        .muted-text {{ color: var(--muted); font-size: 14px; margin: 0; }}
        .empty-state {{
            padding: 30px;
            text-align: center;
            color: var(--muted);
        }}
    </style>
    <script>
        function toggleEditMode() {{
            var panel = document.getElementById("edit-panel");
            if (panel.style.display === "none") {{
                panel.style.display = "block";
            }} else {{
                panel.style.display = "none";
            }}
        }}
    </script>
</head>
<body>
    <header>
        <div class="header-inner" style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
            <div>
                <h1 class="site-title">Loretta's <span>Ledger</span> &bull; Review Dashboard</h1>
                <p class="notice-banner">This page runs only on your computer. Nothing here is public until you approve it and push.</p>
            </div>
            <div>
                <a href="/docs/index.html" target="_blank" class="btn btn-secondary" style="text-decoration: none; font-size: 13px; padding: 7px 14px;">
                    View Public Website &rarr;
                </a>
            </div>
        </div>
    </header>

    <div class="container">
        {banner_html}

        <!-- Section 1: Needs your review -->
        <div class="section">
            <h2>1. Needs your review ({waiting_count})</h2>
            {''.join(needs_review_cards) if needs_review_cards else '<p class=\"muted-text\">No drafts waiting for review.</p>'}
        </div>

        <!-- Section 2: Matter Links to Confirm -->
        <div class="section">
            <h2>2. Cross-Meeting Matter Links to Confirm ({len(proposed_events)})</h2>
            <p class="muted-text" style="margin-bottom: 16px;">These agenda items share substantive keywords or tracking identifiers across sessions. Confirm to link to the matter timeline or Separate to keep standalone.</p>
            {proposed_section_html}
        </div>

        <!-- Section 3: Pattern Alerts -->
        <div class="section">
            <h2>3. Pattern Alerts ({len(pattern_alerts)})</h2>
            <p class="muted-text" style="margin-bottom: 16px;">Actors appearing across multiple local matters. Only verified factual touches publish to public actor dossiers.</p>
            {patterns_section_html}
        </div>

        <!-- Section 4: Draft view -->
        <div class="section">
            <h2>4. Draft view</h2>
            {draft_view_html}
        </div>

        <!-- Section 5: Published -->
        <div class="section">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; border-bottom: 1px solid var(--border); padding-bottom: 10px;">
                <h2 style="margin: 0; border: none; padding: 0;">5. Published ({len(published)})</h2>
                <a href="/docs/index.html" target="_blank" class="btn btn-secondary" style="font-size: 12px; padding: 5px 12px; text-decoration: none;">View Public Website &rarr;</a>
            </div>
            {published_section_html}
        </div>
    </div>
</body>
</html>
"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. Alias /docs, /docs/, /public, /public/ to /docs/index.html
        if path in ("/docs", "/docs/", "/public", "/public/"):
            path = "/docs/index.html"

        # 2. Allow direct access to /briefs/ or /action-pages/
        if path.startswith("/briefs/") or path.startswith("/action-pages/"):
            path = "/docs" + path

        # 3. Serve static site files from docs/ if requested
        if path.startswith("/docs/"):
            rel_path = path[6:]
            docs_base = Path(__file__).resolve().parent.parent / "docs"
            doc_file = docs_base / rel_path
            if doc_file.is_dir():
                doc_file = doc_file / "index.html"

            # Fallback if a relative navbar link requested e.g. /docs/briefs/index.html or /docs/briefs/briefs.html
            if not doc_file.exists() and "/" in rel_path:
                flat_name = rel_path.split("/")[-1]
                parent_fallback = docs_base / flat_name
                if parent_fallback.exists() and parent_fallback.is_file():
                    doc_file = parent_fallback

            if doc_file.exists() and doc_file.is_file():
                content_type = "text/html" if doc_file.suffix == ".html" else "text/plain"
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.end_headers()
                with open(doc_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        selected_id = None
        if "draft_id" in query:
            try:
                selected_id = int(query["draft_id"][0])
            except (ValueError, TypeError):
                selected_id = None

        approved_id = None
        if "approved" in query:
            try:
                approved_id = int(query["approved"][0])
            except (ValueError, TypeError):
                approved_id = None

        html_content = generate_html_dashboard(selected_draft_id=selected_id, approved_draft_id=approved_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content.encode("utf-8"))

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length).decode("utf-8")
        params = urllib.parse.parse_qs(post_data)

        conn = get_db_connection()
        try:
            if self.path == "/action/approve":
                draft_id = int(params.get("draft_id", [0])[0])
                if draft_id:
                    approve_draft(conn, draft_id)
                    draft_item_row = conn.execute("SELECT item_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
                    if draft_item_row:
                        conn.execute(
                            "UPDATE drafts SET reviewed = 1, reviewed_at = datetime('now') WHERE item_id = ? AND kind = 'action_page'",
                            (draft_item_row["item_id"],),
                        )
                        conn.commit()
                    export_site_content(conn)
                self.send_response(303)
                self.send_header("Location", f"/?approved={draft_id}")
                self.end_headers()
                return

            elif self.path == "/action/reject":
                draft_id = int(params.get("draft_id", [0])[0])
                if draft_id:
                    reject_draft(conn, draft_id)
                    export_site_content(conn)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            elif self.path == "/action/save-edit":
                draft_id = int(params.get("draft_id", [0])[0])
                new_markdown = params.get("markdown", [""])[0]
                if draft_id and new_markdown:
                    conn.execute(
                        "UPDATE drafts SET markdown = ? WHERE id = ?",
                        (new_markdown, draft_id),
                    )
                    conn.commit()
                self.send_response(303)
                self.send_header("Location", f"/?draft_id={draft_id}#draft-view")
                self.end_headers()
                return

            elif self.path == "/action/confirm-matter-link":
                event_id = params.get("event_id", [""])[0]
                if event_id:
                    conn.execute("UPDATE matter_events SET confidence = 'confirmed' WHERE id = ?", (event_id,))
                    conn.commit()
                self.send_response(303)
                self.send_header("Location", "/#matter-links")
                self.end_headers()
                return

            elif self.path == "/action/reject-matter-link":
                event_id = params.get("event_id", [""])[0]
                if event_id:
                    conn.execute("DELETE FROM matter_events WHERE id = ?", (event_id,))
                    conn.commit()
                self.send_response(303)
                self.send_header("Location", "/#matter-links")
                self.end_headers()
                return
        finally:
            conn.close()

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def serve_dashboard(port: int = 8000):
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), DashboardHandler) as httpd:
        print("\n" + "=" * 80)
        print(" LORETTA'S LEDGER — REVIEW DASHBOARD READY")
        print("=" * 80)
        print(f" Review page: http://localhost:{port}")
        print(" This page runs only on your computer. Nothing publishes until approved.")
        print(" Press Ctrl+C in terminal to stop.")
        print("=" * 80 + "\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


def main():
    parser = argparse.ArgumentParser(description="View Loretta's Ledger ingested items and database")
    parser.add_argument("--list", action="store_true", help="List recent items in the terminal")
    parser.add_argument("--item", type=str, default=None, help="Display full details for an item ID")
    parser.add_argument("--limit", type=int, default=25, help="Number of items to list")
    parser.add_argument("--offset", type=int, default=0, help="Offset for item listing")
    parser.add_argument("--serve", action="store_true", help="Start local review dashboard in browser")
    parser.add_argument("--port", type=int, default=8000, help="Port for local web server (default: 8000)")

    args = parser.parse_args()

    if args.serve:
        serve_dashboard(port=args.port)
    elif args.item:
        show_item(args.item)
    else:
        list_items(limit=args.limit, offset=args.offset)


if __name__ == "__main__":
    main()
