# Secret Agent — Connector Interchange Format v0.1

**The contract between connectors (raw company data → files) and the memory pipeline
(files → claims → graph → agent).** If a connector emits files that follow this page,
the rest of the system works without knowing where the data came from.

Status: DRAFT for team agreement. Owners: connectors (ingest side), extraction (consume side).

## The one rule

> A connector's job is to turn one unit of company communication into **one markdown
> file with YAML frontmatter**. Nothing else. No summarizing, no interpretation, no
> claim extraction — that happens downstream where it can be reviewed and re-run.

Markdown + frontmatter is the interchange because it is (a) human-auditable — a company
admin can open any file and see exactly what the system knows, (b) gbrain's native input,
and (c) already proven by the Clarion reference corpus (`clarion-corpus/sources/**`).

## Required frontmatter (every file)

```yaml
---
id: src0142            # unique, stable per document; connector-assigned, never reused
type: slack            # one of the company schema pack's declared types
date: 2026-08-03       # YYYY-MM-DD; the document's own date, not the ingest date
title: "#claims-ops: UHC reconsideration deadline"
participants: ["[[people/elena-vasquez]]", "[[people/dana-ortiz]]"]
---
```

- `type` MUST come from the company's schema pack (e.g. `schema/clarion-v1.yaml`
  declares slack/meeting/ticket/email/sop/… for Clarion; a different company declares
  different types). A connector encountering an undeclared type must fail loudly, not
  invent one.
- `participants` are wikilinks into the company entity registry. If a person can't be
  resolved to a registered entity, use the raw string and add `unresolved: true` —
  the extraction stage's entity-identity pass handles it. **Never guess.**

## Optional frontmatter (when the source has it)

| Field | Types | Example |
|---|---|---|
| `channel` | slack | `"#claims-ops"` |
| `ticket`, `status`, `assignee`, `reporter`, `client` | ticket | see `clarion-corpus/sources/tickets/` |
| `from`, `to` | email | wikilinks or raw addresses |
| `status` / `superseded_by` / `superseded_scope` | sop, any doc | marks stale docs — load-bearing for change detection |

Unknown extra fields are preserved, never dropped.

## Body rules

1. **Verbatim over pretty.** Keep the original utterance structure: speaker labels and
   timestamps for chat/meetings (`**Elena Vasquez** [11:35]`), comment blocks for tickets.
   The claim extractor anchors quotes to lines — paraphrasing destroys provenance.
2. **Wikilinks for entity mentions** where the connector can resolve them cheaply
   (`[[clients/lakeview-orthopedics]]`); plain text otherwise. First mention per file
   is enough. Links resolve by path-suffix against the registry.
3. **One document = one file.** A Slack thread is one file; a ticket with its comment
   history is one file; an email thread is one file. Don't split, don't merge days.
4. **Redaction happens in the connector** (the only stage that sees raw data): strip
   credentials/PHI per company policy *before* the file is written. Downstream stages
   assume files are safe to index.

## File layout a connector must produce

```
<company>/sources/<type-dir>/<date-or-key>-<slug>.md     # e.g. sources/slack/2026-08-03-uhc-timely-filing.md
<company>/schema/<pack>.yaml                              # declared once at onboarding, not per-run
```

## What consumes this (so connectors don't have to)

`onboard/extract.py` (Phase 2): claims extraction (who said what, when, stance) →
entity linking → supersedes/contradiction candidates → human review → graph build →
QA (`validate.py` generic checks). The Clarion corpus is the golden example of the
*output* of this whole pipeline; connectors only owe the input format on this page.

## Open questions for the team

1. Incremental sync: connector re-emits a changed file with the same `id` — agreed?
2. Attachments/binaries: out of scope v1 (text only) — agreed?
3. `id` scheme per connector (`slack-<ts>`, `jira-<key>`…) — propose in the connector PR.
