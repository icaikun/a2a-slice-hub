"""Round 4 counter-proofs: real SQLite/Git/ASGI and consumer schema validation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from threading import Barrier

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing.exceptions import Unresolvable

from asgi_client import ASGITestClient as TestClient
from a2a_slice_hub.api import Principal, create_app
from a2a_slice_hub.approvals import Approvals
from a2a_slice_hub.config import load_app
from a2a_slice_hub.errors import Conflict, Forbidden, InfrastructureError
from a2a_slice_hub.gatekeeper import Gatekeeper
from a2a_slice_hub.im import BOTS, BotIdentity, DingTalkAdapter, FeishuAdapter, OutboxDispatcher
from a2a_slice_hub.review import schema_compatible
from a2a_slice_hub.runner import TrustedPytestRunner
from a2a_slice_hub.slices import SliceRegistry
from a2a_slice_hub.store import SCHEMA, Store
from conftest import Approve, new_item, submit
from test_qa import pending, report
from test_round2 import specs, online, item_for, working, dependency, patches
from test_round3 import command, rpc


@pytest.mark.parametrize('provider', ['feishu', 'dingtalk'])
def test_remote_200_crash_before_ack_redelivers_stable_event_id(store, clock, monkeypatch, provider):
    sent = []
    with store.transaction() as conn:
        key = store.emit(conn, 'attempt.submitted', {})
    def remote(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'code': 0, 'errcode': 0})
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        adapter = (FeishuAdapter({b: BotIdentity(b, lambda: 'fixture') for b in BOTS}, 'chat', client)
                   if provider == 'feishu' else DingTalkAdapter(client, webhook_url='https://oapi.dingtalk.com/robot/send?access_token=fixture'))
        dispatcher = OutboxDispatcher(store, adapter)
        ack = store.ack_outbox
        def crash(*args, **kwargs):
            raise OSError('crashed after remote 200 before local ack')
        monkeypatch.setattr(store, 'ack_outbox', crash)
        with pytest.raises(OSError):
            dispatcher.once()
        assert len(sent) == 1
        assert not dispatcher.once()
        clock.now += 31
        monkeypatch.setattr(store, 'ack_outbox', ack)
        assert dispatcher.once()
    assert len(sent) == 2 and sent[0] == sent[1]
    assert (sent[0]['uuid'] == key if provider == 'feishu' else key in sent[0]['markdown']['text'])


@pytest.mark.parametrize('topic', ['approval.requested', 'worker.offline', 'item.escalated'])
def test_urgent_beats_fresh_poison_backlog(store, topic):
    item = new_item(store)
    with store.transaction() as conn:
        for _ in range(250):
            key = store.emit(conn, 'attempt.submitted', {})
            conn.execute('UPDATE outbox SET tries=2 WHERE id=?', (key,))
        if topic == 'item.escalated':
            conn.execute("UPDATE work_items SET state='BLOCKED' WHERE id=?", (item['id'],))
        else:
            store.emit(conn, topic, {})
    event = store.claim_outbox()
    assert event['topic'] == topic and event['priority'] == 10


def test_dead_letter_replay_authorized_single_item_atomic_audit(store, api, clock):
    client, headers, _ = api
    with store.transaction() as conn:
        keys = [store.emit(conn, 'poison', {}) for _ in range(2)]
        conn.execute("UPDATE outbox SET dead_letter=1,tries=5,last_error='old error',claim='old',claimed_until=99999")
    url = '/hub/outbox/dead-letters/' + keys[0] + '/replay'
    assert client.post(url, headers=headers['worker']).status_code == 403
    row = client.post(url, headers=headers['coordinator']).json()
    assert row['id'] == keys[0] and row['dead_letter'] == row['tries'] == row['claimed_until'] == 0
    assert row['claim'] is None and row['available'] == clock() and row['replay_count'] == 1
    assert row['last_error'] == 'old error'
    assert [r['id'] for r in store.dead_letters()] == [keys[1]]
    assert client.post(url, headers=headers['coordinator']).status_code == 409
    with store.connect() as conn:
        audit = json.loads(conn.execute("SELECT payload FROM outbox WHERE topic='outbox.replayed'").fetchone()[0])
    assert audit == {'actor': 'coordinator', 'event_id': keys[0], 'replay_count': 1}
    with store.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON outbox WHEN NEW.topic='outbox.replayed' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(Conflict):
        store.replay_dead_letter(keys[1], 'coordinator')
    assert store.dead_letters()[0]['replay_count'] == 0


def test_dingtalk_same_ms_distinct_msg_and_first_arrival_boundary(store, clock):
    secret = 'fixture'
    timestamp = str(int(clock() * 1000))
    headers = {'timestamp': timestamp, 'sign': DingTalkAdapter.sign(timestamp, secret), 'Authorization': 'Bearer ' + 'g' * 32}
    app = create_app(store, Approvals(store, b'a' * 32), {'g' * 32: Principal('gateway', 'im_gateway')},
                     dingtalk_secret=secret, dingtalk_users={'staff': 'human', 'other': 'substituted'})
    with TestClient(app) as client:
        a = {'msgId': 'msg-1', 'senderStaffId': 'staff'}
        b = a | {'msgId': 'msg-2'}
        for body in (a, b, a):
            assert client.post('/im/dingtalk/events', headers=headers, json=body).status_code == 200
        assert client.post('/im/dingtalk/events', headers=headers, json=a | {'text': 'changed'}).status_code == 409
        assert client.post('/im/dingtalk/events', headers=headers, json={'senderStaffId': 'staff'}).status_code == 200
        assert client.post('/im/dingtalk/events', headers=headers, json={'senderStaffId': 'other'}).status_code == 409
        # A compromised authenticated gateway can substitute a FIRST body: timestamp HMAC is not body authentication.
        forged = {'msgId': 'first-arrival', 'senderStaffId': 'other'}
        response = client.post('/im/dingtalk/events', headers=headers, json=forged)
        assert response.json() == {'sender': 'substituted', 'accepted_as_command': False}
        assert client.post('/im/dingtalk/events', headers=headers, json=forged | {'senderStaffId': 'staff'}).status_code == 409
        assert client.post('/im/dingtalk/events', headers={k:v for k,v in headers.items() if k != 'Authorization'}, json=b).status_code == 401
        for body in ([], {'encrypt': 'opaque'}):
            assert client.post('/im/dingtalk/events', headers=headers, json=body).status_code == 422


def test_candidate_execution_requires_explicit_isolation_opt_in():
    with pytest.raises(InfrastructureError, match='isolated runner'):
        TrustedPytestRunner().run(None, None, None, None)


@pytest.mark.parametrize('constraint', [
    {'additionalProperties': {'type': 'integer'}}, {'additionalProperties': False},
    {'unevaluatedProperties': False}, {'maxProperties': 1},
    {'propertyNames': {'enum': ['id']}}, {'patternProperties': {'^note$': {'type': 'integer'}}},
    {'dependentSchemas': {'note': {'required': ['missing']}}}, {'not': {'required': ['note']}},
    {'oneOf': [{'maxProperties': 1}]}, {'anyOf': [{'maxProperties': 1}]},
    {'allOf': [{'maxProperties': 1}]}, {'if': {'required': ['note']}, 'then': {'required': ['missing']}},
    {'if': {'not': {'required': ['note']}}, 'else': {'required': ['missing']}},
    {'$ref': '#/$defs/limit', '$defs': {'limit': {'maxProperties': 1}}},
])
def test_added_property_constraints_are_rejected_with_real_consumer_validation(constraint):
    before = {'type': 'object', 'properties': {'id': {'type': 'string'}}, **constraint}
    after = deepcopy(before)
    after['properties']['note'] = {'type': 'string'}
    assert not schema_compatible(before, after)
    validator = Draft202012Validator(before)
    validator.validate({'id': 'x'})
    with pytest.raises(ValidationError):
        validator.validate({'id': 'x', 'note': 'text'})


@pytest.mark.parametrize('addition', [False, {'$ref': '#/$defs/Missing'}, {'anyOf': [{'$ref': '#/definitions/Missing'}]}])
def test_added_false_and_dangling_refs_break_real_consumer(addition):
    before = {'type': 'object', 'properties': {}}
    after = {'type': 'object', 'properties': {'ext': addition}}
    assert not schema_compatible(before, after)
    Draft202012Validator(before).validate({'ext': 'x'})
    with pytest.raises((ValidationError, Unresolvable)):
        Draft202012Validator(after).validate({'ext': 'x'})


@pytest.mark.parametrize('ref', ['https://invalid.example/schema', '#/properties/id', '#/$defs/Missing', 3])
def test_optional_ref_resolution_fails_closed_without_network(ref):
    before = {'type': 'object', 'properties': {}}
    assert not schema_compatible(before, before | {'properties': {'ext': {'$ref': ref}}})


@pytest.mark.parametrize('container', ['$defs', 'definitions'])
def test_resolved_optional_ref_and_annotation_addition_remain_compatible(container):
    before = {'type': 'object', 'properties': {}, container: {'Text': {'type': 'string'}}}
    after = before | {'properties': {'ext': {'$ref': '#/' + container + '/Text', 'description': 'optional'}}}
    assert schema_compatible(before, after)
    Draft202012Validator(after).validate({'ext': 'text'})
    nested_bad = deepcopy(before)
    nested_bad[container]['Text'] = {'$ref': '#/$defs/Missing'}
    assert not schema_compatible(nested_bad, nested_bad | {'properties': after['properties']})


def test_prepare_merge_grant_id_never_shadows_fresh_intent_id(store, repo):
    item = store.create_item('CONTRACT_PATCH', 'orders', 'worker-a', ['contracts/orders.json'], change_set_id='change')
    submit(store, item, repo)
    item = store.get_item(item['id'])
    approvals = Approvals(store, b'a' * 32)
    card = approvals.issue(item['id'], {'contracts': ['orders'], 'change_set_id': 'change'}, action='breaking_contract')
    approvals.approve(card, 'human')
    intent = store.prepare_merge('epic/demo', repo.base, repo.base, {item['id']: item['version']}, approval_grants=[card['approval_id']])
    assert intent != card['approval_id'] and len(intent) == 32
    with store.connect() as conn:
        assert conn.execute('SELECT id FROM merge_intents').fetchone()[0] == intent


@pytest.mark.parametrize('crash_at', ['prepared', 'post_cas'])
def test_merge_crash_recovery_receipt_then_external_rewind(store, repo, monkeypatch, crash_at):
    source = repo.source({'src/orders/service.py': 'def total():\n    return 4\n'})
    item = new_item(store)
    attempt = submit(store, item, repo, source)
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    publish = repo.git.publish
    def crash(intent):
        if crash_at == 'post_cas':
            assert publish(intent)
        raise OSError('process stopped')
    monkeypatch.setattr(repo.git, 'publish', crash)
    kwargs = {'request_id': 'gate:recover', 'request_digest': 'digest'}
    with pytest.raises(OSError):
        gate.merge('epic/demo', [attempt['id']], **kwargs)
    assert store.get_item(item['id'])['state'] == 'GATING'
    monkeypatch.setattr(repo.git, 'publish', publish)
    result = gate.merge('epic/demo', [attempt['id']], **kwargs)
    assert repo.git.ancestor(result['candidate_sha'], repo.git.ref('epic/demo'))
    assert gate.merge('epic/demo', [attempt['id']], **kwargs) == result
    repo.git.run('update-ref', 'refs/heads/epic/demo', repo.base)
    with pytest.raises(Conflict, match='rewound outside Hub'):
        gate.merge('epic/demo', [attempt['id']], **kwargs)


def test_prepared_external_divergence_recovers_stale_not_fake_success(store, repo, monkeypatch):
    source = repo.source({'src/orders/service.py': 'def total():\n    return 4\n'})
    item = new_item(store)
    attempt = submit(store, item, repo, source)
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    publish = repo.git.publish
    def crash(intent):
        assert publish(intent)
        raise OSError('post CAS')
    monkeypatch.setattr(repo.git, 'publish', crash)
    with pytest.raises(OSError):
        gate.merge('epic/demo', [attempt['id']], request_id='recover', request_digest='d')
    divergent = repo.source({'src/payments/client.py': 'def charge(): return 5\n'})
    repo.git.run('update-ref', 'refs/heads/epic/demo', divergent)
    monkeypatch.setattr(repo.git, 'publish', publish)
    with pytest.raises(Conflict, match='stale integration'):
        gate.merge('epic/demo', [attempt['id']], request_id='recover', request_digest='d')
    with store.connect() as conn:
        assert conn.execute('SELECT state FROM merge_intents').fetchone()[0] == 'STALE'
        assert conn.execute('SELECT count(*) FROM inbox').fetchone()[0] == 0
    assert store.get_item(item['id'])['state'] == 'REWORK'


def test_competing_gatekeepers_real_cas_only_one_commits(store, repo):
    attempts = []
    for slice_id, owner, path, content in [('orders', 'worker-a', 'src/orders/service.py', 'def total(): return 2\n'),
                                         ('payments', 'worker-b', 'src/payments/client.py', 'def charge(): return 3\n')]:
        attempts.append(submit(store, new_item(store, owner=owner, slice_id=slice_id), repo, repo.source({path: content})))
    barrier = Barrier(2)
    class SynchronizedRunner:
        def run(self, *args):
            result = TrustedPytestRunner(allow_local_execution=True).run(*args)
            barrier.wait(timeout=30)
            return result
    def merge(index):
        gate = Gatekeeper(Store(store.database, clock=store.clock), repo.git, SynchronizedRunner(), Approve())
        try:
            return gate.merge('epic/demo', [attempts[index]['id']], request_id='race:' + str(index), request_digest=str(index))
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(merge, range(2)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert repo.git.ref('epic/demo') == winners[0]['candidate_sha']
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM merge_intents WHERE state='COMMITTED'").fetchone()[0] == 1


def test_same_human_distinct_slices_cannot_escalate_or_kill_peer(store):
    store.registry = SliceRegistry(specs())
    online(store)
    online(store, 'li-pc', 'li')
    parent = store.create_item('BUG', 'orders', 'zhang', ['src/orders'], change_set_id='family')
    children = [item_for(store, s, parent_id=parent['id'], change_set_id='family') for s in ('orders', 'checkout', 'payments')]
    peer = working(store, children[2], 'li-pc')
    store.dispute(children[0]['id'], 'zhang-pc-01', 'orders rejects')
    with pytest.raises(Conflict, match='this owner already disputed'):
        store.dispute(children[1]['id'], 'zhang-pc-01', 'checkout rejects')
    assert store.get_item(parent['id'])['dispute_round'] == 1
    assert store.get_attempt(peer['id'])['state'] == 'working'
    store.dispute(children[2]['id'], 'li-pc', 'independent owner rejects')
    assert store.get_item(parent['id'])['state'] == 'BLOCKED'
    assert store.get_attempt(peer['id'])['state'] == 'failed'
    with store.connect() as conn:
        assert {r[0] for r in conn.execute('SELECT owner FROM disputes')} == {'zhang', 'li'}
    approvals = Approvals(store, b'a' * 32)
    with pytest.raises(Conflict, match='aggregate parent'):
        approvals.issue(children[0]['id'], {}, action='resume')
    card = approvals.issue(parent['id'], {}, action='resume')
    approvals.approve(card, 'human')
    assert {store.get_item(r['id'])['dispute_generation'] for r in [parent, *children]} == {1}
    assert {store.get_item(r['id'])['state'] for r in [parent, *children]} == {'REWORK'}


@pytest.mark.parametrize('owner', [None, '', '   '])
def test_registry_qa_alias_requires_human_owner_in_app_and_config(store, tmp_path, monkeypatch, owner):
    store.registry = SliceRegistry(specs())
    with pytest.raises(ValueError, match='owner_user_id'):
        create_app(store, Approvals(store, b'a' * 32), {'q' * 32: Principal('qa-alias', 'qa', owner)})
    slices = tmp_path / 'slices'
    slices.mkdir()
    for spec in specs():
        (slices / (spec['slice_id'] + '.yaml')).write_text(yaml.safe_dump(spec), encoding='utf-8')
    config = {'database': str(tmp_path / 'config.sqlite'), 'slices': str(slices), 'approval_key_env': 'ROUND4_KEY',
              'principals': [{'name': 'qa-alias', 'role': 'qa', 'owner_user_id': owner, 'credential_env': 'ROUND4_TOKEN'}]}
    path = tmp_path / 'app.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    monkeypatch.setenv('ROUND4_TOKEN', 'q' * 32)
    monkeypatch.setenv('ROUND4_KEY', 'a' * 32)
    with pytest.raises(ValueError, match='owner_user_id'):
        load_app(path)


@pytest.mark.parametrize('late_join', [False, True])
@pytest.mark.parametrize('termination', ['failed', 'canceled'])
@pytest.mark.parametrize('provider_online', [False, True])
def test_shared_patch_deadline_and_canceled_waiter_resumes_with_snapshot(store, clock, late_join, termination, provider_online):
    store.registry = SliceRegistry(specs())
    online(store, ttl=3600)
    online(store, 'li-pc', 'li', ttl=3600)
    short = item_for(store, deadline=1005)
    dependency(store, working(store, short))
    if late_join:
        clock.now = 1006
    long = item_for(store, 'payments', deadline=51000)
    attempt = dependency(store, working(store, long, 'li-pc'))
    patch = patches(store)[0]
    assert patch['deadline'] == 51000
    assert len(patches(store)) == 1
    clock.now = 1006
    patch_attempt = working(store, patch)
    ended = store.transition(attempt['id'], 'li-pc', attempt['epoch'], attempt['version'], termination)
    assert ended['state'] == termination
    row = store.get_item(long['id'])
    assert row['state'] == 'WAITING' and row['blocked_reason'] == 'dependency'
    with pytest.raises(Conflict, match='dependency'):
        store.claim(long['id'], 'li-pc', row['version'])
    sha = 'a' * 40
    # QA workflow fixture: publication itself is covered by real Git tests above.
    store.transition(patch_attempt['id'], patch_attempt['worker_id'], patch_attempt['epoch'], patch_attempt['version'],
                     'completed', base_sha=sha, source_sha=sha)
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET state='MERGED_PENDING_QA',integration_sha=? WHERE id=?", (sha, patch['id']))
        if not provider_online:
            conn.execute("UPDATE workers SET expires=0 WHERE owner_user_id='li'")
    patch = store.get_item(patch['id'])
    store.qa(patch['id'], patch['version'], sha, True, 'independent-qa')
    row = store.get_item(long['id'])
    assert row['state'] == ('READY' if provider_online else 'WAITING_WORKER')
    assert row['integration_sha'] == sha
    assert store.get_item(short['id'])['deadline'] < clock()


def legacy_database(path, version):
    schema = SCHEMA
    if version == 1:
        schema = schema.replace("'WAITING_WORKER','L0_PENDING_APPROVAL','BLOCKED_ENV','FLAKY_BLOCKED',", '')
        schema = schema.replace(",\n epic_id TEXT REFERENCES epics(id), dependency_key TEXT, dependency_request TEXT,\n env_round INTEGER NOT NULL DEFAULT 0, flaky_round INTEGER NOT NULL DEFAULT 0, flaky_observed TEXT", '')
        schema = schema.replace("evidence_json TEXT NOT NULL DEFAULT '{}', ", '')
        schema = schema.replace('id TEXT PRIMARY KEY, item TEXT NOT NULL,\n subject_digest',
                                'id TEXT PRIMARY KEY, item TEXT NOT NULL REFERENCES work_items(id),\n subject_digest')
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(schema)
        if version == 3:
            Store.migrate(conn, SCHEMA)
            Store.migrate_round3(conn)
        conn.execute("INSERT INTO work_items(id,kind,slice_id,owner,allowed_paths,deadline) VALUES('item','DEV','orders','worker','[]',99999)")
        conn.execute("INSERT INTO attempts(id,work_item_id,worker_id,epoch,state,expires) VALUES('attempt','item','worker',1,'completed',99999)")
        conn.execute("INSERT INTO merge_intents(id,branch,base_sha,candidate_sha,items) VALUES('intent','epic/demo','base','candidate','[\"item\"]')")
        conn.execute("INSERT INTO outbox(id,topic,payload,available) VALUES('urgent','approval.requested','{}',1000)")
        conn.execute("INSERT INTO inbox VALUES('legacy','digest','attempt')")
        if version == 3:
            conn.execute("INSERT INTO disputes VALUES('item','item',0,'orders',0,0,'worker','evidence')")
        conn.execute('PRAGMA user_version=' + str(version))


@pytest.mark.parametrize('version', [0, 1, 2, 3, 4])
def test_concurrent_bootstrap_and_all_migrations_atomic_idempotent(tmp_path, clock, version):
    path = tmp_path / 'concurrent.sqlite'
    if version in {1, 2, 3}:
        legacy_database(path, version)
    elif version == 4:
        Store(path, clock=clock)
    barrier = Barrier(6)
    def start(_):
        barrier.wait(timeout=15)
        return Store(path, clock=clock)
    with ThreadPoolExecutor(max_workers=6) as pool:
        stores = list(pool.map(start, range(6)))
    with stores[0].connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 4
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        if version in {1, 2, 3}:
            assert conn.execute('SELECT state FROM merge_intents').fetchone()[0] == 'PREPARED'
            assert conn.execute("SELECT priority FROM outbox WHERE id='urgent'").fetchone()[0] == 10
            assert stores[0].claim('item', 'worker', 0, request_id='legacy', request_digest='digest')['state'] == 'completed'
        if version == 3:
            row = conn.execute('SELECT owner,evidence FROM disputes').fetchone()
            assert tuple(row) == ('worker', 'evidence')
            assert next(r for r in conn.execute('PRAGMA table_info(disputes)') if r[1] == 'owner')[3] == 1


@pytest.mark.parametrize('version', [0, 1, 2, 3])
def test_migration_failure_rolls_back_bootstrap_and_user_version(tmp_path, clock, monkeypatch, version):
    path = tmp_path / 'rollback.sqlite'
    if version:
        legacy_database(path, version)
    original = Store.migrate_round4
    def failing(conn):
        original(conn)
        raise RuntimeError('crash during migration')
    monkeypatch.setattr(Store, 'migrate_round4', staticmethod(failing))
    with pytest.raises(RuntimeError):
        Store(path, clock=clock)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == version
        if version:
            assert 'priority' not in {r[1] for r in conn.execute('PRAGMA table_info(outbox)')}
            assert conn.execute('SELECT state FROM merge_intents').fetchone()[0] == 'PREPARED'
        else:
            assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
    monkeypatch.setattr(Store, 'migrate_round4', staticmethod(original))
    Store(path, clock=clock)


def test_prune_is_bounded_delivered_only_preserves_dead_letters_and_prepared(store, clock):
    with store.transaction() as conn:
        for i in range(5):
            key = store.emit(conn, 'old', {})
            conn.execute('UPDATE outbox SET delivered=1,available=1 WHERE id=?', (key,))
        undelivered = store.emit(conn, 'undelivered', {})
        dead = store.emit(conn, 'dead', {})
        recent = store.emit(conn, 'recent', {})
        conn.execute('UPDATE outbox SET dead_letter=1,available=0 WHERE id=?', (dead,))
        conn.execute('UPDATE outbox SET delivered=1 WHERE id=?', (recent,))
        conn.execute("INSERT INTO merge_intents(id,branch,base_sha,candidate_sha,items) VALUES('prepared','branch','base','candidate','[]')")
    assert store.prune_history(delivered_before=clock(), limit=2) == 2
    assert store.prune_history(delivered_before=clock()) == 3
    with store.connect() as conn:
        assert {r[0] for r in conn.execute('SELECT id FROM outbox')} == {undelivered, dead, recent}
        assert conn.execute('SELECT state FROM merge_intents').fetchone()[0] == 'PREPARED'
    with pytest.raises(ValueError):
        store.prune_history(delivered_before=clock(), limit=1001)


@pytest.mark.parametrize('raw,code', [('[]', -32600), ('[{}]', -32600), ('null', -32600), ('42', -32600),
    ('"text"', -32600), ('{', -32700), ('\xff', -32700), ('{}', -32600),
    ('{"jsonrpc":"2.0","id":false,"method":"tasks/get"}', -32600), (' ' * 1_000_001, -32600)])
def test_rpc_raw_envelope_errors_not_fastapi_422(api, raw, code):
    client, headers, _ = api
    response = client.post('/a2a', headers=headers['worker'], content=raw.encode('latin1'))
    assert response.status_code == 200
    assert response.json()['error']['code'] == code and response.json()['id'] is None


@pytest.mark.parametrize('bad', [False, True, -1, 0.0, '0'])
def test_strict_version_claim_rest_rpc_and_store_parity(store, api, bad):
    client, headers, _ = api
    item = new_item(store)
    assert client.post('/a2a/tasks', headers=headers['worker'], json={'item_id': item['id'], 'expected_version': bad}).status_code == 422
    message = dict(role='user', messageId='bad', parts=[{'kind': 'text', 'text': 'claim'}],
                   metadata={'work_item_id': item['id'], 'expected_state_version': bad})
    assert rpc(client, headers['worker'], message)['error']['code'] == -32602
    with pytest.raises(ValueError):
        store.claim(item['id'], 'worker-a', bad)
    assert store.get_item(item['id']) == item


@pytest.mark.parametrize('field,bad', [('epoch', True), ('epoch', False), ('epoch', 0), ('expected_version', False), ('expected_version', True)])
def test_strict_transition_and_heartbeat_rest_rpc_store(store, api, field, bad):
    client, headers, _ = api
    item = new_item(store)
    attempt = store.claim(item['id'], 'worker-a', 0)
    body = {'epoch': attempt['epoch'], 'expected_version': attempt['version'], 'state': 'working', field: bad}
    assert client.post('/a2a/tasks/' + attempt['id'] + '/status', headers=headers['worker'], json=body).status_code == 422
    metadata = {'lease_epoch': body['epoch'], 'expected_state_version': body['expected_version']}
    msg = command('bad', {'schema_version': 'task-status/v1', 'state': 'working'}, taskId=attempt['id'], contextId=item['id'], metadata=metadata)
    assert rpc(client, headers['worker'], msg)['error']['code'] == -32602
    with pytest.raises(ValueError):
        store.transition(attempt['id'], 'worker-a', body['epoch'], body['expected_version'], 'working')
    if field == 'epoch':
        assert client.post('/a2a/tasks/' + attempt['id'] + '/heartbeat', headers=headers['worker'], json={'epoch': bad}).status_code == 422
        with pytest.raises(ValueError):
            store.heartbeat(attempt['id'], 'worker-a', bad)
    assert store.get_attempt(attempt['id']) == attempt


@pytest.mark.parametrize('schema', ['bug-report/v1', 'qa-verdict/v1'])
def test_qa_context_mismatch_and_valid_rest_rpc_differential(store, api, repo, schema):
    client, headers, _ = api
    item = pending(store, repo, new_item(store))
    other = new_item(store, owner='other', slice_id='payments')
    other_attempt = store.claim(other['id'], other['owner'], other['version'])
    payload = report(item['integration_sha'])
    if schema == 'qa-verdict/v1':
        payload = {'schema_version': schema, 'outcome': 'FAIL', 'integration_sha': item['integration_sha'], 'report': payload}
    msg = command('qa', payload, contextId=item['id'], taskId=other_attempt['id'], metadata={'expected_state_version': item['version']})
    assert rpc(client, headers['qa'], msg)['error']['code'] == -32602
    assert store.get_item(item['id']) == item
    del msg['taskId']
    rpc_result = rpc(client, headers['qa'], msg)
    assert rpc(client, headers['qa'], msg) == rpc_result
    other_pending = pending(store, repo, new_item(store))
    rest = client.post('/hub/items/' + other_pending['id'] + '/qa', headers=headers['qa'], json={
        'expected_version': other_pending['version'], 'integration_sha': other_pending['integration_sha'],
        'outcome': 'FAIL', 'report': report(other_pending['integration_sha'])}).json()
    result = rpc_result['result']['parts'][0]['data']
    assert (rest['state'], rest['rework_round'], rest['blocked_reason']) == (result['state'], result['rework_round'], result['blocked_reason'])


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'rejected', 'canceled'])
def test_tasks_cancel_idempotency_and_ownership(store, api, clock, terminal):
    client, headers, _ = api
    item = new_item(store)
    attempt = store.claim(item['id'], 'worker-a', item['version'])
    with store.transaction() as conn:
        conn.execute('UPDATE attempts SET state=? WHERE id=?', (terminal, attempt['id']))
    body = {'jsonrpc': '2.0', 'id': 'cancel', 'method': 'tasks/cancel', 'params': {'id': attempt['id']}}
    first = client.post('/a2a', headers=headers['worker'], json=body).json()
    clock.now += 100000
    second = client.post('/a2a', headers=headers['worker'], json=body).json()
    assert first == second
    if terminal == 'canceled':
        assert first['result']['status']['state'] == 'canceled'
    else:
        assert first['error']['code'] == -32002
    assert client.post('/a2a', headers=headers['other'], json=body).json()['error']['code'] == -32000


def test_rpc_cancel_waiting_dependency_keeps_waiting(store, api):
    client, headers, _ = api
    consumer = new_item(store)
    attempt = store.claim(consumer['id'], 'worker-a', 0)
    attempt = store.transition(attempt['id'], 'worker-a', attempt['epoch'], 0, 'working')
    attempt = store.transition(attempt['id'], 'worker-a', attempt['epoch'], 1, 'input-required')
    patch = new_item(store, owner='provider', slice_id='payments')
    with store.transaction() as conn:
        conn.execute('INSERT INTO dependencies VALUES(?,?)', (consumer['id'], patch['id']))
    body = {'jsonrpc': '2.0', 'id': 'cancel', 'method': 'tasks/cancel', 'params': {'id': attempt['id']}}
    first = client.post('/a2a', headers=headers['worker'], json=body).json()
    assert first['result']['status']['state'] == 'canceled'
    assert client.post('/a2a', headers=headers['worker'], json=body).json() == first
    row = store.get_item(consumer['id'])
    assert row['state'] == 'WAITING' and row['blocked_reason'] == 'dependency'


def test_owner_vote_uniqueness_is_enforced_by_database_not_only_handler(store):
    parent = store.create_item('BUG', 'integration', 'coordinator', ['src'], change_set_id='family')
    a = new_item(store, parent_id=parent['id'], change_set_id='family')
    b = new_item(store, slice_id='checkout', parent_id=parent['id'], change_set_id='family')
    store.dispute(a['id'], a['owner'], 'first')
    with pytest.raises(Conflict):
        with store.transaction() as conn:
            conn.execute('INSERT INTO disputes VALUES(?,?,?,?,?,?,?,?,?)',
                         (b['id'], parent['id'], 0, b['slice_id'], 0, 0, 'worker-a', 'bypass handler', a['owner']))


def test_conflicting_legacy_owner_votes_fail_migration_without_losing_evidence(tmp_path, clock):
    path = tmp_path / 'conflicting.sqlite'
    legacy_database(path, 3)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO work_items(id,kind,slice_id,owner,allowed_paths,deadline) VALUES('second','DEV','checkout','worker','[]',99999)")
        conn.execute("INSERT INTO disputes VALUES('second','item',0,'checkout',0,0,'machine-alias','second evidence')")
    with pytest.raises(sqlite3.IntegrityError):
        Store(path, clock=clock)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
        assert conn.execute('SELECT count(*) FROM disputes').fetchone()[0] == 2
        assert 'priority' not in {r[1] for r in conn.execute('PRAGMA table_info(outbox)')}


@pytest.mark.parametrize('role', ['qa', 'qa-verdict/v1', 'bug-report/v1'])
def test_qa_human_binding_blocks_alias_self_review(store, role):
    store.registry = SliceRegistry(specs())
    item = item_for(store, 'orders')
    sha = 'a' * 40
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET state='MERGED_PENDING_QA',integration_sha=? WHERE id=?", (sha, item['id']))
    item = store.get_item(item['id'])
    app = create_app(store, Approvals(store, b'a' * 32), {'q' * 32: Principal('independent-looking-alias', 'qa', 'zhang')})
    headers = {'Authorization': 'Bearer ' + 'q' * 32}
    with TestClient(app) as client:
        if role == 'qa':
            assert client.post('/hub/items/' + item['id'] + '/qa', headers=headers, json={
                'expected_version': item['version'], 'integration_sha': sha, 'passed': True}).status_code == 403
        else:
            payload = report(sha) if role == 'bug-report/v1' else {'schema_version': role, 'outcome': 'PASS', 'integration_sha': sha}
            msg = command('self', payload, contextId=item['id'], metadata={'expected_state_version': item['version']})
            assert rpc(client, headers, msg)['error']['code'] == -32000
    assert store.get_item(item['id']) == item


def test_rejected_attempt_with_unmet_dependency_is_waiting(store):
    item = new_item(store)
    attempt = store.claim(item['id'], 'worker-a', 0)
    patch = new_item(store, owner='provider', slice_id='payments')
    # A submitted attempt may be rejected; unlike working/input-required, this edge permits rejection.
    with store.transaction() as conn:
        conn.execute('INSERT INTO dependencies VALUES(?,?)', (item['id'], patch['id']))
    store.transition(attempt['id'], 'worker-a', attempt['epoch'], 0, 'rejected')
    assert store.get_item(item['id'])['state'] == 'WAITING'


def test_replayed_claim_still_rejects_boolean_version(store):
    item = new_item(store)
    first = store.claim(item['id'], 'worker-a', 0, request_id='claim', request_digest='d')
    with pytest.raises(ValueError):
        store.claim(item['id'], 'worker-a', False, request_id='claim', request_digest='d')
    with pytest.raises(ValueError):
        store.transition(first['id'], 'worker-a', True, 0, 'working')


def test_prune_preserves_even_inconsistent_delivered_dead_letter(store):
    with store.transaction() as conn:
        key = store.emit(conn, 'dead', {})
        conn.execute('UPDATE outbox SET delivered=1,dead_letter=1,available=0 WHERE id=?', (key,))
    assert store.prune_history(delivered_before=10000) == 0
    with pytest.raises(Conflict):
        store.replay_dead_letter(key, 'coordinator')


def test_qa_boolean_version_rest_rpc_and_store_rejected(store, api):
    client, headers, _ = api
    item = new_item(store)
    sha = 'a' * 40
    with store.transaction() as conn:
        conn.execute("UPDATE work_items SET state='MERGED_PENDING_QA',integration_sha=? WHERE id=?", (sha, item['id']))
    assert client.post('/hub/items/' + item['id'] + '/qa', headers=headers['qa'], json={
        'expected_version': False, 'integration_sha': sha, 'passed': True}).status_code == 422
    msg = command('qa-bool', {'schema_version': 'qa-verdict/v1', 'outcome': 'PASS', 'integration_sha': sha},
                  contextId=item['id'], metadata={'expected_state_version': False})
    assert rpc(client, headers['qa'], msg)['error']['code'] == -32602
    with pytest.raises(ValueError):
        store.qa(item['id'], False, sha, True, 'qa')


@pytest.mark.parametrize('msg_id', ['', '   ', 123, None])
def test_dingtalk_fallback_only_when_msg_id_absent(store, clock, msg_id):
    from a2a_slice_hub.im import DingTalkVerifier
    timestamp = str(int(clock() * 1000))
    headers = {'timestamp': timestamp, 'sign': DingTalkAdapter.sign(timestamp, 'fixture')}
    verifier = DingTalkVerifier('fixture', clock)
    assert verifier.verify(headers, b'{}').startswith('dingtalk:ts:')
    with pytest.raises(ValueError, match='msgId'):
        verifier.verify(headers, json.dumps({'msgId': msg_id}).encode())
