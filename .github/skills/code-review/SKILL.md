---
name: code-review
description: Review guidance for repo-intelligence — what this codebase's bugs actually look like, so review targets them instead of generic style.
---

# Reviewing repo-intelligence

This project builds a Neo4j graph out of a repository: files, functions,
classes, imports, calls, and git history. It indexes **many repositories into
one database**, and it is a local developer tool with no deployment.

## The bug shape here is "wrong result, reports success"

Every defect this repo has actually shipped was silent. Nothing raised,
nothing failed, the output looked finished. Grade against that, not style:

- A leftover `.venv-1` directory was indexed in full, because exclusion matched
  directory names exactly and `.venv-1` is not `.venv`. The graph reached 96k+
  `Function` nodes and the run reported success.
- A `.gitignore` saved as UTF-16 was silently ignored by Git entirely.
- A superseded `Issue.number IS UNIQUE` constraint stayed in the database after
  being deleted from the code, because `CREATE ... IF NOT EXISTS` matches on
  the *definition*, not the name. Every second repository indexed then failed.
- A pure-deletion diff hunk was mapped onto the nearest new-side line, which is
  the line *before* the removed block — so deleting a function created a
  `CHANGES` edge to the function above it.

## Questions that catch things in this codebase

1. **What does this delete, and what is the widest thing that `WHERE` could
   match?** Read Cypher assuming the property it filters on may be missing.
   `n.repo = $repo` matches nothing when `repo` was never set — a silent no-op,
   not an error.
2. **Would this still be correct with two repositories indexed?** Most scoping
   bugs are invisible on a single-repo database. Ask specifically whether a
   `MATCH` binds to the repo under index or to every repo.
3. **If a `MERGE` key or constraint changed, what happens to existing nodes?**
   They are not migrated — they are orphaned, and every query still runs and
   just returns fewer rows. Does the change say a reindex is required?
4. **Does an exclusion change match by prefix or by exact name?** `.venv-1` is
   not `.venv`.
5. **Is attribution resolved against the right revision?** Commit-to-function
   attribution parses the file *as it existed at that commit*; deletions
   resolve against the *parent*. Matching against current line numbers
   misattributes every commit made before the code moved.
6. **What number did the author check?** A report saying "indexed successfully"
   with no count is itself the finding.
7. **Is there a test that would have failed before this change?** A test that
   documents a fix is weaker than one that would have caught the bug.

## Deliberate designs — do not flag these as bugs

- `Module` and `Developer` are **global, not repo-scoped**. A dependency and a
  person are the same entity across repositories; that is what makes
  cross-repo questions answerable.
- Diffs are taken with **zero context** (`unified=0`). Default context spills a
  hunk into neighbouring functions and blames them.
- **Merge commits claim no files or functions.** Diffing a merge against its
  first parent reports the whole merged branch as its own work.
- `--clean` and `--skip-history` are **refused together** on purpose.
- Functions deleted since a commit have no node, so no edge is created. That is
  correct, not a missing feature.

## Scope

Prefer few high-confidence findings over breadth. State the concrete input or
state that triggers a bug rather than "this could be risky" — a specific
failing scenario is what makes a finding actionable.
