"""Back-end viewer CLI and local web server for Loretta's Ledger."""

import argparse
import html
import http.server
import json
import socketserver
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from pipeline.db import DEFAULT_DB_PATH, get_db_connection


def list_items(db_path: Optional[Path] = None, limit: int = 25, offset: int = 0):
    conn = get_db_connection(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        rows = conn.execute(
            """
            SELECT id, jurisdiction, title, meeting_date, url
            FROM items
            ORDER BY meeting_date DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

        print(f"\nLoretta's Ledger — Ingested Items (Total: {total}, Showing: {len(rows)})")
        print("=" * 90)
        for r in rows:
            print(f"[{r['meeting_date'] or 'No Date'}] ({r['jurisdiction'].upper()}) {r['id']}")
            print(f"  Title: {r['title']}")
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

        print("\n" + "=" * 90)
        print(f"ITEM: {row['id']}")
        print("=" * 90)
        print(f"Jurisdiction: {row['jurisdiction']}")
        print(f"Title:        {row['title']}")
        print(f"Meeting Date: {row['meeting_date']}")
        print(f"URL:          {row['url']}")
        print(f"Published:    {row['published_at']}")
        print(f"Status:       {row['status']}")
        print(f"Hash:         {row['hash']}")
        print("\nBODY TEXT:")
        print("-" * 90)
        print(row["body_text"] or "(None)")
        print("=" * 90 + "\n")
    finally:
        conn.close()


def generate_html_dashboard(db_path: Optional[Path] = None) -> str:
    conn = get_db_connection(db_path)
    try:
        items = conn.execute(
            "SELECT * FROM items ORDER BY meeting_date DESC, id DESC"
        ).fetchall()
        actors = conn.execute("SELECT * FROM upstream_actors ORDER BY name").fetchall()
    finally:
        conn.close()

    rows_html = []
    for it in items:
        rows_html.append(f"""
        <tr class="item-row">
            <td><strong>{html.escape(str(it['meeting_date'] or ''))}</strong></td>
            <td><span class="badge badge-jur">{html.escape(it['jurisdiction'].upper())}</span></td>
            <td>
                <div class="item-title">{html.escape(it['title'])}</div>
                <div class="item-id">{html.escape(it['id'])}</div>
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
    <title>Loretta's Ledger — Policy Pipeline Dashboard</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --border: #334155;
            --text: #f8fafc;
            --muted: #94a3b8;
            --accent: #38bdf8;
            --accent-rgb: 56, 189, 248;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        header {{
            border-bottom: 1px solid var(--border);
            padding-bottom: 20px;
            margin-bottom: 24px;
        }}
        h1 {{ margin: 0 0 8px 0; font-size: 28px; }}
        .subtitle {{ color: var(--muted); font-size: 15px; margin: 0; }}
        .stats-bar {{
            display: flex;
            gap: 16px;
            margin: 20px 0;
        }}
        .stat-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 14px 20px;
            border-radius: 8px;
            flex: 1;
        }}
        .stat-num {{ font-size: 24px; font-weight: 700; color: var(--accent); }}
        .stat-label {{ color: var(--muted); font-size: 13px; text-transform: uppercase; }}
        .actors-section {{
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 16px;
            border-radius: 8px;
            margin-bottom: 24px;
        }}
        .actors-title {{ font-size: 14px; font-weight: 600; text-transform: uppercase; color: var(--muted); margin-bottom: 10px; }}
        .badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }}
        .badge-jur {{ background: rgba(56, 189, 248, 0.15); color: var(--accent); }}
        .badge-actor {{ background: #334155; color: #e2e8f0; margin: 2px 4px; }}
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
        th {{
            background: #1e293b;
            color: var(--muted);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .item-row:hover {{ background: rgba(255, 255, 255, 0.02); }}
        .item-title {{ font-size: 15px; font-weight: 600; line-height: 1.4; color: #fff; }}
        .item-id {{ font-size: 11px; color: var(--muted); font-family: monospace; margin-top: 4px; }}
        .item-details {{
            margin-top: 8px;
            font-size: 13px;
        }}
        .item-details summary {{
            cursor: pointer;
            color: var(--accent);
            outline: none;
        }}
        .item-details pre {{
            background: #0f172a;
            padding: 12px;
            border-radius: 6px;
            white-space: pre-wrap;
            word-break: break-word;
            font-size: 12px;
            color: #cbd5e1;
            border: 1px solid var(--border);
            margin-top: 8px;
        }}
        a.source-link {{
            color: var(--accent);
            text-decoration: none;
            font-weight: 500;
            font-size: 13px;
        }}
        a.source-link:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Loretta's Ledger — Back-End Policy Dashboard</h1>
            <p class="subtitle">Olympia & Thurston County Local Policy Pipeline &bull; People-First Policy Framework</p>
        </header>

        <div class="stats-bar">
            <div class="stat-card">
                <div class="stat-num">{len(items)}</div>
                <div class="stat-label">Ingested Items</div>
            </div>
            <div class="stat-card">
                <div class="stat-num">{len(actors)}</div>
                <div class="stat-label">Upstream Actors Monitored</div>
            </div>
            <div class="stat-card">
                <div class="stat-num">SQLite</div>
                <div class="stat-label">Database Storage (data/ledger.db)</div>
            </div>
        </div>

        <div class="actors-section">
            <div class="actors-title">Monitored Upstream Actors & Registry</div>
            <div>{''.join(actor_badges)}</div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 110px;">Meeting Date</th>
                    <th style="width: 90px;">Jurisdiction</th>
                    <th>Policy Item & Details</th>
                    <th style="width: 100px;">Source Link</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows_html)}
            </tbody>
        </table>
    </div>
</body>
</html>
"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        html_content = generate_html_dashboard()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # Quiet logging


def serve_dashboard(port: int = 8000):
    with socketserver.TCPServer(("", port), DashboardHandler) as httpd:
        print(f"\nServing Loretta's Ledger Dashboard on http://localhost:{port}")
        print("Press Ctrl+C to stop.")
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
    parser.add_argument("--serve", action="store_true", help="Start local web server to browse dashboard in browser")
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
