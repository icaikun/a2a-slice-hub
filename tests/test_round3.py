"""Round 3 executable adversarial regressions (no network service required)."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import secrets
import sqlite3

import httpx
import pytest
from asgi_client import ASGITestClient as TestClient

from a2a_slice_hub.api import Principal, create_app
from a2a_slice_hub.approvals import Approvals
from a2a_slice_hub.errors import Conflict, Forbidden, GateRejected
from a2a_slice_hub.gatekeeper import Gatekeeper
from a2a_slice_hub.im import (DingTalkAdapter, DingTalkVerifier, OutboxDispatcher,
                              collaboration_summary)
from a2a_slice_hub.paths import enforce
from a2a_slice_hub.review import schema_compatible
from a2a_slice_hub.runner import TrustedPytestRunner
from a2a_slice_hub.slices import SliceRegistry
from a2a_slice_hub.store import Store, SCHEMA
from conftest import Approve, new_item, submit
from test_qa import pending, report
from test_round2 import specs, online, item_for, working, dependency, patches


@pytest.mark.parametrize('outcome', ['PASS', 'FAIL', 'ENV_ERROR', 'FLAKY'])
def test_all_qa_outcomes_replay_original_snapshot(store, repo, outcome):
    item = pending(store, repo, new_item(store))
    detail = report(item['integration_sha']) if outcome == 'FAIL' else {'observed': 'PASS'}
    args = (item['id'], item['version'], item['integration_sha'], None, 'qa', detail)
    kwargs = dict(outcome=outcome, request_id='qa:replay', request_digest='digest')
    first = store.qa(*args, **kwargs)
    assert store.qa(*args, **kwargs) == first
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM inbox WHERE id=?', ('qa:replay',)).fetchone()[0] == 1
    with pytest.raises(Conflict):
        store.qa(*args, **(kwargs | {'request_digest': 'changed'}))
    assert store.get_item(item['id']) == first


def test_qa_receipt_insert_failure_rolls_back_state(store, repo):
    item = pending(store, repo, new_item(store))
    with store.transaction() as conn:
        conn.execute("CREATE TRIGGER deny_inbox BEFORE INSERT ON inbox BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(Conflict):
        store.qa(item['id'], item['version'], item['integration_sha'], None, 'qa',
                 outcome='ENV_ERROR', request_id='qa:atomic', request_digest='digest')
    assert store.get_item(item['id']) == item


def test_automatic_patch_grants_provider_implementation_and_only_target_contract(store, repo):
    store.registry = SliceRegistry(specs())
    online(store)
    consumer = item_for(store)
    dependency(store, working(store, consumer))
    patch = patches(store)[0]
    allowed = json.loads(patch['allowed_paths'])
    assert set(allowed) == {'contracts/orders.json', 'src/orders', 'tests/unit/orders'}
    for path in ('src/payments/service.py', 'tests/test_acceptance.py', 'contracts/payments.json'):
        with pytest.raises(GateRejected):
            enforce(path, allowed)
    contract = json.loads(repo.git.blob(repo.base, 'contracts/orders.json'))
    contract['properties']['expire_time'] = {'type': 'string'}
    source = repo.source({'contracts/orders.json': json.dumps(contract),
                          'src/orders/service.py': 'def total():\n    return 2\n\ndef order():\n    return {"id": "x", "expire_time": "tomorrow"}\n',
                          'tests/unit/orders/test_new.py': 'def test_new():\n    assert True\n'})
    a = working(store, patch)
    a = store.transition(a['id'], a['worker_id'], a['epoch'], a['version'], 'completed', base_sha=repo.base, source_sha=source)
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    result = gate.merge('epic/demo', [a['id']])
    row = store.get_item(patch['id'])
    store.qa(row['id'], row['version'], result['candidate_sha'], True, 'independent-qa')
    assert store.get_item(consumer['id'])['state'] == 'READY'


def family(store):
    parent = store.create_item('BUG', 'integration', 'coordinator', ['src'], change_set_id='group')
    a = new_item(store, parent_id=parent['id'], change_set_id='group')
    b = new_item(store, owner='worker-b', slice_id='payments', parent_id=parent['id'], change_set_id='group')
    task = store.claim(a['id'], a['owner'], a['version'])
    task = store.transition(task['id'], a['owner'], task['epoch'], task['version'], 'working')
    return parent, a, b, task


def test_duplicate_dispute_cannot_kill_sibling_and_distinct_modules_can_escalate(store):
    parent, a, b, task = family(store)
    store.dispute(b['id'], b['owner'], 'not me')
    with pytest.raises(Conflict):
        store.dispute(b['id'], b['owner'], 'not me again')
    assert store.get_item(parent['id'])['dispute_round'] == 1
    assert store.get_item(parent['id'])['state'] == 'READY'
    assert store.get_attempt(task['id'])['state'] == 'working'
    store.dispute(a['id'], a['owner'], 'trace shows conflicting ownership')
    assert store.get_item(parent['id'])['state'] == 'BLOCKED'
    assert store.get_attempt(task['id'])['state'] == 'failed'


def test_parallel_duplicate_disputes_count_once(store):
    parent, a, b, task = family(store)
    def dispute(_):
        try:
            store.dispute(b['id'], b['owner'], 'not me')
            return 'accepted'
        except Conflict:
            return 'duplicate'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(dispute, range(2))) == ['accepted', 'duplicate']
    assert store.get_attempt(task['id'])['state'] == 'working'
    assert store.get_item(parent['id'])['dispute_round'] == 1


def test_dispute_new_versions_do_not_reset_dedup_but_signed_reassignment_does(store):
    parent, a, b, task = family(store)
    store.dispute(b['id'], b['owner'], 'not me')
    claimed = store.claim(b['id'], b['owner'], store.get_item(b['id'])['version'])
    with pytest.raises(Conflict):
        store.dispute(b['id'], b['owner'], 'version changed')
    approvals = Approvals(store, secrets.token_bytes(32))
    card = approvals.issue(parent['id'], {'decision': 'coordinate and reassign same owner'})
    approvals.approve(card, 'human')
    assert store.get_attempt(claimed['id'])['state'] == 'canceled'
    store.dispute(b['id'], b['owner'], 'still disputed after human coordination')
    # Round 4: a new generation requires two independent owners, not lifetime votes.
    assert store.get_item(parent['id'])['state'] == 'REWORK'
    assert store.get_item(parent['id'])['dispute_round'] == 1
    store.dispute(a['id'], a['owner'], 'second independent owner after coordination')
    assert store.get_item(parent['id'])['state'] == 'BLOCKED'


def test_same_module_children_and_nonholder_cannot_escalate(store):
    store.registry = SliceRegistry(specs())
    online(store, 'one')
    online(store, 'two')
    parent = store.create_item('BUG', 'checkout', 'zhang', ['src/checkout'], change_set_id='g')
    children = [item_for(store, 'orders', parent_id=parent['id'], change_set_id='g') for _ in range(2)]
    working(store, children[0], 'one')
    with pytest.raises(Conflict):
        store.dispute(children[0]['id'], 'two', 'not lease holder')
    store.dispute(children[0]['id'], 'one', 'trace')
    with pytest.raises(Conflict):
        store.dispute(children[1]['id'], 'two', 'same module')
    assert store.get_item(parent['id'])['dispute_round'] == 1


def test_outbox_exponential_backoff_dlq_and_healthy_message_not_blocked(store, clock, api):
    with store.transaction() as conn:
        poison = store.emit(conn, 'poison', {})
        store.emit(conn, 'healthy', {})
    class Transport:
        def send(self, event_id, topic, payload):
            if topic == 'poison':
                raise OSError('private URL and token must never enter last_error')
    dispatcher = OutboxDispatcher(store, Transport())
    # IDs randomize tie order; drain all currently available events.
    dispatcher.once(); dispatcher.once()
    for tries, delay in [(1, 5), (2, 10), (3, 20), (4, 40)]:
        with store.connect() as conn:
            row = dict(conn.execute('SELECT * FROM outbox WHERE id=?', (poison,)).fetchone())
        assert row['tries'] == tries and row['available'] == clock.now + delay
        assert dispatcher.once() is False
        clock.now += delay
        assert dispatcher.once() is False
    dead = store.dead_letters()
    assert len(dead) == 1 and dead[0]['id'] == poison and dead[0]['tries'] == 5
    assert dead[0]['last_error'] == 'OSError'
    clock.now += 10000
    assert dispatcher.once() is False
    client, headers, _ = api
    assert client.get('/hub/outbox/dead-letters', headers=headers['worker']).status_code == 403
    assert client.get('/hub/outbox/dead-letters', headers=headers['coordinator']).json()[0]['id'] == poison


def test_outbox_expired_claims_reach_dlq_even_without_ack(store, clock):
    with store.transaction() as conn:
        store.emit(conn, 'crashing', {})
    for _ in range(5):
        assert store.claim_outbox(ttl=1)
        clock.now += 2
    assert store.claim_outbox(ttl=1) is None
    assert store.dead_letters()[0]['last_error'] == 'delivery lease expired'


def test_outbox_malformed_payload_retried_and_isolated(store, clock):
    with store.transaction() as conn:
        key = store.emit(conn, 'broken', {})
        conn.execute('UPDATE outbox SET payload=? WHERE id=?', ('{', key))
    assert not OutboxDispatcher(store, object(), max_tries=1).once()
    assert store.dead_letters()[0]['last_error'] == 'JSONDecodeError'


def test_round2_database_migration_preserves_data_and_is_idempotent(tmp_path, clock):
    path = tmp_path / 'legacy.sqlite'
    # SCHEMA remains the v2 schema; v3 is applied additively under a write lock.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO outbox(id,topic,payload,available,tries) VALUES('old','event','{}',0,3)")
    for _ in range(2):
        migrated = Store(path, clock=clock)
    with migrated.connect() as conn:
        row = dict(conn.execute("SELECT * FROM outbox WHERE id='old'").fetchone())
    assert row['tries'] == 3 and row['dead_letter'] == 0 and row['last_error'] is None


def rpc(client, headers, message):
    return client.post('/a2a', headers=headers, json={'jsonrpc': '2.0', 'id': message['messageId'],
        'method': 'message/send', 'params': {'message': message}}).json()


def command(mid, payload, **extra):
    return dict(role='user', messageId=mid, parts=[{'kind': 'data', 'data': payload}], **extra)


def test_pure_jsonrpc_claim_work_complete_gate_qa_and_replay(store, repo):
    tokens = {r: secrets.token_urlsafe(32) for r in ('worker', 'coordinator', 'qa')}
    principals = {t: Principal('worker-a' if r == 'worker' else r, r) for r,t in tokens.items()}
    headers = {r: {'Authorization': 'Bearer '+t} for r,t in tokens.items()}
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    app = create_app(store, Approvals(store, secrets.token_bytes(32)), principals, gatekeeper=gate)
    item = new_item(store)
    source = repo.source({'src/orders/service.py': 'def total():\n    return 3\n'})
    with TestClient(app) as client:
        claim = dict(role='user', messageId='claim', parts=[{'kind': 'text', 'text': 'claim'}],
                     metadata={'work_item_id': item['id'], 'expected_state_version': item['version']})
        first = rpc(client, headers['worker'], claim)
        a = first['result']
        work = command('working', {'schema_version': 'task-status/v1', 'state': 'working'},
            taskId=a['id'], contextId=item['id'], metadata={'lease_epoch': a['metadata']['lease_epoch'], 'expected_state_version': 0})
        worked = rpc(client, headers['worker'], work)
        a = worked['result']
        complete = command('complete', {'schema_version': 'dev-submission/v1', 'base_sha': repo.base, 'source_sha': source},
            taskId=a['id'], contextId=item['id'], metadata={'lease_epoch': a['metadata']['lease_epoch'], 'expected_state_version': 1})
        completed = rpc(client, headers['worker'], complete)
        assert completed['result']['status']['state'] == 'completed'
        assert rpc(client, headers['worker'], work) == worked  # old response, not current state
        assert rpc(client, headers['worker'], claim) == first
        merge = command('merge', {'schema_version': 'gate-merge/v1', 'branch': 'epic/demo', 'attempt_ids': [a['id']]})
        assert 'error' in rpc(client, headers['worker'], merge)
        merged = rpc(client, headers['coordinator'], merge)
        assert rpc(client, headers['coordinator'], merge) == merged
        row = store.get_item(item['id'])
        verdict = command('qa', {'schema_version': 'qa-verdict/v1', 'integration_sha': merged['result']['candidate_sha'], 'outcome': 'PASS'},
                          contextId=item['id'], metadata={'expected_state_version': row['version']})
        assert 'error' in rpc(client, headers['worker'], verdict)
        accepted = rpc(client, headers['qa'], verdict)
        assert accepted['result']['parts'][0]['data']['state'] == 'ACCEPTED'
        assert rpc(client, headers['qa'], verdict) == accepted
        assert rpc(client, headers['worker'], complete) == completed
        changed = deepcopy(complete); changed['parts'][0]['data']['source_sha'] = repo.base
        assert 'error' in rpc(client, headers['worker'], changed)


@pytest.mark.parametrize('outcome', ['ENV_ERROR', 'FLAKY', 'FAIL'])
def test_rpc_exceptional_qa_receipts(api, store, repo, outcome):
    client, headers, _ = api
    item = pending(store, repo, new_item(store))
    message = command('verdict', {'schema_version': 'qa-verdict/v1', 'integration_sha': item['integration_sha'],
        'outcome': outcome, 'report': report(item['integration_sha']) if outcome == 'FAIL' else {'observed': 'PASS'}},
        contextId=item['id'], metadata={'expected_state_version': item['version']})
    result = rpc(client, headers['qa'], message)
    assert 'result' in result
    assert rpc(client, headers['qa'], message) == result


@pytest.mark.parametrize("package", [True, False])
def test_app_root_layout_and_trusted_test_precedence(repo, package):
    base = repo.source({'app/orders/__init__.py': '', 'app/__init__.py': '',
        'app/orders/service.py': 'VALUE = 1\n', **({'tests/__init__.py': ''} if package else {}),
        'tests/test_acceptance.py': 'from app.orders.service import VALUE\nfrom tests.helper import TRUSTED\n\ndef test_total():\n    assert VALUE == 2 and TRUSTED\n',
        'tests/helper.py': 'TRUSTED = True\n', 'src/orders/service.py': None, 'src/payments/client.py': None})
    candidate = repo.source({'app/orders/service.py': 'VALUE = 2\n',
        'tests/__init__.py': 'raise RuntimeError("untrusted tests package")\n',
        'tests/helper.py': 'raise RuntimeError("candidate tests shadowed trusted")\n',
        'conftest.py': 'raise RuntimeError("candidate conftest loaded")\n',
        'tests/conftest.py': 'raise RuntimeError("candidate tests conftest loaded")\n'}, base=base)
    evidence = TrustedPytestRunner(allow_local_execution=True).run(repo.git, base, candidate, {'orders'})
    assert evidence['passed'] == ['tests/test_acceptance.py::test_total']


@pytest.mark.parametrize('topic', ['contract.patch_requested', 'gate.rejected', 'qa.result', 'ownership.dispute', 'epic.l0_pending', 'epic.release_ready'])
def test_human_readable_event_summaries(topic):
    text = collaboration_summary(topic, {'item': 'task', 'slice_id': 'orders', 'owner': 'zhang',
        'request': {'contract': 'orders', 'expected_change': {'optional': 'expire_time'}},
        'requester_slice': 'checkout', 'requester_owner': 'li', 'passed': True})
    assert 'task' in text and any('\u4e00' <= c <= '\u9fff' for c in text)
    assert '"item":' not in text


@pytest.mark.parametrize('state', ['BLOCKED', 'BLOCKED_ENV', 'FLAKY_BLOCKED'])
def test_blocked_state_atomic_escalation_once(store, state):
    item = new_item(store)
    with store.transaction() as conn:
        conn.execute('UPDATE work_items SET state=?,blocked_reason=? WHERE id=?', (state, 'test', item['id']))
        conn.execute('UPDATE work_items SET state=? WHERE id=?', (state, item['id']))
    with store.connect() as conn:
        events = conn.execute("SELECT payload FROM outbox WHERE topic='item.escalated'").fetchall()
    assert len(events) == 1
    payload = json.loads(events[0][0])
    assert payload['mention_user_id'] == 'coordinator'
    assert '@coordinator' in collaboration_summary('item.escalated', payload)


def test_dingtalk_webhook_and_openapi_payloads(clock):
    requests = []
    def send(request):
        requests.append(request)
        return httpx.Response(200, json={'errcode': 0} if request.url.host == 'oapi.dingtalk.com' else {'processQueryKey': 'fixture'})
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        adapter = DingTalkAdapter(client, webhook_url='https://oapi.dingtalk.com/robot/send?access_token=fixture',
            secret='test-signing-secret', clock=clock, mention_ids={'coordinator': 'staff-1'})
        adapter.send('event', 'item.escalated', {'state': 'BLOCKED', 'mention_user_id': 'coordinator'})
        DingTalkAdapter(client, token=lambda: 'test-runtime-token', robot_code='robot', conversation_id='group').send('event', 'qa.result', {'passed': True})
    assert requests[0].url.params['access_token'] == 'fixture'
    assert requests[0].url.params['sign'] == DingTalkAdapter.sign('1000000', 'test-signing-secret')
    body = json.loads(requests[0].content)
    assert body['msgtype'] == 'markdown' and body['at']['atUserIds'] == ['staff-1']
    assert requests[1].headers['x-acs-dingtalk-access-token'] == 'test-runtime-token'
    assert json.loads(requests[1].content)['msgKey'] == 'sampleMarkdown'


def test_dingtalk_signature_expiry_mapping_and_callback_gateway(store, clock):
    secret = 'fixture-secret'
    verifier = DingTalkVerifier(secret, clock, {'staff': 'human'})
    timestamp = str(int(clock() * 1000))
    headers = {'timestamp': timestamp, 'sign': DingTalkAdapter.sign(timestamp, secret)}
    assert verifier.verify(headers, b'{}').startswith('dingtalk:')
    assert verifier.sender({'senderStaffId': 'staff'}) == 'human'
    with pytest.raises(Forbidden): verifier.sender({'senderStaffId': 'attacker'})
    with pytest.raises(Forbidden): verifier.verify(headers | {'sign': 'wrong'}, b'{}')
    with pytest.raises(Forbidden): verifier.verify(headers | {'timestamp': '0'}, b'{}')
    token = secrets.token_urlsafe(32)
    app = create_app(store, Approvals(store, secrets.token_bytes(32)), {token: Principal('gateway', 'im_gateway')},
                     dingtalk_secret=secret, dingtalk_users={'staff': 'human', 'staff2': 'other'})
    with TestClient(app) as client:
        body = {'senderStaffId': 'staff', 'text': {'content': 'hello'}}
        assert client.post('/im/dingtalk/events', headers=headers, json=body).status_code == 401
        headers['Authorization'] = 'Bearer ' + token
        result = client.post('/im/dingtalk/events', headers=headers, json=body)
        assert result.json() == {'sender': 'human', 'accepted_as_command': False}
        assert client.post('/im/dingtalk/events', headers=headers, json=body).json() == result.json()
        assert client.post('/im/dingtalk/events', headers=headers, json=body | {'senderStaffId': 'staff2'}).status_code == 409


def test_merge_rpc_receipt_survives_crash_between_git_cas_and_db_commit(store, repo):
    item = new_item(store)
    attempt = submit(store, item, repo)
    gate = Gatekeeper(store, repo.git, TrustedPytestRunner(allow_local_execution=True), Approve())
    publish = repo.git.publish
    def crash_after_publish(intent):
        assert publish(intent)
        raise OSError('simulated crash after CAS')
    repo.git.publish = crash_after_publish
    with pytest.raises(OSError):
        gate.merge('epic/demo', [attempt['id']], request_id='gate:crash', request_digest='digest')
    assert store.get_item(item['id'])['state'] == 'GATING'
    repo.git.publish = publish
    other = new_item(store, owner='other', slice_id='payments')
    with pytest.raises(Conflict):
        store.claim(other['id'], other['owner'], other['version'], request_id='gate:crash', request_digest='different')
    result = gate.merge('epic/demo', [attempt['id']], request_id='gate:crash', request_digest='digest')
    assert result == gate.merge('epic/demo', [attempt['id']], request_id='gate:crash', request_digest='digest')
    assert store.get_item(item['id'])['state'] == 'MERGED_PENDING_QA'
    with pytest.raises(Conflict):
        gate.merge('epic/demo', [attempt['id']], request_id='gate:crash', request_digest='other')
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE topic='merge.committed'").fetchone()[0] == 1


def test_outbox_backoff_caps_at_300_seconds(store, clock):
    with store.transaction() as conn:
        store.emit(conn, 'poison', {})
    for delay in (5, 10, 20, 40, 80, 160, 300, 300):
        event = store.claim_outbox(max_tries=10)
        store.ack_outbox(event['id'], event['claim'], False, max_tries=10)
        with store.connect() as conn:
            assert conn.execute('SELECT available FROM outbox WHERE id=?', (event['id'],)).fetchone()[0] == clock.now + delay
        clock.now += delay


def test_escalation_rollback_and_aggregate_dispute_rejected(store):
    parent, a, b, task = family(store)
    with pytest.raises(Conflict):
        store.dispute(parent['id'], parent['owner'], 'impersonate a child')
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            conn.execute("UPDATE work_items SET state='BLOCKED' WHERE id=?", (parent['id'],))
            raise RuntimeError('rollback')
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE topic='item.escalated'").fetchone()[0] == 0


def test_test_client_runs_lifespan_without_deprecated_transport():
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    observed = []
    @asynccontextmanager
    async def lifespan(app):
        observed.append('startup')
        yield
        observed.append('shutdown')
    app = FastAPI(lifespan=lifespan)
    @app.get('/health')
    def health():
        return {'ok': True}
    with TestClient(app) as client:
        assert observed == ['startup']
        assert client.get('/health').json() == {'ok': True}
    assert observed == ['startup', 'shutdown']
