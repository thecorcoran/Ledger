# Test-Case Watch Prompt

Analyze whether the following policy item qualifies as a Test Case under Loretta's Ledger criteria.

## Criteria for Test Cases
Flag when matching any of:
1. First application of a new ordinance, rule, or plan.
2. A permit, appeal, or lawsuit challenging a new policy.
3. A variance or exception others will cite.
4. A land-use, well, or water decision affecting a small owner or family farm.
5. A hearing examiner or Growth Management Hearings Board decision involving local policy.
6. A pilot program or one-off agreement that could become standard.
7. A local decision where an upstream mandate or template is being applied for the first time.

## Item
- **Title**: {{ item.title }}
- **Jurisdiction**: {{ item.jurisdiction }}
- **Text**:
{{ item.body_text }}

---

## Output Format:
```markdown
# Test-Case Watch: {{ item.title }}

- **Status**: [watching | decided | appealed | closed]
- **Precedent Factor**: [Which criterion applies]
- **Why This Matters**: [Analysis of precedent risks or significance for small owners]
- **Key Actors & Venue**: [Decision maker, appellants, affected parties]
```

