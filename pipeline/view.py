"""Back-end Control Center and web dashboard for Loretta's Ledger.

Implements Requirement 4, 6, and 8:
- Tabs: Review | Links to confirm | Alerts | Published | Archive.
- Review tab: shows only matters with unreviewed updates ('Update to review'), with a summary of what is new
  since last approved, plus the living brief editor, versions, and history.
- Links to confirm tab: proposes uncertain links (`confidence = 'proposed'`) with Confirm and Dismiss actions.
- Alerts tab: shows Dormant->Active woken matters, Test Cases, multi-matter actor patterns, and contact warnings.
- Published tab: shows 'Live' vs. 'Approved – not yet pushed' per matter.
- Archive tab: shows Closed and Dormant matters with search and a Restore button. Never deletes.
"""

import argparse
import hashlib
import html
import http.server
import json
import re
import socketserver
import sqlite3
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional
import yaml

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.review import approve_draft, reject_draft
from pipeline.export import export_site_content, markdown_to_html
from pipeline.matters import (
    approve_matter_update,
    close_matter,
    confirm_proposed_link,
    link_matters,
    mark_all_approved_as_published,
    reject_proposed_link,
    restore_matter_to_active,
)


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
    selected_matter_id: Optional[str] = None,
    approved_draft_id: Optional[int] = None,
    active_tab: str = "review",
) -> str:
    """Generates the 5-tab editorial dashboard:
    Tabs: Review | Links to confirm | Alerts | Published | Archive
    """
    conn = get_db_connection(db_path)
    try:
        # Ensure matters are linked if table is empty
        m_count = conn.execute("SELECT COUNT(*) FROM matters").fetchone()[0]
        if m_count == 0:
            link_matters(conn)

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

        # 1. Matters with unreviewed updates ('Update to review') for the Review Queue
        # Only matters with needs_review = 1 (or unreviewed brief draft) and in active state
        review_matters = conn.execute(
            """
            SELECT m.*,
                   mb.item_id as brief_item_id,
                   mb.markdown as brief_markdown,
                   mb.updated_date as brief_updated_date,
                   mb.changelog_json,
                   mb.reviewed as brief_reviewed,
                   d.id as draft_id
            FROM matters m
            LEFT JOIN matter_briefs mb ON m.id = mb.matter_id
            LEFT JOIN drafts d ON mb.item_id = d.item_id AND d.kind = 'brief'
            WHERE m.archive_state = 'active'
              AND (m.needs_review = 1 OR (d.id IS NOT NULL AND d.reviewed = 0))
            ORDER BY m.woke_from_dormant DESC, m.is_test_case DESC, m.last_action_date DESC, m.id ASC
            """
        ).fetchall()

        # 2. Published matters query (Reviewed briefs on active or closed matters)
        published_matters = conn.execute(
            """
            SELECT m.*,
                   mb.item_id as brief_item_id,
                   mb.updated_date as brief_updated_date,
                   mb.reviewed as brief_reviewed,
                   mb.reviewed_at,
                   mb.approved_hash,
                   mb.published_hash,
                   d.id as draft_id
            FROM matters m
            JOIN matter_briefs mb ON m.id = mb.matter_id
            LEFT JOIN drafts d ON mb.item_id = d.item_id AND d.kind = 'brief'
            WHERE mb.reviewed = 1 OR (d.id IS NOT NULL AND d.reviewed = 1)
            ORDER BY mb.reviewed_at DESC, m.last_action_date DESC
            """
        ).fetchall()

        # 3. Archive matters query (Closed and Dormant matters)
        archived_matters = conn.execute(
            """
            SELECT m.*,
                   mb.item_id as brief_item_id,
                   mb.reviewed as brief_reviewed
            FROM matters m
            LEFT JOIN matter_briefs mb ON m.id = mb.matter_id
            WHERE m.archive_state IN ('closed', 'dormant')
            ORDER BY m.archive_state ASC, m.last_action_date DESC
            """
        ).fetchall()

        waiting_count = len(review_matters)

        # Check contacts verification age (flag > 90 days)
        contacts_path = Path(__file__).resolve().parent.parent / "config" / "contacts.yaml"
        stale_contacts = []
        if contacts_path.exists():
            with open(contacts_path, "r", encoding="utf-8") as f:
                cdata = yaml.safe_load(f) or {}
                for cid, cinfo in cdata.get("contacts", {}).items():
                    v_str = cinfo.get("last_verified")
                    if v_str:
                        try:
                            v_dt = datetime.strptime(v_str, "%Y-%m-%d")
                            age = (datetime.now() - v_dt).days
                            if age > 90:
                                stale_contacts.append(f"{cinfo.get('entity_name', cid)} ({age}d)")
                        except Exception:
                            stale_contacts.append(f"{cinfo.get('entity_name', cid)}")
                    else:
                        stale_contacts.append(f"{cinfo.get('entity_name', cid)}")

        # Determine which matter/draft is actively open in the Review editor
        active_matter = None
        if selected_matter_id:
            for rm in review_matters:
                if rm["id"] == selected_matter_id:
                    active_matter = rm
                    break
            if not active_matter:
                active_matter = conn.execute(
                    """
                    SELECT m.*,
                           mb.item_id as brief_item_id,
                           mb.markdown as brief_markdown,
                           mb.updated_date as brief_updated_date,
                           mb.changelog_json,
                           mb.reviewed as brief_reviewed,
                           d.id as draft_id
                    FROM matters m
                    LEFT JOIN matter_briefs mb ON m.id = mb.matter_id
                    LEFT JOIN drafts d ON mb.item_id = d.item_id AND d.kind = 'brief'
                    WHERE m.id = ?
                    """,
                    (selected_matter_id,),
                ).fetchone()
        elif selected_draft_id:
            for rm in review_matters:
                if rm["draft_id"] == selected_draft_id:
                    active_matter = rm
                    break
            if not active_matter:
                active_matter = conn.execute(
                    """
                    SELECT m.*,
                           mb.item_id as brief_item_id,
                           mb.markdown as brief_markdown,
                           mb.updated_date as brief_updated_date,
                           mb.changelog_json,
                           mb.reviewed as brief_reviewed,
                           d.id as draft_id
                    FROM matters m
                    JOIN matter_briefs mb ON m.id = mb.matter_id
                    JOIN drafts d ON mb.item_id = d.item_id
                    WHERE d.id = ?
                    """,
                    (selected_draft_id,),
                ).fetchone()
        elif review_matters:
            active_matter = review_matters[0]

        active_refs = []
        active_events = []
        active_versions = []
        if active_matter:
            active_events = conn.execute(
                """
                SELECT * FROM matter_events
                WHERE matter_id = ? AND confidence = 'confirmed'
                ORDER BY action_date DESC, id DESC
                """,
                (active_matter["id"],),
            ).fetchall()
            active_versions = conn.execute(
                """
                SELECT * FROM matter_versions
                WHERE matter_id = ?
                ORDER BY version_number DESC
                """,
                (active_matter["id"],),
            ).fetchall()
            if active_matter["brief_item_id"]:
                active_refs = conn.execute(
                    """
                    SELECT r.*, a.name as actor_name, a.upstream_type, a.home_url
                    FROM upstream_refs r
                    JOIN upstream_actors a ON r.actor_id = a.id
                    WHERE r.item_id = ?
                    """,
                    (active_matter["brief_item_id"],),
                ).fetchall()

        # Query proposed matter links waiting for confirmation
        proposed_events = conn.execute(
            """
            SELECT me.id as event_id, me.matter_id, me.action_date, me.match_reason, me.notes,
                   m.plain_title as matter_plain_title, m.official_title as matter_title,
                   m.stable_id, m.jurisdiction,
                   i.id as item_id, i.title as item_title, i.url as item_url
            FROM matter_events me
            JOIN matters m ON me.matter_id = m.id
            JOIN items i ON me.item_id = i.id
            WHERE me.confidence = 'proposed'
            ORDER BY me.action_date DESC
            LIMIT 25
            """
        ).fetchall()

        # Alerts: woken from dormant, test cases, and multi-matter upstream actors
        woken_matters = conn.execute(
            "SELECT * FROM matters WHERE woke_from_dormant = 1 ORDER BY last_action_date DESC"
        ).fetchall()
        testcase_matters = conn.execute(
            "SELECT * FROM matters WHERE is_test_case = 1 ORDER BY last_action_date DESC"
        ).fetchall()
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

    # Tab 1: Review queue ("Update to review")
    needs_review_cards = []
    for rm in review_matters:
        is_selected = active_matter and rm["id"] == active_matter["id"]
        jur_name = "Olympia" if rm["jurisdiction"].lower() == "olympia" else "Thurston County"
        tc_flag = ""
        if rm["is_test_case"] and rm["test_case_reason"]:
            tc_flag = f'<span class="badge badge-testcase">&#9888; Test case: {html.escape(rm["test_case_reason"])}</span>'

        woke_flag = ""
        if rm["woke_from_dormant"]:
            woke_flag = '<span class="badge" style="background:rgba(16, 185, 129, 0.2); color:#34d399; border:1px solid #10b981;">Woke from Dormant</span>'

        update_badge = '<span class="badge" style="background:rgba(245, 158, 11, 0.2); color:#fbbf24; border:1px solid rgba(245, 158, 11, 0.4);">Update to review</span>'
        m_title = rm["plain_title"] or rm["official_title"] or rm["title"]
        whats_new = rm["whats_new_summary"] or f"Latest action ({rm['last_action_date']}): {rm['last_action_summary']}"

        needs_review_cards.append(f"""
        <div class="draft-row {'active-row' if is_selected else ''}">
            <div class="draft-row-meta" style="flex-wrap: wrap;">
                <span class="badge badge-jur">{html.escape(jur_name)}</span>
                <span class="badge badge-mech">{html.escape(rm['stable_id'] or '')}</span>
                {update_badge}
                {woke_flag}
                {tc_flag}
                <span class="date-text">Last action: {html.escape(str(rm['last_action_date'] or 'None'))} &bull; Next: {html.escape(str(rm['next_action_date'] or 'not yet posted'))}</span>
            </div>
            <div class="draft-row-title" style="margin-top: 4px;">
                <a href="/?tab=review&matter_id={urllib.parse.quote(rm['id'])}#draft-view"><strong>{html.escape(m_title)}</strong></a>
                <div style="font-size: 12px; color: #94a3b8; margin-top: 2px;">Official: {html.escape(rm['official_title'] or rm['title'])} &bull; Status: {html.escape(rm['lifecycle_status'] or '')}</div>
            </div>
            <div style="font-size: 13px; color: #fde68a; background: rgba(245, 158, 11, 0.08); border-left: 3px solid #f59e0b; padding: 6px 10px; margin-top: 8px; border-radius: 0 4px 4px 0;">
                <strong>What is new since last approved:</strong> {html.escape(whats_new)}
            </div>
        </div>
        """)

    # Active Matter & Living Brief Review Editor
    draft_view_html = ""
    if active_matter:
        jur_label = "Olympia" if active_matter["jurisdiction"].lower() == "olympia" else "Thurston County"
        tc_notice = ""
        if active_matter["is_test_case"] and active_matter["test_case_reason"]:
            tc_notice = f"""
            <div class="testcase-alert">
                <strong>&#9888; Precedent-Setting Test Case:</strong> {html.escape(active_matter['test_case_reason'])}
            </div>
            """

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
            who_behind_html = "<p class='muted-text'>No upstream influences identified in official documents for this matter.</p>"

        md_text = active_matter["brief_markdown"] or ""
        rendered_draft_body = markdown_to_html(md_text)

        # Render versions & history summary inside editor
        ver_rows = []
        for v in active_versions:
            ver_rows.append(
                f"<li><strong>{html.escape(v['version_label'])}</strong> ({html.escape(v['version_date'])}): "
                f"{html.escape(v['what_changed'] or '')} "
                f"<a href='{html.escape(v['doc_url'] or '#')}' target='_blank' class='evidence-link'>[Packet]</a></li>"
            )
        versions_summary_html = f"<ul style='margin:0; padding-left:18px; font-size:13px; color:#cbd5e1;'>{''.join(ver_rows)}</ul>" if ver_rows else "<p class='muted-text'>Single document version.</p>"

        ev_rows = []
        for ev in active_events:
            min_link = f" &bull; <a href='{html.escape(ev['minutes_url'])}' target='_blank' class='evidence-link'>[Minutes]</a>" if ev["minutes_url"] else ""
            ev_rows.append(
                f"<li><strong>{html.escape(ev['action_date'])}</strong> ({html.escape(ev['body_name'] or '')}): "
                f"{html.escape(ev['outcome_text'] or '')} "
                f"<a href='{html.escape(ev['record_url'] or '#')}' target='_blank' class='evidence-link'>[Packet]</a>{min_link}</li>"
            )
        events_summary_html = f"<ul style='margin:0; padding-left:18px; font-size:13px; color:#cbd5e1;'>{''.join(ev_rows)}</ul>" if ev_rows else "<p class='muted-text'>No events recorded.</p>"

        draft_id_val = active_matter["draft_id"] or 0
        source_url = active_matter["last_action_url"] or "#"

        draft_view_html = f"""
        <div id="draft-view" class="active-draft-box">
            <div class="draft-header">
                <div class="draft-meta" style="flex-wrap: wrap;">
                    <span class="badge badge-jur">{html.escape(jur_label)}</span>
                    <span class="badge badge-mech">{html.escape(active_matter['stable_id'] or '')}</span>
                    <span class="date-text">Updated: {html.escape(str(active_matter['brief_updated_date'] or active_matter['last_action_date'] or ''))}</span>
                    <span class="date-text">Status: <strong>{html.escape(active_matter['lifecycle_status'] or '')}</strong></span>
                    <a href="{html.escape(source_url)}" target="_blank" class="source-link">View Latest Source &rarr;</a>
                </div>
                <h2>{html.escape(active_matter['plain_title'] or active_matter['official_title'])}</h2>
                <div style="font-size: 13px; color: #94a3b8; margin-bottom: 8px;">Official Title: {html.escape(active_matter['official_title'] or active_matter['title'])}</div>
                {tc_notice}
                <div style="font-size: 13px; color: #fde68a; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3); padding: 10px 14px; border-radius: 6px; margin-top: 8px;">
                    <strong>What is new since last approved:</strong> {html.escape(active_matter['whats_new_summary'] or 'Initial review.')}
                </div>
            </div>

            <!-- Action Buttons Bar -->
            <div class="button-bar" style="flex-wrap: wrap;">
                <form method="POST" action="/action/approve" style="display:inline;">
                    <input type="hidden" name="draft_id" value="{draft_id_val}">
                    <input type="hidden" name="matter_id" value="{html.escape(active_matter['id'])}">
                    <button type="submit" class="btn btn-green">&#10003; Approve Update</button>
                </form>

                <button type="button" class="btn btn-blue" onclick="toggleEditMode();">&#9998; Edit Living Brief</button>

                <form method="POST" action="/action/close-matter" style="display:inline;">
                    <input type="hidden" name="matter_id" value="{html.escape(active_matter['id'])}">
                    <button type="submit" class="btn btn-secondary">Move to Closed Archive</button>
                </form>
            </div>

            <!-- Editable Form -->
            <div id="edit-panel" style="display: none; margin-top: 16px;">
                <form method="POST" action="/action/save-edit">
                    <input type="hidden" name="draft_id" value="{draft_id_val}">
                    <input type="hidden" name="matter_id" value="{html.escape(active_matter['id'])}">
                    <div style="margin-bottom: 8px;">
                        <label for="markdown-editor" style="font-weight:600; font-size:13px; color:#cbd5e1;">Edit living brief markdown (includes Updated [date] line):</label>
                    </div>
                    <textarea id="markdown-editor" name="markdown" rows="18" class="text-editor">{html.escape(md_text)}</textarea>
                    <div style="margin-top: 10px;">
                        <button type="submit" class="btn btn-green">&#128190; Save edits</button>
                        <button type="button" class="btn btn-secondary" onclick="toggleEditMode();">Cancel</button>
                    </div>
                </form>
            </div>

            <!-- Versions & History in Editor -->
            <div class="section-card">
                <h3>Document Versions ({len(active_versions)}) &amp; Action History ({len(active_events)})</h3>
                <div style="margin-bottom: 10px;"><strong style="font-size:13px; color:#fff;">Versions:</strong> {versions_summary_html}</div>
                <div><strong style="font-size:13px; color:#fff;">History:</strong> {events_summary_html}</div>
            </div>

            <!-- Who's Behind This Section -->
            <div class="section-card">
                <h3>Who's behind this?</h3>
                {who_behind_html}
            </div>

            <!-- Living Brief Preview -->
            <div class="section-card">
                <h3>Living Brief Content &amp; Litmus Reading</h3>
                <div class="draft-rendered">
                    {rendered_draft_body}
                </div>
            </div>
        </div>
        """
    else:
        draft_view_html = """
        <div class="empty-state">
            <p>All active matters are reviewed and up to date.</p>
        </div>
        """

    # Tab 2: Links to Confirm (`confidence = 'proposed'`)
    proposed_links_html = []
    for pe in proposed_events:
        jur_lbl = "Olympia" if pe["jurisdiction"].lower() == "olympia" else "Thurston County"
        m_disp = pe["matter_plain_title"] or pe["matter_title"]
        reason_txt = pe["match_reason"] or pe["notes"] or ""
        proposed_links_html.append(f"""
        <div class="draft-row" style="display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;">
            <div style="flex: 1; min-width: 260px;">
                <div class="draft-row-meta">
                    <span class="badge badge-jur">{html.escape(jur_lbl)}</span>
                    <span class="badge badge-mech">{html.escape(pe['stable_id'] or '')}</span>
                    <span class="date-text">{html.escape(pe['action_date'])}</span>
                    <span class="badge" style="background: rgba(245, 158, 11, 0.15); color: #fbbf24;">Proposed Link</span>
                </div>
                <div style="font-size: 14px; font-weight: 600; color: #fff; margin: 4px 0;">
                    Item: {html.escape(pe['item_title'])}
                </div>
                <div style="font-size: 13px; color: #94a3b8;">
                    &rarr; Proposed to Matter: <strong>{html.escape(m_disp)}</strong> ({html.escape(pe['stable_id'] or '')})
                </div>
                <div style="font-size: 12px; color: #64748b; margin-top: 4px;">
                    Reason: {html.escape(reason_txt)} &bull; <a href="{html.escape(pe['item_url'])}" target="_blank" style="color: var(--accent);">Official Record &rarr;</a>
                </div>
            </div>
            <div style="display: flex; gap: 8px;">
                <form method="POST" action="/action/confirm-matter-link">
                    <input type="hidden" name="event_id" value="{html.escape(pe['event_id'])}">
                    <button type="submit" class="btn btn-green" style="font-size: 12px; padding: 6px 12px;">Confirm Link</button>
                </form>
                <form method="POST" action="/action/reject-matter-link">
                    <input type="hidden" name="event_id" value="{html.escape(pe['event_id'])}">
                    <button type="submit" class="btn btn-red" style="font-size: 12px; padding: 6px 12px;">Separate / Dismiss</button>
                </form>
            </div>
        </div>
        """)
    proposed_section_html = "".join(proposed_links_html) if proposed_links_html else "<p class='muted-text'>No uncertain matter links pending confirmation.</p>"

    # Tab 3: Alerts (Woken from Dormant, Test Cases, Multi-Matter Actor Patterns, Contact Warnings)
    alerts_items_html = []
    for wm in woken_matters:
        alerts_items_html.append(f"""
        <div class="draft-row" style="border-color: #10b981;">
            <div class="draft-row-meta">
                <span class="badge" style="background:rgba(16,185,129,0.2); color:#34d399;">Auto-Woke from Dormant</span>
                <span class="badge badge-mech">{html.escape(wm['stable_id'] or '')}</span>
                <span class="date-text">Last Action: {html.escape(str(wm['last_action_date'] or ''))}</span>
            </div>
            <div style="font-size:14px; font-weight:600; color:#fff; margin-top:4px;">
                <a href="/?tab=review&matter_id={urllib.parse.quote(wm['id'])}#draft-view" style="color:#fff; text-decoration:none;">{html.escape(wm['plain_title'] or wm['official_title'])}</a>
            </div>
            <div style="font-size:13px; color:#cbd5e1; margin-top:4px;">{html.escape(wm['whats_new_summary'] or '')}</div>
        </div>
        """)
    for tm in testcase_matters:
        alerts_items_html.append(f"""
        <div class="draft-row" style="border-color: #f59e0b;">
            <div class="draft-row-meta">
                <span class="badge badge-testcase">&#9888; Test Case Watch</span>
                <span class="badge badge-mech">{html.escape(tm['stable_id'] or '')}</span>
                <span class="date-text">Status: {html.escape(tm['lifecycle_status'] or '')}</span>
            </div>
            <div style="font-size:14px; font-weight:600; color:#fff; margin-top:4px;">
                <a href="/?tab=review&matter_id={urllib.parse.quote(tm['id'])}#draft-view" style="color:#fff; text-decoration:none;">{html.escape(tm['plain_title'] or tm['official_title'])}</a>
            </div>
            <div style="font-size:13px; color:#fde68a; margin-top:4px;">{html.escape(tm['test_case_reason'] or '')}</div>
        </div>
        """)
    for pa in pattern_alerts:
        alerts_items_html.append(f"""
        <div style="padding: 10px 14px; background: #0f172a; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;">
            <div>
                <strong style="color: #fff;">{html.escape(pa['actor_name'])}</strong>
                <span class="badge badge-jur" style="margin-left: 8px;">{html.escape(pa['upstream_type'])}</span>
                <span style="font-size: 13px; color: #94a3b8; margin-left: 8px;">Detected in <strong>{pa['touch_count']}</strong> separate matters/agenda items</span>
            </div>
            <div>
                <span class="badge" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8;">Cross-Matter Actor Pattern</span>
            </div>
        </div>
        """)
    alerts_section_html = "".join(alerts_items_html) if alerts_items_html else "<p class='muted-text'>No active alerts.</p>"

    # Tab 4: Published ("Live" vs. "Approved – not yet pushed" per matter)
    published_items_html = []
    not_pushed_count = 0
    for p in published_matters:
        jur_title = "Olympia" if p["jurisdiction"].lower() == "olympia" else "Thurston County"
        public_url = f"/docs/briefs/brief-{p['brief_item_id']}.html"
        p_title = p["plain_title"] or p["official_title"] or p["title"]

        is_live = bool(p["published_hash"] and p["approved_hash"] and p["published_hash"] == p["approved_hash"])
        if is_live:
            sync_badge = '<span class="badge" style="background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid #10b981;">Live</span>'
        else:
            not_pushed_count += 1
            sync_badge = '<span class="badge" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid #f59e0b;">Approved – not yet pushed</span>'

        published_items_html.append(f"""
        <div class="published-row" style="flex-wrap: wrap; gap: 10px;">
            <div class="published-row-meta" style="min-width: 240px;">
                <span class="badge badge-jur">{html.escape(jur_title)}</span>
                <span class="badge badge-mech">{html.escape(p['stable_id'] or '')}</span>
                {sync_badge}
            </div>
            <div class="published-row-title" style="flex: 1; margin: 0 12px; min-width: 240px;">
                <a href="{html.escape(public_url)}" target="_blank"><strong>{html.escape(p_title)}</strong></a>
                <div style="font-size: 12px; color: #94a3b8; margin-top: 2px;">
                    Status: {html.escape(p['lifecycle_status'] or '')} &bull; Updated {html.escape(str(p['brief_updated_date'] or p['last_action_date'] or ''))}
                </div>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
                <a href="/?tab=review&matter_id={urllib.parse.quote(p['id'])}#draft-view" class="btn btn-secondary" style="font-size: 12px; padding: 4px 10px;">Edit</a>
                <a href="{html.escape(public_url)}" target="_blank" class="btn btn-blue" style="font-size: 12px; padding: 4px 12px; text-decoration: none; white-space: nowrap;">View Public Page &rarr;</a>
            </div>
        </div>
        """)
    published_section_html = "".join(published_items_html) if published_items_html else "<p class='muted-text'>No matters have been approved yet.</p>"

    # Tab 5: Archive (Closed and Dormant matters with search and Restore button)
    archive_rows_html = []
    for am in archived_matters:
        jur_title = "Olympia" if am["jurisdiction"].lower() == "olympia" else "Thurston County"
        a_title = am["plain_title"] or am["official_title"] or am["title"]
        state_lbl = (am["archive_state"] or "dormant").capitalize()
        state_badge_style = (
            "background: rgba(148, 163, 184, 0.2); color: #cbd5e1; border: 1px solid #64748b;"
            if am["archive_state"] == "closed"
            else "background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3);"
        )
        perm_link_html = ""
        if am["brief_item_id"] and am["brief_reviewed"]:
            perm_url = f"/docs/briefs/brief-{am['brief_item_id']}.html"
            perm_link_html = f'<a href="{html.escape(perm_url)}" target="_blank" class="btn btn-secondary" style="font-size: 12px; padding: 5px 10px;">Permanent Link &rarr;</a>'

        archive_rows_html.append(f"""
        <div class="published-row archive-item-row" data-search="{html.escape((a_title + ' ' + (am['official_title'] or '') + ' ' + (am['stable_id'] or '') + ' ' + jur_title + ' ' + state_lbl).lower())}" style="flex-wrap: wrap; gap: 10px;">
            <div class="published-row-meta" style="min-width: 210px;">
                <span class="badge" style="{state_badge_style}">{html.escape(state_lbl)}</span>
                <span class="badge badge-jur">{html.escape(jur_title)}</span>
                <span class="badge badge-mech">{html.escape(am['stable_id'] or '')}</span>
            </div>
            <div class="published-row-title" style="flex: 1; margin: 0 12px; min-width: 240px;">
                <strong style="color: #fff; font-size: 14px;">{html.escape(a_title)}</strong>
                <div style="font-size: 12px; color: #94a3b8; margin-top: 2px;">
                    Last action ({html.escape(str(am['last_action_date'] or 'None'))}): {html.escape(am['last_action_summary'] or am['lifecycle_status'] or '')}
                </div>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
                {perm_link_html}
                <form method="POST" action="/action/restore-matter" style="margin: 0;">
                    <input type="hidden" name="matter_id" value="{html.escape(am['id'])}">
                    <button type="submit" class="btn btn-green" style="font-size: 12px; padding: 5px 12px;">Restore to Active</button>
                </form>
            </div>
        </div>
        """)
    archive_section_html = "".join(archive_rows_html) if archive_rows_html else "<p class='muted-text'>No Closed or Dormant matters in the archive.</p>"

    contacts_warning_html = ""
    if stale_contacts:
        contacts_warning_html = f"""
        <div class="alert alert-warning" style="background:rgba(234, 179, 8, 0.15); border:1px solid #eab308; color:#fde047; padding:12px 16px; border-radius:6px; margin-bottom:20px; font-size:13px;">
            <strong>&#9888; Contact Verification Warning:</strong> The following contacts in config/contacts.yaml were verified over 90 days ago: <em>{', '.join(stale_contacts)}</em>. Please re-verify against official directories.
        </div>
        """

    approved_banner_html = ""
    if approved_item:
        approved_url = f"/docs/briefs/brief-{approved_item['item_id']}.html"
        approved_banner_html = f"""
        <div class="banner banner-done" style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 20px;">
            <div>
                <strong>&#10003; Matter Brief Approved Locally (Approved – not yet pushed):</strong> {html.escape(approved_item['item_title'])}
            </div>
            <div style="display: flex; gap: 10px;">
                <a href="{html.escape(approved_url)}" target="_blank" class="btn btn-blue" style="font-size: 12px; padding: 6px 14px; text-decoration: none;">Open Local Brief &rarr;</a>
                <a href="/docs/briefs.html" target="_blank" class="btn btn-secondary" style="font-size: 12px; padding: 6px 14px; text-decoration: none;">View Public Tracker &rarr;</a>
            </div>
        </div>
        """

    if waiting_count > 0:
        waiting_banner_html = f"""
        <div class="banner banner-waiting">
            <strong>{waiting_count} {'matter has an unreviewed update' if waiting_count == 1 else 'matters have unreviewed updates'} waiting for your review.</strong>
        </div>
        """
    else:
        waiting_banner_html = """
        <div class="banner banner-done">
            <strong>All caught up!</strong> No matters have unreviewed updates waiting for review.
        </div>
        """

    banner_html = approved_banner_html + waiting_banner_html
    alerts_total = len(woken_matters) + len(testcase_matters) + len(pattern_alerts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Loretta's Ledger — Review Dashboard</title>
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
            padding: 16px 24px 0 24px;
        }}
        .header-inner {{
            max-width: 1020px;
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
        .dashboard-tabs {{
            display: flex;
            gap: 6px;
            margin-top: 16px;
            flex-wrap: wrap;
        }}
        .tab-btn {{
            background: transparent;
            color: var(--muted);
            border: 1px solid transparent;
            border-bottom: 3px solid transparent;
            padding: 10px 16px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
        }}
        .tab-btn:hover {{
            color: #fff;
        }}
        .tab-btn.active {{
            color: var(--accent);
            border-bottom-color: var(--accent);
            background: rgba(56, 189, 248, 0.08);
        }}
        .container {{
            max-width: 1020px;
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
        }}
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
        }}
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
            margin: 8px 0 8px 0;
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
        .published-row-meta {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
        .muted-text {{ color: var(--muted); font-size: 14px; margin: 0; }}
        .empty-state {{
            padding: 30px;
            text-align: center;
            color: var(--muted);
        }}
        .tab-panel {{ display: none; }}
        .tab-panel.active {{ display: block; }}
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
        function switchDashboardTab(tabName) {{
            document.querySelectorAll('.tab-btn').forEach(function(btn) {{
                btn.classList.toggle('active', btn.getAttribute('data-tab') === tabName);
            }});
            document.querySelectorAll('.tab-panel').forEach(function(panel) {{
                panel.classList.toggle('active', panel.id === 'panel-' + tabName);
            }});
        }}
        function filterArchiveRows() {{
            var q = (document.getElementById('archive-search').value || '').toLowerCase().trim();
            document.querySelectorAll('.archive-item-row').forEach(function(row) {{
                var hay = row.getAttribute('data-search') || '';
                row.style.display = (!q || hay.indexOf(q) !== -1) ? 'flex' : 'none';
            }});
        }}
    </script>
</head>
<body>
    <header>
        <div class="header-inner">
            <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
                <div>
                    <h1 class="site-title">Loretta's <span>Ledger</span> &bull; Matter Tracker Dashboard</h1>
                    <p class="notice-banner">This page runs only on your computer. Nothing here is public until you approve it and push.</p>
                </div>
                <div style="display: flex; gap: 10px;">
                    <a href="/docs/briefs.html" target="_blank" class="btn btn-secondary" style="text-decoration: none; font-size: 13px; padding: 7px 14px;">
                        Public Matter Tracker &rarr;
                    </a>
                    <a href="/docs/index.html" target="_blank" class="btn btn-secondary" style="text-decoration: none; font-size: 13px; padding: 7px 14px;">
                        View Public Website &rarr;
                    </a>
                </div>
            </div>
            <!-- 5 Required Dashboard Tabs: Review | Links to confirm | Alerts | Published | Archive -->
            <nav class="dashboard-tabs" aria-label="Dashboard Tabs">
                <button type="button" class="tab-btn {'active' if active_tab == 'review' else ''}" data-tab="review" onclick="switchDashboardTab('review')">
                    Review ({waiting_count})
                </button>
                <button type="button" class="tab-btn {'active' if active_tab == 'links' else ''}" data-tab="links" onclick="switchDashboardTab('links')">
                    Links to confirm ({len(proposed_events)})
                </button>
                <button type="button" class="tab-btn {'active' if active_tab == 'alerts' else ''}" data-tab="alerts" onclick="switchDashboardTab('alerts')">
                    Alerts ({alerts_total})
                </button>
                <button type="button" class="tab-btn {'active' if active_tab == 'published' else ''}" data-tab="published" onclick="switchDashboardTab('published')">
                    Published ({len(published_matters)})
                </button>
                <button type="button" class="tab-btn {'active' if active_tab == 'archive' else ''}" data-tab="archive" onclick="switchDashboardTab('archive')">
                    Archive ({len(archived_matters)})
                </button>
            </nav>
        </div>
    </header>

    <div class="container">
        {banner_html}
        {contacts_warning_html}

        <!-- TAB 1: REVIEW -->
        <div id="panel-review" class="tab-panel {'active' if active_tab == 'review' else ''}">
            <div class="section">
                <h2>Matters with Unreviewed Updates ({waiting_count})</h2>
                <p class="muted-text" style="margin-bottom: 16px;">Only matters with unreviewed updates ('Update to review') appear here, with a summary of what is new since you last approved.</p>
                {''.join(needs_review_cards) if needs_review_cards else '<p class="muted-text">No matters have unreviewed updates.</p>'}
            </div>

            <div class="section">
                <h2>Matter &amp; Living Brief Review</h2>
                {draft_view_html}
            </div>
        </div>

        <!-- TAB 2: LINKS TO CONFIRM -->
        <div id="panel-links" class="tab-panel {'active' if active_tab == 'links' else ''}">
            <div class="section">
                <h2>Links to Confirm ({len(proposed_events)})</h2>
                <p class="muted-text" style="margin-bottom: 16px;">Uncertain item-to-matter links proposed from specific shared subject nouns or same-day hearing packets. Never matched on generic words.</p>
                {proposed_section_html}
            </div>
        </div>

        <!-- TAB 3: ALERTS -->
        <div id="panel-alerts" class="tab-panel {'active' if active_tab == 'alerts' else ''}">
            <div class="section">
                <h2>Alerts ({alerts_total})</h2>
                <p class="muted-text" style="margin-bottom: 16px;">Dormant matters that automatically woke on new activity, precedent-setting test cases, and multi-matter upstream actors.</p>
                {alerts_section_html}
            </div>
        </div>

        <!-- TAB 4: PUBLISHED -->
        <div id="panel-published" class="tab-panel {'active' if active_tab == 'published' else ''}">
            <div class="section">
                <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; border-bottom: 1px solid var(--border); padding-bottom: 10px; flex-wrap: wrap; gap: 10px;">
                    <h2 style="margin: 0; border: none; padding: 0;">Published Matters ({len(published_matters)}) &mdash; Live vs. Approved &ndash; not yet pushed ({not_pushed_count})</h2>
                    <div style="display: flex; gap: 8px;">
                        <form method="POST" action="/action/mark-pushed" style="margin: 0;">
                            <button type="submit" class="btn btn-secondary" style="font-size: 12px; padding: 5px 12px;">Mark All Approved as Live</button>
                        </form>
                        <a href="/docs/briefs.html" target="_blank" class="btn btn-secondary" style="font-size: 12px; padding: 5px 12px; text-decoration: none;">View Public Tracker &rarr;</a>
                    </div>
                </div>
                {published_section_html}
            </div>
        </div>

        <!-- TAB 5: ARCHIVE -->
        <div id="panel-archive" class="tab-panel {'active' if active_tab == 'archive' else ''}">
            <div class="section">
                <h2>Archive: Closed &amp; Dormant Matters ({len(archived_matters)})</h2>
                <p class="muted-text" style="margin-bottom: 14px;">Closed and Dormant matters are moved out of active views, stay searchable, keep permanent links, and can be restored to Active at any time. Nothing is ever deleted.</p>
                <div style="margin-bottom: 16px;">
                    <input type="search" id="archive-search" oninput="filterArchiveRows()" placeholder="Search Closed and Dormant matters by title, ID, or jurisdiction..." class="text-editor" style="padding: 8px 12px;">
                </div>
                {archive_section_html}
            </div>
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

        if path in ("/reading", "/docs/reading"):
            self.send_response(301)
            self.send_header("Location", path + "/")
            self.end_headers()
            return

        # 2. Allow direct access to /reading/, /briefs/, /actors/, or top-level static pages
        if (
            path.startswith("/briefs/")
            or path.startswith("/actors/")
            or path.startswith("/reading/")
            or path.startswith("/action-pages/")
            or path in ("/index.html", "/briefs.html", "/actors.html", "/principles.html", "/about.html", "/glossary.html", "/reading.html")
        ):
            path = "/docs" + path

        # 3. Serve static site files from docs/ if requested
        if path.startswith("/docs/"):
            rel_path = path[6:]
            docs_base = Path(__file__).resolve().parent.parent / "docs"
            doc_file = docs_base / rel_path
            if doc_file.is_dir():
                doc_file = doc_file / "index.html"

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

        selected_matter_id = query.get("matter_id", [None])[0]
        active_tab = query.get("tab", ["review"])[0]

        approved_id = None
        if "approved" in query:
            try:
                approved_id = int(query["approved"][0])
            except (ValueError, TypeError):
                approved_id = None

        html_content = generate_html_dashboard(
            selected_draft_id=selected_id,
            selected_matter_id=selected_matter_id,
            approved_draft_id=approved_id,
            active_tab=active_tab,
        )
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
                draft_id = int(params.get("draft_id", [0])[0] or 0)
                matter_id = params.get("matter_id", [""])[0]
                if draft_id:
                    approve_draft(conn, draft_id)
                if matter_id:
                    approve_matter_update(conn, matter_id)
                export_site_content(conn)
                self.send_response(303)
                self.send_header("Location", f"/?tab=review&approved={draft_id}")
                self.end_headers()
                return

            elif self.path == "/action/reject":
                draft_id = int(params.get("draft_id", [0])[0] or 0)
                if draft_id:
                    reject_draft(conn, draft_id)
                    export_site_content(conn)
                self.send_response(303)
                self.send_header("Location", "/?tab=review")
                self.end_headers()
                return

            elif self.path == "/action/save-edit":
                draft_id = int(params.get("draft_id", [0])[0] or 0)
                matter_id = params.get("matter_id", [""])[0]
                new_markdown = params.get("markdown", [""])[0]
                if new_markdown:
                    if draft_id:
                        conn.execute(
                            "UPDATE drafts SET markdown = ? WHERE id = ?",
                            (new_markdown, draft_id),
                        )
                    if matter_id:
                        conn.execute(
                            "UPDATE matter_briefs SET markdown = ?, reviewed = 0 WHERE matter_id = ?",
                            (new_markdown, matter_id),
                        )
                        conn.execute(
                            "UPDATE matters SET needs_review = 1 WHERE id = ?",
                            (matter_id,),
                        )
                    conn.commit()
                self.send_response(303)
                loc = f"/?tab=review&matter_id={urllib.parse.quote(matter_id)}#draft-view" if matter_id else f"/?tab=review&draft_id={draft_id}#draft-view"
                self.send_header("Location", loc)
                self.end_headers()
                return

            elif self.path == "/action/confirm-matter-link":
                event_id = params.get("event_id", [""])[0]
                if event_id:
                    confirm_proposed_link(conn, event_id)
                self.send_response(303)
                self.send_header("Location", "/?tab=links")
                self.end_headers()
                return

            elif self.path == "/action/reject-matter-link":
                event_id = params.get("event_id", [""])[0]
                if event_id:
                    reject_proposed_link(conn, event_id)
                self.send_response(303)
                self.send_header("Location", "/?tab=links")
                self.end_headers()
                return

            elif self.path == "/action/restore-matter":
                matter_id = params.get("matter_id", [""])[0]
                if matter_id:
                    restore_matter_to_active(conn, matter_id)
                    export_site_content(conn)
                self.send_response(303)
                self.send_header("Location", "/?tab=archive")
                self.end_headers()
                return

            elif self.path == "/action/close-matter":
                matter_id = params.get("matter_id", [""])[0]
                if matter_id:
                    close_matter(conn, matter_id)
                    export_site_content(conn)
                self.send_response(303)
                self.send_header("Location", "/?tab=archive")
                self.end_headers()
                return

            elif self.path == "/action/mark-pushed":
                mark_all_approved_as_published(conn)
                self.send_response(303)
                self.send_header("Location", "/?tab=published")
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
