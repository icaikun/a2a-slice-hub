"""Conservative schema checks and bounded, advisory Python caller extraction."""
import ast
from dataclasses import dataclass
import json
from typing import Protocol

from .errors import GateRejected, InfrastructureError

ANNOTATIONS = {"title", "description", "$comment", "examples"}


JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object", "null"}
POSITIVE = {"type", "properties", "required", "items", "additionalProperties",
            "format", "enum", "const", "default", "$ref"} | ANNOTATIONS


def positive_habitable(schema, root):
    """Prove a witness for a deliberately small profile; unknown/recursive schemas fail closed."""
    missing = object()

    def json_value(value):
        try:
            json.dumps(value, allow_nan=False)
            return not isinstance(value, dict) or all(isinstance(k, str) for k in value)
        except (TypeError, ValueError):
            return False

    def equal(a, b):
        # JSON booleans are not numbers; dict order is irrelevant.
        if isinstance(a, bool) != isinstance(b, bool):
            return False
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return a == b

    def matches(value, node):
        types = node.get("type", list(JSON_TYPES))
        types = [types] if isinstance(types, str) else types
        kind = ("null" if value is None else "boolean" if isinstance(value, bool) else
                "number" if isinstance(value, (int, float)) else "string" if isinstance(value, str)
                else "array" if isinstance(value, list) else "object")
        if kind not in types and not (kind == "number" and "integer" in types and value == int(value)):
            return False
        if "const" in node and not equal(value, node["const"]):
            return False
        if "enum" in node and not any(equal(value, e) for e in node["enum"]):
            return False
        if isinstance(value, dict):
            if not set(node.get("required", [])) <= value.keys():
                return False
            for key, val in value.items():
                child = node.get("properties", {}).get(key, node.get("additionalProperties", True))
                if child is False or (isinstance(child, dict) and not valid(val, child)):
                    return False
        return not isinstance(value, list) or "items" not in node or all(valid(v, node["items"]) for v in value)

    def target(node):
        ref = node["$ref"]
        parts = ref.split("/") if isinstance(ref, str) else []
        if len(parts) != 3 or parts[0] != "#" or parts[1] not in {"$defs", "definitions"}:
            raise ValueError("nonlocal reference")
        name = parts[2].replace("~1", "/").replace("~0", "~")
        return root[parts[1]][name]

    def valid(value, node):
        return matches(value, node) and ("$ref" not in node or valid(value, target(node)))

    cache = {}

    def witness(schemas, seen=()):
        key = (tuple(id(n) for n in schemas), seen)
        if key not in cache:
            cache[key] = construct(schemas, seen)
        return cache[key]

    def construct(schemas, seen):
        if len(seen) > 64:
            raise ValueError("profile depth exceeded")
        nodes = []
        for node in schemas:
            if not isinstance(node, dict) or node.keys() - POSITIVE:
                raise ValueError("outside positive profile")
            types = node.get("type", list(JSON_TYPES))
            types = [types] if isinstance(types, str) else types
            if not isinstance(types, list) or not types or any(not isinstance(t, str) or t not in JSON_TYPES for t in types):
                raise ValueError("invalid type")
            if len(set(types)) != len(types):
                raise ValueError("duplicate types")
            props, required = node.get("properties", {}), node.get("required", [])
            if (not isinstance(props, dict) or not isinstance(required, list) or
                    any(not isinstance(k, str) or k not in props for k in required)):
                raise ValueError("undefined required property")
            if ("required" in node and "object" not in types) or len(set(required)) != len(required):
                raise ValueError("required must describe a new object without duplicates")
            children = list(props.values())
            if "items" in node:
                children.append(node["items"])
            additional = node.get("additionalProperties", True)
            if type(additional) is not bool:
                children.append(additional)
            if any(witness([child], seen + (None,)) is missing for child in children):
                raise ValueError("uninhabitable child")
            if "format" in node and not isinstance(node["format"], str):
                raise ValueError("invalid format")
            if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"] or
                                   any(isinstance(v, list) for v in node["enum"])):
                raise ValueError("enum requires nonempty primitive/object alternatives")
            if any(not json_value(node[k]) for k in ("enum", "const", "default") if k in node):
                raise ValueError("not JSON")
            nodes.append(node)
            if "$ref" in node:
                ref = node["$ref"]
                if ref in seen:
                    raise ValueError("cyclic reference")
                child = target(node)
                candidate = witness([child], seen + (ref,))
                if candidate is missing:
                    return missing
        # Explicit finite constraints take precedence over structural witnesses.
        candidates = [n["const"] for n in nodes if "const" in n]
        candidates += [v for n in nodes for v in n.get("enum", [])]
        candidates += [witness([target(n)], seen + (n["$ref"],)) for n in nodes if "$ref" in n]
        obj = {}
        for key in {k for n in nodes for k in n.get("required", [])}:
            children = [n.get("properties", {}).get(key, n.get("additionalProperties", True)) for n in nodes]
            if False in children:
                obj = missing
                break
            obj[key] = witness([c for c in children if isinstance(c, dict)], seen + (None,))
            if obj[key] is missing:
                obj = missing
                break
        candidates += [None, False, 0, "", [], obj]
        return next((v for v in candidates if v is not missing and all(valid(v, n) for n in nodes)), missing)

    try:
        return witness([schema]) is not missing
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        return False


