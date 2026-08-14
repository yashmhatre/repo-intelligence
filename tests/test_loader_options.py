"""Option handling in the loader.

These do not touch Neo4j. Importing the loader builds a driver object, but
the neo4j driver connects lazily, and the check under test runs before any
session is opened.
"""

import pytest

from graph.neo4j_loader import ContradictoryOptions, index_repository


def test_clean_with_skip_history_is_refused():
    """The combination silently produced a misleading graph.

    --clean DETACH DELETEs the File and Function nodes that MODIFIES and
    CHANGES point at, taking those relationships with them, and
    --skip-history never rebuilds them. The run reported success while
    leaving every Commit attached to nothing.
    """
    with pytest.raises(ContradictoryOptions):
        index_repository(".", clean=True, skip_history=True)


def test_the_error_names_both_working_alternatives():
    with pytest.raises(ContradictoryOptions) as excinfo:
        index_repository(".", clean=True, skip_history=True)

    message = str(excinfo.value)

    assert "--clean" in message
    assert "--skip-history" in message
