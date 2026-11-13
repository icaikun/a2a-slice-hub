"""A single execution slot with actual Git worktrees; input-required releases the slot."""
from dataclasses import dataclass
import threading
import inspect
import json

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

    def execute(self, attempt_id, base_sha, task, coding, *, record_recovery=False, checkpoint_patch=None, checkpoint_base_sha=None, rework_feedback=None):
        if not self._slot.acquire(blocking=False):
            raise Conflict("worker execution slot busy")
        attempt = None
        stop = threading.Event()
        cancel_event = threading.Event()
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
                        cancel_event.set()
                        return
            renewer = threading.Thread(target=renew, name="lease-renewal", daemon=True)
            renewer.start()
            item = self.store.get_item(attempt["work_item_id"])
            allowed_paths = json.loads(item["allowed_paths"])
            recall_kwargs = {"task": task}
            try:
                parameters = inspect.signature(self.experience.recall_experience).parameters
                for name, value in {"base_sha": base_sha, "slice_id": item["slice_id"],
                                    "paths": allowed_paths}.items():
                    parameter = parameters.get(name)
                    if parameter is not None and parameter.kind in (
                            inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
                        recall_kwargs[name] = value
            except (TypeError, ValueError, AttributeError):
                pass  # Opaque/legacy hooks keep their original task-only call.
            memories = self._hook("recall_experience", **recall_kwargs)
            with self.git.worktree(base_sha) as directory:
                from .coding import CLICodingAdapter, ExecutionContext
                from .context import build_task_bundle, write_task_bundle
                contextual = isinstance(coding, CLICodingAdapter)
                if not contextual:
                    try:
                        inspect.signature(coding).bind(directory, None)
                        contextual = True
                    except (TypeError, ValueError):
                        pass
                if contextual:
                    bundle = build_task_bundle(self.git, base_sha, attempt_id=attempt_id,
                        work_item_id=item["id"], slice_id=item["slice_id"], kind=item["kind"],
                        allowed_paths=allowed_paths, task_prompt=task,
                        memories=memories, worktree_dir=directory, rework_feedback=rework_feedback)
                    task_file = write_task_bundle(self.git.scratch, bundle)
                    ctx = ExecutionContext(attempt_id, item["id"], item["slice_id"], item["kind"],
                        base_sha, tuple(allowed_paths), bundle, task_file.parent, cancel_event,
                        checkpoint_patch, checkpoint_base_sha, memories)
                    result = coding(directory, ctx)
                else:
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
            # Legacy raw trajectory recording is NOT verified knowledge promotion.
            # KnowledgeStore implementations must promote only through record_verified_recovery.
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
            cancel_event.set()
            stop.set()
            if renewer:
                renewer.join(timeout=6)
            self._slot.release()

    def record_verified_recovery(self, *, messages, qa_verified=False):
        """Server-side post-QA hook; raw legacy records are advisory only."""
        if not qa_verified:
            raise Conflict("verified QA required for knowledge promotion")
        return self._hook("record_verified_recovery", messages=messages, verified_only=True)
