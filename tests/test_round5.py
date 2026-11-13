"""Round 5 counter-proofs: real SQLite/Git/ASGI, bounded schema and queue policies."""
from copy import deepcopy
import json
import secrets

import pytest
from jsonschema import Draft202012Validator
from asgi_client import ASGITestClient as TestClient
from a2a_slice_hub.api import Principal, create_app
from a2a_slice_hub.approvals import Approvals
from a2a_slice_hub.errors import Conflict, GateRejected
from a2a_slice_hub.gatekeeper import Gatekeeper
from a2a_slice_hub.review import schema_compatible
from a2a_slice_hub.runner import TrustedPytestRunner
from a2a_slice_hub.slices import SliceRegistry
from conftest import Approve, new_item, submit
from test_qa import pending, report
from test_round2 import specs, online, item_for, working, dependency

ROLES = ['worker', 'coordinator', 'qa', 'gatekeeper', 'approver', 'im_gateway']
COMMANDS = {'claim': {'worker', 'coordinator'}, 'task-status/v1': {'worker'},
            'dev-submission/v1': {'worker'}, 'dependency-request/v1': {'worker'},
            'qa-verdict/v1': {'qa'}, 'bug-report/v1': {'qa'},
            'gate-merge/v1': {'gatekeeper', 'coordinator'}}


def rpc(client, token, message):
    return client.post('/a2a', headers={'Authorization': 'Bearer ' + token}, json={
        'jsonrpc': '2.0', 'id': 'r5', 'method': 'message/send', 'params': {'message': message}}).json()


@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('command', COMMANDS)
def test_a2a_role_command_differential(store, repo, role, command):
    token = secrets.token_urlsafe(32)
    principal = Principal('worker-a' if role == 'worker' else role, role, 'independent' if role == 'qa' else None)
    item = new_item(store)
    message = dict(role='user', messageId='role-matrix', contextId=item['id'],
                   metadata={'work_item_id': item['id'], 'expected_state_version': item['version']})
    payload = {'schema_version': command}
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    if command in {'qa-verdict/v1', 'bug-report/v1'}:
        item = pending(store, repo, item)
        message['metadata']['expected_state_version'] = item['version']
        payload = (report(item['integration_sha']) if command == 'bug-report/v1' else
                   payload | {'integration_sha': item['integration_sha'], 'outcome': 'PASS'})
    elif command == 'gate-merge/v1':
        attempt = submit(store, item, repo)
        payload |= {'branch': 'epic/demo', 'attempt_ids': [attempt['id']]}
    elif command != 'claim':
        if command == 'dependency-request/v1':
            store.registry = SliceRegistry(specs())
            online(store, 'worker-a', 'li')
            item = item_for(store, 'payments')
            message['contextId'] = item['id']
            attempt = working(store, item, 'worker-a')
            payload |= {'contract': 'orders', 'expected_change': {'optional': 'extra'}}
        else:
            attempt = store.claim(item['id'], item['owner'], item['version'])
            payload |= {'state': 'working'}
            if command == 'dev-submission/v1':
                attempt = store.transition(attempt['id'], item['owner'], attempt['epoch'], attempt['version'], 'working')
                payload = {'schema_version': command, 'base_sha': repo.base, 'source_sha': repo.base}
        message['taskId'] = attempt['id']
        message['metadata'] = {'lease_epoch': attempt['epoch'], 'expected_state_version': attempt['version']}
    message['parts'] = ([{'kind': 'text', 'text': 'claim'}] if command == 'claim'
                        else [{'kind': 'data', 'data': payload}])
    before = store.get_item(item['id'])
    app = create_app(store, Approvals(store, b'a' * 32), {token: principal}, gatekeeper=gate, a2a_worker_id='worker-a')
    with TestClient(app) as client:
        result = rpc(client, token, message)
    if role in COMMANDS[command]:
        assert 'result' in result, result
        assert store.get_item(item['id']) != before
    else:
        assert result['error']['code'] == -32000, result
        assert store.get_item(item['id']) == before


