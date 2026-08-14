---
name: 'Write path (ingest, graph)'
description: 'Rules for the code that builds the Neo4j graph'
applyTo: '{ingest,graph}/**'
---

# You are editing the write path

Failures here **corrupt data**, and this project's defects have all been silent
ones — a wrong graph that reported success.

- **Predict the node and relationship counts, then check them.** "Indexed
  successfully" is not a result. This repo once shipped a graph 30x too large
  while reporting success.
- **Check for stray `python.exe` indexer processes** before trusting a count.
  One left running from an earlier session keeps writing stale data.
- Use `.venv\Scripts\python.exe`, never the system Python.

## Multi-repo invariants — do not break these

One Neo4j database holds many repositories:

- `--clean` wipes **only** the repository being indexed.
- `CALLS` edges are built within one repository. Two repos that both define
  `run()` must never cross-link.
- `Issue` is keyed on `(repo, number)`; `Commit` on `(repo, hash)`. Two clones
  of one remote share commit hashes, so a globally-keyed `Commit` lets a clean
  on one repo delete another's history.
- `Module` and `Developer` are **deliberately global** — a dependency and a
  person are the same entity everywhere. Do not scope them.

**Test any scoping change with two repositories indexed.** A scoping bug is
invisible on one, every time.

## Attribution rules that are load-bearing

`CHANGES` maps a commit to the functions it touched. Each of these prevents a
specific misattribution — do not "simplify" them away:

- Diffs are taken with **zero context** (`unified=0`); default context spills a
  hunk into neighbouring functions.
- Files are parsed **as they existed at that commit**, not as they exist now.
- Names are **qualified** (`Class.method`); bare names cross-attribute.
- Ranges start at the **first decorator**, not at `def`.
- **Pure deletions resolve against the parent revision** — mapping them onto
  the nearest new-side line blames the preceding function.
- **Merge commits claim nothing**; their branch's own commits already carry it.

## Escalation

Destructive Cypher, `MERGE` keys, `CONSTRAINTS`/`INDEXES`, repository scoping,
and the file-exclusion lists need an independent review before merge. Flag the
change rather than merging it yourself. See `docs/agent_governance.md`.
