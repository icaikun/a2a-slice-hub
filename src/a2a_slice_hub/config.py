"""Small environment-backed application factory; sample configuration contains no secrets."""
import os
from pathlib import Path

import yaml

from .api import Principal, create_app
from .approvals import Approvals
from .gatekeeper import Gatekeeper
from .git import Git
from .slices import SliceRegistry
from .runner import TrustedPytestRunner
from .store import Store


def load_app(filename, *, reviewer=None):
    config = yaml.safe_load(Path(filename).read_text(encoding="utf-8"))
    base = Path(filename).expanduser().resolve().parent
    def host_path(value):
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (base / path).resolve()
    database = host_path(config["database"])
    principals = {}
    for identity in config["principals"]:
        credential = os.environ[identity["credential_env"]]
        if credential in principals:
            raise ValueError("principals must not share a credential")
        principals[credential] = Principal(identity["name"], identity["role"], identity.get("owner_user_id"))
    git = Git(host_path(config["repository"]), host_path(config["scratch"])) if config.get("repository") else None
    registry = (SliceRegistry.load(host_path(config["slices"])) if config.get("slices") else
                SliceRegistry.from_git(git, git.ref(config.get("integration_branch", "epic/demo"))) if git else None)
    if registry is None and any(p.role == "worker" for p in principals.values()):
        raise ValueError("worker deployments require protected slice registry")
    if registry is not None or any(p.role == "worker" for p in principals.values()):
        if any(p.role == "qa" and (not isinstance(p.owner_user_id, str) or not p.owner_user_id.strip())
               for p in principals.values()):
            raise ValueError("QA principals require owner_user_id")
    store = Store(database, registry=registry)
    approvals = Approvals(store, os.environ[config["approval_key_env"]].encode("utf-8"))
    gatekeeper = None
    if config.get("repository"):
        gatekeeper = Gatekeeper(store, git, TrustedPytestRunner(
            allow_local_execution=config.get("allow_local_execution", False)), reviewer)
    return create_app(store, approvals, principals, gatekeeper=gatekeeper,
                      public_url=config.get("public_url", "http://localhost:8000"),
                      a2a_worker_id=config.get("worker_id"),
                      feishu_encrypt_key=os.environ[config["feishu_encrypt_key_env"]] if config.get("feishu_encrypt_key_env") else None,
                      feishu_users=config.get("feishu_users", {}),
                      dingtalk_secret=os.environ[config["dingtalk_secret_env"]] if config.get("dingtalk_secret_env") else None,
                      dingtalk_users=config.get("dingtalk_users", {}))
