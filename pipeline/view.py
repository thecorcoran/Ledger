"""Back-end Control Center and web dashboard for Loretta's Ledger."""

import argparse
import html
import http.server
import json
import socketserver
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.review import approve_draft, reject_draft
from pipeline.run import run_full_pipeline
from pipeline.export import export_site_content


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


def generate_html_dashboard(db_path: Optional[Path] = None, tab: str = "items") -> str:
    conn = get_db_connection(db_path)
    try:
        items = conn.execute(
            """
            SELECT i.*, 
                   t.flagged_reason as test_case_reason, 
                   t.status as test_case_status
            FROM items i
            LEFT JOIN test_cases t ON i.id = t.item_id
            ORDER BY i.meeting_date DESC, i.id DESC
            """
        ).fetchall()

        topic_rows = conn.execute("SELECT item_id, topic FROM item_topics").fetchall()
        item_topics_map = {}
        for r in topic_rows:
            item_topics_map.setdefault(r["item_id"], []).append(r["topic"])

        ref_rows = conn.execute(
            """
            SELECT r.*, a.name as actor_name, a.upstream_type
            FROM upstream_refs r
            JOIN upstream_actors a ON r.actor_id = a.id
            """
        ).fetchall()
        item_refs_map = {}
        for r in ref_rows:
            item_refs_map.setdefault(r["item_id"], []).append(r)

        actors = conn.execute("SELECT * FROM upstream_actors ORDER BY name").fetchall()
        test_case_count = conn.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0]
        total_refs_count = conn.execute("SELECT COUNT(*) FROM upstream_refs").fetchone()[0]

        drafts = conn.execute(
            """
            SELECT d.*, i.title as item_title, i.jurisdiction, i.meeting_date
            FROM drafts d
            JOIN items i ON d.item_id = i.id
            ORDER BY d.reviewed ASC, d.id DESC
            """
        ).fetchall()
        pending_drafts_count = len([d for d in drafts if not d["reviewed"]])
        approved_drafts_count = len([d for d in drafts if d["reviewed"]])
    finally:
        conn.close()

    # Drafts Review HTML
    drafts_rows_html = []
    for d in drafts:
        status_badge = (
            '<span class="badge" style="background:#10b981; color:#0b1120;">PUBLISHED</span>'
            if d["reviewed"]
            else '<span class="badge" style="background:#f59e0b; color:#0b1120;">PENDING REVIEW</span>'
        )
        actions_btn = ""
        if not d["reviewed"]:
            actions_btn = f"""
            <form method="POST" action="/action/approve" style="display:inline;">
                <input type="hidden" name="draft_id" value="{d['id']}">
                <button type="submit" class="btn btn-green">Approve & Publish</button>
            </form>
            <form method="POST" action="/action/reject" style="display:inline; margin-left:6px;">
                <input type="hidden" name="draft_id" value="{d['id']}">
                <button type="submit" class="btn btn-red" onclick="return confirm('Delete this draft?');">Reject</button>
            </form>
            """
        else:
            actions_btn = f"""
            <form method="POST" action="/action/reject" style="display:inline;">
                <input type="hidden" name="draft_id" value="{d['id']}">
                <button type="submit" class="btn btn-red" onclick="return confirm('Unpublish and remove this draft?');">Unpublish</button>
            </form>
            """

        drafts_rows_html.append(f"""
        <div class="card">
            <div class="card-meta">
                <span class="badge badge-jur">{d['jurisdiction'].upper()}</span>
                <span class="badge" style="background:#334155; color:#cbd5e1;">{d['kind'].upper()}</span>
                {status_badge}
                <span>Meeting: {d['meeting_date']}</span>
            </div>
            <h3 class="card-title">{html.escape(d['item_title'])}</h3>
            <div style="margin: 12px 0;">{actions_btn}</div>
            <details class="item-details">
                <summary>Read Draft Content ({d['kind']})</summary>
                <pre>{html.escape(d['markdown'])}</pre>
            </details>
        </div>
        """)

    # Items Table HTML
    rows_html = []
    for it in items:
        topics = item_topics_map.get(it["id"], [])
        topic_badges = " ".join([f'<span class="badge badge-topic">{html.escape(t)}</span>' for t in topics])

        refs = item_refs_map.get(it["id"], [])
        upstream_html = ""
        if refs:
            ref_badges = []
            for rf in refs:
                ref_badges.append(
                    f'<div class="upstream-ref-card">'
                    f'<strong>Who\'s Behind This:</strong> <span class="badge badge-actor">{html.escape(rf["actor_name"])}</span> '
                    f'<span class="badge badge-mechanism">{html.escape(rf["mechanism"])}</span><br>'
                    f'<span class="upstream-evidence">&ldquo;{html.escape(rf["evidence_ref"])}&rdquo;</span>'
                    f'</div>'
                )
            upstream_html = "".join(ref_badges)

        tc_html = ""
        if it["test_case_reason"]:
            tc_html = f'<div class="badge badge-testcase">&#9888; TEST CASE WATCH: {html.escape(it["test_case_reason"])}</div>'

        deadline_html = ""
        if it["comment_deadline"]:
            deadline_html = f'<div class="deadline-note">&#9201; Comment Deadline: {html.escape(it["comment_deadline"])}</div>'

        rows_html.append(f"""
        <tr class="item-row">
            <td>
                <strong>{html.escape(str(it['meeting_date'] or ''))}</strong>
                {deadline_html}
            </td>
            <td><span class="badge badge-jur">{html.escape(it['jurisdiction'].upper())}</span></td>
            <td>
                <div class="item-title">{html.escape(it['title'])}</div>
                <div class="item-meta">
                    <span class="item-id">{html.escape(it['id'])}</span>
                    <span class="topics-wrap">{topic_badges}</span>
                </div>
                {tc_html}
                {upstream_html}
                <details class="item-details">
                    <summary>View Details & Attachments</summary>
                    <pre>{html.escape(it['body_text'] or '')}</pre>
                </details>
            </td>
            <td><a href="{html.escape(it['url'])}" target="_blank" class="source-link">Source &rarr;</a></td>
        </tr>
        """)

    actor_badges = []
    for a in actors:
        actor_badges.append(
            f'<span class="badge badge-actor" title="{html.escape(a["notes"] or "")}">{html.escape(a["name"])} ({html.escape(a["upstream_type"])})</span>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Loretta's Ledger — Pipeline Control Center</title>
    <style>
        :root {{
            --bg: #0b1120;
            --surface: #1e293b;
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
            position: sticky;
            top: 0;
            z-index: 100;
        }}
        .header-inner {{
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .logo {{ font-size: 20px; font-weight: 700; color: #fff; text-decoration: none; }}
        .logo span {{ color: var(--accent); }}
        .actions-bar {{ display: flex; gap: 12px; }}
        .btn {{
            display: inline-block;
            background: var(--accent);
            color: #0b1120;
            font-weight: 600;
            font-size: 13px;
            padding: 7px 14px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            text-decoration: none;
        }}
        .btn:hover {{ opacity: 0.9; }}
        .btn-green {{ background: var(--green); color: #0b1120; }}
        .btn-red {{ background: var(--red); color: #fff; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
        .nav-tabs {{
            display: flex;
            gap: 12px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 12px;
            margin-bottom: 24px;
        }}
        .nav-tab {{
            color: var(--muted);
            text-decoration: none;
            font-size: 15px;
            font-weight: 600;
            padding: 6px 12px;
            border-radius: 6px;
        }}
        .nav-tab.active {{
            background: var(--surface);
            color: var(--accent);
        }}
        .stats-bar {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .stat-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 14px 18px;
            border-radius: 8px;
        }}
        .stat-num {{ font-size: 24px; font-weight: 700; color: var(--accent); }}
        .stat-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; margin-top: 2px; }}
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }}
        .badge-jur {{ background: rgba(56, 189, 248, 0.15); color: var(--accent); }}
        .badge-actor {{ background: #334155; color: #e2e8f0; margin: 2px 4px; }}
        .badge-mechanism {{ background: rgba(168, 85, 247, 0.2); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.4); margin-left: 4px; }}
        .badge-topic {{ background: #1e3a5f; color: #7dd3fc; margin-left: 4px; }}
        .badge-testcase {{
            background: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.3);
            margin-top: 6px;
            font-size: 12px;
            padding: 4px 8px;
            border-radius: 4px;
            display: inline-block;
        }}
        .upstream-ref-card {{
            background: rgba(168, 85, 247, 0.08);
            border: 1px solid rgba(168, 85, 247, 0.25);
            padding: 8px 12px;
            border-radius: 6px;
            margin-top: 8px;
            font-size: 13px;
        }}
        .upstream-evidence {{ font-size: 12px; color: #cbd5e1; font-style: italic; display: inline-block; margin-top: 4px; }}
        .deadline-note {{ font-size: 11px; color: #f59e0b; margin-top: 6px; line-height: 1.3; }}
        .item-meta {{ display: flex; align-items: center; gap: 8px; margin-top: 4px; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: var(--surface);
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--border);
        }}
        th, td {{
            padding: 12px 16px;
            text-align: left;
            border-bottom: 1px solid var(--border);
            vertical-align: top;
        }}
        th {{ background: #1e293b; color: var(--muted); font-size: 12px; text-transform: uppercase; }}
        .item-row:hover {{ background: rgba(255, 255, 255, 0.02); }}
        .item-title {{ font-size: 15px; font-weight: 600; line-height: 1.4; color: #fff; }}
        .item-id {{ font-size: 11px; color: var(--muted); font-family: monospace; }}
        .item-details {{ margin-top: 8px; font-size: 13px; }}
        .item-details summary {{ cursor: pointer; color: var(--accent); }}
        .item-details pre, .card pre {{
            background: #060913;
            padding: 12px;
            border-radius: 6px;
            white-space: pre-wrap;
            word-break: break-word;
            font-size: 12px;
            color: #cbd5e1;
            border: 1px solid var(--border);
            margin-top: 8px;
        }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 18px;
            border-radius: 8px;
            margin-bottom: 16px;
        }}
        .card-title {{ margin: 0 0 6px 0; font-size: 17px; color: #fff; }}
        .card-meta {{ font-size: 13px; color: var(--muted); margin-bottom: 8px; display: flex; gap: 8px; align-items: center; }}
        a.source-link {{ color: var(--accent); text-decoration: none; font-weight: 500; font-size: 13px; }}
    </style>
</head>
<body>
    <header>
        <div class="header-inner">
            <a href="/" class="logo">Loretta's <span>Ledger</span> &bull; Control Center</a>
            <div class="actions-bar">
                <form method="POST" action="/action/run-pipeline" style="display:inline;">
                    <button type="submit" class="btn">&#9654; Run Full Pipeline Now</button>
                </form>
                <a href="http://localhost:8000/docs/index.html" target="_blank" class="btn" style="background:#334155; color:#fff;">View Public Site &rarr;</a>
            </div>
        </div>
    </header>

    <div class="container">
        <div class="stats-bar">
            <div class="stat-card">
                <div class="stat-num">{len(items)}</div>
                <div class="stat-label">Ingested Items</div>
            </div>
            <div class="stat-card">
                <div class="stat-num" style="color:var(--gold);">{pending_drafts_count}</div>
                <div class="stat-label">Pending Drafts to Review</div>
            </div>
            <div class="stat-card">
                <div class="stat-num" style="color:var(--green);">{approved_drafts_count}</div>
                <div class="stat-label">Approved & Published Briefs</div>
            </div>
            <div class="stat-card">
                <div class="stat-num">{test_case_count}</div>
                <div class="stat-label">Test-Case Watch Items</div>
            </div>
            <div class="stat-card">
                <div class="stat-num">{total_refs_count}</div>
                <div class="stat-label">Upstream Influence Links</div>
            </div>
        </div>

        <div class="nav-tabs">
            <a href="/?tab=items" class="nav-tab {'active' if tab == 'items' else ''}">All Policy Items ({len(items)})</a>
            <a href="/?tab=drafts" class="nav-tab {'active' if tab == 'drafts' else ''}">Editorial Review & Drafts ({pending_drafts_count} pending)</a>
        </div>

        {''.join(drafts_rows_html) if tab == 'drafts' else f"""
        <div style="background:var(--surface); border:1px solid var(--border); padding:16px; border-radius:8px; margin-bottom:20px;">
            <div style="font-size:12px; font-weight:700; text-transform:uppercase; color:var(--muted); margin-bottom:8px;">Monitored Upstream Actors</div>
            <div>{''.join(actor_badges)}</div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 120px;">Meeting Date</th>
                    <th style="width: 90px;">Jurisdiction</th>
                    <th>Policy Proposal & Details</th>
                    <th style="width: 100px;">Source Link</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows_html)}
            </tbody>
        </table>
        """}
    </div>
</body>
</html>
"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Serve static site files from docs/ if requested
        if path.startswith("/docs/"):
            rel_path = path[6:]
            doc_file = Path(__file__).resolve().parent.parent / "docs" / rel_path
            if doc_file.exists() and doc_file.is_file():
                content_type = "text/html" if doc_file.suffix == ".html" else "text/plain"
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.end_headers()
                with open(doc_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        tab = query.get("tab", ["items"])[0]
        html_content = generate_html_dashboard(tab=tab)
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
            if self.path == "/action/run-pipeline":
                run_full_pipeline()
                self.send_response(303)
                self.send_header("Location", "/?tab=items")
                self.end_headers()
                return

            elif self.path == "/action/approve":
                draft_id = int(params.get("draft_id", [0])[0])
                if draft_id:
                    approve_draft(conn, draft_id)
                    export_site_content(conn)
                self.send_response(303)
                self.send_header("Location", "/?tab=drafts")
                self.end_headers()
                return

            elif self.path == "/action/reject":
                draft_id = int(params.get("draft_id", [0])[0])
                if draft_id:
                    reject_draft(conn, draft_id)
                    export_site_content(conn)
                self.send_response(303)
                self.send_header("Location", "/?tab=drafts")
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
        print(" LORETTA'S LEDGER — CONTROL CENTER READY")
        print("=" * 80)
        print(f" Web Dashboard: http://localhost:{port}")
        print(f" One-Click Actions: Run Pipeline, Approve/Reject Drafts, View Live Site")
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
    parser.add_argument("--serve", action="store_true", help="Start local web Control Center in browser")
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
