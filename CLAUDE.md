# repo-intelligence

Local code intelligence tool. Parses repositories into a Neo4j graph — files,
functions, classes, imports, calls, and git history — so an agent can answer
questions about a codebase without reading all of it.

**This file is read by both Claude Code and GitHub Copilot Chat.** It holds the
rules that apply on either substrate. Per-role detail lives in
`.claude/agents/` (Claude Code) and `.github/agents/` (Copilot); the tiers and
escalation paths live in `docs/agent_governance.md`.

## Commands

Always use the venv Python, never the system one:

```
.venv\Scripts\python.exe -m pytest tests
.venv\Scripts\python.exe graph\neo4j_loader.py            # index this repo
.venv\Scripts\python.exe graph\neo4j_loader.py <path>     # index another repo
.venv\Scripts\python.exe graph\neo4j_loader.py . --clean  # wipe this repo's nodes first
```

Neo4j connection settings read `REPO_INTEL_NEO4J_{URI,USER,PASSWORD,DATABASE}`
and fall back to the defaults in `graph/neo4j_loader.py`.

## The failure mode this project actually has

**A wrong result that reports success.** Every defect this repo has shipped has
had that shape — nothing raised, nothing failed, the output looked finished:

- A leftover `.venv-1` directory was indexed in full, because exclusion matched
  directory names exactly and `.venv-1` is not `.venv`. The graph reached 96k+
  `Function` nodes.
- A `.gitignore` saved as UTF-16 was silently ignored by Git entirely.
- A superseded `Issue.number IS UNIQUE` constraint stayed in the database after
  being removed from the code, breaking every second repository indexed.

So:

- **Count something, and compare it to a number you predicted.** State the
  expected node and relationship counts *before* checking them. "Indexed
  successfully" is not a result; a count you have not compared to an
  expectation is not evidence.
- **Check for stray indexer processes before trusting any count.** One left
  running from an earlier session keeps writing stale data:
  `Get-CimInstance Win32_Process -Filter "Name = 'python.exe'"`
- **Prefer a test that would have caught the defect** over one that documents
  the fix. The `.venv-1` bug was invisible to every test that existed, because
  none of them asserted on a count.

## Multi-repo is a hard requirement

This tool indexes **other people's repositories**, not just its own, and one
Neo4j database holds many at once. Three invariants keep them from corrupting
each other:

1. `--clean` wipes only the repository being indexed.
2. `CALLS` edges are built within one repository — two repos that both define
   `run()` must never cross-link.
3. `Issue` is keyed on `(repo, number)` and `Commit` on `(repo, hash)`, because
   `#1` differs per repository and two clones of one remote share hashes.

`Module` and `Developer` are deliberately global — a dependency and a person
are the same entity everywhere. Do not "fix" that by scoping them.

**Test any scoping change against a second repository.** Indexing one repo
proves nothing about isolation; the bug always shows up on the second.

## Ownership

| Surface | Role |
| --- | --- |
| `ingest/`, `graph/` — the write path | `indexer` |
| `embeddings/`, `retrieval/`, `agents/`, `cli/` — the read path | `retrieval` |
| `tests/` | whichever role owns the code under test |
| Routing, PRs, merges | `orchestrator` (Claude Code only) |
| Pre-merge review on an escalation path | `reviewer` (Claude Code only) |
| Summaries, issue triage, cheap graph queries | `scout` (local Ollama) |

`indexer` owns both `ingest/` and `graph/` on purpose: a parser change that
adds a field is useless until the loader persists it, so splitting them would
put a handoff in the middle of one edit.

## Before merging anything

`orchestrator` merges into `master` on its own authority once the conditions in
`.claude/agents/orchestrator.md` are met. **`master` is public** — a merge
publishes, and a revert removes the code but not the disclosure. Anything that
should not be public is a blocker, not a note.
