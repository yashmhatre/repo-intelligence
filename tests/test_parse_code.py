from ingest.parse_code import parse_python_source, iter_function_ranges


SOURCE = '''\
import os
from pathlib import Path


def top_level(a):
    return a + 1


class Widget:

    def method_one(self):
        return 1

    async def method_two(self):
        return 2
'''


def test_functions_carry_end_line():
    parsed = parse_python_source(SOURCE, "sample.py")

    fn = parsed["functions"][0]

    assert fn["name"] == "top_level"
    assert fn["line"] == 5
    assert fn["end_line"] == 6


def test_methods_are_not_double_counted_as_top_level():
    parsed = parse_python_source(SOURCE, "sample.py")

    assert [f["name"] for f in parsed["functions"]] == ["top_level"]
    assert [c["name"] for c in parsed["classes"]] == ["Widget"]


def test_iter_function_ranges_includes_methods():
    parsed = parse_python_source(SOURCE, "sample.py")

    ranges = {name: (start, end) for name, start, end in iter_function_ranges(parsed)}

    assert ranges["top_level"] == (5, 6)
    # Methods are yielded qualified, so two classes in one file can each
    # define the same method name without collapsing into one target.
    assert ranges["Widget.method_one"] == (11, 12)
    # async methods count too
    assert ranges["Widget.method_two"] == (14, 15)


def test_imports_are_collected():
    parsed = parse_python_source(SOURCE, "sample.py")

    assert parsed["imports"] == ["os", "pathlib"]


SAME_NAME_IN_TWO_CLASSES = '''class Alpha:

    def run(self):
        return 1


class Beta:

    def run(self):
        return 2
'''


def test_same_method_name_in_two_classes_stays_distinct():
    parsed = parse_python_source(SAME_NAME_IN_TWO_CLASSES, "sample.py")

    names = [name for name, _, _ in iter_function_ranges(parsed)]

    assert names == ["Alpha.run", "Beta.run"]


DECORATED = '''import functools


@functools.lru_cache(maxsize=8)
def fetch():
    return 1
'''


def test_range_starts_at_the_decorator_not_the_def():
    """Editing a decorator edits the function.

    node.lineno points at `def`, so a decorator-only change would fall
    outside the range and be attributed to nothing.
    """
    parsed = parse_python_source(DECORATED, "sample.py")

    ranges = {name: (start, end) for name, start, end in iter_function_ranges(parsed)}

    # decorator is line 4, def is line 5
    assert ranges["fetch"][0] == 4