def accept(store, item):
    current = store.get_item(item['id'])
    return store.qa(item['id'], current['version'], current['integration_sha'], True, 'independent-qa')


def commit(store, repo, item, base=None, changes=None, branch='epic/demo'):
    """Real Git journal setup; selected merge tests additionally exercise the full gate."""
    base = base or repo.git.ref(branch)
    source = repo.source(changes or {}, base)
    attempt = store.claim(item['id'], item['owner'], store.get_item(item['id'])['version'])
    attempt = store.transition(attempt['id'], item['owner'], attempt['epoch'], attempt['version'], 'working')
    store.transition(attempt['id'], item['owner'], attempt['epoch'], attempt['version'], 'completed', base_sha=base, source_sha=source)
    candidate = repo.git.candidate(base, [source])
    intent = store.prepare_merge(branch, base, candidate, {item['id']: store.get_item(item['id'])['version']})
    assert store.finish_merge(intent, repo.git.publish) == 'COMMITTED'
    return candidate


@pytest.mark.parametrize('aggregate', [False, True])
@pytest.mark.parametrize('blocker', ['dependency', 'l0', 'offline', 'none'])
def test_signed_resume_recomputes_leaf_dispatchability(store, repo, aggregate, blocker):
    store.registry = SliceRegistry(specs())
    online(store, 'zhang', 'zhang')
    online(store, 'li', 'li')
    epic = None
    if blocker == 'l0':
        epic = store.create_epic('e', 'epic/demo', repo.base)['id']
    # Assemble the frozen aggregate before applying its L0 binding.
    pending_epic = epic
    if aggregate:
        epic = None
    parent = store.create_item('BUG' if aggregate else 'DEV', 'orders', 'zhang', ['src/orders'], epic_id=epic, change_set_id='set' if aggregate else None)
    leaves = ([item_for(store, 'orders', parent_id=parent['id'], change_set_id='set', epic_id=epic),
               item_for(store, 'payments', parent_id=parent['id'], change_set_id='set', epic_id=epic)] if aggregate else [parent])
    if aggregate and pending_epic:
        with store.transaction() as conn:
            conn.execute('UPDATE work_items SET epic_id=? WHERE id=? OR parent_id=?', (pending_epic, parent['id'], parent['id']))
    patch = store.create_item('CONTRACT_PATCH', 'checkout', 'zhang', ['contracts/checkout.json'])
    if blocker in {'dependency', 'l0'}:
        for leaf in leaves:
            store.add_dependency(leaf['id'], patch['id'])
    if blocker == 'offline':
        with store.transaction() as conn:
            conn.execute('UPDATE workers SET expires=0')
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET state='BLOCKED',blocked_reason='ownership_dispute',dispute_round=2,negotiation_round=3,rework_round=3 WHERE id=? OR parent_id=?", (parent['id'], parent['id']))
    approvals = Approvals(store, b'a' * 32)
    approvals.approve(approvals.issue(parent['id'], {'reason': 'human arbitration'}), 'human')
    expected, reason = {'dependency': ('WAITING', 'dependency'), 'l0': ('L0_PENDING_APPROVAL', 'l0_approval'),
                        'offline': ('WAITING_WORKER', 'offline_owner'), 'none': ('REWORK', None)}[blocker]
    for leaf in leaves:
        row = store.get_item(leaf['id'])
        assert (row['state'], row['blocked_reason']) == (expected, reason)
        assert row['dispute_generation'] == 1
        assert (row['dispute_round'], row['negotiation_round'], row['rework_round']) == (0, 0, 0)
    if aggregate:
        assert store.get_item(parent['id'])['state'] == 'REWORK'
    if blocker == 'offline':
        with store.connect() as conn:
            assert conn.execute("SELECT count(*) FROM outbox WHERE topic='worker.offline'").fetchone()[0] >= len(leaves)
    if blocker == 'dependency':
        sha = commit(store, repo, patch)
        accept(store, patch)
        for leaf in leaves:
            row = store.get_item(leaf['id'])
            assert row['state'] == 'READY' and row['integration_sha'] == sha
        with store.connect() as conn:
            events = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM outbox WHERE topic='dependency.ready'")]
        assert {e['item'] for e in events} == {leaf['id'] for leaf in leaves}
        assert all(e['integration_sha'] == sha for e in events)


