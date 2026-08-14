"""ingest/parse_notebook.py - .ipynb JSON notebook parsing."""

import json
from pathlib import Path

from ingest.parse_notebook import (
    list_notebook_files,
    notebook_source,
    parse_notebook_file,
)


def _write_notebook(path: Path, code_cells):
    data = {
        "cells": [
            {"cell_type": "code", "source": src.splitlines(keepends=True)}
            for src in code_cells
        ]
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def test_code_cells_are_flattened_in_order(tmp_path):
    nb = tmp_path / "sample.ipynb"
    _write_notebook(nb, ["a = 1\n", "b = 2\n"])

    source = notebook_source(nb)

    assert "a = 1" in source
    assert "b = 2" in source
    assert source.index("a = 1") < source.index("b = 2")


def test_non_code_cells_are_skipped(tmp_path):
    nb = tmp_path / "sample.ipynb"
    data = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Title\n"]},
            {"cell_type": "code", "source": ["x = 1\n"]},
        ]
    }
    nb.write_text(json.dumps(data), encoding="utf-8")

    source = notebook_source(nb)

    assert "Title" not in source
    assert "x = 1" in source


def test_magics_and_shell_escapes_are_commented_out(tmp_path):
    nb = tmp_path / "sample.ipynb"
    _write_notebook(nb, ["%pip install foo\n", "!ls\n", "y = 2\n"])

    source = notebook_source(nb)

    assert "#%pip install foo" in source
    assert "#!ls" in source
    # A real .py parse must not choke on the magics/shell lines.
    parsed = parse_notebook_file(nb)
    assert parsed["is_notebook"] is True


def test_parse_notebook_file_sets_is_notebook_regardless_of_marker(tmp_path):
    nb = tmp_path / "sample.ipynb"
    _write_notebook(nb, ["spark.table('main.sales.transactions')\n"])

    parsed = parse_notebook_file(nb)

    assert parsed["is_notebook"] is True
    # Module-level table access is reported via module_tables, not a
    # synthetic "<module>" Function - see ingest/parse_code.py.
    assert parsed["module_tables"][0]["table"] == "main.sales.transactions"
    assert [fn["name"] for fn in parsed["functions"]] == []


def test_list_notebook_files_excludes_checkpoints(tmp_path):
    (tmp_path / ".ipynb_checkpoints").mkdir()
    _write_notebook(tmp_path / ".ipynb_checkpoints" / "sample-checkpoint.ipynb", ["x = 1\n"])
    _write_notebook(tmp_path / "sample.ipynb", ["x = 1\n"])

    found = list_notebook_files(tmp_path)

    assert [p.name for p in found] == ["sample.ipynb"]


def test_list_notebook_files_excludes_venv(tmp_path):
    (tmp_path / ".venv").mkdir()
    _write_notebook(tmp_path / ".venv" / "leftover.ipynb", ["x = 1\n"])
    _write_notebook(tmp_path / "sample.ipynb", ["x = 1\n"])

    found = list_notebook_files(tmp_path)

    assert [p.name for p in found] == ["sample.ipynb"]
