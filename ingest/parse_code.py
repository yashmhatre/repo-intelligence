import ast
from pathlib import Path
import typer

app = typer.Typer()

EXCLUDE_PREFIXES = (".venv", "venv", "env")
EXCLUDE_EXACT = {"__pycache__", ".git", "cache", "data"}


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
                "calls": extract_call_names(node)
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
                        "calls": extract_call_names(item)
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

    return {
        "file": str(path),
        "functions": functions,
        "classes": classes,
        "imports": imports
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