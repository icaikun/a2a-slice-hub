# Round 3 修补、验证与 Round 4 对抗任务书

日期：2026-11-13。范围：当前仓库源码、测试、示例配置与文档；未部署线上服务，未发送真实群消息，未改变代理或私有凭据。

## 1. 裁决与结论

接受四个红队反例，也接受对 `required + default` 言行不一的批评。`default` 不能作为新增必填字段的兼容证明，本轮已删除该例外。Round 2 报告作为历史证据保留，其第 34 行的放行政策已被本报告取代。

**最终全量结果：待写入最终运行证据。**

实施与本地自动化验收不等于真实飞书/钉钉租户验收，不等于完整 A2A 标准认证，也不等于恶意代码沙箱安全认证。

## 2. 逐项修补与直接证据

### 2.1 契约 required/default 漏洞

- `review.py::schema_compatible` 要求 required 集合完全相同；新增、删除、替换必填字段都不再自动放行，即使新字段有 default。
- 原有属性必须保留其约束；原对象 `additionalProperties: false` 仍拒绝新增属性。
- Breaking Change 仍通过原有精确 diff、版本、change-set 绑定的签名审批链，不另设旁路。
- 更新 Round 2 中旧的“带默认新增必填通过”断言为拒绝；保留原有签名审批集成用例。
- 这是受限兼容政策，不是 JSON Schema 全面蕴含证明；实现补值、客户端接受性仍需可信测试。

### 2.2 QA 异常分支 inbox 漏写

- ENV_ERROR / FLAKY 分支在同一状态事务内写入 inbox，再返回当时的完整快照。
- 正常 PASS / FAIL 同样返回事务内快照，不再提交后另开连接读取可能已变化的状态。
- 同 ID、同 digest 返回原结果；同 ID、不同 digest 拒绝。新增 claim / transition 回执也存原始快照；兼容读取历史 ID-only 回执，但旧回执无法追溯重建原快照。
- 回归覆盖四种 QA outcome 的原版本重试、摘要冲突、inbox INSERT 故障回滚；覆盖 `/a2a` 的异常 QA 重试。

### 2.3 自动 CONTRACT_PATCH 业务死锁

- 自动补丁授权为目标契约文件 + Provider 自己的 owned_paths + `tests/unit/<provider>`。
- 不授权 Provider 的其他契约，不授权消费者代码，不授权受保护的验收测试。
- 真 Git 集成测试同时修改契约、业务实现与 Provider 单测，经过 Gatekeeper 的路径/契约检查、可信 pytest、测试 reviewer，再独立 QA PASS，消费者回到 READY。
- 测试 reviewer 是确定性 fixture，不是本轮调用真实模型的结果；新增 Provider 单测也不替代 baseline-owned 可信验收套件。

### 2.4 单 Worker 刷争议冻死父单

- 新增持久化 disputes 表，记录 item、target、模块、仲裁 generation、原 version/rework_round、责任 Worker 和 evidence。
- 唯一约束为 `(item, generation)` 与 `(target, generation, slice_id)`；重复点击、并发点击、同模块多子单、领取后版本增长均不能刷有效次数。
- 有运行中 attempt 时，只允许未过期的当前租约持有者争议；未领取责任方可提出一次争议。冻结/终态/过期工单不可争议；聚合父单不能冒充一个子模块。
- 首次争议不杀兄弟任务。第二个独立模块提出争议才触发父子 BLOCKED 和活动租约失败。
- 首次争议也允许协调人签发 resume 审批。签名通过才增加仲裁 generation、取消旧运行租约并重派；保留累计有效争议次数，重派后再次争议会升级。
- 回归包括原始双击攻击、两线程竞争、兄弟任务存活、两模块真实升级、非持有者、同模块双子单与人工解锁再争议。

### 2.5 Outbox 毒丸、退避与 DLQ

- 重试延迟 `min(300, 5 * 2**(tries-1))`；默认最多 5 次投递尝试。
- 新增 dead_letter、last_error；死信不再进入 claim 查询。
- Worker 最后一次发送后崩溃、没有 ack，也会在租约过期后达到 DLQ 上限，避免绕过重试次数。
- JSON 解码、工单补充信息、适配器发送全部置于失败处理范围，损坏 payload 也会隔离。
- 错误只保留异常类别/HTTP 状态，避免异常字符串把 Webhook Token 或私有 URL 带入日志/数据库。
- `Store.dead_letters(limit)` 与协调人专用 `GET /hub/outbox/dead-letters` 提供查询；不添加无审计的自动重放/删除入口。
- 回归覆盖 5/10/20/40 秒、300 秒封顶、第五次失败、健康消息继续处理、过期 claim、损坏 payload、查询权限与 v2 数据库重复迁移。
- 投递语义仍是 at-least-once；“远端成功但本地 ack 前崩溃”可能重复，不能谎称 exactly-once。

