"""Real Git subprocesses, immutable candidates and disposable detached worktrees."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from .errors import GateRejected, InfrastructureError
from .paths import safe_path


def checked_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise GateRejected("full object ID required")
    return value


class Git:
    def __init__(self, repository, scratch):
        self.repository = Path(repository).resolve()
        self.scratch = Path(scratch).resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)

    def run(self, *args, cwd=None, data=None, timeout=30, check=True, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Slice Hub", GIT_AUTHOR_EMAIL="hub@example.invalid",
                   GIT_COMMITTER_NAME="Slice Hub", GIT_COMMITTER_EMAIL="hub@example.invalid")
        env.update(extra_env or {})
        command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false",
                   "-c", "commit.gpgSign=false", "-c", "core.attributesFile=" + os.devnull,
                   "-c", "protocol.file.allow=never", "-c", "submodule.recurse=false", *args]
        try:
            result = subprocess.run(command, cwd=cwd or self.repository, env=env, input=data,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfrastructureError("Git process unavailable or timed out") from exc
        if check and result.returncode:
            if b".lock" in result.stderr or b"Permission denied" in result.stderr:
                raise InfrastructureError("Git lock or filesystem unavailable; retry")
            # Do not relay absolute workspace paths, credentials or hook output to clients.
            raise GateRejected("Git operation failed: " + args[0])
        return result

    def text(self, *args, **kwargs):
        return self.run(*args, **kwargs).stdout.decode("utf-8", errors="strict").strip()

    def commit(self, sha):
        checked_sha(sha)
        return self.text("rev-parse", "--verify", sha + "^{commit}")

    def ref(self, branch):
        self.run("check-ref-format", "refs/heads/" + branch)
        return self.text("rev-parse", "--verify", "refs/heads/" + branch)

    def ancestor(self, before, after):
        self.commit(before)
        self.commit(after)
        return self.run("merge-base", "--is-ancestor", before, after, check=False).returncode == 0

    def blob(self, sha, filename):
        self.commit(sha)
        safe_path(filename)
        return self.run("show", sha + ":" + filename).stdout

    def tree(self, sha):
        self.commit(sha)
        entries = {}
        for record in self.run("ls-tree", "-r", "-z", sha).stdout.split(b"\0"):
            if not record:
                continue
            metadata, filename = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            name = filename.decode("utf-8", errors="strict")
            safe_path(name)
            entries[name] = (mode, kind, oid)
        folded = [p.casefold() for p in entries]
        if len(set(folded)) != len(folded):
            raise GateRejected("case-colliding paths are not portable")
        return entries

    def changes(self, before, after):
        self.commit(before)
        self.commit(after)
        parts = self.run("diff", "--name-status", "-M", "-z", before, after, "--").stdout.split(b"\0")
        changes, i = [], 0
        while i < len(parts) and parts[i]:
            status = parts[i].decode("ascii")
            count = 2 if status[0] in {"R", "C"} else 1
            paths = [p.decode("utf-8", errors="strict") for p in parts[i + 1:i + 1 + count]]
            if len(paths) != count:
                raise GateRejected("malformed Git diff")
            changes.append((status, paths))
            i += count + 1
        return changes

    @contextmanager
    def worktree(self, sha):
        self.commit(sha)
        directory = Path(tempfile.mkdtemp(prefix="tree-", dir=self.scratch))
        added = False
        try:
            self.run("worktree", "add", "--detach", str(directory), sha)
            added = True
            yield directory
        finally:
            if added:
                self.run("worktree", "remove", "--force", str(directory), check=False)
            # Antivirus may briefly retain handles after Git/pytest exits on Windows.
            for attempt in range(6):
                try:
                    if directory.exists():
                        shutil.rmtree(directory)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            self.run("worktree", "prune", check=False)

    def candidate(self, base, sources):
        self.commit(base)
        sources = sorted(set(sources))
        if not sources:
            raise GateRejected("empty candidate")
        current = base
        stamp = self.text("show", "-s", "--format=%cI", base)
        dates = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        with self.worktree(base) as work:
            for source in sources:
                self.commit(source)
                self.run("merge", "--no-commit", "--no-ff", source, cwd=work)
                tree = self.text("write-tree", cwd=work)
                current = self.text("commit-tree", tree, "-p", current, "-p", source,
                                    data=b"Validated slice candidate\n", extra_env=dates)
                self.run("reset", "--hard", current, cwd=work)
            self.run("update-ref", "refs/hub/candidates/" + current, current)
        return current

    def publish(self, intent):
        # One total wall-time budget, not a fresh long timeout for each Git command.
        deadline = time.monotonic() + 5
        branch = "refs/heads/" + intent["branch"]
        candidate, base = checked_sha(intent["candidate_sha"]), checked_sha(intent["base_sha"])
        def bounded(*args, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateRejected("ref publication timed out; recover the durable intent")
            return self.run(*args, timeout=remaining, **kwargs)
        def current_ref():
            return bounded("rev-parse", "--verify", branch).stdout.decode("ascii").strip()
        current = current_ref()
        if current == candidate:
            return True
        if current != base:
            result = bounded("merge-base", "--is-ancestor", candidate, current, check=False)
            if result.returncode not in {0, 1}:
                raise GateRejected("cannot reconcile Git ancestry")
            return result.returncode == 0
        result = bounded("update-ref", branch, candidate, base, check=False)
        if result.returncode:
            if current_ref() != base:
                return False
            raise GateRejected("Git ref update failed; intent remains recoverable")
        return True
