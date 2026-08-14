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

- **Ingestion** (`ingest/`) — parses source files (Python AST today, Tree-sitter/SQL/YAML later) and Git history.
- **Neo4j** (`graph/`) — stores repository structure and relationships (files, functions, classes, imports, calls).
- **Chroma** (`embeddings/`, planned) — semantic search over code/doc embeddings.
- **Retrieval** (`retrieval/`, planned) — combines graph traversal + semantic search into a compact context package.
- **Ollama** (planned) — local LLM (`qwen2.5-coder:7b`) for summarization/classification, never for full-repo exploration.
- **CLI** (`cli/`) — Typer-based entrypoints.

Neo4j answers "what is structurally related?" (deterministic, explainable). Chroma will answer "what is
semantically similar?". Every piece of retrieved context should be traceable back to a file/line/function/commit.

## Project Structure

```
repo-intelligence/
├── ingest/          # parse_code.py (AST parser), git_history.py (planned)
├── graph/           # neo4j_loader.py — loads parsed data into Neo4j
├── embeddings/       # planned: Chroma embedding pipeline
├── retrieval/        # planned: graph + vector retrieval layer
├── agents/           # planned: agent-facing context assembly
├── cli/              # main.py — Typer CLI
├── data/, cache/      # local working data (excluded from indexing)
└── README.md
```

## Local Setup

1. Python virtual environment (`.venv`) on Windows.
2. `pip install -r requirements.txt`
3. Neo4j running locally (Desktop or Docker) — see below.
4. (Later) Ollama running locally with `qwen2.5-coder:7b` pulled.

### Neo4j Setup

- Bolt URI: `neo4j://127.0.0.1:7687`
- Database: `neo4j` (the database name, not the Desktop project name)
- Credentials are set in `graph/neo4j_loader.py` (`USER`/`PASSWORD`) — update for your local instance.

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
(:File   {path})
(:Function {name, file, line, calls})
(:Class  {name, file, line})
(:Module {name})
```

Relationships:

```
(:File)-[:CONTAINS]->(:Function)      # top-level functions
(:File)-[:DEFINES]->(:Class)
(:Class)-[:CONTAINS]->(:Function)     # methods
(:File)-[:IMPORTS]->(:File)           # import resolves to another file in this repo
(:File)-[:IMPORTS]->(:Module)         # import is external (stdlib/3rd-party)
(:Function)-[:CALLS]->(:Function)     # naive, name-based matching (no type/scope resolution yet)
```

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

## Token-Efficiency Strategy

Instead of letting an agent search the whole repository, Repo Intelligence answers structural questions
(what calls what, what imports what, what's in a file) via graph traversal, and will answer semantic
questions (what code relates to this idea) via Chroma. The retrieval layer assembles a small, explainable
context package (task, relevant files/functions/tables, dependencies, recent Git changes) instead of
sending full file contents or the whole repo to the LLM.

## Roadmap

1. **Code graph (current stage)** — File/Function/Class/Module nodes; CONTAINS, DEFINES, IMPORTS, CALLS. ✅
2. Git history — Commit/Issue/Developer nodes; MODIFIES/CHANGES/REFERENCES relationships.
3. Databricks/SQL awareness — READS/WRITES relationships to Delta tables, notebooks, jobs, Unity Catalog objects.
4. Chroma embeddings for semantic retrieval.
5. Retrieval layer combining graph + vector search into compact context packages.
6. Ollama-backed summarization/classification for retrieval and agent context.

## Agent Team

This repo is worked on by a small Claude Code agent team defined in
`.claude/agents/`. Five roles, split by the surfaces this project actually has:

| Agent | Owns |
| --- | --- |
| `orchestrator` | Routes work, opens PRs, assembles the merge packet. Delegates only |
| `indexer` | The write path — `ingest/`, `graph/` |
| `retrieval` | The read path — `embeddings/`, `retrieval/`, `agents/`, `cli/` |
| `reviewer` | Adversarial read on escalation paths. Read-only, never merges |
| `scout` | Free local-model summaries and read-only graph queries |

- `docs/agent_org_chart.md` — the roster, and why it is five roles rather than seven
- `docs/agent_governance.md` — the tiers, the escalation paths, and the evidence behind them

The boundary is the pull request: everything up to an open PR belongs to the
agent team, merging to `master` belongs to the repo owner. `reviewer` gates
the paths where a wrong change **reports success and produces a wrong graph** —
destructive Cypher, node keys, repository scoping, and file exclusion. That
list is drawn from defects this repo actually shipped, not from a template.

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