def test_add_dependency_recomputes_waiting_version_and_wakes(store, repo):
    consumer = new_item(store, 'consumer', 'payments')
    deps = [new_item(store, 'a'), new_item(store, 'b')]
    version = consumer['version']
    for dep in deps:
        store.add_dependency(consumer['id'], dep['id'])
        row = store.get_item(consumer['id'])
        assert row['state'] == 'WAITING' and row['blocked_reason'] == 'dependency'
        assert row['version'] > version
        version = row['version']
    sha1 = commit(store, repo, deps[0]); sha2 = commit(store, repo, deps[1])
    accept(store, deps[1]); accept(store, deps[0])
    row = store.get_item(consumer['id'])
    assert row['state'] == 'READY' and row['integration_sha'] == sha2 != sha1
    with store.connect() as conn:
        event = json.loads(conn.execute("SELECT payload FROM outbox WHERE topic='dependency.ready'").fetchone()[0])
    assert event['integration_sha'] == sha2


@pytest.mark.parametrize('state', ['ACCEPTED', 'CLOSED'])
def test_add_already_accepted_dependency_pins_snapshot(store, repo, state):
    dep = new_item(store)
    sha = commit(store, repo, dep); accept(store, dep)
    if state == 'CLOSED':
        with store.transaction() as conn:
            conn.execute("UPDATE work_items SET state='CLOSED' WHERE id=?", (dep['id'],))
    consumer = new_item(store, 'consumer', 'payments')
    store.add_dependency(consumer['id'], dep['id'])
    row = store.get_item(consumer['id'])
    assert row['state'] == 'READY' and row['integration_sha'] == sha and row['version'] > consumer['version']


@pytest.mark.parametrize('missing', ['origin', 'branch', 'unaccepted'])
def test_gate_checks_each_dependency_not_just_scalar_pin(store, repo, missing):
    dep = new_item(store)
    sha = commit(store, repo, dep, changes={'src/orders/new.py': 'VALUE = 1\n'})
    if missing != 'unaccepted':
        accept(store, dep)
    consumer = new_item(store, 'consumer', 'payments')
    # Reconstruct stale persisted metadata independently of the readiness fix.
    with store.transaction() as conn:
        conn.execute('INSERT INTO dependencies VALUES(?,?)', (consumer['id'], dep['id']))
    origin = repo.base if missing == 'origin' else sha
    if missing == 'branch':
        origin = repo.source({'src/payments/other.py': 'OTHER = 1\n'}, repo.base)
        repo.git.run('update-ref', 'refs/heads/epic/other', origin)
        assert not repo.git.ancestor(sha, origin) and not repo.git.ancestor(origin, sha)
    source = repo.source({'src/payments/new.py': 'VALUE = 2\n'}, origin)
    # Unaccepted dependencies cannot normally be claimed; forge only legacy persisted state for defense-in-depth.
    if missing == 'unaccepted':
        with store.transaction() as conn:
            conn.execute('DELETE FROM dependencies WHERE item=?', (consumer['id'],))
    attempt = store.claim(consumer['id'], 'consumer', store.get_item(consumer['id'])['version'])
    attempt = store.transition(attempt['id'], 'consumer', attempt['epoch'], attempt['version'], 'working')
    attempt = store.transition(attempt['id'], 'consumer', attempt['epoch'], attempt['version'], 'completed', base_sha=origin, source_sha=source)
    if missing == 'unaccepted':
        with store.transaction() as conn:
            conn.execute('INSERT INTO dependencies VALUES(?,?)', (consumer['id'], dep['id']))
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    with pytest.raises(GateRejected, match='missing an accepted dependency snapshot'):
        gate.merge('epic/other' if missing == 'branch' else 'epic/demo', [attempt['id']])


