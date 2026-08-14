"""Table.qualified_name MERGE key - the guard against fusing two unrelated
repos' same-named tables into one global node.

These do not touch Neo4j: table_merge_key() and table_params() are pure
functions of their arguments, and the fusion bug they guard against is
entirely visible at that level - it never needed a live database to prove.
"""

from graph.neo4j_loader import table_merge_key, table_params


def test_unqualified_names_from_different_repos_do_not_collide():
    """The test that would have caught the fusion.

    A naive global-merge implementation - `table_merge_key = lambda name,
    qualified, repo_key: name` - passes every "does lineage work" test and
    fails only this one: two repos' unrelated `transactions` tables would
    MERGE into a single Table node the moment both are indexed, and nothing
    would raise.
    """
    key_in_repo_a = table_merge_key("transactions", qualified=False, repo_key="/repos/a")
    key_in_repo_b = table_merge_key("transactions", qualified=False, repo_key="/repos/b")

    assert key_in_repo_a != key_in_repo_b


def test_unqualified_name_key_is_stable_for_the_same_repo():
    """Re-indexing the same repo must MERGE onto the same node, not mint a
    fresh one on every run."""
    first_run = table_merge_key("transactions", qualified=False, repo_key="/repos/a")
    second_run = table_merge_key("transactions", qualified=False, repo_key="/repos/a")

    assert first_run == second_run


def test_qualified_names_are_shared_globally_across_repos():
    """A real catalog.schema.table is one physical object - two repos that
    reference it must MERGE onto the same node, the same as Module."""
    key_in_repo_a = table_merge_key(
        "main.sales.transactions", qualified=True, repo_key="/repos/a"
    )
    key_in_repo_b = table_merge_key(
        "main.sales.transactions", qualified=True, repo_key="/repos/b"
    )

    assert key_in_repo_a == key_in_repo_b
    # And it is exactly the qualified name, not some derived/obscured form -
    # this is what makes it queryable directly.
    assert key_in_repo_a == "main.sales.transactions"


def test_table_params_folds_repo_into_unqualified_key_only():
    tables = [
        {"table": "main.sales.transactions", "access": "READS", "qualified": True},
        {"table": "transactions", "access": "WRITES", "qualified": False},
    ]

    params = table_params(tables, repo_key="/repos/a")

    qualified_entry = next(p for p in params if p["qualified"])
    unqualified_entry = next(p for p in params if not p["qualified"])

    assert qualified_entry["qualified_name"] == "main.sales.transactions"
    assert unqualified_entry["qualified_name"] != "transactions"
    assert "/repos/a" in unqualified_entry["qualified_name"]
