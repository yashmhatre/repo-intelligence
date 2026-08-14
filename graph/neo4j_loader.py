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
# scopes a wipe, a call-graph link, or an issue number to one repo.
#
# Declared as (label, properties) rather than as Cypher strings, so the
# schema can be reconciled against what is actually in the database. Names
# are derived from the content, which means inserting or removing an entry
# never rebinds an existing name to a different definition.
#
# Module and Developer are deliberately NOT repo-scoped - a shared dependency
# and a person are the same entity across repos, and keeping them global is
# what makes "which repos import requests" and "what has this person touched"
# answerable.
CONSTRAINTS = [
    ("Repository", ["path"]),
    ("File", ["path"]),
    ("Module", ["name"]),
    ("Developer", ["email"]),
]

# Composite MERGE keys. Uniqueness across multiple properties is a node key
# constraint, which is Enterprise-only, so these are plain indexes - they make
# each MERGE a lookup instead of a full label scan.
INDEXES = [
    ("Function", ["name", "file", "line"]),
    ("Function", ["repo", "name"]),
    ("Function", ["file", "qualname"]),
    ("Class", ["name", "file"]),
    ("Issue", ["repo", "number"]),
    ("File", ["repo"]),
    ("Commit", ["repo", "hash"]),
]

# Every schema object this loader owns carries this prefix. Anything else in
# the database - including Neo4j's own LOOKUP indexes - is left alone.
SCHEMA_PREFIX = "repo_intel_"


def schema_object_name(kind: str, label: str, properties):
    return f"{SCHEMA_PREFIX}{kind}_{label.lower()}_{'_'.join(properties)}"


def ensure_schema(reconcile: bool = False):
    """Create this loader's schema and remove its own superseded objects.

    Reconciliation is the point. CREATE ... IF NOT EXISTS matches on the
    *definition*, not the name, so renaming an object silently leaves the old
    one in place, and dropping one from this file leaves it in the database
    forever. A stale `Issue.number IS UNIQUE` from an earlier version is
    exactly how multi-repo indexing breaks: the second repository's issue #1
    collides with the first's.
    """
    expected_constraints = {(label, tuple(props)) for label, props in CONSTRAINTS}
    expected_indexes = {(label, tuple(props)) for label, props in INDEXES}

    with driver.session(database=DATABASE) as session:

        for label, props in CONSTRAINTS:
            if len(props) != 1:
                raise ValueError(
                    f"CONSTRAINTS entry {label}{props} has multiple properties. "
                    "Composite uniqueness is a node key constraint, which is "
                    "Enterprise-only - declare it in INDEXES instead."
                )

            name = schema_object_name("c", label, props)
            prop = props[0]
            session.run(
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )

        for label, props in INDEXES:
            name = schema_object_name("i", label, props)
            on = ", ".join(f"n.{prop}" for prop in props)
            session.run(
                f"CREATE INDEX {name} IF NOT EXISTS "
                f"FOR (n:{label}) ON ({on})"
            )

        # Drop constraints first: dropping one takes its backing index with
        # it, and an index that backs a constraint cannot be dropped directly.
        obsolete = [
            record["name"]
            for record in session.run(
                "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties "
                "RETURN name, labelsOrTypes, properties"
            )
            if record["name"].startswith(SCHEMA_PREFIX)
            and (record["labelsOrTypes"][0], tuple(record["properties"]))
            not in expected_constraints
        ]

        for name in obsolete:
            if not reconcile:
                print(f"Superseded constraint left in place: {name} (--clean drops it)")
                continue

            session.run(f"DROP CONSTRAINT {name} IF EXISTS")
            print(f"Dropped superseded constraint: {name}")

        obsolete = [
            record["name"]
            for record in session.run(
                "SHOW INDEXES YIELD name, labelsOrTypes, properties, owningConstraint "
                "RETURN name, labelsOrTypes, properties, owningConstraint"
            )
            if record["name"].startswith(SCHEMA_PREFIX)
            and record["owningConstraint"] is None
            and (record["labelsOrTypes"][0], tuple(record["properties"]))
            not in expected_indexes
        ]

        for name in obsolete:
            if not reconcile:
                print(f"Superseded index left in place: {name} (--clean drops it)")
                continue

            session.run(f"DROP INDEX {name} IF EXISTS")
            print(f"Dropped superseded index: {name}")

    print("Schema ready")


