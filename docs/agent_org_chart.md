# Agent org chart

```mermaid
graph TD
    Yash["Yash — owner<br/>(human; merges to master, Tier 2 only)"]
    O["orchestrator (Claude)<br/>routes work, opens PRs,<br/>assembles the merge packet"]
    I["indexer (Claude/Copilot)<br/>ingest/ + graph/ — the write path"]
    R["retrieval (Claude/Copilot)<br/>embeddings/ retrieval/ agents/ cli/ — the read path"]
    V["reviewer (Claude)<br/>adversarial read on escalation paths"]
    S["scout (local model)<br/>issue/git/graph summarization — utility, not in reporting line"]

    Yash --> O
    O --> I
    O --> R
    O --> V
    I -->|escalation paths| V
    R -->|escalation paths| V
    S -.->|callable by any agent, or Yash directly| O
    S -.-> I
    S -.-> R
    S -.-> V
```

**Five roles**, split by the surfaces this repo actually has: a write path
that builds the graph, a read path that queries it, a reviewer for the
changes that fail silently, a router, and a free local summarizer.

## Why five and not seven

This roster is adapted from the one in
[`ingredion-agent-config`](https://github.com/yashmhatre/ingredion-agent-config),
but it is not a copy. That project has seven roles because it has a
production Delta pipeline, deployed Databricks notebooks with two known live
defects, bundle configs that decide which identity production runs as, and a
`dev`/`staging`/`main` promotion chain. Every one of those roles maps to a
surface that can break something outside the repo.

This repo has none of that. It is a local developer tool with one long-lived
branch and no deployment. Three roles were dropped because the surface they
guard does not exist here:

- **`platform`** — no `databricks.yml`, no bundle targets, no service
  principals, no cloud provisioning of any kind.
- **`architect`** — the design authority for a solo tool with a five-stage
  roadmap in `README.md` is the person reading the roadmap. A role whose only
  output is a design doc handed to one implementer is the ceremony that
  project already cut once.
- **`notebook-qa`** — no notebooks.

Carrying those over would repeat exactly the mistake that project's own org
chart describes fixing: roles that exist to relay work rather than to own a
surface.

## Where authority sits

**The boundary is the pull request.** Everything up to an open PR belongs to
the agent team; merging to `master` belongs to Yash. There is no `dev` branch
here to absorb a bad merge, so the merge itself is the irreversible step and
stays with the human. See `docs/agent_governance.md` for the tiers.

`orchestrator` is the one funnel below Yash — every other agent is reachable
through it. It has no `Edit` or `Write` tool, deliberately.

`reviewer` never merges and never reviews work it produced itself. On this
repo its trigger list is short and evidence-based: destructive Cypher, node
keys and schema, repository scoping, and file-exclusion logic. Those are the
paths where a wrong change **reports success and produces a wrong graph** —
the failure mode that actually bit this repo, twice, on 2026-08-14. Ordinary
code changes do not need it.

## The write/read split

`indexer` and `retrieval` divide on the direction data flows, not on
subsystem tidiness:

- `indexer` **writes** the graph — parsing, schema, loading, Cypher that
  mutates. Its failures corrupt data.
- `retrieval` **reads** it — embeddings, vector search, context assembly, the
  CLI. Its failures return a bad answer but leave the graph intact.

Those are different risk profiles and different review needs, which is why
the split is here rather than, say, between `ingest/` and `graph/`. Those two
are one transaction — a parser field is useless until the loader persists it
— so they stay with one agent.

## scout — a utility, not a report

`scout` sits outside the hierarchy on purpose. It reports to no one and no
one reports to it: it is a shared, free pre-processing step that any agent,
or Yash directly, calls before spending Claude tokens on raw material. It
runs on a local Ollama model, is read-only, and cannot spawn other agents.

On this repo it has one capability the Ingredion version does not: **read-only
Cypher against the local graph.** Answering "which functions call this one"
from the graph is cheaper and more accurate than reading the source, and this
is the one repo where that index is guaranteed to exist.

Much of `scout`'s issue and CI work is already available without a subagent
at all, through the `context-scout` MCP server from that same config repo —
`context_scout_summarize_issue`, `context_scout_git_context`,
`context_scout_pr_ci_status`. **Prefer the MCP tools when they cover the
question**; they are a direct call with no subagent turn around them. Use the
`scout` agent when the work needs several such lookups stitched together, or
needs a graph query.

**Its output is an index, never evidence.** A small local model produces
fluent, confident, wrong summaries. It tells you where to look. If a decision
rests on a `scout` summary being right, verify it first.
