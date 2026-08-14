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
    source = path.read_text(encoding="utf-8")
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
                "line": node.lineno,
                "calls": extract_call_names(node)
            })

        elif isinstance(node, ast.ClassDef):
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({
                        "name": item.name,
                        "line": item.lineno,
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