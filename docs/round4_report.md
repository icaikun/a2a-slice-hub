# Round 4：对抗反例修复与 Round 5 交接

日期：2026-11-13。范围：当前工作区源码、测试与操作文档。未部署生产服务、未调用真实 IM 租户、未修改机器代理配置。

## 1. 结论与证据入口

本轮实现了请求中的十项边界/修复，保留单机 SQLite + 本地 Git CAS 架构；没有引入 sent 表、重放微服务、Python AST 沙箱或新消息总线。

**最终全量：253 passed in 241.30s；0 failures、0 errors、0 skips、0 warnings。**
其中 `tests/test_round4.py` 为新增 **98 个**测试实例；原有 155 个均通过（含按新仲裁语义加强的一条 Round 3 测试）。最终源码哈希、模块计数与证据哈希见 JSON 摘要。

证据：

- `tests/test_round4.py`：本轮可执行反例与集成回归。
- `.codex_round4_tmp/full-results.xml`、`.codex_round4_tmp/full.log`：最终全量 JUnit 和终端输出（本地忽略目录）。
- `docs/round4_test_summary.json`：可交付的机器可读测试计数与命令摘要。
- `SECURITY.md`、`docs/state_transitions.md`、`README.md`：现行边界与使用说明。

没有声称重新执行用户的 `redteam_round4_exploits.py` 原文件；这些反例按给定 JSON/时序重建为测试。原有源码在本工作区本来就是未跟踪状态；本轮不 git add/commit，也不覆盖历史 Round 1–3 报告。

## 2. 十项修复及验证

| 挑战 | 实现与直接证据 | 状态 |
|---|---|---|
| 1. IM at-least-once | 两个真实 adapter 经 HTTPX MockTransport 收到 200 后，在 ack 前注入崩溃；过期前不重发，过期后重复发送。断言 Feishu uuid、DingTalk 正文 event ID 和完整载荷稳定。没有伪造 exactly-once。 | 本地 PASS；真实供应商去重未验证 |
| 2. 优先级、DLQ 重放 | v4 新增 priority/replay_count；三类紧急事件含 SQL trigger 自动 priority=10，压过 250 条 available=0 的旧事件/毒丸。单条重放校验未交付 DLQ，重置租约/重试、保留 ID、递增计数、事务性写 actor 审计。鉴权、重复重放冲突、审计失败回滚均覆盖。 | PASS |
| 3. 钉钉回执 | HMAC/时效之后验证 JSON 对象并拒绝 encrypt；有效 msgId 哈希键，只有未提供 msgId 才回退 timestamp/sign。显式空白、非字符串、null ID 拒绝。查 inbox 后返回原快照；同 ID 改正文 409，同毫秒不同 ID 均 200。独立网关被攻陷后 first-arrival 替换仍可成功的边界也锁定为测试。 | PASS；不是正文认证 |
| 4. Python 执行隔离 | 默认 allow_local_execution=False 的拒绝行为保留并测试；文档明确无控制面凭据/挂载、只读输入、默认断网、资源限制、进程树终止、销毁环境及证据校验的执行合同。没有增加 Python 黑名单。 | 默认拒绝 PASS；真实 OS 隔离未验证 |
| 5. Schema 保守性 | 新字段要求原 additionalProperties/unevaluatedProperties 严格为 True；禁止指定的对象级约束；新字段必须是 dict；递归检查新增字段中的本地定义引用，并追踪定义内的悬空引用。jsonschema Draft202012Validator 证明消费者失败，不只比较字符串。 | 请求列举的反例 PASS；一般双向兼容性不成立，见 Round 5 |
| 6. Git 与回执 | grant_id 不覆盖 intent UID。COMMITTED 回执和 PREPARED 恢复后的返回均检查 candidate 仍在分支 ancestry。真实 Git 测试覆盖 PREPARED、post-CAS 异常恢复、COMMITTED 后外部回退、PREPARED 后外部改到不相容分支以及两个 Gatekeeper 竞争。 | 本地 PASS；恶意外部写者仍不受 Hub 约束 |
| 7. 人类主体独立性 | disputes.owner NOT NULL 从 work_items.owner 回填，并有 UNIQUE(target,generation,owner)。同 owner 两个 slice 无法升级/杀 peer；第二独立 owner 可以升级。直接绕过 handler 的 SQL 仍受约束。registry-backed create_app/load_app 拒绝缺失/空白 QA owner；三个 QA 入口均无法用 principal 别名自审。 | PASS |
| 8. 共享依赖与恢复 | 复用非 accepted patch 时 MAX 延长 deadline，覆盖过期后加入。failed/canceled/rejected 有未满足依赖则 WAITING+dependency；补丁 QA 后独立消费者进入 READY/WAITING_WORKER 并固定 SHA，过期消费者不阻塞其他人。child resume 禁止，aggregate parent 同步恢复。 | PASS |
| 9. 迁移与清理 | bootstrap/v1/v2/v3/v4 DDL 在单一 BEGIN IMMEDIATE 下执行；不使用隐式提交的 executescript；事务末 FK 检查与 user_version=4。六连接同时启动、DDL 异常回滚、旧回执/intent/争议证据保留覆盖。WAL 首次切换有单独 30 秒有界 busy/locked 重试；连接 busy timeout=30。prune 默认 100、最大 1000，只删 cutoff 前 delivered=1 且非 DLQ 的 outbox。 | PASS；真实断电/磁盘满未验证 |
| 10. REST/A2A 差分 | Request 流式读取 <=1,000,000 bytes；parse error=-32700、batch/非对象=-32600，非 FastAPI 422。解析后仍在线程池运行同步业务，避免新 async 包装阻塞事件循环。Store/REST/A2A 拒绝 bool 版本/epoch；bug-report 与 qa-verdict 共用 handler/context 校验；owning-worker cancel 重试返回 canceled，不复活终态。 | PASS |

