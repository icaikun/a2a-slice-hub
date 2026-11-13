"""A single execution slot with actual Git worktrees; input-required releases the slot."""
from dataclasses import dataclass
import threading

from .errors import Conflict
from .experience import NoExperience


@dataclass(frozen=True)
class WorkResult:
    state: str = "completed"
    artifact: dict | None = None
    trajectory: list[dict] | None = None


class Worker:
    def __init__(self, worker_id, store, git, experience=None):
        self.worker_id, self.store, self.git = worker_id, store, git
        self.experience = experience or NoExperience()
        self._slot = threading.Lock()
        self.hook_errors = 0

    def _hook(self, method, **kwargs):
        try:
            return getattr(self.experience, method)(**kwargs)
        except Exception:
            # Experience service availability must not corrupt task state or leak exception text.
            self.hook_errors += 1
            return None

    def execute(self, attempt_id, base_sha, task, coding, *, record_recovery=False):
        if not self._slot.acquire(blocking=False):
            raise Conflict("worker execution slot busy")
        attempt = None
        stop = threading.Event()
        renewer = None
        lease_lost = []
        try:
            attempt = self.store.get_attempt(attempt_id)
            attempt = self.store.transition(attempt_id, self.worker_id, attempt["epoch"], attempt["version"], "working")
            interval = max(0.1, min(20, (attempt["expires"] - self.store.clock()) / 3))
            def renew():
                while not stop.wait(interval):
                    try:
                        self.store.heartbeat(attempt_id, self.worker_id, attempt["epoch"])
                    except Exception:
                        lease_lost.append(True)
                        return
            renewer = threading.Thread(target=renew, name="lease-renewal", daemon=True)
            renewer.start()
            self._hook("recall_experience", task=task)
            with self.git.worktree(base_sha) as directory:
                result = coding(directory)
                if lease_lost:
                    raise Conflict("worker lease renewal failed")
                if result.state not in {"completed", "input-required", "failed"}:
                    raise ValueError("invalid worker result")
                source = self.git.text("rev-parse", "HEAD", cwd=directory) if result.state == "completed" else None
                if result.state == "completed" and self.git.text("status", "--porcelain", cwd=directory):
                    raise Conflict("worker must commit all changes before submission")
                if source:
                    self.git.run("update-ref", "refs/hub/submissions/" + source, source)
                completed = self.store.transition(attempt_id, self.worker_id, attempt["epoch"], attempt["version"],
                    result.state, base_sha=base_sha if source else None, source_sha=source, artifact=result.artifact)
            if record_recovery and result.trajectory:
                self._hook("record_trajectory", messages=result.trajectory)
            return completed
        except Exception:
            if attempt and attempt["state"] == "working":
                try:
                    self.store.transition(attempt_id, self.worker_id, attempt["epoch"], attempt["version"], "failed")
                except Conflict:
                    pass  # Expired epoch must never be revived to report a local failure.
            raise
        finally:
            stop.set()
            if renewer:
                renewer.join(timeout=6)
            self._slot.release()
