import pytest

from ingest.git_history import extract_issue_numbers, parse_hunk_ranges


@pytest.mark.parametrize("message, expected", [
    ("Fixes #12", [12]),
    ("Closes #3 and resolves #7", [3, 7]),
    ("no reference here", []),
    ("#1", [1]),
    # deduplicated and sorted, not in message order
    ("Refs #9, #2, #9", [2, 9]),
    # a trailing word char after the digits is not an issue ref
    ("build #1a2b", []),
    # preceded by a word char or hyphen - not a GitHub-style ref
    ("channel C#5", []),
    ("ISO-#5", []),
])
def test_extract_issue_numbers(message, expected):
    assert extract_issue_numbers(message) == expected


def test_hunk_ranges_with_explicit_count():
    patch = "@@ -1,4 +10,3 @@ def thing():\n context\n+added\n"

    assert parse_hunk_ranges(patch) == [(10, 12)]


def test_hunk_ranges_with_omitted_count_means_one_line():
    patch = "@@ -1 +7 @@\n+one\n"

    assert parse_hunk_ranges(patch) == [(7, 7)]


def test_pure_deletion_hunk_is_a_zero_width_range():
    """A deletion still changed the function it was removed from.

    Dropping these would make "who last changed this function" silently
    skip every cleanup commit.
    """
    patch = "@@ -5,3 +4,0 @@" + chr(10) + "-gone" + chr(10)

    assert parse_hunk_ranges(patch) == [(4, 4)]


def test_deletion_at_start_of_file_clamps_to_line_one():
    # Git reports +0,0 when the very first lines of a file are removed.
    patch = "@@ -1,2 +0,0 @@" + chr(10) + "-gone" + chr(10)

    assert parse_hunk_ranges(patch) == [(1, 1)]


def test_multiple_hunks():
    patch = (
        "@@ -1,2 +1,2 @@\n context\n"
        "@@ -20,1 +20,5 @@\n+more\n"
    )

    assert parse_hunk_ranges(patch) == [(1, 2), (20, 24)]


def test_non_hunk_lines_are_ignored():
    patch = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,1 +1,1 @@\n"
    )

    assert parse_hunk_ranges(patch) == [(1, 1)]
