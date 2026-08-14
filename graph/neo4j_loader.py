from neo4j import GraphDatabase
from pathlib import Path
import sys

# Allow imports from project root
sys.path.append(str(Path(__file__).resolve().parents[1]))

from ingest.parse_code import parse_python_file, list_python_files


URI = "neo4j://127.0.0.1:7687"
USER = "neo4j"
PASSWORD = "repo12345"

# IMPORTANT:
# This is the Neo4j DATABASE name, not necessarily the
# Neo4j Desktop DBMS/project name.
DATABASE = "neo4j"

driver = GraphDatabase.driver(
    URI,
    auth=(USER, PASSWORD)
)


def test_connection():
    with driver.session(database=DATABASE) as session:
        result = session.run(
            "RETURN 'Connected to Repo Intelligence' AS message"
        )
        print(result.single()["message"])


def index_parsed_file(parsed, repo_modules):
    file_path = parsed["file"]

    with driver.session(database=DATABASE) as session:

        # Create file
        session.run(
            """
            MERGE (f:File {path: $path})
            """,
            path=file_path
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
                SET fn.calls = $calls

                MERGE (f)-[:CONTAINS]->(fn)
                """,
                file_path=file_path,
                function_name=fn["name"],
                line=fn["line"],
                calls=fn["calls"]
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
                SET c.line = $line

                MERGE (f)-[:DEFINES]->(c)
                """,
                file_path=file_path,
                class_name=cls["name"],
                line=cls["line"]
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
                    SET fn.calls = $calls

                    MERGE (c)-[:CONTAINS]->(fn)
                    """,
                    file_path=file_path,
                    class_name=cls["name"],
                    method_name=method["name"],
                    line=method["line"],
                    calls=method["calls"]
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


def link_calls():
    """Naive name-based CALLS linking, run once after all files are indexed."""
    with driver.session(database=DATABASE) as session:
        session.run(
            """
            MATCH (caller:Function)
            WHERE caller.calls IS NOT NULL
            UNWIND caller.calls AS call_name
            MATCH (callee:Function {name: call_name})
            WHERE callee <> caller
            MERGE (caller)-[:CALLS]->(callee)
            """
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


def index_repository(repo_path: str):

    repo = Path(repo_path).resolve()

    py_files = list_python_files(repo)
    repo_modules = build_repo_modules(repo, py_files)

    print(f"Repository: {repo}")
    print(f"Found {len(py_files)} Python files to index")

    for py in py_files:

        try:
            parsed = parse_python_file(py)
            index_parsed_file(parsed, repo_modules)

        except Exception as e:
            print(f"Failed: {py}")
            print(f"Reason: {e}")

    link_calls()


if __name__ == "__main__":

    test_connection()

    # IMPORTANT:
    # Run this from the repository root that you want to index.
    index_repository(".")