### 2.6 纯 `/a2a` JSON-RPC 闭环

新增以下 **Hub 应用层 data schema**，不是虚构的 A2A 官方扩展方法：

| schema_version | 角色/主要字段 | 执行动作 |
|---|---|---|
| task-status/v1 | Worker；state=working | 租约/版本校验后转 working |
| dev-submission/v1 | Worker；base_sha/source_sha；state 默认 completed | 完成开发提交，进入 GATE_PENDING |
| qa-verdict/v1 | QA；integration_sha/outcome/report | 精确快照独立验收 |
| gate-merge/v1 | coordinator/gatekeeper；branch/attempt_ids | 执行真实三门禁与 Git CAS |

- Worker 元数据使用 lease_epoch / expected_state_version；QA 元数据使用工单 expected_state_version。
- taskId/contextId 必须匹配；QA 与开发身份隔离；messageId + 完整消息摘要绑定回执。
- Gate 请求绑定 merge_intents；成功的合并结果与 inbox 在 finish_merge 状态事务中落地。CAS 后事务崩溃可以沿已有 intent 恢复，不重新合并。
- 已保留的 Gate request ID 不允许另一条普通任务命令占用，避免跨命令回执冲突破坏 CAS 恢复。
- E2E 测试的初始工单/分支是 fixture；此后 Claim → Working → Completed → Gatekeeper → QA PASS **全部通过 `/a2a` 的 message/send**，没有通过 REST 代办中间步骤。另有原响应重放、越权、不同内容同 ID、CAS/DB 间故障注入测试。

### 2.7 统一 IM 与中文镜像

- FeishuAdapter / DingTalkAdapter 均实现 `send(event_id, topic, payload)`。
- 关键六类协作事件有中文摘要，契约请求包含请求方、Provider、契约与变更；不再裸发整包 payload。
- BLOCKED / BLOCKED_ENV / FLAKY_BLOCKED 的进入事件由 SQLite trigger 原子写入 outbox，包括父子批量更新；同状态重复写不重复告警，事务回滚则告警一起回滚。
- 告警携带 `mention_user_id=coordinator` 和可读 `@coordinator`。真实平台通知必须配置有效的 mention_ids；仅有文字或 fixture 不能证明手机收到提醒。
- 飞书仍保留三个真实应用身份的分流要求及原有交互审批卡片；普通事件正文转义，避免 evidence 注入飞书 at 标记。
- 钉钉支持自定义机器人 Webhook 与企业机器人 OpenAPI 的 Markdown 消息；Webhook 保留 access_token 查询项并添加毫秒 timestamp/sign；OpenAPI 使用运行时 Token。不是钉钉交互审批卡片全实现。
- DingTalkVerifier 实现指定的 HMAC-SHA256/Base64 时间戳签名、新鲜度检查、senderStaffId 白名单映射；工厂支持从环境读取签名 Secret。
- **重要安全边界**：`timestamp + "\n" + secret` 签名不包含消息正文，不能单独证明 senderStaffId。`/im/dingtalk/events` 还强制独立 im_gateway Bearer 身份，回调只返回映射身份、不执行审批/群聊命令；同签名不同正文拒绝。机器人明文回调不等于企业加密事件订阅解密协议。
- 发送/验签/映射/网关/回放已通过 MockTransport 和真实 ASGI 路由测试。外部官方文档检索未返回可核对内容，专用下载代理连接也不可用；未切到日常代理，没有真实租户连通性或当日服务端协议认证结论。

### 2.8 Runner 非 src 布局与可信测试优先

- 支持 candidate 根包；src 存在时再加入；可信根目录始终在候选路径之前。
- 使用 pytest importlib 收集，避免收集过程重排顶层导入路径；无 `tests/__init__.py` 的可信套件绑定可信 tests namespace，避免候选同名 regular package 抢占。
- `app/orders` 测试同时覆盖可信 tests 为 regular package / namespace 两种情况；候选 root conftest、tests/conftest、tests helper、tests package 注入均未覆盖可信测试。
- 仍然明确：候选业务 Python 被执行，路径顺序不是操作系统沙箱。

### 2.9 零警告与测试基础设施

- pyproject.toml 加入 `asyncio_default_fixture_loop_scope = "function"`。
- 当前已安装 Starlette 的 TestClient 对旧 HTTPX 发出弃用警告；没有过滤警告，也没有为了测试升级生产依赖。
- 新增小型测试专用 ASGITestClient，使用现有 HTTPX ASGITransport + AnyIO portal；仍通过真实 FastAPI 路由、认证、状态机与异常处理，并显式运行 lifespan。新增启动/关闭验证。
- 修正新旧迁移 fixture 的 SQLite 连接关闭，消除 Python 资源泄漏警告。
- 全量命令启用 `-W error`，警告会失败；不以 warning filter、skip、xfail 掩盖结果。

