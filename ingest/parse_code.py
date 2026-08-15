import ast
import re
from pathlib import Path
import typer

app = typer.Typer()

EXCLUDE_PREFIXES = (".venv", "venv", "env")
EXCLUDE_EXACT = {"__pycache__", ".git", "cache", "data"}

# The classic Databricks .py notebook export starts with this exact comment,
# and Cmd/COMMAND separators after it are still plain Python comments - the
# rest of the file parses with ast unmodified.
DATABRICKS_NOTEBOOK_MARKER = "# Databricks notebook source"

# catalog.table / catalog.schema.table / `quoted`.`parts`, up to three dotted
# segments. Backtick-quoting is stripped, not required.
_QUALIFIED_IDENT = r"`?[A-Za-z_][A-Za-z0-9_]*`?(?:\.`?[A-Za-z_][A-Za-z0-9_]*`?){0,2}"

# Checked in order, and independently of the READ patterns below: "DELETE
# FROM x" must be recorded as a write of x, not (also) a read, so the READ
# pattern for FROM excludes anything immediately preceded by "DELETE ".
_SQL_WRITE_PATTERNS = [
    re.compile(rf"\bINSERT\s+(?:OVERWRITE(?:\s+TABLE)?|INTO)\s+({_QUALIFIED_IDENT})", re.IGNORECASE),
    re.compile(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_QUALIFIED_IDENT})", re.IGNORECASE),
    re.compile(rf"\bMERGE\s+INTO\s+({_QUALIFIED_IDENT})", re.IGNORECASE),
    re.compile(rf"\bUPDATE\s+({_QUALIFIED_IDENT})\s+SET\b", re.IGNORECASE),
    re.compile(rf"\bDELETE\s+FROM\s+({_QUALIFIED_IDENT})", re.IGNORECASE),
]

_SQL_READ_PATTERNS = [
    re.compile(rf"(?<!DELETE )\bFROM\s+({_QUALIFIED_IDENT})", re.IGNORECASE),
    re.compile(rf"\bJOIN\s+({_QUALIFIED_IDENT})", re.IGNORECASE),
]

# PySpark call shapes this stage recognizes. Only the *method name* is
# matched, deliberately - `spark.table(...)`, `spark.read.table(...)`, and
# `some_df.table(...)` all end in `.table(literal)`, and resolving the
# receiver's real type is out of scope for an AST-only pass.
_READ_METHODS = {"table"}
_WRITE_METHODS = {"saveAsTable", "insertInto"}
_SQL_METHODS = {"sql"}


def normalize_table_name(raw: str):
    """Strip backtick-quoting and lowercase.

    Unity Catalog and Hive Metastore identifiers are case-insensitive, so
    `Main.Sales.Transactions` and `main.sales.transactions` are the same
    physical table and must resolve to the same node.
    """
    parts = [p.strip("`") for p in raw.split(".")]
    return ".".join(parts).lower()