def test_connection():
    with driver.session(database=DATABASE) as session:
        result = session.run(
            "RETURN 'Connected to Repo Intelligence' AS message"
        )
        print(result.single()["message"])


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

        # Drop definitions that no longer exist at these positions. Function
        # is keyed on (name, file, line), so an edit that shifts a def leaves
        # the old node behind - and CHANGES, which matches on qualname, then
        # attaches the commit to the stale node as well as the current one.
        function_lines = [fn["line"] for fn in parsed["functions"]]
        function_lines += [
            method["line"]
            for cls in parsed["classes"]
            for method in cls["methods"]
        ]

        session.run(
            """
            MATCH (fn:Function {file: $file_path})
            WHERE NOT fn.line IN $lines
            DETACH DELETE fn
            """,
            file_path=file_path,
            lines=function_lines,
        )

        session.run(
            """
            MATCH (c:Class {file: $file_path})
            WHERE NOT c.name IN $names
            DETACH DELETE c
            """,
            file_path=file_path,
            names=[cls["name"] for cls in parsed["classes"]],
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
                MERGE (c:Commit {repo: $repo, hash: $hash})
                SET c.message = $message,
                    c.author_name = $author_name,
                    c.author_email = $author_email,
                    c.authored_at = $authored_at

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
                MATCH (c:Commit {repo: $repo, hash: $hash})
                MERGE (c)-[:AUTHORED_BY]->(d)
                """,
                email=commit["author_email"],
                name=commit["author_name"],
                hash=commit["hash"],
                repo=repo_key,
            )

            # 3. Issues referenced in the commit message.
            # Scoped to the repo: issue #1 means something different in
            # every repository.
            for issue_number in extract_issue_numbers(commit["message"]):
                session.run(
                    """
                    MERGE (i:Issue {repo: $repo, number: $number})

                    WITH i
                    MATCH (c:Commit {repo: $repo, hash: $hash})
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
                    MATCH (c:Commit {repo: $repo, hash: $hash})
                    MATCH (f:File {path: $file_path})
                    MERGE (c)-[:MODIFIES]->(f)
                    """,
                    hash=commit["hash"],
                    file_path=file_path,
                    repo=repo_key,
                )

                if not git_path.endswith(".py"):
                    continue

                if git_path not in commit["changed_ranges"]:
                    # Old side of a rename - no new-side content to read.
                    continue

                ranges = commit["changed_ranges"][git_path]
                deleted = commit["deleted_ranges"].get(git_path)

                for qualname in changed_function_names(
                    repo_path, commit["hash"], git_path, ranges, deleted
                ):
                    # Matched on the qualified name: two classes in one file
                    # can each define run(), and a bare name would attribute
                    # an edit in one of them to both.
                    session.run(
                        """
                        MATCH (c:Commit {repo: $repo, hash: $hash})
                        MATCH (fn:Function {
                            qualname: $qualname,
                            file: $file_path,
                            repo: $repo
                        })
                        MERGE (c)-[:CHANGES]->(fn)
                        """,
                        hash=commit["hash"],
                        qualname=qualname,
                        file_path=file_path,
                        repo=repo_key,
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

    # Dropping schema objects is destructive and this database is shared
    # between repositories, so it only happens on an explicit --clean.
    ensure_schema(reconcile=clean)

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

        except (SyntaxError, UnicodeDecodeError, OSError) as e:
            # Only unreadable or unparseable source is skipped. Driver errors
            # propagate: exiting 0 on a half-written graph is how a wrong
            # result gets reported as a successful one.
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
