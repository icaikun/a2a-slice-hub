"""Protected slice configuration; never take ownership from worker request bodies."""
from pathlib import Path
import re
import yaml
from pydantic import Field
from .models import StrictModel
from .paths import safe_path, inside, protected
from .errors import Forbidden


class Slice(StrictModel):
    slice_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    owner_user_id: str = Field(min_length=1)
    backup_user_id: str | None = None
    owned_paths: list[str] = Field(min_length=1)
    provides_contracts: list[str] = Field(default_factory=list)
    consumes_contracts: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)


def contract_path(name):
    path = name if name.startswith("contracts/") else "contracts/" + name + ".json"
    safe_path(path)
    if not path.endswith(".json"):
        raise ValueError("JSON contract required")
    return path


class SliceRegistry:
    def __init__(self, entries):
        self.slices, self.providers = {}, {}
        for raw in entries:
            item = Slice.model_validate(raw)
            if item.slice_id in self.slices:
                raise ValueError("duplicate slice")
            for path in item.owned_paths:
                safe_path(path)
                if protected(path) or path.startswith("contracts"):
                    raise ValueError("owned_paths cannot grant control or contract authority")
                if path.startswith("tests/") and not inside(path, "tests/unit/" + item.slice_id):
                    raise ValueError("unit test ownership must be slice scoped")
                for other in self.slices.values():
                    if any(inside(path.casefold(), p.casefold()) or inside(p.casefold(), path.casefold()) for p in other.owned_paths):
                        raise ValueError("overlapping ownership")
            self.slices[item.slice_id] = item
            for name in item.provides_contracts:
                path = contract_path(name)
                if path in self.providers:
                    raise ValueError("ambiguous contract provider")
                self.providers[path] = item.slice_id

    @classmethod
    def load(cls, directory):
        entries = []
        for path in sorted(Path(directory).glob("*.yaml")):
            if path.is_symlink():
                raise ValueError("slice symlinks forbidden")
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if raw["slice_id"] != path.stem:
                raise ValueError("slice filename mismatch")
            entries.append(raw)
        if not entries:
            raise ValueError("no protected slice configuration")
        return cls(entries)

    @classmethod
    def from_git(cls, git, sha):
        entries = []
        for path, entry in git.tree(sha).items():
            if re.fullmatch(r"slices/[^/]+\.yaml", path):
                if entry[0] != "100644":
                    raise ValueError("slice must be regular data")
                raw = yaml.safe_load(git.blob(sha, path))
                if raw["slice_id"] != Path(path).stem:
                    raise ValueError("slice filename mismatch")
                entries.append(raw)
        if not entries:
            raise ValueError("no protected slice configuration")
        return cls(entries)

    def get(self, slice_id):
        if slice_id not in self.slices:
            raise Forbidden("unknown slice")
        return self.slices[slice_id]

    def provider(self, contract):
        path = contract_path(contract)
        if path not in self.providers:
            raise Forbidden("unknown contract provider")
        return self.get(self.providers[path])

    def paths(self, slice_id, kind):
        item = self.get(slice_id)
        paths = [*item.owned_paths, "tests/unit/" + slice_id]
        if kind == "CONTRACT_PATCH":
            paths += [contract_path(n) for n in item.provides_contracts]
        return paths
