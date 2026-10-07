# Loretta's Ledger — Build Spec (v3)

## 1. Purpose

A local policy publication. **The main product is policy briefs about Olympia and Thurston County**, analyzed through a Distributist / Chestertonian lens (subsidiarity, widely held property, family and local economy, skepticism of concentrated power).

Its distinguishing feature is **upstream influence tracking**: showing where local policy actually comes from (state law, state agencies, regional bodies, funding conditions, model ordinances, outside policy groups, other jurisdictions). Every brief answers the question "Who's behind this?"

Named for Loretta Corcoran, whose home was cleared for the Olympia Center.

Audiences:
- **Citizens**: plain-language action pages (what's happening, when, how to comment).
- **Officials and organizers**: PDF policy briefs.

**Tone rule:** do not lead with the word "Distributism." Lead with concrete effects on families, property owners, small business, and neighborhoods. Use plain language (local control, affordability, people who own a stake in their place).

## 2. Scope

### Coverage (what gets briefs)
- City of Olympia
- Thurston County

Nothing else gets its own briefs.

### Upstream influences (context for local briefs)
Anything outside Olympia/Thurston that shapes a local decision. This is the only way other places enter the system, including other counties.

Types (`upstream_type`):
- `state_law` — statutes (e.g. Growth Management Act, housing and zoning mandates)
- `state_agency` — WA Dept of Commerce, Ecology, others, via rules, guidance, or required plans
- `regional_body` — e.g. Thurston Regional Planning Council, Puget Sound bodies
- `funding_condition` — grants or funding with strings attached
- `model_policy` — template ordinances or plans circulated by outside groups
- `outside_group` — advocacy groups, associations, consultants
- `peer_jurisdiction` — another city or county whose ordinance, plan, or decision a local document references or copies (this is where Mason, Lewis, or any other county could appear)
- `court_or_board` — court rulings, Growth Management Hearings Board decisions
- `federal` — federal law or program requirements

An upstream item is brought into the system only when:
1. A local item cites or references it (staff report, ordinance text, agenda memo, grant condition), or
2. It names Olympia or Thurston County directly, or
3. Text-similarity detection (Phase 5) finds a local document closely matching a known model policy or a peer jurisdiction's document.

### Topics (tag each item with one or more)
`housing` · `land-use` · `water` · `wells` · `stormwater` · `shoreline` · `agriculture` · `small-business` · `taxes-fees` · `public-process`

Water policy stays in scope, but only as it lands in Olympia/Thurston County.

## 3. Upstream influence system (core feature)

### 3.1 Mechanisms
Record *how* the influence works, not just who it is (`mechanism`):
- `mandate` — local government is required to act
- `funding_strings` — money conditioned on adopting something
- `template` — language adopted from a model or another jurisdiction
- `approval_gate` — state/regional sign-off required
- `litigation_threat_or_ruling` — a ruling or appeal pressure
- `advisory` — guidance, comment letters, recommendations
- `unknown` — relationship shown but mechanism not stated

### 3.2 Influence record (`upstream_refs`)
- `upstream_name`, `upstream_type`, `mechanism`
- `evidence_url` and `evidence_ref` (page, section, or excerpt location in the source)
- `confidence`: `documented` (source text says so) | `inferred` (analyst judgment, labeled as analysis)
- `item_id` (the local item) and optional `parent_ref_id` to chain influences (e.g. federal rule -> state agency guidance -> county ordinance)

### 3.3 Influence chains
Support multi-step lineage: local ordinance <- state agency rule <- state statute <- court ruling. Briefs show the chain in order, each link with its evidence.

### 3.4 Influence registry
A maintained list (`config/upstream.yaml` plus the database) of known upstream actors with: name, type, aliases and name variants to detect in text, home URL, and notes. Grows as the Ledger finds new ones.

### 3.5 Public outputs
- **"Who's behind this?"** section in every brief and action page (chain plus evidence links).
- **Influence pages**: one page per upstream actor listing every local item it touched, with mechanisms and dates. Example: "Dept of Commerce: 14 local items since January."
- **Lineage view** for any local policy: a simple top-down chain diagram.
- **Pattern alerts**: the same upstream actor or template showing up across multiple local items, or language reused from a template or peer jurisdiction.

### 3.6 Rules
- Every upstream claim needs a source link.
- Not stated in a source = `inferred`, labeled as analysis.
- Describe documented relationships only. Never assert motive or coordination.
- Text-similarity matches are flagged as "similar language to X," not "copied from X," unless a source says so.
- If none found: "No upstream source identified in the documents reviewed."

## 4. Test-case watch

A **test case** is an item likely to set precedent or reveal how a policy works in practice. Flag `test_case: true` when it matches any of:
- First application of a new ordinance, rule, or plan
- A permit, appeal, or lawsuit challenging a new policy
- A variance or exception others will cite
- A land-use, well, or water decision affecting a small owner or family farm
- A hearing examiner or Growth Management Hearings Board decision involving local policy
- A pilot program or one-off agreement that could become standard
- A local decision where an upstream mandate or template is being applied for the first time

Each flagged item gets a "Why this could be a test case" note and a status: `watching`, `decided`, `appealed`, `closed`.

## 5. Architecture

```
core sources (Olympia, Thurston) -> ingest -> normalize + dedupe (SQLite)
        -> classify (topic, test-case flag)
        -> upstream detection (registry match, citations, text similarity)
        -> fetch triggered upstream sources
        -> analyze (LLM-assisted draft, human review)
        -> publish (static site + PDF)
```

- **Language:** Python 3.12+
- **Storage:** SQLite (`data/ledger.db`)
- **Site:** Eleventy (static, Markdown content)
- **PDF briefs:** Markdown to PDF via WeasyPrint or Pandoc
- **Automation:** GitHub Actions daily; deploy to GitHub Pages
- **Human in the loop:** nothing publishes without review. LLM output is always a draft.

### Repo layout
```
loretta-ledger/
  README.md
  pyproject.toml
  config/
    sources.yaml        # core feeds + upstream trigger sources
    topics.yaml
    upstream.yaml       # influence registry: actors, aliases, URLs
  pipeline/
    ingest/
    normalize.py
    classify.py
    upstream/
      detect.py         # registry/alias/citation detection
      fetch.py          # pull triggered upstream sources
      similarity.py     # text similarity vs model policies / peer docs
      chains.py         # build influence chains
    analyze.py
    export.py
  prompts/
    brief.md
    action_page.md
    test_case.md
    influence_page.md
  data/
  site/src/{items,briefs,test-cases,influences}/
  briefs/
  tests/
  .github/workflows/
```

## 6. Data model (SQLite)

**items** — `id`, `source_id`, `jurisdiction` (`olympia` | `thurston`), `title`, `url`, `published_at`, `body_text`, `meeting_date`, `comment_deadline`, `status`, `hash`, `created_at`

**item_topics** — `item_id`, `topic`

**upstream_actors** — `id`, `name`, `upstream_type`, `aliases` (JSON), `home_url`, `notes`

**upstream_docs** — `id`, `actor_id`, `title`, `url`, `published_at`, `body_text`, `doc_kind` (`statute` | `rule` | `guidance` | `model_policy` | `ruling` | `peer_ordinance` | `other`)

**upstream_refs** — `id`, `item_id`, `actor_id`, `upstream_doc_id` (nullable), `mechanism`, `evidence_url`, `evidence_ref`, `confidence`, `parent_ref_id` (nullable), `created_at`

**similarity_matches** — `id`, `item_id`, `upstream_doc_id`, `score`, `matched_passages` (JSON), `reviewed` (bool)

**test_cases** — `item_id`, `flagged_reason`, `status`, `notes`, `updated_at`

**drafts** — `item_id`, `kind` (`action_page` | `brief` | `test_case` | `influence_page`), `markdown`, `reviewed` (bool), `reviewed_at`

## 7. Sources (`config/sources.yaml`)

Each source: name, jurisdiction, type (`rss` | `html` | `api`), URL, poll frequency, `role` (`core` | `upstream_trigger`).

Core (verify each URL and format in Phase 1; do not assume):
- Olympia: Granicus/Legistar agendas and meetings
- Thurston County: commissioner agendas, planning commission, Public Health & Social Services (wells/septic)

Upstream trigger sources (fetched only when triggered, see Section 2):
- WA Dept of Ecology rulemaking and comment listings
- WA Dept of Commerce growth management pages
- Thurston Regional Planning Council
- Growth Management Hearings Board decisions
- Revised Code of Washington / Washington Administrative Code for cited statutes and rules
- Other jurisdictions' public documents, only when referenced or matched

Respect robots.txt and rate limits. Cache raw responses.

## 8. Phases

**Phase 1 — Ingest (MVP)**
1. Scaffold repo, `pyproject.toml`, SQLite schema (including upstream tables, unused for now).
2. Olympia (Legistar/Granicus) ingester.
3. Dedupe and store. CLI: `python -m pipeline.ingest --all`.
4. Tests with saved fixtures.
*Done when:* the CLI populates `items` with real recent Olympia entries.

**Phase 2 — Thurston County ingest**
Same pattern for Thurston County sources.

**Phase 3 — Classify**
1. Keyword topic tagging from `topics.yaml`.
2. Extract meeting dates and comment deadlines.
3. Test-case flagging (rules first, LLM assist second).

**Phase 4 — Upstream detection (citations and registry)**
1. Seed `upstream.yaml` with a starter registry (GMA, Commerce, Ecology, TRPC, GMHB, a few known policy groups).
2. `detect.py`: find registry names/aliases and statute/rule citations (RCW, WAC) in item text; create `upstream_refs` with `confidence = documented` and an evidence reference.
3. `fetch.py`: pull cited upstream documents into `upstream_docs`.
4. Detect mechanism phrases ("required by," "pursuant to," "grant conditions," "consistent with") to suggest a `mechanism`; unknown if unclear.
*Done when:* items that cite an upstream source show a linked, evidenced record.

**Phase 5 — Chains and similarity**
1. `chains.py`: link refs via `parent_ref_id` (e.g. ordinance -> rule -> statute).
2. `similarity.py`: compare local documents against model policies and peer-jurisdiction documents; store matches with passages for human review.
3. Pattern alerts for repeated actors/templates.
*Done when:* a local item can display a multi-step lineage and any flagged language matches.

**Phase 6 — Draft generation and review**
1. Prompt templates: action pages, briefs (with "Who's behind this?"), test-case notes, influence pages.
2. Drafts saved with `reviewed = false`.
3. Prompts must: cite source URLs, separate facts from analysis, invent nothing, follow the tone rule.
4. Review CLI: list, approve, edit, reject (including similarity matches).

**Phase 7 — Site, PDFs, automation**
1. Eleventy site: latest and upcoming deadlines, by topic, test-case tracker, brief archive, influence pages, lineage views.
2. Action pages: what it is, key date, how to comment, plain-language effect, upstream line.
3. Only `reviewed = true` drafts render.
4. PDF brief template.
5. GitHub Actions: daily ingest, classify, and upstream detection; deploy on merge.

## 9. Editorial rules (encode in prompts and review checklist)

- Facts first, with source link. Analysis clearly labeled.
- Never state a deadline or vote outcome that is not in the source.
- No invented quotes. Quote accurately or paraphrase.
- Name tradeoffs honestly, including where a policy has merit.
- Always give citizens a concrete next step (date, email, hearing link).
- Upstream claims: documented or labeled inferred. Never imply motive or coordination.

## 10. Out of scope (for now)

Briefs for any jurisdiction other than Olympia and Thurston County, accounts or comments on the site, email newsletter, paid tiers. Revisit after Phase 7.

