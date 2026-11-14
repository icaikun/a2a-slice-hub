"""Baseline-owned pytest runner. Local execution is NOT an adversarial security sandbox."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from .errors import GateRejected, InfrastructureError
from .paths import protected, safe_path

# This driver is owned by the Hub, never read from a candidate commit.
DRIVER = r'''
import json, pathlib, sys, types
import pytest
trusted, candidate, output, selection = sys.argv[1:]
sys.path.insert(0, candidate)
if (pathlib.Path(candidate) / "src").is_dir():
    sys.path.insert(0, str(pathlib.Path(candidate) / "src"))
sys.path.insert(0, trusted)
# A candidate regular tests package must not override a trusted namespace suite.
if not (pathlib.Path(trusted) / "tests" / "__init__.py").exists():
    suite = types.ModuleType("tests")
    suite.__path__ = [str(pathlib.Path(trusted) / "tests")]
    sys.modules["tests"] = suite
class Evidence:
    def __init__(self):
        self.collected = []
        self.passed = []
        self.skipped = []
        self.failed = []
    def pytest_collection_finish(self, session):
        self.collected = [i.nodeid for i in session.items]
    def pytest_runtest_logreport(self, report):
        if report.skipped:
            self.skipped.append(report.nodeid)
        if report.failed or hasattr(report, "wasxfail"):
            self.failed.append(report.nodeid)
        if report.when == "call" and report.passed:
            self.passed.append(report.nodeid)
e = Evidence()
args = ["-q", "--import-mode=importlib", "-p", "no:cacheprovider", "--strict-config", "--strict-markers",
        "--rootdir", trusted, "--confcutdir", trusted, "-c", str(pathlib.Path(trusted) / "pytest.ini")]
args += json.loads(selection)
code = pytest.main(args, plugins=[e])
pathlib.Path(output).write_text(json.dumps(vars(e)), encoding="utf-8", newline="\n")
raise SystemExit(code)
'''


class TrustedPytestRunner:
    def __init__(self, *, allow_local_execution=False, timeout=120):
        self.allow_local_execution = allow_local_execution
        self.timeout = timeout

    def run(self, git, base, candidate, slices):
        if not self.allow_local_execution:
            raise InfrastructureError("configure an isolated runner or explicitly opt into trusted local execution")
        try:
            manifest = json.loads(git.blob(base, ".hub/tests.json"))
            selected = set(manifest["accepted"])
            for slice_id in slices:
                selected.update(manifest["required"][slice_id])
            if not selected or any(not isinstance(n, str) or "::" not in n for n in selected):
                raise ValueError("explicit test node IDs required")
            for node in selected:
                filename = safe_path(node.split("::", 1)[0])
                if not filename.startswith("tests/"):
                    raise ValueError("test outside protected suite")
        except (KeyError, TypeError, ValueError) as exc:
            raise GateRejected("invalid trusted test manifest") from exc
        tree = git.tree(base)
        for _ in range(16):
            trusted = git.scratch / ("suite-" + secrets.token_hex(4))
            try:
                trusted.mkdir(parents=False, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise InfrastructureError("cannot allocate test runner directory")
        try:
            with git.worktree(candidate) as work:
                for name, (mode, _, _) in tree.items():
                    if protected(name) or name.startswith("tests/"):
                        if mode not in {"100644", "100755"}:
                            raise GateRejected("non-regular trusted baseline file")
                        target = trusted / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(git.blob(base, name))
                for name, (mode, _, _) in git.tree(candidate).items():
                    if name.startswith("contracts/") and not protected(name):
                        if mode not in {"100644", "100755"}:
                            raise GateRejected("non-regular contract file")
                        target = trusted / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(git.blob(candidate, name))
                config = trusted / "pytest.ini"
                if not config.exists():
                    # Never inherit candidate configuration through discovery.
                    config.write_text("[pytest]\n", encoding="utf-8", newline="\n")
                output = trusted / "evidence.json"
                allowed_env = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG"}
                env = {k: v for k, v in os.environ.items()
                       if not k.upper().startswith(("PYTHON", "PYTEST", "GIT_")) and k.upper() in allowed_env}
                env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
                try:
                    result = subprocess.run([sys.executable, "-I", "-c", DRIVER, str(trusted), str(work),
                        str(output), json.dumps(sorted(selected))], cwd=trusted, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout, check=False)
                    evidence = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                    raise InfrastructureError("test runner failed or timed out") from exc
                if (result.returncode != 0 or set(evidence.get("collected", [])) != selected
                        or set(evidence.get("passed", [])) != selected or evidence.get("skipped")
                        or evidence.get("failed") or len(evidence["collected"]) != len(selected)):
                    raise GateRejected("trusted tests failed, skipped, xfailed, or collected unexpected IDs")
                return evidence
        finally:
            for attempt in range(6):
                try:
                    if trusted.exists():
                        shutil.rmtree(trusted)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
