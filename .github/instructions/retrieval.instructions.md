---
name: 'Read path (embeddings, retrieval, agents, cli)'
description: 'Rules for the code that queries the graph and assembles context'
applyTo: '{embeddings,retrieval,agents,cli}/**'
---

# You are editing the read path

Failures here do not corrupt the graph — they return something **plausible and
wrong**, which is harder to notice.

- **A retrieval that silently returns nothing is a bug, not an empty result.**
  If a traversal matches zero nodes, establish whether that is true or whether
  the query is wrong. `MATCH` failing silently is how a scoping bug hides.
- **Say what a result rests on.** A context bundle citing file paths and line
  numbers can be checked; one that paraphrases code cannot.
- **Never invent a citation.** If the graph doesn't have the answer, the answer
  is that the graph doesn't have it.

## Scope every query to one repository

The database holds many repositories. An unscoped
`MATCH (fn:Function {name: "run"})` returns matches from all of them and looks
perfectly correct on a database that only has one.

Cross-repo questions are real and worth supporting deliberately — "which repos
import `requests`", "what has this developer touched" — but they are opt-in.
`Module` and `Developer` are global precisely to make them possible; everything
else scopes.

**Test against a database with at least two repositories indexed.**

## Read the schema, do not change it

Node labels, properties and relationship types are documented in `README.md`
and owned by the write path (`ingest/`, `graph/`). If a query you need is
awkward or impossible, say so and hand it over rather than adding a property
from this side — a schema written from two directions stops being a contract.

A query that only works because of an undocumented property will break on the
next reindex.

## Local models

Ollama runs on `localhost:11434`. If a model isn't available, say so and stop —
do not silently degrade to a worse answer, and do not escalate to a paid lane
the caller didn't ask for.

## Verifying

`.venv\Scripts\python.exe -m pytest tests`, then run the actual query path
against a real indexed graph and show what came back. A retrieval test that
only asserts "returned something" is not a test.
