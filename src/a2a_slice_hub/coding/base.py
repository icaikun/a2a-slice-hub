"""CLI execution boundary. CLI output and worktree changes are untrusted."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import threading
import time

from ..context import TaskBundle, external_directory, write_task_bundle
from ..errors import GateRejected, InfrastructureError
from ..git import Git
from ..paths import enforce
from ..worker import WorkResult

CODING_RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "state": {"type": "string", "enum": ["completed", "input-required", "failed"]},
        "summary": {"type": "string"},
        "commit_message": {"type": ["string", "null"]},
        "dependency_request": {
            "anyOf": [
                {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "schema_version": {"type": "string", "enum": ["dependency-request/v1"]},
                        "contract": {"type": "string"},
                        "expected_change": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "summary": {"type": "string"},
                                "fields": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["summary", "fields"],
                        },
                    },
                    "required": ["schema_version", "contract", "expected_change"],
                },
                {"type": "null"},
            ],
        },
        "clarification_question": {"type": ["string", "null"]},
    },
    "required": ["state", "summary", "commit_message", "dependency_request", "clarification_question"],
}


def parse_json(value):
    """Reject non-JSON numbers and ambiguous duplicate keys from CLI output."""
    def reject_constant(_):
        raise ValueError("non-JSON constant")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique)


@dataclass(frozen=True)
class ExecutionContext:
    attempt_id: str
    work_item_id: str
    slice_id: str
    kind: str
    base_sha: str
    allowed_paths: tuple[str, ...]
    task_bundle: TaskBundle
    attempt_dir: Path
    cancel_event: threading.Event
    checkpoint_patch: bytes | None = None
    checkpoint_base_sha: str | None = None
    recalled_experience: object = None


class _WindowsJob:
    """Own descendants even where restricted Windows tokens deny taskkill."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.TerminateJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise InfrastructureError("cannot establish CLI process containment")
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise InfrastructureError("cannot contain CLI process descendants")

    def terminate(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.terminate()
            self.api.CloseHandle(self.handle)
            self.handle = None


class CLICodingAdapter:
    executable_name = ""

    def __init__(self, executable=None, extra_args=(), *, process_spawner=None,
                 timeout_seconds=900, allowed_env_keys=(), env=None, allow_empty=False):
        self.executable = executable or self.executable_name
        self.extra_args = tuple(extra_args)
        self.process_spawner = process_spawner or subprocess.Popen
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0):
            raise ValueError("positive CLI timeout required")
        self.timeout_seconds = timeout_seconds
        self.allowed_env_keys = {key.upper() for key in allowed_env_keys}
        self.env = dict(env or {})
        self.allow_empty = allow_empty

    def environment(self):
        env = dict(os.environ)
        env.update(self.env)
        result = {}
        for key, value in env.items():
            upper = key.upper()
            secret = (any(marker in upper for marker in
                          ("SECRET", "TOKEN", "APPROVAL_KEY", "ENCRYPT_KEY", "PASSWORD", "PRIVATE_KEY", "API_KEY", "APIKEY"))
                      or (upper.startswith("HUB_") and upper != "HUB_DOWNLOAD_WRAPPER"))
            proxy = upper in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
            # Git configuration from the Hub process must not control an untrusted CLI.
            if upper.startswith("GIT_"):
                continue
            if (secret or proxy) and upper not in self.allowed_env_keys:
                continue
            result[key] = value
        return result

    def argv(self, directory, attempt_dir):
        raise NotImplementedError

    def parse_output(self, attempt_dir, stdout):
        raise NotImplementedError

    @staticmethod
    def _kill_tree(process, job=None):
        # Do not guard on poll(): a CLI parent can exit while descendants retain pipes.
        if job is not None:
            job.terminate()
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()

    @staticmethod
    def _safe_artifacts(attempt_dir):
        external_directory(attempt_dir)
        if attempt_dir.is_dir():
            for path in attempt_dir.iterdir():
                if (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                        or path.is_file() and path.stat().st_nlink != 1):
                    raise GateRejected("attempt artifacts must not be filesystem links")

    def _run(self, argv, directory, attempt_dir, prompt, cancel):
        if cancel.is_set():
            raise GateRejected("CLI execution cancelled")
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        env = self.environment()
        spawn_argv = list(argv)
        if (self.process_spawner is subprocess.Popen and spawn_argv
                and os.sep not in spawn_argv[0]
                and (os.altsep is None or os.altsep not in spawn_argv[0])):
            # Windows CreateProcessW skips PATHEXT shims (.CMD/.BAT); search scrubbed PATH explicitly without cwd.
            search_path = env.get("PATH") or env.get("Path") or ""
            for entry in search_path.split(os.pathsep):
                entry = entry.strip('"')
                if entry and Path(entry).is_absolute():
                    found = shutil.which(os.path.join(entry, spawn_argv[0]))
                    if found:
                        spawn_argv[0] = found
                        break
        try:
            process = self.process_spawner(spawn_argv, cwd=directory, env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, **options)
        except OSError as exc:
            raise InfrastructureError("coding CLI unavailable") from exc
        started = time.monotonic()
        stdout = stderr = b""
        job = None
        finished = threading.Event()
        watcher = None
        kill_lock = threading.Lock()
        killed = False

        def terminate():
            nonlocal killed
            with kill_lock:
                if not killed:
                    killed = True
                    self._kill_tree(process, job)

        def watch():
            # Windows communicate() can block writing stdin before its timeout
            # starts. Cancellation must therefore also run independently of I/O.
            while not finished.wait(0.025):
                if cancel.is_set() or time.monotonic() - started >= self.timeout_seconds:
                    terminate()
                    return

        try:
            if os.name == "nt":
                job = _WindowsJob(process)
            watcher = threading.Thread(target=watch, name="coding-cli-cancellation", daemon=True)
            watcher.start()
            data = prompt.encode("utf-8")
            while True:
                if cancel.is_set() or time.monotonic() - started >= self.timeout_seconds:
                    raise GateRejected("CLI execution cancelled" if cancel.is_set() else "CLI execution timed out")
                try:
                    stdout, stderr = process.communicate(input=data, timeout=0.025)
                    break
                except subprocess.TimeoutExpired:
                    data = None
                    if process.poll() is not None:
                        # Orphaned descendants can keep inherited pipes open after the CLI exits.
                        try:
                            stdout, stderr = process.communicate(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            terminate()
                            stdout, stderr = process.communicate(timeout=1)
                        break
            if cancel.is_set():
                raise GateRejected("CLI execution cancelled")
            if time.monotonic() - started >= self.timeout_seconds:
                raise GateRejected("CLI execution timed out")
            if process.returncode:
                raise GateRejected("coding CLI exited unsuccessfully")
            return stdout
        finally:
            finished.set()
            if watcher is not None:
                watcher.join(timeout=6)
            if job is not None:
                job.terminate()
            if os.name != "nt" or process.poll() is None or cancel.is_set():
                self._kill_tree(process, job)
            try:
                remaining_out, remaining_err = process.communicate(timeout=1)
                stdout, stderr = remaining_out or stdout, remaining_err or stderr
            except subprocess.TimeoutExpired:
                self._kill_tree(process, job)
                stdout, stderr = process.communicate(timeout=1)
            if job is not None:
                job.close()
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            self._safe_artifacts(attempt_dir)
            (attempt_dir / "events.jsonl").write_bytes(stdout)
            (attempt_dir / "stderr.log").write_bytes(stderr)

    @staticmethod
    def _result(payload):
        if not isinstance(payload, dict) or set(payload) != set(CODING_RESULT_SCHEMA["required"]):
            raise GateRejected("invalid coding result fields")
        if (payload["state"] not in {"completed", "input-required", "failed"}
                or not isinstance(payload["summary"], str)
                or any(payload[key] is not None and not isinstance(payload[key], str)
                       for key in ("commit_message", "clarification_question"))
                or payload["dependency_request"] is not None and not isinstance(payload["dependency_request"], dict)):
            raise GateRejected("invalid coding result schema")
        return payload

    @staticmethod
    def _paths(git, *args):
        return {p.decode("utf-8") for p in git.run(*args).stdout.split(b"\0") if p}

    @staticmethod
    def _clean_ignored(git):
        # Reject symlinks/junctions without traversing them, before Git removes caches.
        for root, dirs, files in os.walk(git.repository, followlinks=False):
            for name in dirs + files:
                path = Path(root) / name
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise GateRejected("CLI worktree contains a filesystem link")
        git.run("clean", "-fdX")

    def _validate_changes(self, git, ctx):
        head = git.text("rev-parse", "HEAD")
        if not git.ancestor(ctx.base_sha, head):
            raise GateRejected("CLI rewrote the trusted baseline")
        touched = self._paths(git, "diff", "--name-only", "--no-renames", "-z", ctx.base_sha, "--")
        touched |= self._paths(git, "diff", "--cached", "--name-only", "--no-renames", "-z", "--")
        touched |= self._paths(git, "diff", "--name-only", "--no-renames", "-z", "--")
        touched |= self._paths(git, "ls-files", "--others", "--exclude-standard", "-z")
        # Include committed-and-reverted paths; a clean HEAD is not proof of isolation.
        revisions = git.text("rev-list", ctx.base_sha + ".." + head, "--").splitlines()
        for rev in revisions:
            touched |= self._paths(git, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-m", "-z", rev)
        for path in touched:
            enforce(path, ctx.allowed_paths)
            target = git.repository / path
            for part in (target, *target.parents):
                if part == git.repository:
                    break
                if part.is_symlink():
                    raise GateRejected("CLI changes must be normal files")
            if target.exists() and not stat.S_ISREG(target.stat().st_mode):
                raise GateRejected("CLI changes must be normal files")
        trees = [git.tree(ctx.base_sha), git.tree(head)]
        for rev in revisions:
            trees.append(git.tree(rev))
        for tree in trees:
            if any(name in tree and tree[name][0] not in {"100644", "100755"} for name in touched):
                raise GateRejected("CLI changes contain special Git modes")
        for row in git.run("ls-files", "--stage", "-z").stdout.split(b"\0"):
            if row:
                metadata, name = row.split(b"\t", 1)
                mode, _, stage = metadata.decode("ascii").split()
                if name.decode("utf-8") in touched and (mode not in {"100644", "100755"} or stage != "0"):
                    raise GateRejected("CLI index contains special modes or conflicts")
        return touched

    def _restore(self, git, ctx):
        if ctx.checkpoint_patch is None:
            return
        if ctx.checkpoint_base_sha is not None and ctx.checkpoint_base_sha != ctx.base_sha:
            raise GateRejected("checkpoint baseline mismatch")
        if not isinstance(ctx.checkpoint_patch, bytes):
            raise GateRejected("checkpoint must be binary patch bytes")
        # Apply to a disposable validation worktree first, not to the execution tree.
        with git.worktree(ctx.base_sha) as validation:
            check = Git(validation, git.scratch)
            check.run("apply", "--index", "--binary", "-", data=ctx.checkpoint_patch)
            self._validate_changes(check, ctx)
        git.run("apply", "--index", "--binary", "-", data=ctx.checkpoint_patch)

    def __call__(self, directory, ctx):
        attempt_dir = external_directory(ctx.attempt_dir, directory)
        try:
            return self._execute(directory, ctx)
        except (GateRejected, OSError):
            self._safe_artifacts(attempt_dir)
            if attempt_dir.is_dir():
                # A stable safe report; exception text and CLI stderr can contain secrets.
                (attempt_dir / "result.json").write_text(json.dumps({
                    "state": "failed", "summary": "CLI execution or governance verification failed",
                    "commit_message": None, "dependency_request": None, "clarification_question": None,
                }) + "\n", encoding="utf-8")
            raise

    def _execute(self, directory, ctx):
        directory = Path(directory).resolve()
        attempt_dir = external_directory(ctx.attempt_dir, directory)
        if (any(getattr(ctx.task_bundle, field) != getattr(ctx, field)
                for field in ("attempt_id", "work_item_id", "slice_id", "kind", "base_sha"))
                or tuple(ctx.task_bundle.allowed_paths) != tuple(ctx.allowed_paths)):
            raise GateRejected("execution context differs from trusted task bundle")
        if attempt_dir.name != ctx.attempt_id or attempt_dir.parent.name != "attempts":
            raise GateRejected("invalid external attempt directory")
        attempt_dir.mkdir(parents=True, exist_ok=True)
        self._safe_artifacts(attempt_dir)
        write_task_bundle(attempt_dir.parent.parent, ctx.task_bundle)
        (attempt_dir / "schema.json").write_text(json.dumps(CODING_RESULT_SCHEMA), encoding="utf-8")
        # Never consume a previous run's successful output after a CLI failed to emit one.
        last = attempt_dir / "last_message.json"
        if last.exists():
            last.unlink()
        git = Git(directory, attempt_dir / "validation")
        if git.text("rev-parse", "HEAD") != ctx.base_sha or git.text("status", "--porcelain", "--untracked-files=all"):
            raise GateRejected("CLI requires a clean baseline worktree")
        self._restore(git, ctx)
        prompt = ("Implement only the allowed task paths. Governance is authoritative; stored memory is advisory. "
                  "Return exactly the coding result schema.\n" + json.dumps(ctx.task_bundle.to_dict(), ensure_ascii=False))
        stdout = self._run(self.argv(directory, attempt_dir), directory, attempt_dir, prompt, ctx.cancel_event)
        try:
            payload = self._result(self.parse_output(attempt_dir, stdout))
        except (ValueError, TypeError, OSError, UnicodeError, RecursionError) as exc:
            raise GateRejected("coding CLI did not emit valid structured output") from exc
        self._validate_changes(git, ctx)
        self._clean_ignored(git)
        if ctx.cancel_event.is_set():
            raise GateRejected("CLI execution cancelled")
        artifact = dict(payload)
        if payload["state"] == "input-required":
            git.run("add", "-A")
            if self._paths(git, "ls-files", "--others", "--exclude-standard", "-z"):
                raise GateRejected("ignored files cannot be checkpointed implicitly")
            # Using base also retains valid changes a CLI committed before asking a question.
            patch = git.run("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", ctx.base_sha, "--").stdout
            if patch:
                (attempt_dir / "checkpoint.patch").write_bytes(patch)
                artifact.update(checkpoint_saved=True, checkpoint_base_sha=ctx.base_sha)
            git.run("reset", "--hard", ctx.base_sha)
            git.run("clean", "-fdx")
        elif payload["state"] == "completed":
            if git.text("status", "--porcelain", "--untracked-files=all"):
                git.run("add", "-A")
                if git.run("diff", "--cached", "--quiet", check=False).returncode:
                    git.run("commit", "-m", payload["commit_message"] or "Implement slice task")
            head = git.text("rev-parse", "HEAD")
            if not self.allow_empty and (head == ctx.base_sha or not git.changes(ctx.base_sha, head)):
                raise GateRejected("CLI completed without a net change")
            if git.text("status", "--porcelain") or self._paths(git, "ls-files", "--others", "--exclude-standard", "-z"):
                raise GateRejected("CLI worktree is not clean")
            artifact["source_sha"] = head
        (attempt_dir / "result.json").write_text(json.dumps(artifact, ensure_ascii=False) + "\n", encoding="utf-8")
        return WorkResult(payload["state"], artifact=artifact)