def schema_compatible(before, after):
    """Bounded producer-additive evolution, NOT bidirectional JSON Schema entailment.

    Existing fields/constraints stay fixed. New optional fields require a constructive
    inhabitant in the positive profile; complex changes need signed breaking approval.
    format/default/annotations are metadata, not asserted format validation.
    """
    def normalize(value):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key in ANNOTATIONS:
                    continue
                if key in {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"} and isinstance(child, dict):
                    # Property names are data: a property named 'description' is not an annotation.
                    result[key] = {name: normalize(schema) for name, schema in child.items()}
                elif key in {"enum", "const", "default"}:
                    result[key] = child
                elif key in {"items", "additionalProperties", "unevaluatedProperties", "contains", "not", "if", "then", "else", "propertyNames"}:
                    result[key] = normalize(child)
                elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} and isinstance(child, list):
                    result[key] = [normalize(v) for v in child]
                else:
                    result[key] = child
            return result
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value
    def same(a, b):
        # Python True == 1 must never turn a contract change into a no-op.
        return json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(b, sort_keys=True, allow_nan=False)

    before, after = normalize(before), normalize(after)
    if same(before, after):
        return True
    if not isinstance(before, dict) or not isinstance(after, dict) or before.get("type") != "object" or after.get("type") != "object":
        return False
    old, new = before.get("properties", {}), after.get("properties", {})
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if any(name not in new or not same(new[name], schema) for name, schema in old.items()):
        return False
    old_required, new_required = before.get("required", []), after.get("required", [])
    if not isinstance(old_required, list) or not isinstance(new_required, list) or any(not isinstance(n, str) for n in old_required + new_required):
        return False
    if set(old_required) != set(new_required):
        return False
    added = new.keys() - old.keys()
    if added:
        if added & set(new_required):
            return False
        if before.get("additionalProperties", True) is not True or before.get("unevaluatedProperties", True) is not True:
            return False
        if before.keys() & {"maxProperties", "propertyNames", "patternProperties", "dependentSchemas",
                            "not", "oneOf", "anyOf", "allOf", "if", "then", "else", "$ref"}:
            return False

        if any(not positive_habitable(new[name], after) for name in added):
            return False
    # Unknown keywords/constraints and schema references remain fail-closed.
    return same({k: v for k, v in before.items() if k not in {"properties", "required"}},
                {k: v for k, v in after.items() if k not in {"properties", "required"}})


@dataclass(frozen=True)
class ReviewContext:
    base_sha: str
    candidate_sha: str
    changed_paths: tuple[str, ...]
    changed_symbols: tuple[str, ...]
    diff: str
    callers: tuple[dict, ...]
    incomplete: bool