### 2.1 必须说明的语义变更

Round 3 的 `test_dispute_new_versions_do_not_reset_dedup_but_signed_reassignment_does` 曾要求同一个人在人工恢复后再次投票即可 BLOCKED。这与本轮“两个人、两个 slice、同一仲裁代”的要求矛盾。

本轮不是删除测试：保留版本改变不能重新投票、signed resume 可开启新代、旧租约取消的断言；将跨代累计升级改为 **当前代一个 owner 不升级，第二独立 owner 才升级**。`dispute_round` 在普通 signed resume 时归零，计票来自当前代实际 disputes，历史行不删。

### 2.2 遗留数据与保留策略

- v1/v2/v3 都可直接原子升级到 v4。未来 user_version >4 明确拒绝，不降级。
- 若旧库已有同 target/generation 的重复 human owner 票，无法无损地同时满足新唯一约束。本轮选择 **迁移整体回滚，保留原票据/版本**，不静默去重、不伪造 owner。需管理员备份并仲裁历史证据后再迁移；自动修复工具未实现。
- pre-v3 ID-only inbox 只能查出当前 attempt 状态，不能重建历史响应。新结构化 receipt 才保存当时快照。
- prune 的时间用成功 ack 写入的 available；不触碰 inbox、死信、未交付事件、任何 merge_intents。审计 outbox 并非不可变账本，归档之后再按业务保留期清理。
- 延长共享 patch 不会自动放行 BLOCKED/GATING 等状态，也不绕过 lease/epoch/owner/dependency 检查；短期消费者自身 deadline 没有被延长。

## 3. 验证过程与复现命令

```powershell
New-Item -ItemType Directory -Force .codex_round4_tmp | Out-Null
.\.venv\Scripts\python.exe tests/run_windows_inherited_temp.py `
  -W error -p no:cacheprovider --basetemp=.codex_round4_tmp/full -q `
  --junitxml=.codex_round4_tmp/full-results.xml
```

- 首次定向执行因 basetemp 的父目录尚不存在而失败；创建工作区内父目录后继续，没有改机器 ACL。
- Store + Round 3 定向：49 passed in 41.40s。
- Round 4 第一轮：81 passed / 4 failed；四个失败均为并发第一次 WAL journal_mode 切换的真实锁竞争。修复有界初始化重试后迁移/仲裁定向 12 passed。
- Round 4 + Round 3 定向：129 passed in 76.24s。随后补充严格“只有 msgId 缺省才回退”的四个测试；最终全量 253 passed in 241.30s，Round 4 共 98 个。
- 第一轮全量为 249 passed in 238.04s；严格 msgId 边界修改后重新执行了最终全量。旧证据保留在 full-pre-msgid-results.xml / full-pre-msgid.log，不与最终结果混用。
- 两个 Round 5 已实测观察保存在 `.codex_round4_tmp/round5-observations.json`，不计入回归测试通过数。
- 测试使用原有 Windows inherited-temp 启动器、当前 venv、-W error；没有 ignore warnings/skip/xfail 掩盖失败。
- 新增 jsonschema 仅为 dev 依赖，uv.lock 同步；使用用户指定专用下载代理获取，未更改系统代理或使用日常代理下载。
- 崩溃测试是明确边界的异常注入，事务/真实 Git/本地 HTTP 调用链实际执行；不是拔电、内核 kill、真实 IM 消息已读或供应商 SLA 验收。

