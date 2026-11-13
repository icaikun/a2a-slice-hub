"""Server-owned canonical identities; channel contact is never authorization."""
from dataclasses import dataclass
import re

from .errors import Forbidden


def safe_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("non-empty safe identifier required")
    return value


@dataclass(frozen=True)
class ExternalAccount:
    provider: str
    tenant_or_corp: str
    app_id: str
    external_user_id: str

    def __post_init__(self):
        for value in (self.provider, self.tenant_or_corp, self.app_id, self.external_user_id):
            safe_identifier(value)


@dataclass(frozen=True)
class MemoryScope:
    """Construct only from authenticated server-side project configuration."""
    org_id: str
    project_id: str
    repo_id: str
    visibility: str = "project"

    def __post_init__(self):
        for value in (self.org_id, self.project_id, self.repo_id, self.visibility):
            safe_identifier(value)


@dataclass(frozen=True)
class PersonRecord:
    person_id: str
    roles: frozenset[str]
    owned_slices: frozenset[str] = frozenset()
    scopes: frozenset[MemoryScope] = frozenset()

    def __post_init__(self):
        safe_identifier(self.person_id)
        for field in ("roles", "owned_slices", "scopes"):
            object.__setattr__(self, field, frozenset(getattr(self, field)))
        for value in self.roles | self.owned_slices:
            safe_identifier(value)
        if any(not isinstance(scope, MemoryScope) for scope in self.scopes):
            raise ValueError("MemoryScope required")


class IdentityPolicy:
    def __init__(self, persons=(), bindings=None, *, contacts=None):
        records = persons.values() if isinstance(persons, dict) else persons
        self.persons = {}
        for person in records:
            if not isinstance(person, PersonRecord) or person.person_id in self.persons:
                raise ValueError("unique PersonRecord required")
            self.persons[person.person_id] = person
        self.bindings = dict(bindings or {})
        if any(not isinstance(account, ExternalAccount) or person not in self.persons
               for account, person in self.bindings.items()):
            raise ValueError("bindings require registered identities")
        self.contacts = dict(contacts or {})

    def can_contact(self, account):
        return self.contacts.get(account) in {"pairing", "allowlist"}

    def resolve_person(self, account: ExternalAccount) -> PersonRecord:
        person_id = self.bindings.get(account)
        if person_id not in self.persons:
            raise Forbidden("external account is unbound")
        return self.persons[person_id]

    def _person(self, value):
        if isinstance(value, ExternalAccount):
            return self.resolve_person(value)
        # Never trust a caller-supplied record's roles/scopes.
        key = value.person_id if isinstance(value, PersonRecord) else value
        if not isinstance(key, str) or key not in self.persons:
            raise Forbidden("person is unregistered")
        return self.persons[key]

    def authorize_approval(self, account_or_person, *, item_owner=None, action="resume", allow_self_approval=False):
        person = self._person(account_or_person)
        if "approver" not in person.roles:
            raise Forbidden("approver role required")
        if not allow_self_approval and person.person_id == item_owner:
            raise Forbidden("independent approval required")
        return person.person_id

    def authorize_independent_qa(self, account_or_person, *, owners):
        person = self._person(account_or_person)
        if "qa" not in person.roles or person.person_id in owners:
            raise Forbidden("independent QA role required")
        return person.person_id