@pytest.mark.parametrize('addition', [True, False, {'not': {}}, {'enum': []}, {'type': []}, {'type': 'mystery'},
    {'type': ['string', 1]}, {'type': 'string', 'const': 1}, {'type': 'integer', 'enum': [True]},
    {'enum': [1], 'const': 2}, {'required': ['undefined']}, {'properties': {'x': False}}, {'items': False},
    {'additionalProperties': None}, {'format': 1}, {'minimum': 10, 'maximum': 1},
    {'minLength': 9, 'maxLength': 1}])
def test_uninhabitable_or_malformed_additions_fail_closed(addition):
    before = {'type': 'object', 'properties': {}}
    assert not schema_compatible(before, before | {'properties': {'ext': addition}})


@pytest.mark.parametrize('keyword', ['not', 'oneOf', 'anyOf', 'allOf', 'if', 'then', 'else', 'pattern',
    'minLength', 'maxLength', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf',
    'minItems', 'maxItems', 'uniqueItems', 'minProperties', 'maxProperties', 'unevaluatedProperties',
    'patternProperties', 'propertyNames', 'dependentSchemas', 'dependentRequired', 'contains', 'unknown'])
def test_complex_keywords_require_breaking_approval(keyword):
    before = {'type': 'object', 'properties': {}}
    assert not schema_compatible(before, before | {'properties': {'ext': {keyword: {}}}})


@pytest.mark.parametrize('schema,sample', [({}, None), ({'type': ['null', 'string']}, 'ok'),
    ({'type': 'array', 'items': {'type': 'integer'}}, [1]),
    ({'type': 'object', 'properties': {'x': {'enum': [1, 2]}}, 'required': ['x'], 'additionalProperties': False}, {'x': 1}),
    ({'type': 'object', 'additionalProperties': {'type': 'boolean'}}, {'x': True}),
    ({'const': {'x': [1]}}, {'x': [1]}), ({'enum': [None, {'x': 1}]}, None),
    ({'type': 'string', 'format': 'email', 'default': 'a@example.test'}, 'a@example.test')])
def test_positive_profile_has_real_validator_witness(schema, sample):
    before = {'type': 'object', 'properties': {}}
    after = before | {'properties': {'ext': schema}}
    assert schema_compatible(before, after)
    Draft202012Validator(before).validate({'ext': sample})
    Draft202012Validator(after).validate({'ext': sample})


@pytest.mark.parametrize('defs', ['$defs', 'definitions'])
@pytest.mark.parametrize('target', [{'not': {}}, {'enum': []}, {'$ref': '#/$defs/Text'}, {'type': 'string'}])
def test_referenced_profile_checked_and_cycles_rejected(defs, target):
    if '$ref' in target:
        target = {'$ref': '#/' + defs + '/Text'}
    before = {'type': 'object', 'properties': {}, defs: {'Text': target}}
    after = deepcopy(before); after['properties']['ext'] = {'$ref': '#/' + defs + '/Text'}
    assert schema_compatible(before, after) == (target == {'type': 'string'})
    if target == {'type': 'string'}:
        after['properties']['ext']['type'] = 'integer'
        assert not schema_compatible(before, after)


def test_policy_is_producer_additive_not_bidirectional_entailment():
    before = {'type': 'object', 'properties': {}}
    after = before | {'properties': {'ext': {'type': 'string'}}}
    assert schema_compatible(before, after)
    assert Draft202012Validator(before).is_valid({'ext': 1})
    assert not Draft202012Validator(after).is_valid({'ext': 1})


@pytest.mark.parametrize('age', [299, 300, 301])
def test_outbox_aging_prevents_urgent_starvation(store, clock, age):
    with store.transaction() as conn:
        normal = store.emit(conn, 'normal', {})
    clock.now += age
    with store.transaction() as conn:
        urgent = store.emit(conn, 'approval.requested', {})
    assert store.claim_outbox()['id'] == (normal if age >= 300 else urgent)


