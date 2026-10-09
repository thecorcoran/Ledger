# Loretta's Ledger

A local policy publication providing consistent, evidence-based policy briefs for the **City of Olympia** and **Thurston County**, analyzed through a people-first policy lens (subsidiarity, widely held property, family and local economy, skepticism of concentrated power).

Named for Loretta Corcoran, who ran Loretta's Cafe in downtown Olympia until the Olympia Center was built.

---

## How It Fits Together

All running, analyzing, and reviewing happens **strictly on your local machine**. There is **no cloud automation**. Nothing is published until you explicitly approve it and push it.

```text
[ 1. Data In: Olympia & Thurston County Agendas + Packets ]  (Local: ./ledger run)
                                │
                                ▼
[ 2. Review on Your Computer: http://localhost:8000 ]        (Local: ./ledger web)
                                │
                                ▼
[ 3. Publish to Public Website: Loretta's Ledger ]            (Manual: ./ledger push)
```

1. **Data In (`./ledger run`)**: Runs locally. The system fetches public agendas from the City of Olympia and Thurston County, downloads attached official document packets (staff reports, ordinances, contracts, SEPA checklists), extracts concrete facts and upstream citations, and prepares draft policy briefs in your local database.
2. **Review on Your Computer (`./ledger web`)**: Runs locally. You open `http://localhost:8000` in your web browser. This dashboard runs strictly on your computer:
   - **Section 1: Needs your review**: Prioritized list of unreviewed drafts (flagging precedent-setting test cases and upstream influences first).
   - **Section 2: Draft view**: Full policy brief with a factual summary, 8-principle Litmus Test evaluation, "Who's behind this?" evidence chain, and actionable citizen next steps. Three simple buttons: **Approve**, **Reject**, or **Save edits**.
   - **Section 3: Published**: A list of approved briefs with direct buttons to view the local public pages.
3. **Public Site Out (`./ledger push`)**: Publishes to GitHub Pages. When you are satisfied with your approved drafts and local preview, `./ledger push` stages the generated public site (`docs/`) and tracked source files, commits them, and pushes them to GitHub. It **never commits or touches `data/`**.

---

## The Three Daily Commands

```bash
./ledger run    # 1. Check for new agenda items, download packets, and build drafts (local)
./ledger web    # 2. Open the review page on your computer at http://localhost:8000 (local)
./ledger push   # 3. Publish your approved drafts to the public website (docs/ only)
```

Running `./ledger` with no arguments will always display this quick reminder.

*(Developers can also run `./ledger test` to execute the automated test suite).*

---

## Safety & Publishing Rules

- **Zero Unreviewed Publishing**: The public site generator will never publish an unreviewed draft. Items must have explicit human review (`reviewed = true`) in the local dashboard before appearing in public archives.
- **Local-Only Operation**: All running (`./ledger run`), reviewing (`./ledger web`), and draft approvals happen strictly on your local machine. Your machine is the sole writer.
- **Single-Writer Database**: The SQLite database (`data/ledger.db`) lives only on your computer and is ignored by git (`.gitignore`). It is never pushed to GitHub, completely eliminating binary merge conflicts.
- **No Cloud Automation**: There are no GitHub Actions workflows, scheduled cron jobs, or automated cloud bots in this repository. Nothing runs or publishes in the background.
- **Safe Publishing Boundary**: `./ledger push` commits and pushes strictly the public website in `docs/` and tracked source files—never `data/`.


---

## The 8 Litmus Test Principles

Every local policy item is evaluated against eight grounded design principles:

1. **Subsidiarity**: Is the decision made at the most local practical level, or is it driven by distant state/federal mandates?
2. **Ownership**: Does it support the ability of ordinary families and small independent enterprises to hold real property, or does it impose centralized regulatory burdens?
3. **Small & Local vs. Large & Distant**: Does it favor homegrown local contractors and residents over large institutional entities?
4. **Family & Household**: Does it bolster household independence and self-reliance rather than bureaucratic programming?
5. **Cost & Who Pays**: Who bears the financial burden (fees, taxes, utility rate adjustments), and is the accounting transparent?
6. **Consent & Process**: Was there adequate advance notice, transparent agendas, and genuine opportunity for public comment and hearing?
7. **Reversibility & Accountability**: Can voters hold decision-makers directly accountable, and can the policy be revisited or reversed if it fails?
8. **Place**: Does it preserve the tangible character, shorelines, farms, and neighborhoods of our local community?

---

## Upstream Influence Tracking ("Who's Behind This?")

A core feature of Loretta's Ledger is unmasking where local policies originate:
- **State Statutes**: Revised Code of Washington (e.g., Growth Management Act, Shoreline Management Act).
- **State Agencies**: Washington State Department of Commerce, Department of Ecology, Department of Health.
- **Federal Programs & Grants**: HUD Community Development Block Grants, federal environmental mandates.
- **Funding Strings & Precedents**: Matching fund conditions, interlocal agreements, and court rulings.

Every upstream influence is documented with direct citations and links to official records—never speculation or boilerplate.

---

## Project Structure

- `ledger`: Simple one-command executable runner (`./ledger run`, `./ledger web`, `./ledger push`).
- `pipeline/`:
  - `ingest/`: Agenda crawlers for City of Olympia and Thurston County.
  - `documents.py`: Document extraction engine for PDF staff reports, ordinances, and presentations.
  - `analyze.py`: In-depth analysis and draft generator for the 8 Litmus Test principles.
  - `upstream/`: Registry matching, citation detection, and influence chain tracing.
  - `view.py`: Single-page editorial Control Center (`http://localhost:8000`).
  - `export.py`: Static site builder and PDF brief generator (`docs/`).
- `data/`: Local-only SQLite database (`ledger.db`) and cached document attachments (untracked in git).
- `docs/`: The public website files published to GitHub Pages.
- `tests/`: Automated unit and integration tests.