## 3. 执行记录与工作树边界

定向回归：**59 passed，44.81 秒，0 warnings**。此后增加了 Gate request ID 预留冲突保护，并纳入最终全量重验。

最终命令：

```powershell
.\.venv\Scripts\python.exe tests/run_windows_inherited_temp.py -q `
  -p no:cacheprovider --basetemp=.codex_round3_tmp/full -W error `
  --junitxml=.codex_round3_tmp/full-results.xml
```

- 原始直接 pytest 尝试暴露 cache 目录 ACL 拒绝，以及原有 Starlette 弃用警告，未冒充通过。
- 最终使用仓库现有 Windows 测试启动器，仅在本工作区新建临时目录时保留继承 ACL；运行的仍是本 venv 的完整 pytest，未改生产代码安全边界、未改既有目录 ACL。
- cacheprovider 禁用，避免无关缓存写入；独立 basetemp 不复用跨会话 `.pytest_tmp`。
- `.codex*_tmp*/`、`pytest-cache-files-*/` 已加入 .gitignore。中间日志/临时 Git 仓库/证据 XML 都在忽略目录，不混入交付源码。
- 当前仓库原有源码、文档本来就是未跟踪状态；本轮不自动 git add/commit，不删除历史成果。因此“干净”指无新增未忽略的测试垃圾，不声称 git status 为空。
- Round 1/2 历史报告不重写；现行 README、SECURITY、状态转换文档与示例配置同步更新。

## 4. Round 4：发给 Antigravity 的深度对抗拷问

以下是**待实测挑战/已知边界**，不是未经证据确认的新漏洞。请每题提交最小复现脚本、输入时序、数据库/Git/HTTP 证据与预期不变量；不要只给概念性断言。

1. **远端成功、本地未 ack 的双写窗口**：让 IM 已收消息，然后 kill dispatcher，租约过期再投递。没有平台幂等保证时，能否保证群里只出现一次？请明确证明做不到的边界。不要再添加一张“已发送表”就宣称解决跨系统 exactly-once。
2. **DLQ 重放与告警饥饿**：积累一万条毒丸，混入一条真实 BLOCKED 告警，测量最大延迟。人工重放谁能授权，次数预算如何重置，错误配置是否会造成告警风暴？当前没有重放接口，是否确实需要新服务，而不是一个审计过的小运维入口？
3. **钉钉签名的正文替换**：保留有效 timestamp/sign，把 senderStaffId 与正文换掉。只凭该 HMAC 能检测吗？请攻击独立网关身份、时间窗和同毫秒多合法回调的冲突策略；用真实租户验证机器人回调与加密事件订阅，不能混用协议。
4. **候选 Python 越过可信导入边界**：在合法业务文件里访问文件系统、网络、sys.modules 或 pytest hooks。importlib/trusted-first 不是沙箱；给出最小可信执行边界，以及必须交给容器/独立账户的部分。避免继续堆 Python 过滤器假装安全隔离。
5. **契约“可选”仍不兼容的反例**：测试 schema-valued additionalProperties、嵌套 $ref、oneOf、输入/输出方向、严格反序列化客户端。required 不变也不能证明全面兼容。请给实际 consumer 失败，不要只比较 JSON 文本。
6. **Git CAS 与数据库恢复交叉竞争**：在 PREPARED、CAS 前后、COMMITTED 前后分别 kill，随后外部管理员移动/回退分支、另一个 gate 抢跑、旧请求重试。每一条 inbox 返回的快照是否可由可达 Git 对象与持久化 intent 证明？
7. **责任模块与人的身份是否可合谋扩增**：同一自然人控制两模块、同 owner 多机器、多 Token、QA 身份别名。两模块争议是否就该视为“独立意见”？当前策略只保证模块计数，不保证人类主体独立；请提出可验证且不过度设计的授权规则。
8. **共享补丁的取消/期限/史诗边界**：两个 consumer 复用同一 CONTRACT_PATCH，其中一个超时或撤销；Provider 失联又恢复；补丁已合并但 QA 不通过。另一个 consumer 是否被错误释放或永久等待？给出依赖图和每一步版本。
9. **迁移并发与历史幂等回执**：v1/v2/v3 两实例同时启动、磁盘满、DDL 中断。旧 ID-only inbox 回放只能返回当前 attempt，不具备历史快照；是否需要显式版本标记和有界保留政策，而不是声称能补造过去证据？
10. **缩减重复入口与协议边界**：REST 和 `/a2a` 都调用相同 Store，但请求校验、错误映射与 cancel 幂等是否一致？生成相同命令的差分测试。哪些入口值得删除，哪些是运维必要？不要为了“统一”重新实现一套分布式消息总线。

验收要求：明确 PASS / FAIL / 未验证。出现有效新反例就补最小回归；没有反例不新增抽象。生产租户、系统隔离与真人通知必须分开验收。
