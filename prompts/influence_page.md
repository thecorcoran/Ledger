# Upstream Influence Dossier Prompt

Draft an Upstream Actor Influence Dossier for Loretta's Ledger.

## Actor
- **Name**: {{ actor.name }}
- **Type**: {{ actor.upstream_type }}
- **Home URL**: {{ actor.home_url }}
- **Notes**: {{ actor.notes }}

## Documented Influences on Olympia & Thurston
{% for ref in refs %}
- **Date**: {{ ref.meeting_date }}
- **Local Item**: {{ ref.item_title }} ({{ ref.jurisdiction }})
- **Mechanism**: {{ ref.mechanism }}
- **Evidence**: {{ ref.evidence_ref }} ([Source]({{ ref.evidence_url }}))
- **Confidence**: {{ ref.confidence }}
{% endfor %}

---

## Instructions
1. Summarize the role and legal/institutional authority of this upstream entity in shaping local policy.
2. Outline observed patterns across the documented items (e.g. repeated grant conditions, mandate compliance deadlines, model code language).
3. Keep tone objective and descriptive. Never assert conspiracy, bad faith, or coordinated motives.

