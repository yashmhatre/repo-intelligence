"""Integration tests for commit -> function attribution.

These build a real throwaway git repository rather than mocking GitPython:
the logic under test is precisely the interaction between diff hunks and the
AST of a file *at a past revision*, which a mock would define away.
"""

import pytest

from git import Repo

from ingest.git_history import changed_function_names, get_git_history


TWO_FUNCTIONS = '''\
def alpha():
    return 1


def beta():
    return 2
'''

BETA_EDITED = '''\
def alpha():
    return 1


def beta():
    value = 2
    return value
'''

# alpha grows by one line, which shifts beta down without changing it.
ALPHA_EDITED = '''\
def alpha():
    extra = 0
    return 1 + extra


def beta():
    value = 2
    return value
'''


@pytest.fixture
def repo(tmp_path):
    """A git repo with a single committed file."""
    git_repo = Repo.init(tmp_path)

    with git_repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test User")

    (tmp_path / "mod.py").write_text(TWO_FUNCTIONS, encoding="utf-8")
    git_repo.index.add(["mod.py"])
    git_repo.index.commit("initial")

    return tmp_path, git_repo


def commit_change(tmp_path, git_repo, content, message):
    (tmp_path / "mod.py").write_text(content, encoding="utf-8")
    git_repo.index.add(["mod.py"])
    return git_repo.index.commit(message)


def names_for(tmp_path, history_entry):
    return changed_function_names(
        str(tmp_path),
        history_entry["hash"],
        "mod.py",
        history_entry["changed_ranges"]["mod.py"],
    )


def test_initial_commit_attributes_every_function(repo):
    tmp_path, git_repo = repo

    history = get_git_history(str(tmp_path))
    initial = history[-1]

    # No parent, so the whole file is new.
    assert initial["changed_ranges"]["mod.py"] is None
    assert names_for(tmp_path, initial) == ["alpha", "beta"]


def test_editing_one_function_attributes_only_that_function(repo):
    tmp_path, git_repo = repo
    commit_change(tmp_path, git_repo, BETA_EDITED, "edit beta")

    history = get_git_history(str(tmp_path))

    assert names_for(tmp_path, history[0]) == ["beta"]


def test_a_function_merely_shifted_down_is_not_attributed(repo):
    """The regression this whole approach exists to prevent.

    Editing alpha pushes beta to different line numbers. Matching the hunk
    against *current* line numbers would wrongly blame beta.
    """
    tmp_path, git_repo = repo
    commit_change(tmp_path, git_repo, BETA_EDITED, "edit beta")
    commit_change(tmp_path, git_repo, ALPHA_EDITED, "edit alpha")

    history = get_git_history(str(tmp_path))

    assert names_for(tmp_path, history[0]) == ["alpha"]


def test_methods_are_attributed_like_functions(repo):
    tmp_path, git_repo = repo

    commit_change(
        tmp_path,
        git_repo,
        TWO_FUNCTIONS + '\n\nclass Holder:\n\n    def gamma(self):\n        return 3\n',
        "add class",
    )

    history = get_git_history(str(tmp_path))

    assert names_for(tmp_path, history[0]) == ["Holder.gamma"]


def test_deleted_file_attributes_nothing(repo):
    tmp_path, git_repo = repo

    git_repo.index.remove(["mod.py"], working_tree=True)
    git_repo.index.commit("remove mod.py")

    # The path is gone from this commit's tree, so there is no revision to
    # parse and nothing to attribute.
    assert changed_function_names(
        str(tmp_path), git_repo.head.commit.hexsha, "mod.py", [(1, 5)]
    ) == []


def test_unparseable_revision_is_skipped_not_raised(repo):
    tmp_path, git_repo = repo
    commit_change(tmp_path, git_repo, "def broken(:\n", "syntax error")

    history = get_git_history(str(tmp_path))

    assert names_for(tmp_path, history[0]) == []


def test_issue_reference_is_captured_from_a_real_commit(repo):
    tmp_path, git_repo = repo
    commit_change(tmp_path, git_repo, BETA_EDITED, "Fix the thing, closes #42")

    history = get_git_history(str(tmp_path))

    from ingest.git_history import extract_issue_numbers

    assert extract_issue_numbers(history[0]["message"]) == [42]


TWO_CLASSES_SAME_METHOD = '''class Alpha:

    def run(self):
        return 1


class Beta:

    def run(self):
        return 2
'''

ALPHA_RUN_EDITED = '''class Alpha:

    def run(self):
        value = 1
        return value


class Beta:

    def run(self):
        return 2
'''


def test_same_method_name_in_two_classes_is_not_cross_attributed(repo):
    """Editing Alpha.run must not also blame Beta.run.

    Bare-name matching would attribute the commit to both, which quietly
    breaks the "which functions the diff actually touched" claim.
    """
    tmp_path, git_repo = repo
    commit_change(tmp_path, git_repo, TWO_CLASSES_SAME_METHOD, "two classes")
    commit_change(tmp_path, git_repo, ALPHA_RUN_EDITED, "edit Alpha.run")

    history = get_git_history(str(tmp_path))

    assert names_for(tmp_path, history[0]) == ["Alpha.run"]


DEAD_LINES = '''def alpha():
    unused = 1
    dead = 2
    return 3


def beta():
    return 4
'''

DEAD_LINES_REMOVED = '''def alpha():
    return 3


def beta():
    return 4
'''


def test_a_deletion_only_commit_still_attributes_to_the_function(repo):
    tmp_path, git_repo = repo
    commit_change(tmp_path, git_repo, DEAD_LINES, "add dead lines")
    commit_change(tmp_path, git_repo, DEAD_LINES_REMOVED, "remove dead lines")

    history = get_git_history(str(tmp_path))

    assert names_for(tmp_path, history[0]) == ["alpha"]


def test_a_merge_commit_claims_nothing(repo):
    """Otherwise every merged change is counted twice.

    Diffing a merge against its first parent reports the whole merged
    branch as the merge commit's work, so "who last changed beta" would
    return whoever pressed merge rather than the author.
    """
    tmp_path, git_repo = repo

    main_branch = git_repo.active_branch.name
    git_repo.create_head("feat").checkout()
    commit_change(tmp_path, git_repo, BETA_EDITED, "edit beta on a branch")

    git_repo.heads[main_branch].checkout()
    git_repo.git.merge("feat", "--no-ff", "-m", "merge feat")

    history = get_git_history(str(tmp_path))
    merge_commit = history[0]

    assert merge_commit["is_merge"] is True
    assert merge_commit["changed_files"] == []
    assert merge_commit["changed_ranges"] == {}
