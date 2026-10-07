# Action Page Prompt

You are drafting an Action Page for citizens of Olympia or Thurston County.
Action pages are plain-language, rapid summaries intended to empower local residents to participate before decisions are finalized.

## Item
- **Title**: {{ item.title }}
- **Jurisdiction**: {{ item.jurisdiction }}
- **Meeting Date**: {{ item.meeting_date }}
- **Comment Deadline**: {{ item.comment_deadline }}
- **Source Link**: {{ item.url }}
- **Details**:
{{ item.body_text }}

{% if item.upstream_refs %}
## Upstream Influence
{% for ref in item.upstream_refs %}
- {{ ref.actor_name }} ({{ ref.mechanism }}): {{ ref.evidence_ref }}
{% endfor %}
{% endif %}

---

## Instructions
1. Write clearly for ordinary working residents, property owners, and local businesses.
2. Clearly explain:
   - What is happening.
   - When and where it will be decided.
   - Plain-language effect on families, property owners, and pocketbooks.
   - Who is driving this (upstream mandates, funding strings, or local staff).
   - How to comment or attend (specific deadline, email, portal link).
3. Do not invent dates or contact info. If missing from source, note where to check.

## Required Output Format:

```markdown
# Citizen Action: {{ item.title }}

**Key Date**: {{ item.meeting_date }}  
**Comment Deadline**: {{ item.comment_deadline or 'Check agenda for sign-up deadline' }}  
**Jurisdiction**: {{ item.jurisdiction | capitalize }}  

### What's Happening
[2-3 concise sentences explaining the proposal]

### How It Affects You
[Direct practical impacts on residents, homeowners, renters, or small business owners]

### Who's Behind This?
[Upstream state, regional, or funding source, or "Local initiative"]

### How to Have Your Say
- **Public Hearing / Meeting**: [Details]
- **Submit Written Comments**: [Email / link]
- **Official Agenda Packet**: [View Documents]({{ item.url }})
```

