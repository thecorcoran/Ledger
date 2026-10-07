# Loretta's Ledger

A local policy publication providing consistent, evidence-based policy briefs for the **City of Olympia** and **Thurston County**, analyzed through a Distributist / Chestertonian lens (subsidiarity, widely held property, family and local economy, skepticism of concentrated power).

## Key Features
- **Litmus Test Analysis:** Consistent evaluation of policies across 8 core principles (subsidiarity, ownership, small/local vs large/distant, family/household, cost/burden, consent/process, reversibility/accountability, place).
- **Upstream Influence Tracking:** Lineage tracking of where local ordinances and rules originate (mandates, funding conditions, model policies, state agencies, statutes).
- **Test-Case Watch:** Early alerting for precedent-setting land use, water, and local property decisions.

## Architecture & Quick Start
The project uses Python 3.12+ with SQLite for data pipeline storage and Eleventy for static site generation.

```bash
# Ingest local feeds (Phase 1)
python3 -m pipeline.ingest --all
```