def test_fresh_urgent_jumps_ten_thousand_fresh_poison_events(store):
    with store.transaction() as conn:
        for _ in range(10000):
            key = store.emit(conn, 'poison', {})
            conn.execute('UPDATE outbox SET tries=2 WHERE id=?', (key,))
        urgent = store.emit(conn, 'approval.requested', {})
    assert store.claim_outbox()['id'] == urgent


@pytest.mark.parametrize('budget', [1, 3, 10])
def test_manual_replay_cap_atomic_audit_and_no_rejection_storm(store, budget):
    with store.transaction() as conn:
        key = store.emit(conn, 'poison', {})
    for count in range(1, budget + 1):
        with store.transaction() as conn:
            conn.execute('UPDATE outbox SET dead_letter=1 WHERE id=?', (key,))
        assert store.replay_dead_letter(key, 'coordinator', budget)['replay_count'] == count
    with store.transaction() as conn:
        conn.execute('UPDATE outbox SET dead_letter=1 WHERE id=?', (key,))
    for _ in range(5):
        with pytest.raises(Conflict, match='budget exhausted'):
            store.replay_dead_letter(key, 'coordinator', budget)
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE topic='outbox.replayed'").fetchone()[0] == budget
        assert conn.execute('SELECT dead_letter FROM outbox WHERE id=?', (key,)).fetchone()[0] == 1


@pytest.mark.parametrize('budget', [0, 11, True, 1.0, '3'])
def test_replay_budget_strict_bounds(store, budget):
    with pytest.raises(ValueError):
        store.replay_dead_letter('unused', 'coordinator', budget)


def test_http_default_replay_budget_exhaustion(store, api):
    client, headers, _ = api
    with store.transaction() as conn:
        key = store.emit(conn, 'poison', {})
    url = '/hub/outbox/dead-letters/' + key + '/replay'
    for count in range(4):
        with store.transaction() as conn:
            conn.execute('UPDATE outbox SET dead_letter=1 WHERE id=?', (key,))
        response = client.post(url, headers=headers['coordinator'])
        assert response.status_code == (200 if count < 3 else 409)
    assert 'budget exhausted' in response.json()['detail']

@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('method', ['tasks/get', 'tasks/cancel'])
@pytest.mark.parametrize('same_name', [False, True])
def test_a2a_task_methods_role_and_name_differential(store, role, method, same_name):
    item = new_item(store)
    attempt = store.claim(item['id'], 'worker-a', item['version'])
    token = secrets.token_urlsafe(32)
    name = 'worker-a' if same_name else 'other'
    app = create_app(store, Approvals(store, b'a' * 32), {token: Principal(name, role)})
    with TestClient(app) as client:
        result = client.post('/a2a', headers={'Authorization': 'Bearer ' + token}, json={
            'jsonrpc': '2.0', 'id': 'roles', 'method': method, 'params': {'id': attempt['id']}}).json()
    allowed = (role == 'worker' and same_name) or (method == 'tasks/get' and role in {'coordinator', 'qa', 'gatekeeper'})
    assert ('result' in result) == allowed, result
    assert store.get_attempt(attempt['id'])['state'] == ('canceled' if allowed and method == 'tasks/cancel' else 'submitted')


@pytest.mark.parametrize('keyword', ['const', 'enum', 'default'])
def test_existing_json_boolean_is_not_numeric_equivalent(keyword):
    before = {'type': 'object', 'properties': {'x': {keyword: [True] if keyword == 'enum' else True}}}
    after = deepcopy(before)
    after['properties']['x'][keyword] = [1] if keyword == 'enum' else 1
    assert not schema_compatible(before, after)


