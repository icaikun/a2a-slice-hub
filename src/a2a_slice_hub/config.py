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
from .policy import ExternalAccount, IdentityPolicy, MemoryScope, PersonRecord


def _identity_policy(value):
    if not isinstance(value, dict) or set(value) - {"persons", "bindings", "contacts"}:
        raise ValueError("invalid identity policy configuration")
    def records(key):
        rows = value.get(key, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("identity policy entries must be object lists")
        return rows
    persons = []
    for row in records("persons"):
        if set(row) - {"person_id", "roles", "owned_slices", "scopes"} or "person_id" not in row:
            raise ValueError("invalid person configuration")
        for key in ("roles", "owned_slices", "scopes"):
            if not isinstance(row.get(key, []), list):
                raise ValueError("person roles, slices and scopes must be lists")
        try:
            scopes = frozenset(MemoryScope(**scope) for scope in row.get("scopes", []))
            persons.append(PersonRecord(row["person_id"], row.get("roles", []), row.get("owned_slices", []), scopes))
        except TypeError as exc:
            raise ValueError("invalid person configuration") from exc
    def accounts(key, field):
        result = {}
        for row in records(key):
            if set(row) != {"account", field} or not isinstance(row["account"], dict):
                raise ValueError("invalid external account configuration")
            try:
                account = ExternalAccount(**row["account"])
            except TypeError as exc:
                raise ValueError("invalid external account configuration") from exc
            if account in result or not isinstance(row[field], str):
                raise ValueError("duplicate or invalid external account configuration")
            result[account] = row[field]
        return result
    bindings = accounts("bindings", "person_id")
    contacts = accounts("contacts", "admission")
    if any(admission not in {"pairing", "allowlist"} for admission in contacts.values()):
        raise ValueError("invalid contact admission")
    return IdentityPolicy(persons, bindings, contacts=contacts)


def load_app(filename, *, reviewer=None):
    config = yaml.safe_load(Path(filename).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("application configuration must be a mapping")
    policy = _identity_policy(config["identity_policy"]) if "identity_policy" in config else None
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
    feishu_ingress = ding_ingress = None
    if "feishu_card_ingress" in config or "dingtalk_card_ingress" in config:
        if policy is None:
            raise ValueError("card ingress requires identity_policy")
        from .channels import FeishuIngress, DingTalkCardIngress
        for key in ("feishu_card_ingress", "dingtalk_card_ingress"):
            if key in config and not isinstance(config[key], dict):
                raise ValueError("card ingress configuration must be a mapping")
        if "feishu_card_ingress" in config:
            feishu_ingress = FeishuIngress(approvals, policy, **config["feishu_card_ingress"])
        if "dingtalk_card_ingress" in config:
            ding_ingress = DingTalkCardIngress(approvals, policy, **config["dingtalk_card_ingress"])
    return create_app(store, approvals, principals, gatekeeper=gatekeeper,
                      identity_policy=policy, feishu_card_ingress=feishu_ingress, dingtalk_card_ingress=ding_ingress,
                      public_url=config.get("public_url", "http://localhost:8000"),
                      a2a_worker_id=config.get("worker_id"),
                      feishu_encrypt_key=os.environ[config["feishu_encrypt_key_env"]] if config.get("feishu_encrypt_key_env") else None,
                      feishu_users=config.get("feishu_users", {}),
                      dingtalk_secret=os.environ[config["dingtalk_secret_env"]] if config.get("dingtalk_secret_env") else None,
                      dingtalk_users=config.get("dingtalk_users", {}))