@dataclass(frozen=True)
class ReviewVerdict:
    approved: bool
    reason: str


class SemanticReviewer(Protocol):
    def review(self, context: ReviewContext) -> ReviewVerdict: ...


class RejectUnconfiguredReviewer:
    def review(self, context):
        raise InfrastructureError("semantic reviewer is not configured")


def context_for(git, base, candidate, limit=80):
    changed = sorted({p for _, paths in git.changes(base, candidate) for p in paths})
    old_tree, new_tree = git.tree(base), git.tree(candidate)
    symbols, callers, incomplete = set(), [], False

    def parse(sha, name):
        nonlocal incomplete
        raw = git.blob(sha, name)
        if len(raw) > 256_000:
            incomplete = True
            return None, ""
        text = raw.decode("utf-8", errors="replace")
        try:
            return ast.parse(text), text
        except SyntaxError:
            incomplete = True
            return None, text

    for name in changed:
        if not name.endswith(".py"):
            incomplete = True
            continue
        versions = []
        for sha, tree in ((base, old_tree), (candidate, new_tree)):
            node, _ = parse(sha, name) if name in tree else (None, "")
            versions.append({n.name: ast.dump(n, include_attributes=False) for n in ast.walk(node)
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))} if node else {})
        symbols.update(k for k in versions[0].keys() | versions[1].keys() if versions[0].get(k) != versions[1].get(k))
    for name in sorted(new_tree):
        if not name.endswith(".py") or name in changed:
            continue
        node, text = parse(candidate, name)
        if node is None:
            continue
        aliases = {a.asname or a.name: a.name for n in ast.walk(node)
                   if isinstance(n, ast.ImportFrom) for a in n.names}
        lines = text.splitlines()
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            symbol = call.func.id if isinstance(call.func, ast.Name) else (call.func.attr if isinstance(call.func, ast.Attribute) else "")
            if aliases.get(symbol, symbol) not in symbols:
                continue
            if len(callers) >= limit:
                incomplete = True
                break
            callers.append({"path": name, "line": call.lineno,
                            "source": "\n".join(lines[max(0, call.lineno - 3):call.lineno + 2])})
    patch = git.run("diff", "--no-ext-diff", "--no-textconv", "--unified=3", base, candidate, "--").stdout
    if len(patch) > 256_000:
        incomplete = True
    diff = patch[:256_000].decode("utf-8", errors="replace")
    # Python AST is a heuristic, not a whole-program call graph (reflection/dispatch are unknown).
    return ReviewContext(base, candidate, tuple(changed), tuple(sorted(symbols)), diff, tuple(callers), incomplete)


def check_contracts(git, base, candidate, paths, *, kind="CONTRACT_PATCH", breaking=False):
    old, new = git.tree(base), git.tree(candidate)
    for name in paths:
        if name.casefold().startswith("contracts/"):
            if kind != "CONTRACT_PATCH":
                raise GateRejected("only CONTRACT_PATCH may modify contracts")
            if not name.startswith("contracts/") or not name.endswith(".json"):
                raise GateRejected("unsupported contract format")
            if name not in old or name not in new:
                raise GateRejected("contract creation/deletion requires reviewed baseline promotion")
            try:
                compatible = schema_compatible(json.loads(git.blob(base, name)), json.loads(git.blob(candidate, name)))
            except (ValueError, UnicodeError) as exc:
                raise GateRejected("invalid contract schema") from exc
            if not compatible and not breaking:
                raise GateRejected("contract compatibility unproven; explicit baseline review required")


def contract_subject(git, base, candidate, paths, change_set_id):
    import hashlib
    return {"change_set_id": change_set_id, "contracts": {
        p: {"before": hashlib.sha256(git.blob(base, p)).hexdigest(),
            "after": hashlib.sha256(git.blob(candidate, p)).hexdigest()}
        for p in sorted(paths) if p.startswith("contracts/") and p in git.tree(base) and p in git.tree(candidate)}}
