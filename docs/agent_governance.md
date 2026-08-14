# Agent governance

Who owns which surface, what needs a second agent's read, and what needs
Yash. This file is the authority on the tiers; each `.claude/agents/*.md`
adds only how that role operates.

## The boundary is the pull request

Everything up to and including an open PR against `master` belongs to the
agent team. Merging into `master` belongs to Yash.

That cut is deliberately different from the one used on
`Ingredion_Enhancement_Package`, which has a `dev` branch to absorb a bad
merge. This repo has no such buffer: `master` is the only long-lived branch,
so a merge here is the irreversible step and stays with the human.

## Tiers

**Tier 1 — the agent team acts.** Writing code, tests and docs on a feature
branch; running the indexer against a local Neo4j database; opening a PR.
No approval needed. `reviewer` gates the escalation paths listed below
before the PR is marked ready.

**Tier 2 — Yash's named sign-off.** He names the specific action; a general
"go ahead" earlier in the conversation does not carry over.

- Merging any PR into `master`
- Anything touching the hardcoded `URI`/`USER`/`PASSWORD` in
  `graph/neo4j_loader.py`, or moving them to a real secret
- Dropping or creating a Neo4j **database**, as opposed to clearing a
  repository's nodes within one
- Running destructive Cypher against a database holding an index the user
  has not agreed to lose
- Publishing anything outside this repo: pushing to a new remote, publishing
  a package, posting to an external service
- Weakening or deleting a test, or narrowing what CI runs

There is no Tier 3. This is a local developer tool with no deployment, no
staging environment, and no production data. Inventing a promotion ceremony
for it would be the same mistake as keeping eleven roles for a solo build.

## Escalation paths — a second agent reads before merge

`reviewer` must produce a verdict when a diff touches any of these. The agent
that wrote the change is never the agent that blesses it.

| Path | Why it is here |
| --- | --- |
| Destructive Cypher — `DETACH DELETE`, `DROP`, dropping constraints | A wrong `WHERE` clause silently wipes a graph that took a long reindex to build, and the failure looks like success |
| Node keys and the graph schema — `CONSTRAINTS`, `INDEXES`, any `MERGE` key | Changing a key silently orphans every existing node and invalidates every saved query. Nothing errors; the graph just quietly stops matching |
| Repository scoping — the `repo` property, `clear_repository()`, `link_calls()` | These are what stop one indexed repo corrupting another. Regressions here are invisible until a second repo is indexed |
| File-exclusion logic — `EXCLUDE_PREFIXES`, `EXCLUDE_EXACT`, `list_python_files()` | See below. This is the one path justified by a defect that actually happened |
| Credentials, and `.gitignore` | Both have already failed silently in this repo's history |

### Why exclusion logic is on that list

On 2026-08-14 a leftover `.venv-1` directory with ~15k `.py` files was
indexed in full, because the exclusion check matched directory names exactly
and `.venv-1` is not `.venv`. The graph grew to 96k+ `Function` nodes. Nothing
raised, nothing failed, and the indexer reported success — the graph was just
wrong by a factor of thirty.

The same day, a `.gitignore` saved as UTF-16 was silently ignored by Git
entirely, so nothing it listed was actually excluded.

Both defects share a shape: **a silent wrong answer that reads as a correct
one.** That is the class of failure a second reader catches and a test suite
often does not, because nobody writes an assertion against a number they
haven't noticed is wrong. It is the whole justification for `reviewer`
existing on these paths and not on ordinary code.

## Ownership

| Surface | Agent |
| --- | --- |
| `ingest/` — AST parsing, git history extraction | `indexer` |
| `graph/` — Neo4j schema, loading, Cypher | `indexer` |
| `embeddings/`, `retrieval/`, `agents/` — the read path | `retrieval` |
| `cli/` — Typer entrypoints | `retrieval` |
| `tests/` | whichever agent owns the code under test |
| `README.md`, `docs/` | the agent making the change it documents |
| Pre-merge review on an escalation path | `reviewer` |
| Summaries, issue triage, cheap graph queries | `scout` |

`indexer` owns both `ingest/` and `graph/` on purpose. They are one
transaction: a parser change that adds a field is useless until the loader
persists it, and splitting them across two agents would put a handoff in the
middle of a single edit. The schema is the risky part of that surface, so it
is guarded by a `reviewer` trigger rather than by a separate role.

## Non-negotiables

- **Never point the indexer at a database you would mind losing** without
  saying so first. `--clean` is scoped to one repository, but a bug in that
  scoping is exactly what `reviewer` is watching for.
- **Verify a graph count before reporting it.** "It indexed successfully" is
  not a result; a node count you have compared against an expectation is.
  The `.venv-1` defect passed every check that did not involve counting.
- **Check for stray indexer processes** before trusting a count. A
  `neo4j_loader.py` left running from an earlier session keeps writing stale
  data while you debug, and makes counts move for no visible reason.
- **Use the venv.** `.venv\Scripts\python.exe`, not the system Python.