def _string_literal(node):
    """A plain string constant, or None for anything dynamic.

    An f-string or a variable holds a table name this AST-only pass cannot
    resolve. Guessing at it would silently misattribute lineage; skipping it
    just means that reference is invisible, which is the honest failure mode.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def extract_tables_from_sql(sql: str):
    """Regex-based scan of a SQL string for table references.

    Not a SQL parser: a CTE alias referenced by a later FROM reads as a
    table, and any dynamically-built identifier is invisible by
    construction. That is an accepted gap for this stage - a real SQL
    parser is future work - not a silent correctness bug, because nothing
    here is asserted with more confidence than "this literal text matched a
    keyword".
    """
    refs = []

    for pattern in _SQL_WRITE_PATTERNS:
        for m in pattern.finditer(sql):
            refs.append((m.group(1), "WRITES"))

    for pattern in _SQL_READ_PATTERNS:
        for m in pattern.finditer(sql):
            refs.append((m.group(1), "READS"))

    return refs


def extract_table_refs(node):
    """Scan a subtree for PySpark table access and Spark SQL statements.

    Returns deduplicated {"table", "access", "qualified"} dicts. `qualified`
    reflects the *reference text*, not any external catalog - a bare name
    like `transactions` is unqualified regardless of what it might mean in
    context, because this pass has no way to know.
    """
    raw_refs = []

    for n in ast.walk(node):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue

        attr = n.func.attr
        arg = _string_literal(n.args[0]) if n.args else None

        if arg is None:
            continue

        if attr in _READ_METHODS:
            raw_refs.append((arg, "READS"))
        elif attr in _WRITE_METHODS:
            raw_refs.append((arg, "WRITES"))
        elif attr in _SQL_METHODS:
            raw_refs.extend(extract_tables_from_sql(arg))

    deduped = list(dict.fromkeys(raw_refs))

    return [
        {
            "table": normalize_table_name(name),
            "access": access,
            "qualified": name.count(".") >= 2,
        }
        for name, access in deduped
    ]


def _is_scoped_definition(node):
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                              ast.Import, ast.ImportFrom))


def list_python_files(repo: Path):
    return [
        p for p in repo.rglob("*.py")
        if not any(
            part in EXCLUDE_EXACT or part.startswith(EXCLUDE_PREFIXES)
            for part in p.parts
        )
    ]


def definition_start_line(node):
    """First line that belongs to a definition, decorators included.

    `node.lineno` points at the `def`, so an edit to a decorator falls
    outside the range and gets attributed to no function at all.
    """
    if node.decorator_list:
        return min(d.lineno for d in node.decorator_list)

    return node.lineno


def extract_call_names(func_node):
    """Naive call-name extraction (no scope/type resolution)."""
    calls = []
    for n in ast.walk(func_node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                calls.append(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                calls.append(n.func.attr)
    return calls


def parse_python_file(path: Path):
    return parse_python_source(path.read_text(encoding="utf-8"), path)


def parse_python_source(source: str, path):
    """Parse already-loaded source. Used for both on-disk files and git blobs."""
    tree = ast.parse(source)

    functions = []
    classes = []
    imports = []

    # Only look at module-level nodes so class methods aren't
    # double-counted as top-level functions.
    for node in tree.body:

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "qualname": node.name,
                "line": node.lineno,
                "start_line": definition_start_line(node),
                "end_line": node.end_lineno,
                "calls": extract_call_names(node),
                "tables": extract_table_refs(node),
            })

        elif isinstance(node, ast.ClassDef):
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({
                        "name": item.name,
                        # Qualified so two classes in one file can both
                        # define run() without collapsing into one target.
                        "qualname": f"{node.name}.{item.name}",
                        "line": item.lineno,
                        "start_line": definition_start_line(item),
                        "end_line": item.end_lineno,
                        "calls": extract_call_names(item),
                        "tables": extract_table_refs(item),
                    })
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "methods": methods
            })

        elif isinstance(node, ast.Import):
            for n in node.names:
                imports.append(n.name)

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    # Module-level (a.k.a. notebook top-level) statements: everything that
    # is not itself a def/class/import. A Databricks notebook is mostly this
    # - cells of top-level `spark.sql(...)` and `df.write...saveAsTable(...)`
    # calls, not functions - so without this, the one place lineage actually
    # lives in a notebook would be invisible.
    module_calls = []
    module_tables = []
    for node in tree.body:
        if _is_scoped_definition(node):
            continue
        module_calls.extend(extract_call_names(node))
        module_tables.extend(extract_table_refs(node))

    module_tables = list({(t["table"], t["access"]): t for t in module_tables}.values())

    # Module-level calls/tables are NOT wrapped in a synthetic Function node.
    # A pseudo "<module>" Function in every file that has top-level code
    # would inflate the Function count across every repository this tool
    # indexes and silently change what every "how many/which functions"
    # query returns - exactly this repo's documented failure shape, a wrong
    # count that reports success. The loader attaches this data to the
    # :File node instead (or :Notebook, for the .ipynb case).
    is_notebook = source.lstrip().startswith(DATABRICKS_NOTEBOOK_MARKER)

    return {
        "file": str(path),
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "is_notebook": is_notebook,
        "module_calls": module_calls,
        "module_tables": module_tables,
    }


def iter_function_ranges(parsed):
    """Flatten a parse result to (qualname, start_line, end_line) for every
    function, including methods. Used to map changed line ranges onto the
    functions that contain them.

    Yields the *qualified* name, because attribution matches these against
    nodes in the graph: two classes in one file can each define run(), and a
    bare name would attribute an edit in one to both. The range starts at the
    first decorator, not at `def`.
    """
    for fn in parsed["functions"]:
        yield fn["qualname"], fn["start_line"], fn["end_line"]

    for cls in parsed["classes"]:
        for method in cls["methods"]:
            yield method["qualname"], method["start_line"], method["end_line"]


@app.command()
def repo(repo_path: str):

    repo = Path(repo_path).resolve()

    py_files = list_python_files(repo)

    print(f"Repository: {repo}")
    print(f"Found {len(py_files)} Python files")

    for py in py_files[:5]:

        try:
            result = parse_python_file(py)

            print(f"\nFILE: {result['file']}")

            if result["functions"]:
                print("Functions:")

                for fn in result["functions"]:
                    print(
                        f"  - {fn['name']} "
                        f"(line {fn['line']})"
                    )

            if result["classes"]:
                print("Classes:")

                for cls in result["classes"]:
                    print(
                        f"  - {cls['name']} "
                        f"(line {cls['line']})"
                    )

            if result["imports"]:
                print("Imports:")

                for imp in result["imports"][:5]:
                    print(f"  - {imp}")

        except Exception as e:
            print(f"Failed: {py} - {e}")


if __name__ == "__main__":
    app()
