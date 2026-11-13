# Milestone B integration and adversarial handoff

## Compatibility and schema

Schema v5 upgrades in-place in the same atomic bootstrap transaction as earlier
migrations, checks foreign keys, and refuses future schemas. The legacy
`claim_outbox` / `ack_outbox` / `OutboxDispatcher` API remains unchanged.
The existing concurrent migration regression now expects final schema **5**
instead of 4; its concurrency, old-data and atomicity assertions are retained.

`outbox_destinations` registers stable destination/provider identities.
`outbox_deliveries` owns leases, attempts, retries, receipts and bounded replay
budgets independently for each destination. An `AFTER INSERT` trigger materializes
fan-out even for `blocked_escalation`, which bypasses Python `Store.emit`.
Registration and claim-time backfill cover historical undelivered events and
repair missing delivery rows. Disabling a destination stops claims, but does not
allow pruning its undelivered evidence. Provider rebinding requires a new ID.

`MultiOutboxDispatcher(store, adapters, max_tries=5)` registers its adapters.
`once()` processes at most one available event per destination; an explicit
`once(destination_id)` targets only that destination. The return value means
work was attempted, not that every provider accepted delivery. Retry timing,
priority/starvation promotion and expired-lease retry caps are persisted.
Failures store only a generic safe diagnostic, never provider exception text.
Replay audit events are durable control-plane records, not extra IM messages.
Do not operate legacy and multi-destination consumers as competing production
notification paths: the legacy API has no concept of destination delivery.

## Canonical identity and admission

`ChannelGate.evaluate(ChatEnvelope(...))` returns a `GatedDecision`. Pairing and
allowlists admit contact, never confer roles. Group access requires an allowed
chat and, by default, an explicit bot mention. An all-members mention alone and
bot/app echoes are rejected. `IdentityPolicy` still authorizes each approval
against the target's canonical owner, across platform bindings.

Instantiate `FeishuIngress(approvals, identity_policy, gate, bots=..., bot_open_id=...)`.
All three bots must have distinct app IDs; only the orchestrator accepts ingress.
`bot_open_id` is the orchestrator's actual mention identity, not a display name.
`handle_chat` returns a gating decision; `handle_card` returns the committed Hub
approval result. `card_response` adds the provider toast/card after commit.

DingTalk protocols are deliberately separate:

- `DingTalkChatIngress(gate, app_id=..., chatbot_user_id=...)` handles
  `/v1.0/im/bot/messages/get` chatbot messages.
- `DingTalkCardIngress(approvals, identity_policy, app_id=...)` handles
  `/v1.0/card/instances/callback`, using `corpId`, `userId`, `outTrackId` and
  `cardPrivateData.params.card`. SDK JSON-encoded `content` wrappers are accepted.
  A nonempty `eventId`, `msgId`, or authenticated Stream header `message_id` is
  required; SDK in-memory dedup is never authoritative.

Ingress classes assume an authenticated transport. Do not expose their methods
directly to arbitrary webhook JSON. Feishu HTTP uses the existing original-byte
signature verifier; DingTalk card HTTP requires an `im_gateway` bearer principal.
Callbacks use a 2-second SQLite write timeout, and return success only after
commit. Receipts are namespaced by provider, tenant and app, and bound to the
external user and complete signed card. Recreating ingress objects returns the
stored inbox response without invoking approval again. Authorization is checked
before the ingress cache; an unchanged receipt replay returns its original
`duplicate` value, whereas a new receipt after approval returns `duplicate=True`.

## Outbound cards and synchronization

Configure `FeishuChannelAdapter(bots, chat_id, client, store=store,
 destination_id="feishu-main")`; bot tokens are runtime callables. The existing
three-bot routing remains: QA to QA feed, attempt/merge/gate to dev feed, other
events to orchestrator. Pending cards are posted once the provider returns a
message ID; updates PATCH that message using the orchestrator token.

Configure `DingTalkChannelAdapter(client, token=..., robot_code=...,
 conversation_id=..., card_template_id=..., destination_id="dingtalk-main",
 store=store)`. Standard messages use robot OpenAPI. Approval cards use
`createAndDeliver`, a deterministic per-approval/per-destination `outTrackId`,
and PUT updates. Provision a DingTalk interactive template that binds
`cardParamMap.card` into callback params and uses `showApprove` / `disabled` to
hide or disable the button; terminal payloads also clear the signed card value.
The template is provider-side configuration, not created by this package.

Both destinations share the signed `approval_id`, not the provider card ID.
Projections move from PENDING/version 1 to APPROVED/version 2. Equal/older writes
and terminal-to-PENDING writes are rejected. A destination recovering after
approval posts the initial card directly as APPROVED without a button. A second
DB read after posting closes the approval-during-HTTP-request race; acceptance
can be consumed before the delayed projection exists without losing the update.

Provider I/O is never performed under a SQLite transaction. Delivery remains
**at-least-once**, not distributed exactly-once: a process crash after provider
acceptance and before receipt persistence can require an idempotent provider
retry. The database projection guard cannot itself enforce remote atomic CAS.
Provider/network recovery and replay are required for eventual synchronization.

## API and optional SDK integration

`create_app` accepts `identity_policy`, `feishu_card_ingress`, and
`dingtalk_card_ingress`. Legacy `feishu_users` behavior is retained when the new
policy/ingress is absent. Prefer the configured Feishu ingress for explicit
three-bot topology enforcement; identity-only mode relies on the application's
verified webhook credentials and canonical account bindings.

Coordinator-only inspection/replay:

- `GET /hub/outbox/destinations/dead-letters?destination_id=...`
- `POST /hub/outbox/destinations/{destination_id}/dead-letters/{event_id}/replay`

`build_lark_ws_dispatcher` and `register_dingtalk_stream_handlers` import optional
SDKs only when invoked. The caller owns WS/Stream clients and their credentials.
The DingTalk helper registers both topics separately and takes the durable
receipt from the SDK message header when needed. Chat admission alone does not
execute development or approval commands.

## Verification and acceptance boundary

`tests/test_milestone_b_dual_im.py` uses real SQLite, real Git and both CLI mock
subprocess paths, plus HTTP mock transports. It covers the A1-A3 fixes, shared
approval sync in both directions, concurrent clicks, canonical self-approval
prevention, pairing/topology/mention rules, trigger fan-out, independent failures,
DLQ/replay, delayed cards, in-flight acceptance, restart receipts, migration
rollback/future-version rejection, stale claims, safe diagnostics and API roles.
SDK wiring is tested with contract doubles; no optional SDK installation is
required for import or for the suite.

Required full regression command:

```powershell
.venv\Scripts\python.exe tests/run_windows_inherited_temp.py --basetemp=.pytest_tmp -W error -q
```

No live Feishu/DingTalk account, WS/Stream session or provider-side template was
provisioned or exercised by this milestone. Live transport, template rendering,
permissions and actual user-click acceptance remain deployment checks. No release
publication or automatic configuration change is implied by this implementation.