def test_latest_dependency_pin_is_used_for_actual_gate_submission(store, repo):
    deps = [new_item(store, 'a'), new_item(store, 'b')]
    consumer = new_item(store, 'consumer', 'payments')
    for dep in deps:
        store.add_dependency(consumer['id'], dep['id'])
    older = commit(store, repo, deps[0], changes={'src/orders/old.py': 'OLD = 1\n'})
    newer = commit(store, repo, deps[1], changes={'src/orders/new.py': 'NEW = 1\n'})
    accept(store, deps[1]); accept(store, deps[0])
    pinned = store.get_item(consumer['id'])['integration_sha']
    assert pinned == newer and repo.git.ancestor(older, pinned)
    source = repo.source({'src/payments/new.py': 'VALUE = 3\n'}, pinned)
    a = store.claim(consumer['id'], 'consumer', store.get_item(consumer['id'])['version'])
    a = store.transition(a['id'], 'consumer', a['epoch'], a['version'], 'working')
    a = store.transition(a['id'], 'consumer', a['epoch'], a['version'], 'completed', base_sha=pinned, source_sha=source)
    result = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge('epic/demo', [a['id']])
    assert all(repo.git.ancestor(s, result['candidate_sha']) for s in [older, newer])


def test_l0_dependency_pin_survives_acceptance_before_l0_approval(store, repo):
    dep = new_item(store)
    store.create_epic('l0', 'epic/l0', repo.base)
    consumer = new_item(store, 'consumer', 'payments', epic_id='l0')
    store.add_dependency(consumer['id'], dep['id'])
    assert store.get_item(consumer['id'])['version'] > consumer['version']
    sha = commit(store, repo, dep); accept(store, dep)
    approvals = Approvals(store, b'a' * 32)
    approvals.approve(approvals.issue('l0', {'skeleton_sha': repo.base, 'branch': 'epic/l0'}, action='l0'), 'human')
    row = store.get_item(consumer['id'])
    assert row['state'] == 'READY' and row['integration_sha'] == sha


def test_existing_newer_pin_cannot_regress_on_last_dependency_qa(store, repo):
    older = new_item(store, 'a'); newer = new_item(store, 'b')
    old_sha = commit(store, repo, older); new_sha = commit(store, repo, newer)
    consumer = new_item(store, 'consumer', 'payments')
    store.add_dependency(consumer['id'], older['id'])
    with store.transaction() as conn:
        conn.execute('UPDATE work_items SET integration_sha=? WHERE id=?', (new_sha, consumer['id']))
    accept(store, older)
    assert store.get_item(consumer['id'])['integration_sha'] == new_sha != old_sha

@pytest.mark.parametrize('schema', [{'type': ['string', 'string']}, {'enum': [[1]]},
    {'type': 'string', 'properties': {'x': {}}, 'required': ['x']},
    {'type': 'object', 'properties': {'x': {}}, 'required': ['x', 'x']}])
def test_positive_profile_rejects_invalid_structural_combinations(schema):
    before = {'type': 'object', 'properties': {}}
    assert not schema_compatible(before, before | {'properties': {'ext': schema}})


def test_deep_positive_profile_and_transitive_refs_are_bounded():
    schema = {'type': 'string'}
    for _ in range(20):
        schema = {'type': 'object', 'properties': {'x': schema}, 'required': ['x']}
    before = {'type': 'object', 'properties': {}, '$defs': {'A': {'$ref': '#/$defs/B'}, 'B': schema}}
    assert schema_compatible(before, before | {'properties': {'ext': {'$ref': '#/$defs/A'}}})
    before['$defs']['B'] = {'$ref': '#/$defs/A'}
    assert not schema_compatible(before, before | {'properties': {'ext': {'$ref': '#/$defs/A'}}})


def family(store):
    parent = store.create_item('BUG', 'orders', 'parent', ['src/orders'], change_set_id='family')
    children = [new_item(store, owner, module, parent_id=parent['id'], change_set_id='family')
                for owner, module in [('worker-a', 'orders'), ('worker-b', 'payments')]]
    return parent, children


