import ast
from pathlib import Path
import typer

app = typer.Typer()

def parse_python_file(path: Path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    functions = []
    imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            functions.append({
                "name": node.name,
                "line": node.lineno
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
        "imports": imports
    }

@app.command()
def repo(repo_path: str):
    repo = Path(repo_path)

    exclude_prefixes = (
        ".venv",
        "venv",
        "env",
    )

    exclude_exact = {
        "__pycache__",
        ".git",
        "cache",
        "data"
    }

    py_files = [
        p for p in repo.rglob("*.py")
        if not any(
            part in exclude_exact or part.startswith(exclude_prefixes)
            for part in p.parts
        )
    ]
    print(f"Found {len(py_files)} Python files")

    for py in py_files[:5]:
        try:
            result = parse_python_file(py)
            print(f"\\nFILE: {result['file']}")

            if result["functions"]:
                print("Functions:")
                for fn in result["functions"]:
                    print(f"  - {fn['name']} (line {fn['line']})")

            if result["imports"]:
                print("Imports:")
                for imp in result["imports"][:5]:
                    print(f"  - {imp}")

        except Exception as e:
            print(f"Failed: {py} - {e}")

if __name__ == "__main__":
    app()