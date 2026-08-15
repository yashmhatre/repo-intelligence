"""Databricks/Jupyter .ipynb notebooks.

The classic Databricks *.py* notebook export needs no special handling here
- `# Databricks notebook source` and `# COMMAND ----------` are ordinary
Python comments, so `parse_code.parse_python_source` already parses one
correctly and sets `is_notebook` from the header marker. This module exists
for the *.ipynb* JSON format, which is not Python source until its code
cells are extracted and joined.
"""

import json
from pathlib import Path

from ingest.parse_code import EXCLUDE_EXACT, EXCLUDE_PREFIXES, parse_python_source

# Jupyter's own checkpoint copies are not source, and indexing one alongside
# the real notebook would double every reference in it.
NOTEBOOK_EXCLUDE_EXACT = EXCLUDE_EXACT | {".ipynb_checkpoints"}


def list_notebook_files(repo: Path):
    return [
        p for p in repo.rglob("*.ipynb")
        if not any(
            part in NOTEBOOK_EXCLUDE_EXACT or part.startswith(EXCLUDE_PREFIXES)
            for part in p.parts
        )
    ]


def _clean_cell_source(lines):
    """Comment out Jupyter magics (`%pip ...`) and shell escapes (`!ls`).

    Replacing rather than dropping the line keeps line numbers stable, which
    matters because Function is keyed in part on line number.
    """
    cleaned = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("%") or stripped.startswith("!"):
            cleaned.append("#" + line)
        else:
            cleaned.append(line)
    return cleaned


def notebook_source(path: Path):
    """Flatten a notebook's code cells into one synthetic Python source.

    Cells share a namespace under Jupyter/Databricks execution, so treating
    the whole notebook as one file - rather than parsing each cell alone -
    is what lets a `spark.table(...)` call in one cell resolve against a
    `def` in another.
    """
    data = json.loads(path.read_text(encoding="utf-8"))

    parts = []
    for cell in data.get("cells", []):
        if cell.get("cell_type") != "code":
            continue

        source = cell.get("source", [])
        if isinstance(source, str):
            source = source.splitlines(keepends=True)

        parts.append("".join(_clean_cell_source(source)))

    return "\n\n".join(parts)


def parse_notebook_file(path: Path):
    source = notebook_source(path)
    parsed = parse_python_source(source, path)
    # An .ipynb is a notebook regardless of whether any cell happens to
    # start with the Databricks .py export's text marker.
    parsed["is_notebook"] = True
    return parsed