def test_parent_dependency_real_disputes_resume_qa_wakeup_and_merge(store, repo):
    parent, children = family(store)
    patch = store.create_item('CONTRACT_PATCH', 'orders', 'provider', ['contracts/orders.json'])
    store.add_dependency(parent['id'], patch['id'])
    assert all(store.get_item(c['id'])['state'] == 'WAITING' for c in children)
    for c in children:
        store.dispute(c['id'], c['owner'], 'independent ownership evidence')
    assert store.get_item(parent['id'])['state'] == 'BLOCKED'
    approvals = Approvals(store, b'a' * 32)
    approvals.approve(approvals.issue(parent['id'], {'arbitration': 'resume'}), 'human')
    assert store.get_item(parent['id'])['state'] == 'REWORK'
    for c in children:
        current = store.get_item(c['id'])
        assert current['state'] == 'WAITING' and current['dispute_generation'] == 1
        with pytest.raises(Conflict, match='dependency'):
            store.claim(c['id'], c['owner'], current['version'])
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM disputes WHERE generation=0').fetchone()[0] == 2
    sha = commit(store, repo, patch); accept(store, patch)
    attempts = []
    for c in children:
        row = store.get_item(c['id'])
        assert row['state'] == 'READY' and row['integration_sha'] == sha
        a = store.claim(c['id'], c['owner'], row['version'])
        a = store.transition(a['id'], c['owner'], a['epoch'], a['version'], 'working')
        a = store.transition(a['id'], c['owner'], a['epoch'], a['version'], 'completed', base_sha=sha, source_sha=sha)
        attempts.append(a['id'])
    result = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge('epic/demo', attempts)
    assert repo.git.ancestor(sha, result['candidate_sha'])


def test_parent_dependency_legacy_gate_state_cannot_bypass_check(store, repo):
    parent, children = family(store)
    dep = new_item(store, 'provider')
    attempts = [submit(store, c, repo)['id'] for c in children]
    with store.transaction() as conn:
        conn.execute('INSERT INTO dependencies VALUES(?,?)', (parent['id'], dep['id']))
    with pytest.raises(GateRejected, match='missing an accepted dependency snapshot'):
        Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve()).merge('epic/demo', attempts)


def test_parent_dependency_cannot_mutate_dispatched_child(store):
    parent, children = family(store)
    dep = new_item(store, 'provider')
    store.claim(children[0]['id'], children[0]['owner'], children[0]['version'])
    with pytest.raises(Conflict, match='child dispatch'):
        store.add_dependency(parent['id'], dep['id'])
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM dependencies').fetchone()[0] == 0


@pytest.mark.parametrize('direction', ['parent-child', 'child-parent', 'siblings', 'transitive'])
def test_dependency_dag_accounts_for_atomic_change_sets(store, direction):
    parent, children = family(store)
    a, b = children
    if direction == 'transitive':
        dep = new_item(store, 'provider')
        store.add_dependency(parent['id'], dep['id'])
        source, target = dep, a
    else:
        source, target = {'parent-child': (parent, a), 'child-parent': (a, parent), 'siblings': (a, b)}[direction]
    with pytest.raises(Conflict, match='cycle'):
        store.add_dependency(source['id'], target['id'])

@pytest.mark.parametrize('child_epic', [None, 'other'])
def test_aggregate_cannot_cross_epic_binding(store, repo, child_epic):
    for key in ['parent', 'other']:
        store.create_epic(key, 'epic/' + key, repo.base)
        approvals = Approvals(store, b'a' * 32)
        approvals.approve(approvals.issue(key, {'skeleton_sha': repo.base, 'branch': 'epic/' + key}, action='l0'), 'human')
    parent = store.create_item('BUG', 'orders', 'parent', ['src/orders'], change_set_id='epic-group', epic_id='parent')
    with pytest.raises(Conflict, match='parent epic'):
        new_item(store, parent_id=parent['id'], change_set_id='epic-group', epic_id=child_epic)


def test_merge_preparation_rejects_legacy_cross_epic_parent(store, repo):
    parent, children = family(store)
    store.create_epic('parent-epic', 'epic/elsewhere', repo.base)
    with store.transaction() as conn:
        conn.execute('UPDATE work_items SET epic_id=? WHERE id=?', ('parent-epic', parent['id']))
    for c in children:
        submit(store, c, repo)
    versions = {c['id']: store.get_item(c['id'])['version'] for c in children}
    with pytest.raises(Conflict, match='parent epic'):
        store.prepare_merge('epic/demo', repo.base, repo.base, versions)
