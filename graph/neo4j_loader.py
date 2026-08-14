from neo4j import GraphDatabase
from pathlib import Path
import argparse
import os
import sys

# Allow imports from project root
sys.path.append(str(Path(__file__).resolve().parents[1]))

from ingest.parse_code import parse_python_file, list_python_files
from git.exc import InvalidGitRepositoryError, NoSuchPathError

from ingest.git_history import (
    get_git_history,
    ensure_git_repository,
    get_remote_url,
    changed_function_names,
    resolve_git_file_path,
    extract_issue_numbers,
)


URI = os.environ.get("REPO_INTEL_NEO4J_URI", "neo4j://127.0.0.1:7687")
USER = os.environ.get("REPO_INTEL_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("REPO_INTEL_NEO4J_PASSWORD", "repo12345")

# IMPORTANT:
# This is the Neo4j DATABASE name, not necessarily the
# Neo4j Desktop DBMS/project name.
#
# Neo4j Community Edition and Aura Free support exactly one user database,
# and it must be called "neo4j" - CREATE DATABASE is Enterprise-only. If you
# are not on Desktop or Enterprise, set REPO_INTEL_NEO4J_DATABASE=neo4j.
DATABASE = os.environ.get("REPO_INTEL_NEO4J_DATABASE", "repo-intelligence")

driver = GraphDatabase.driver(
    URI,
    auth=(USER, PASSWORD)
)


# One Neo4j database holds many repositories. Every repo-owned node carries a
# `repo` property set to the repository's absolute path, and that is what
# scopes a wipe, a call-graph link, or an issue number to one repo. Module and
# Developer are deliberately NOT scoped - a shared dependency and a person are
# the same entity across repos, and keeping them global is what makes
# "which repos import requests" and "what has this person touched" answerable.
# Named explicitly rather than by list position. CREATE ... IF NOT EXISTS
# no-ops on the *name*, so a positional name would silently leave an old
# definition in place under a new intent when an entry is inserted.
CONSTRAINTS = [
    ("repo_unique_path", "FOR (r:Repository) REQUIRE r.path IS UNIQUE"),
    ("file_unique_path", "FOR (f:File) REQUIRE f.path IS UNIQUE"),
    ("module_unique_name", "FOR (m:Module) REQUIRE m.name IS UNIQUE"),
    ("commit_unique_hash", "FOR (c:Commit) REQUIRE c.hash IS UNIQUE"),
    ("developer_unique_email", "FOR (d:Developer) REQUIRE d.email IS UNIQUE"),
]

# Composite MERGE keys. Uniqueness across multiple properties is a node key
# constraint, which is Enterprise-only, so these are plain indexes - they
# make each MERGE a lookup instead of a full label scan.
INDEXES = [
    ("function_key", "FOR (fn:Function) ON (fn.name, fn.file, fn.line)"),
    ("function_repo_name", "FOR (fn:Function) ON (fn.repo, fn.name)"),
    ("function_qualname", "FOR (fn:Function) ON (fn.file, fn.qualname)"),
    ("class_name_file", "FOR (c:Class) ON (c.name, c.file)"),
    ("issue_repo_number", "FOR (i:Issue) ON (i.repo, i.number)"),
    ("file_repo", "FOR (f:File) ON (f.repo)"),
    ("commit_repo", "FOR (c:Commit) ON (c.repo)"),
]


def test_connection():
    with driver.session(database=DATABASE) as session:
        result = session.run(
            "RETURN 'Connected to Repo Intelligence' AS message"
        )
        print(result.single()["message"])


def ensure_schema():
    """Idempotent - safe to run on every index."""
    with driver.session(database=DATABASE) as session:

        for name, body in CONSTRAINTS:
            session.run(
                f"CREATE CONSTRAINT repo_intel_{name} IF NOT EXISTS {body}"
            )

        for name, body in INDEXES:
            session.run(
                f"CREATE INDEX repo_intel_{name} IF NOT EXISTS {body}"
            )

    print("Schema ready")


def register_repository(repo_key: str, name: str, remote=None):
    with driver.session(database=DATABASE) as session:
        session.run(
            """
            MERGE (r:Repository {path: $path})
            SET r.name = $name

            // SET prop = null REMOVES the property in Cypher, so a run that
            // could not determine the remote must leave the stored one alone
            // rather than overwrite it with null.
            FOREACH (_ IN CASE WHEN $remote IS NULL THEN [] ELSE [1] END |
                SET r.remote = $remote
            )
            """,
            path=repo_key,
            name=name,
            remote=remote,
        )

    print(f"Repository registered: {name}")


def clear_repository(repo_key: str):
    """Wipe one repository's nodes, leaving every other repo intact.

    Re-indexing only ever MERGEs, so nodes for files that were deleted or
    renamed since the last run would otherwise linger forever. Module and
    Developer survive by design - they are shared across repositories - and
    any left with no remaining relationships are cleaned up below.
    """
    with driver.session(database=DATABASE) as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:File OR n:Function OR n:Class OR n:Commit OR n:Issue)
              AND n.repo = $repo
            DETACH DELETE n
            """,
            repo=repo_key,
        )

        # Shared nodes that no longer connect to anything. Split by label so
        # each is an index-backed label scan rather than one scan of every
        # node in a database that holds many repositories.
        session.run("MATCH (m:Module) WHERE NOT (m)--() DELETE m")
        session.run("MATCH (d:Developer) WHERE NOT (d)--() DELETE d")

    print(f"Cleared existing graph for: {repo_key}")


def index_parsed_file(parsed, repo_modules, repo_key):
    file_path = parsed["file"]

    with driver.session(database=DATABASE) as session:

        # Create file, owned by its repository
        session.run(
            """
            MERGE (f:File {path: $path})
            SET f.repo = $repo

            WITH f
            MATCH (r:Repository {path: $repo})
            MERGE (r)-[:HAS_FILE]->(f)
            """,
            path=file_path,
            repo=repo_key,
        )

        # Create top-level functions and CONTAINS relationships
        for fn in parsed["functions"]:
            session.run(
                """
                MERGE (f:File {path: $file_path})

                MERGE (fn:Function {
                    name: $function_name,
                    file: $file_path,
                    line: $line
                })
                SET fn.calls = $calls,
                    fn.end_line = $end_line,
                    fn.qualname = $qualname,
                    fn.repo = $repo

                MERGE (f)-[:CONTAINS]->(fn)
                """,
                file_path=file_path,
                function_name=fn["name"],
                line=fn["line"],
                end_line=fn["end_line"],
                qualname=fn["qualname"],
                calls=fn["calls"],
                repo=repo_key,
            )

        # Create classes, DEFINES relationships and their methods
        for cls in parsed["classes"]:
            session.run(
                """
                MERGE (f:File {path: $file_path})

                MERGE (c:Class {
                    name: $class_name,
                    file: $file_path
                })
                SET c.line = $line,
                    c.repo = $repo

                MERGE (f)-[:DEFINES]->(c)
                """,
                file_path=file_path,
                class_name=cls["name"],
                line=cls["line"],
                repo=repo_key,
            )

            for method in cls["methods"]:
                session.run(
                    """
                    MERGE (c:Class {name: $class_name, file: $file_path})

                    MERGE (fn:Function {
                        name: $method_name,
                        file: $file_path,
                        line: $line
                    })
                    SET fn.calls = $calls,
                        fn.end_line = $end_line,
                        fn.qualname = $qualname,
                        fn.repo = $repo

                    MERGE (c)-[:CONTAINS]->(fn)
                    """,
                    file_path=file_path,
                    class_name=cls["name"],
                    method_name=method["name"],
                    line=method["line"],
                    end_line=method["end_line"],
                    qualname=method["qualname"],
                    calls=method["calls"],
                    repo=repo_key,
                )

        # Create IMPORTS relationships, resolving local repo modules to
        # File nodes and treating everything else as an external Module.
        for imp in parsed["imports"]:
            target_path = repo_modules.get(imp)

            if target_path and target_path != file_path:
                session.run(
                    """
                    MERGE (f:File {path: $file_path})
                    MERGE (t:File {path: $target_path})
                    MERGE (f)-[:IMPORTS]->(t)
                    """,
                    file_path=file_path,
                    target_path=target_path
                )
            else:
                session.run(
                    """
                    MERGE (f:File {path: $file_path})
                    MERGE (m:Module {name: $module_name})
                    MERGE (f)-[:IMPORTS]->(m)
                    """,
                    file_path=file_path,
                    module_name=imp
                )

    print(f"Indexed: {file_path}")


def link_calls(repo_key: str):
    """Naive name-based CALLS linking, run once after all files are indexed.

    Scoped to one repository: matching callees by bare name across the whole
    database would link a call in one repo to an identically named function
    in an unrelated one.
    """
    with driver.session(database=DATABASE) as session:
        session.run(
            """
            MATCH (caller:Function {repo: $repo})
            WHERE caller.calls IS NOT NULL
            UNWIND caller.calls AS call_name
            MATCH (callee:Function {repo: $repo, name: call_name})
            WHERE callee <> caller
            MERGE (caller)-[:CALLS]->(callee)
            """,
            repo=repo_key,
        )
    print("Linked function calls")


def build_repo_modules(repo: Path, py_files):
    """Map dotted module path (relative to repo root) -> file path."""
    repo_modules = {}

    for py in py_files:
        rel = py.relative_to(repo).with_suffix("")
        module_name = ".".join(rel.parts)
        repo_modules[module_name] = str(py)

    return repo_modules


def index_git_history(repo_path: str, repo_key: str):
    history = get_git_history(repo_path)

    repo = Path(repo_path).resolve()

    print(f"Found {len(history)} commits to index")

    with driver.session(database=DATABASE) as session:

        for commit in history:

            # 1. Create/update Commit, owned by its repository
            session.run(
                """
                MERGE (c:Commit {hash: $hash})
                SET c.message = $message,
                    c.author_name = $author_name,
                    c.author_email = $author_email,
                    c.authored_at = $authored_at,
                    c.repo = $repo

                WITH c
                MATCH (r:Repository {path: $repo})
                MERGE (r)-[:HAS_COMMIT]->(c)
                """,
                hash=commit["hash"],
                message=commit["message"],
                author_name=commit["author_name"],
                author_email=commit["author_email"],
                authored_at=commit["authored_at"],
                repo=repo_key,
            )

            # 2. Create/update Developer + connect author.
            # Developer is intentionally global - the same person across
            # repositories is the same person.
            session.run(
                """
                MERGE (d:Developer {email: $email})
                SET d.name = $name

                WITH d
                MATCH (c:Commit {hash: $hash})
                MERGE (c)-[:AUTHORED_BY]->(d)
                """,
                email=commit["author_email"],
                name=commit["author_name"],
                hash=commit["hash"],
            )

            # 3. Issues referenced in the commit message.
            # Scoped to the repo: issue #1 means something different in
            # every repository.
            for issue_number in extract_issue_numbers(commit["message"]):
                session.run(
                    """
                    MERGE (i:Issue {repo: $repo, number: $number})

                    WITH i
                    MATCH (c:Commit {hash: $hash})
                    MERGE (c)-[:REFERENCES]->(i)
                    """,
                    number=issue_number,
                    hash=commit["hash"],
                    repo=repo_key,
                )

            # 4. Connect Commit -> File, and Commit -> Function.
            # Both MATCH rather than MERGE: a path that no longer exists in
            # the working tree has no node, and inventing one would put
            # history-only files into the code graph.
            # A merge commit reports its whole merged branch as its own
            # work, so it claims no files or functions - see get_git_history.
            for git_path in commit["changed_files"]:

                file_path = resolve_git_file_path(repo, git_path)

                session.run(
                    """
                    MATCH (c:Commit {hash: $hash})
                    MATCH (f:File {path: $file_path})
                    MERGE (c)-[:MODIFIES]->(f)
                    """,
                    hash=commit["hash"],
                    file_path=file_path,
                )

                if not git_path.endswith(".py"):
                    continue

                if git_path not in commit["changed_ranges"]:
                    # Old side of a rename - no new-side content to read.
                    continue

                ranges = commit["changed_ranges"][git_path]

                for qualname in changed_function_names(
                    repo_path, commit["hash"], git_path, ranges
                ):
                    # Matched on the qualified name: two classes in one file
                    # can each define run(), and a bare name would attribute
                    # an edit in one of them to both.
                    session.run(
                        """
                        MATCH (c:Commit {hash: $hash})
                        MATCH (fn:Function {
                            qualname: $qualname,
                            file: $file_path
                        })
                        MERGE (c)-[:CHANGES]->(fn)
                        """,
                        hash=commit["hash"],
                        qualname=qualname,
                        file_path=file_path,
                    )

            print(f"Indexed commit: {commit['hash'][:10]}")


class ContradictoryOptions(ValueError):
    pass


def index_repository(repo_path: str, clean=False, skip_history=False):

    # --clean deletes the File and Function nodes that MODIFIES and CHANGES
    # point at, and DETACH DELETE takes those relationships with them.
    # --skip-history then never rebuilds them, so the run would leave Commit
    # nodes attached to nothing and quietly answer "who last changed this"
    # with silence. Keeping the Commit nodes does not help; the edges are
    # the part that is lost.
    if clean and skip_history:
        raise ContradictoryOptions(
            "--clean and --skip-history cannot be combined: --clean removes "
            "the code nodes that commit history links to, and --skip-history "
            "will not rebuild those links. Run --clean on its own to rebuild "
            "everything, or --skip-history on its own to refresh the code "
            "graph in place."
        )

    repo = Path(repo_path).resolve()

    # A repository's identity is its absolute path on this machine. The
    # remote URL is recorded alongside it but is not the key: the same
    # remote can be cloned to several working copies, and this is a
    # local-first tool that indexes a working copy.
    repo_key = str(repo)

    ensure_schema()

    # Fetched regardless of skip_history: register_repository would otherwise
    # be handed None and, before the FOREACH guard, wipe a remote recorded by
    # an earlier full run.
    remote = None

    try:
        remote = get_remote_url(repo_path)
    except Exception as e:
        print(f"No git remote detected: {e}")

    register_repository(repo_key, repo.name, remote)

    if clean:
        clear_repository(repo_key)

    py_files = list_python_files(repo)
    repo_modules = build_repo_modules(repo, py_files)

    print(f"Repository: {repo}")
    print(f"Found {len(py_files)} Python files to index")

    for py in py_files:

        try:
            parsed = parse_python_file(py)
            index_parsed_file(parsed, repo_modules, repo_key)

        except Exception as e:
            print(f"Failed: {py}")
            print(f"Reason: {e}")

    link_calls(repo_key)

    if skip_history:
        print("Skipping git history")
        return

    # Only repository *discovery* is tolerated. A write failure part-way
    # through the commit loop must not print a friendly message and exit 0,
    # leaving a half-indexed history that looks complete.
    try:
        ensure_git_repository(str(repo))
    except (InvalidGitRepositoryError, NoSuchPathError) as e:
        print(f"Skipped git history, not a git repository: {e}")
        return

    index_git_history(str(repo), repo_key)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Index a repository's code graph and git history into Neo4j."
    )
    parser.add_argument(
        "repo_path",
        nargs="?",
        default=".",
        help="Repository to index (default: current directory)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe this repository's existing nodes before re-indexing",
    )
    parser.add_argument(
        "--skip-history",
        action="store_true",
        help="Index the code graph only, without git history",
    )
    args = parser.parse_args()

    test_connection()

    try:
        index_repository(
            args.repo_path,
            clean=args.clean,
            skip_history=args.skip_history,
        )
    except ContradictoryOptions as e:
        parser.error(str(e))
