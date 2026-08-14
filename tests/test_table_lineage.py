"""SQL/PySpark table reference extraction in ingest/parse_code.py."""

from ingest.parse_code import (
    extract_tables_from_sql,
    normalize_table_name,
    parse_python_source,
)


def test_normalize_table_name_strips_backticks_and_lowercases():
    assert normalize_table_name("`Main`.`Sales`.`Transactions`") == "main.sales.transactions"
    assert normalize_table_name("Main.Sales.Transactions") == "main.sales.transactions"


def test_spark_table_read_is_recorded():
    source = '''\
def load():
    return spark.table("main.sales.transactions")
'''
    parsed = parse_python_source(source, "sample.py")
    fn = parsed["functions"][0]

    assert fn["tables"] == [
        {"table": "main.sales.transactions", "access": "READS", "qualified": True}
    ]


def test_save_as_table_is_recorded_as_write():
    source = '''\
def save(df):
    df.write.saveAsTable("main.sales.transactions")
'''
    parsed = parse_python_source(source, "sample.py")
    fn = parsed["functions"][0]

    assert fn["tables"] == [
        {"table": "main.sales.transactions", "access": "WRITES", "qualified": True}
    ]


def test_insert_into_is_recorded_as_write():
    source = '''\
def run():
    spark.sql("INSERT INTO main.sales.transactions VALUES (1)")
'''
    parsed = parse_python_source(source, "sample.py")
    fn = parsed["functions"][0]

    assert fn["tables"] == [
        {"table": "main.sales.transactions", "access": "WRITES", "qualified": True}
    ]


def test_delete_from_is_a_write_not_a_read():
    """DELETE FROM x must not also match the FROM read pattern."""
    refs = extract_tables_from_sql("DELETE FROM main.sales.transactions WHERE id = 1")

    assert refs == [("main.sales.transactions", "WRITES")]


def test_select_from_is_a_read():
    refs = extract_tables_from_sql("SELECT * FROM main.sales.transactions")

    assert refs == [("main.sales.transactions", "READS")]


def test_unqualified_table_name_is_marked_unqualified():
    source = '''\
def load():
    return spark.table("transactions")
'''
    parsed = parse_python_source(source, "sample.py")
    fn = parsed["functions"][0]

    assert fn["tables"] == [
        {"table": "transactions", "access": "READS", "qualified": False}
    ]


def test_dynamic_table_name_is_invisible_not_guessed():
    """An f-string/variable table name is not resolvable from AST alone -
    it must be skipped, not misattributed."""
    source = '''\
def load(name):
    return spark.table(name)
'''
    parsed = parse_python_source(source, "sample.py")
    fn = parsed["functions"][0]

    assert fn["tables"] == []


def test_module_level_tables_are_recorded_without_a_synthetic_function():
    """A Databricks notebook's lineage mostly lives at the top level of the
    file, not inside a def. That access is reported via the top-level
    `module_tables` key, not wrapped in a synthetic "<module>" Function -
    minting one would inflate the Function count of every file with any
    top-level code, notebook or not."""
    source = '''\
# Databricks notebook source
df = spark.table("main.sales.transactions")
'''
    parsed = parse_python_source(source, "sample.py")

    assert parsed["module_tables"] == [
        {"table": "main.sales.transactions", "access": "READS", "qualified": True}
    ]
    assert [fn["name"] for fn in parsed["functions"]] == []


def test_ordinary_script_does_not_get_a_spurious_module_function():
    source = '''\
if __name__ == "__main__":
    print("hello")
'''
    parsed = parse_python_source(source, "sample.py")

    assert [fn["name"] for fn in parsed["functions"]] == []


def test_databricks_py_export_is_detected_as_notebook():
    source = '''\
# Databricks notebook source
x = 1
'''
    parsed = parse_python_source(source, "sample.py")

    assert parsed["is_notebook"] is True


def test_ordinary_script_is_not_a_notebook():
    source = "x = 1\n"
    parsed = parse_python_source(source, "sample.py")

    assert parsed["is_notebook"] is False