## 4. Round 5：剩余问题与收敛挑战

以下分为已证实缺口、代码审查风险和未验证部署条件。P0 是部署阻断条件，不代表宣称已经实现隔离。

### P0：部署安全边界（未验收）

1. **恶意候选代码隔离**：证明没有数据库/发布 Git/凭据/宿主写挂载及默认网络访问；测试逃逸、子进程、资源耗尽与超时杀进程树。不满足之前，不准为不可信代码开启本地执行。禁止用 AST 或 importlib 代替隔离。
2. **DingTalk 第一到达的恶意正文**：当前测试证明持有网关身份者仍可替换正文。要求验证公网 TLS、网关独立身份、密钥轮换与租户路径；若想由消息执行审批，先提供真正的主体/正文认证协议，不能复用 timestamp HMAC。

### P1：协议、契约和状态机

3. **“可选字段”不等于输入/输出双向兼容（已实测）**：新增 `ext: {"not": {}}` 仍是 dict，满足本轮指定结构门槛，schema_compatible 返回 True；真实 validator 对 `{ext: "x"}` 是 before 接受、after 拒绝。有效引用也可能指向永不匹配 schema。Round 5 应先确定契约方向与“兼容”定义，再决定小型白名单或人工 Breaking 审批；不要声称解决一般 schema 包含关系。
4. **gatekeeper 的纯 A2A 入口（ASGI 已复现）**：message/send 外层 role allowlist 未包括 gatekeeper，内层 gate-merge 又允许该 role；因此独立 gatekeeper principal 的外层权限与文档不一致，实际返回 -32000 / cannot submit work。REST gate 路由与 coordinator A2A 流程可用。请补独立 gatekeeper 的端到端角色差分，再做最小修复，不要扩大其他 command 权限。
5. **签名恢复与未满足依赖（待反例）**：本轮修复 failed/canceled/rejected 与 child resume；普通 aggregate resume 仍批量 REWORK。构造“有共享依赖的 consumer 被 ownership dispute 冻结，再 signed resume”时序，验证是否再次绕开 WAITING/SHA 唤醒路径。
6. **多依赖/多分支 ancestry（待反例）**：消费者的 integration_sha 目前固定最后一个完成依赖的 SHA。对跨 Epic 手工依赖、多个 provider 和并发 QA，证明最终 baseline 包含全部依赖；不只证明最后一个 patch accepted。
7. **外部 Git 写者与恢复（已知限制）**：回放 ancestry 检查存在检查后的竞态；PREPARED 在外部回退到原 base 后可能被合法 recovery 重新发布。不给恶意外部管理员“防篡改”承诺。Round 5 应测试真实 ref ACL、crash/process kill 和 GC 对未发布 candidate 的保留，不扩大 SQL 锁来假装跨系统事务。
8. **旧重复人类票的升级操作（已测试 fail-closed，修复工具缺失）**：需要最小离线审计/仲裁操作规程，保留旧行和证据；不得为“迁移成功率”自动删除不一致历史。

### P2：运维与代码简化

9. **紧急队列自身的饥饿与重放预算（未做压力/SLA 验收）**：priority=10 能压过普通毒丸，但持续紧急流也可能饿死普通流。单条重放并不等于全局速率限制；重复人工 replay 可无限重置 tries。测试告警延迟/审计归档后，再决定是否需要限速或 aging，暂不建调度服务。
10. **保留与入口缩减**：prune 只有 outbox；inbox、终态 intents、历史 disputes 仍增长。删 receipt 会改变幂等期限，先声明保留合同。API 仅合并 QA handler 和薄 raw-RPC 包装，没有新通用命令框架；后续按差分测试决定哪些 REST/A2A 重复入口可删，而不是继续叠抽象。

**验收边界**：本轮本地测试成功不等于安全沙箱、真实 IM 租户接入、系统断电恢复、产品 UX 或生产上线批准。存在上述 Round 5 P1/P2 项，不宣称“所有架构风险清零”。

