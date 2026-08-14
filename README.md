# Repo Intelligence

A local, token-efficient codebase intelligence layer for coding agents (Claude Code, GitHub Copilot, etc.).

## Why

Coding agents repeatedly re-explore a repository to answer questions like "what calls this function?" or
"which files import this module?" — burning tokens on rediscovery every session. Repo Intelligence moves
that understanding into a local structured knowledge layer (a code graph + vector index) so agents can be
handed a small, precise, pre-computed context package instead of re-reading the whole repo.

```
Repository → Ingestion → Neo4j (structure) + Chroma (semantics) → Retrieval → compact context → LLM
```

## Architecture

- **Ingestion** (`ingest/`) — parses source files (Python AST today, Tree-sitter/YAML later), Databricks/Jupyter
  notebooks, PySpark/SQL table lineage, and Git history.
- **Neo4j** (`graph/`) — stores repository structure and relationships (files, functions, classes, imports, calls, tables).
- **Chroma** (`embeddings/`) — semantic search over code/doc embeddings.
- **Retrieval** (`retrieval/`, planned) — combines graph traversal + semantic search into a compact context package.
- **Ollama** (planned) — local LLM (`qwen2.5-coder:7b`) for summarization/classification, never for full-repo exploration.
- **CLI** (`cli/`) — Typer-based entrypoints.

Neo4j answers "what is structurally related?" (deterministic, explainable). Chroma will answer "what is
semantically similar?". Every piece of retrieved context should be traceable back to a file/line/function/commit.

## Project Structure

```
repo-intelligence/
├── ingest/          # parse_code.py (AST parser), git_history.py (git ingestion)
├── graph/           # neo4j_loader.py — loads parsed data into Neo4j
├── tests/           # pytest suite
├── embeddings/       # planned: Chroma embedding pipeline
├── retrieval/        # planned: graph + vector retrieval layer
├── agents/           # planned: agent-facing context assembly
├── cli/              # main.py — Typer CLI
├── data/, cache/      # local working data (excluded from indexing)
└── README.md
```

## Local Setup

1. Python virtual environment (`.venv`) on Windows.
2. `pip install -r requirements.txt` — or `requirements-test.txt` for just the test suite,
   which avoids pulling `sentence-transformers` and torch with it.
3. Neo4j running locally (Desktop or Docker) — see below.
4. (Later) Ollama running locally with `qwen2.5-coder:7b` pulled.

### Neo4j Setup

- Bolt URI: `neo4j://127.0.0.1:7687`
- Database: `repo-intelligence` (the database name, not the Desktop project name)

All four settings read an environment variable first and fall back to the value in
`graph/neo4j_loader.py`:

| Variable | Default |
| --- | --- |
| `REPO_INTEL_NEO4J_URI` | `neo4j://127.0.0.1:7687` |
| `REPO_INTEL_NEO4J_USER` | `neo4j` |
| `REPO_INTEL_NEO4J_PASSWORD` | `repo12345` |
| `REPO_INTEL_NEO4J_DATABASE` | `repo-intelligence` |

> **Community Edition / Aura Free**: these support exactly one user database and it
> must be named `neo4j` — `CREATE DATABASE` is Enterprise-only. Set
> `REPO_INTEL_NEO4J_DATABASE=neo4j` or the loader will fail at connect with
> *Database does not exist*. The default suits Neo4j Desktop, which bundles
> Enterprise for local development.

## CLI / Indexing Workflow

Parse a repository (prints a preview, does not write to Neo4j):

```
python ingest\parse_code.py .
```

Load the parsed repository into Neo4j:

```
python graph\neo4j_loader.py
```

This always indexes the current working directory (`index_repository(".")`), so run it from the repo root
you want to index. `.venv`/`venv`/`env`, `__pycache__`, `.git`, `cache`, and `data` are excluded from parsing.

## Current Graph Schema

Nodes:

```
(:Repository {path, name, remote})
(:File       {path, repo})
(:Function   {name, qualname, file, line, end_line, calls, repo})
(:Class      {name, file, line, repo})
(:Module     {name})                      # global, shared across repositories
(:Commit     {repo, hash, message, author_name, author_email, authored_at})
(:Developer  {email, name})               # global, shared across repositories
(:Issue      {repo, number})
(:Notebook   {path, repo})                # repo-scoped, like File - a .py Databricks export or a .ipynb
(:Table      {qualified_name, name, qualified, repo?})
    # global when `qualified` is true (a real catalog.schema.table is one physical object
    # shared across repos, like Module). `repo` is only set when `qualified` is false: an
    # unqualified reference like `transactions` folds the indexing repo's identity into
    # `qualified_name` so it can never MERGE with an unrelated repo's same-named table -
    # see table_merge_key() in graph/neo4j_loader.py.
```

Relationships:

```
(:Repository)-[:HAS_FILE]->(:File)
(:Repository)-[:HAS_COMMIT]->(:Commit)
(:File)-[:CONTAINS]->(:Function)      # top-level functions
(:File)-[:DEFINES]->(:Class)
(:Class)-[:CONTAINS]->(:Function)     # methods
(:File)-[:IMPORTS]->(:File)           # import resolves to another file in this repo
(:File)-[:IMPORTS]->(:Module)         # import is external (stdlib/3rd-party)
(:File)-[:IS_NOTEBOOK]->(:Notebook)   # the parser recognized this file as a notebook
(:Function)-[:CALLS]->(:Function)     # naive, name-based matching (no type/scope resolution yet)
(:Notebook)-[:CALLS]->(:Function)     # sourced from the owning File's module-level (top-level) calls
(:Function)-[:READS]->(:Table)        # spark.table(...)/.sql("SELECT ... FROM ...") inside a def/method
(:Function)-[:WRITES]->(:Table)       # .saveAsTable(...)/.insertInto(...)/SQL INSERT-MERGE-UPDATE-DELETE, inside a def/method
(:File)-[:READS]->(:Table)            # the same access, but at module/notebook top level - no synthetic
(:File)-[:WRITES]->(:Table)           # Function is minted to hold it; see the note below
(:Commit)-[:MODIFIES]->(:File)
(:Commit)-[:CHANGES]->(:Function)     # which functions the commit's diff actually touched
(:Commit)-[:AUTHORED_BY]->(:Developer)
(:Commit)-[:REFERENCES]->(:Issue)     # parsed from "#12" / "Fixes #12" in the message
```

### Why module-level table access attaches to `File`, not `Function`

A Databricks notebook's lineage mostly lives at the top level of the file (or notebook cell), not inside a
`def`. That code has no `Function` node to attach a `READS`/`WRITES` edge to, and minting a synthetic
`"<module>"` Function for it would inflate the `Function` count of every file with any top-level code -
notebook or not - across every repository this tool has ever indexed, silently changing what a "how many
functions" or "which functions" query returns. Module-level table access is attached to the owning `:File`
instead: no new node label, no inflated count, same `READS`/`WRITES` relationship types. Access inside a
real function or method still attaches to that `Function`, as it always has.

### How `CHANGES` is derived

For each commit, the diff is taken with **zero context lines** (`-U0`) and the file is parsed *as it existed
at that commit*, not as it exists today. Changed line ranges are matched against that revision's function
ranges, and the resulting **qualified** names are linked to the current `Function` nodes.

Each of those choices fixes a specific misattribution:

| Choice | Without it |
| --- | --- |
| Zero context lines | A hunk's range spills into the functions either side of the edit and blames them for code the commit never touched |
| Parse at the commit, not today | Every commit made before the code moved is attributed to whatever now occupies those line numbers |
| Qualified names (`Class.method`) | Two classes in one file that both define `run()` collapse into one target, and editing either blames both |
| Ranges start at the first decorator | A decorator-only change falls outside the `def` range and is attributed to nothing |
| Deletions resolved against the **parent** revision | A removed block has no position in the new file. Mapping it to the nearest new-side line blames whichever function precedes the gap — deleting a function would blame the one above it |
| Merge commits claim nothing | Diffing a merge against its first parent reports the whole merged branch as its work, double-counting every change and returning whoever pressed merge as the author |

Functions deleted since a commit have no node to link to and are skipped, rather than being resurrected
into the code graph.

`Function.calls` is a temporary list of call names captured during parsing; `link_calls()` in
`graph/neo4j_loader.py` uses it to build `CALLS` edges by matching names across all indexed functions. This
is intentionally naive for the MVP — it does not resolve overloaded/duplicate names, imports, or `self.`
scoping precisely.

## Example Cypher Queries

Which functions are in a file:

```cypher
MATCH (f:File {path: "path/to/file.py"})-[:CONTAINS]->(fn:Function)
RETURN fn.name, fn.line
```

Which files import a given file:

```cypher
MATCH (f:File)-[:IMPORTS]->(t:File {path: "path/to/file.py"})
RETURN f.path
```

What does a function call, and what calls it:

```cypher
MATCH (fn:Function {name: "index_repository"})-[:CALLS]->(callee)
RETURN callee.name, callee.file
```

```cypher
MATCH (caller)-[:CALLS]->(fn:Function {name: "parse_python_file"})
RETURN caller.name, caller.file
```

Classes and their methods:

```cypher
MATCH (f:File)-[:DEFINES]->(c:Class)-[:CONTAINS]->(m:Function)
RETURN f.path, c.name, m.name
```

Who last changed a function:

```cypher
MATCH (c:Commit)-[:CHANGES]->(fn:Function {name: "link_calls"})
MATCH (c)-[:AUTHORED_BY]->(d:Developer)
RETURN d.name, c.authored_at, c.message
ORDER BY c.authored_at DESC
LIMIT 1
```

Which files changed when an issue was worked on:

```cypher
MATCH (i:Issue {number: 1})<-[:REFERENCES]-(c:Commit)-[:MODIFIES]->(f:File)
RETURN DISTINCT f.path
```

What a developer has touched, across every indexed repository:

```cypher
MATCH (d:Developer {email: "you@example.com"})<-[:AUTHORED_BY]-(c:Commit)
RETURN c.repo, count(c) AS commits
ORDER BY commits DESC
```

Which repositories depend on a third-party module:

```cypher
MATCH (r:Repository)-[:HAS_FILE]->(:File)-[:IMPORTS]->(m:Module {name: "neo4j"})
RETURN DISTINCT r.name
```

## Token-Efficiency Strategy

Instead of letting an agent search the whole repository, Repo Intelligence answers structural questions
(what calls what, what imports what, what's in a file) via graph traversal, and will answer semantic
questions (what code relates to this idea) via Chroma. The retrieval layer assembles a small, explainable
context package (task, relevant files/functions/tables, dependencies, recent Git changes) instead of
sending full file contents or the whole repo to the LLM.

## Roadmap

1. **Code graph** — File/Function/Class/Module nodes; CONTAINS, DEFINES, IMPORTS, CALLS. ✅
2. **Git history** — Commit/Issue/Developer nodes; MODIFIES/CHANGES/REFERENCES/AUTHORED_BY relationships. ✅
3. Databricks/SQL awareness (current stage) — READS/WRITES relationships to Delta tables, notebooks, jobs, Unity Catalog objects.
4. Chroma embeddings for semantic retrieval.
5. Retrieval layer combining graph + vector search into compact context packages.
6. Ollama-backed summarization/classification for retrieval and agent context.

## Agent Team

This repo is worked on by a small Claude Code agent team defined in
`.claude/agents/`. Five roles, split by the surfaces this project actually has:

| Agent | Owns | Runs on |
| --- | --- | --- |
| `orchestrator` | Routes work, opens PRs, merges to `master`. Delegates only — no `Edit`/`Write` | Claude Code |
| `indexer` | The write path — `ingest/`, `graph/` | **Copilot** by default |
| `retrieval` | The read path — `embeddings/`, `retrieval/`, `agents/`, `cli/` | **Copilot** by default |
| `reviewer` | Adversarial read on escalation paths. Read-only, never merges | Claude Code |
| `scout` | Cheap briefs and read-only graph queries; reading routed to local Ollama | Haiku + MCP |

`indexer` and `retrieval` exist on both substrates — as Copilot agents in
`.github/agents/` and as Claude Code subagents in `.claude/agents/`. Both read
`CLAUDE.md`, so the rules are identical and only the cost differs;
implementation work defaults to Copilot. `.github/instructions/*.instructions.md`
carries the same rules into plain Copilot chat through `applyTo` globs, so
editing anything under `graph/` picks them up without selecting an agent.

- `CLAUDE.md` — the shared contract, loaded automatically by both Copilot and Claude Code
- `docs/agent_org_chart.md` — the roster, the two substrates, and why it is five roles rather than seven
- `docs/agent_governance.md` — the tiers, the escalation paths, and the evidence behind them

`orchestrator` merges into `master` on its own authority once the merge
conditions in `.claude/agents/orchestrator.md` are met. What stays with the
repo owner is what a revert cannot undo — credentials, `master` history
rewrites, repository settings, and anything published outside this repo.

`reviewer` gates the paths where a wrong change **reports success and produces
a wrong graph** — destructive Cypher, node keys, repository scoping, and file
exclusion. That list is drawn from defects this repo actually shipped, not
from a template. Because nothing reviews a merge after `orchestrator`, on
those paths the `reviewer` verdict is the last check rather than the first of
two.

`scout` overlaps with the [`context-scout`](https://github.com/yashmhatre/ingredion-agent-config)
MCP server; prefer the MCP tools when a single call answers the question.

## Troubleshooting

- **Graph has far more nodes than expected**: check for stray virtual-environment folders (e.g. a leftover
  `.venv-1`) or other large non-project directories that aren't excluded — extend the exclusion list in
  `ingest/parse_code.py` (`EXCLUDE_PREFIXES`/`EXCLUDE_EXACT`), which both the parser and loader share via
  `list_python_files()`.
- **Counts change unexpectedly between runs**: make sure no other `graph/neo4j_loader.py` process is still
  running in the background (check for stray `python.exe` processes) and writing stale data concurrently.
- **`ModuleNotFoundError: No module named 'neo4j'`**: you're likely running the system Python instead of
  `.venv\Scripts\python.exe`.
