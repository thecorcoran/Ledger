#!/usr/bin/env bash
# Loretta's Ledger — Easy One-Command Control Script

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

case "$1" in
  run|update|"")
    python3 -m pipeline
    ;;
  web|serve|dashboard)
    python3 -m pipeline.view --serve
    ;;
  review)
    python3 -m pipeline.review --list
    ;;
  test)
    python3 -m unittest discover tests
    ;;
  push)
    git push
    ;;
  *)
    echo "Loretta's Ledger Helper"
    echo "Usage: ./ledger [command]"
    echo ""
    echo "Commands:"
    echo "  ./ledger         - Run full automated pipeline (ingest, classify, upstream, drafts, site)"
    echo "  ./ledger web     - Launch interactive Web Control Center (http://localhost:8000)"
    echo "  ./ledger review  - View pending drafts waiting for review"
    echo "  ./ledger push    - Push updates to GitHub / GitHub Pages"
    echo "  ./ledger test    - Run unit test suite"
    ;;
esac
