from pathlib import Path
from git import Repo
import re
import sys

# Allow imports from project root when run as a script
sys.path.append(str(Path(__file__).resolve().parents[1]))

from ingest.parse_code import parse_python_source, iter_function_ranges


# Reuse one Repo object per path. Reading historical blobs opens the object
# database repeatedly, and re-instantiating Repo for every read is slow.
_REPO_CACHE = {}

# Unified-diff hunk header. Only the new-side range matters here: we map
# changed lines onto the file as it looked *at that commit*.
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _repo(repo_path):
    key = str(Path(repo_path).resolve())

    if key not in _REPO_CACHE:
        _REPO_CACHE[key] = Repo(key)

    return _REPO_CACHE[key]


def ensure_git_repository(repo_path: str):
    """Open the repo and raise if it isn't one.

    Discovery only - the Repo object is cached, so the walk that follows
    reuses it rather than paying for a second traversal.
    """
    return _repo(repo_path)


def parse_hunk_ranges(patch_text: str):
    """Extract (start, end) line ranges on the new side of a unified diff.

    A hunk with a zero new-side count is a pure deletion. It still changed
    the function it was removed from, so it is recorded as a zero-width
    range at the line the deletion sits after, rather than dropped -
    otherwise every cleanup commit attributes to nothing and "who last
    changed this function" silently skips deletions.
    """
    ranges = []

    for line in patch_text.splitlines():
        match = HUNK_HEADER.match(line)

        if not match:
            continue

        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))

        if count > 0:
            ranges.append((start, start + count - 1))
        else:
            # Git reports +c,0 where c is the line the removed block
            # followed; c is 0 only when the file's first lines went.
            point = max(start, 1)
            ranges.append((point, point))

    return ranges


def get_git_history(repo_path: str):
    repo = _repo(repo_path)

    commits = []

    for commit in repo.iter_commits():
        changed_files = []

        # changed_ranges maps a path to the new-side line ranges it touched.
        # A value of None means "the whole file is new" - every function in
        # it counts as changed.
        changed_ranges = {}

        is_merge = len(commit.parents) > 1

        if is_merge:
            # Diffing a merge against its first parent reports the entire
            # merged branch as this commit's work, so every merged change
            # would be counted twice - once for the commit that made it and
            # once for whoever pressed merge. The individual commits are
            # already in the history with their own attribution, so the
            # merge itself claims nothing.
            pass

        elif commit.parents:
            parent = commit.parents[0]
            # unified=0 is load-bearing: with the default 3 lines of
            # context, a hunk's range spills into the functions either side
            # of the edit and attributes the commit to code it never
            # touched. Zero context makes the range exactly the changed
            # lines.
            diffs = parent.diff(commit, create_patch=True, unified=0)

            for diff in diffs:
                if diff.a_path:
                    changed_files.append(diff.a_path)

                if diff.b_path:
                    changed_files.append(diff.b_path)

                    patch = diff.diff

                    if isinstance(patch, bytes):
                        patch = patch.decode("utf-8", errors="replace")

                    changed_ranges[diff.b_path] = parse_hunk_ranges(patch)

        else:
            # Initial commit has no parent.
            # Every blob in the commit is a newly added file.
            for item in commit.tree.traverse():
                if item.type == "blob":
                    changed_files.append(item.path)
                    changed_ranges[item.path] = None

        commits.append({
            "hash": commit.hexsha,
            "message": commit.message.strip(),
            "author_name": commit.author.name,
            "author_email": commit.author.email,
            "authored_at": commit.authored_datetime.isoformat(),
            "changed_files": sorted(set(changed_files)),
            "changed_ranges": changed_ranges,
            "is_merge": is_merge,
        })

    return commits


def read_file_at_commit(repo_path: str, commit_hash: str, git_path: str):
    """Return file content as it existed at a commit, or None if the path
    isn't in that commit's tree (deleted, or renamed away)."""
    repo = _repo(repo_path)

    try:
        blob = repo.commit(commit_hash).tree / git_path
    except KeyError:
        return None

    return blob.data_stream.read().decode("utf-8", errors="replace")


def changed_function_names(repo_path, commit_hash, git_path, ranges):
    """Which functions in git_path a commit actually touched.

    Judged against the file as it looked *at that commit*, not as it looks
    today - line numbers drift as code moves, so matching a historical hunk
    against current line numbers would attribute changes to the wrong
    function. Names are then matched back to current Function nodes.

    ranges of None means the whole file is new (initial commit).
    """
    if ranges is not None and not ranges:
        return []

    source = read_file_at_commit(repo_path, commit_hash, git_path)

    if source is None:
        # Deleted at this commit, or the old side of a rename.
        return []

    try:
        parsed = parse_python_source(source, git_path)
    except SyntaxError:
        # A revision that didn't parse. Nothing to attribute.
        return []

    names = set()

    for name, start_line, end_line in iter_function_ranges(parsed):
        if end_line is None:
            end_line = start_line

        overlaps = ranges is None or any(
            hunk_start <= end_line and start_line <= hunk_end
            for hunk_start, hunk_end in ranges
        )

        if overlaps:
            names.add(name)

    return sorted(names)

def get_remote_url(repo_path: str):
    """Origin URL, or None for a repo with no remote."""
    repo = _repo(repo_path)

    try:
        return repo.remotes.origin.url
    except (AttributeError, ValueError):
        return None


def resolve_git_file_path(repo_path, git_path: str):
    repo = Path(repo_path).resolve()
    return str((repo / git_path).resolve())


def extract_issue_numbers(message: str):
    """
    Extract GitHub-style issue references.

    Examples:
        #12
        Fixes #12
        Closes #12
        Resolves #12
    """
    return sorted({
        int(number)
        for number in re.findall(r"(?<![\w-])#(\d+)\b", message)
    })



if __name__ == "__main__":
    history = get_git_history(".")

    print(f"Found {len(history)} commits")

    for commit in history[:5]:
        print("\nCOMMIT:", commit["hash"][:10])
        print("AUTHOR:", commit["author_name"])
        print("MESSAGE:", commit["message"])
        print("FILES:")

        for path in commit["changed_files"]:
            resolved = resolve_git_file_path(".", path)
            ranges = commit["changed_ranges"].get(path)

            print(f"  - Git:      {path}")
            print(f"    Resolved: {resolved}")
            print(f"    Ranges:   {'whole file' if ranges is None else ranges}")
