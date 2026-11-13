# Round 2 修补与对抗报告

日期：2026-11-13。范围：当前工作区；未部署服务、未调用真实模型或飞书租户、未修改外部代理或凭据。

## 结论与验收边界

7 项审计意见均接受其核心缺陷判断，并修补 Bug #1。保留 merge_intents 预写日志、approvals/inbox/outbox 分表、local runner 显式 opt-in。此处的完成指实现与测试覆盖，不等于生产、真人卡片点击或完整 A2A 协议认证。

**最终全量实测：120 passed、0 failed、0 errors、1 warning，耗时 180.61 秒；比原 65 用例新增 55 个。** 原始指定命令已经实跑，但预先存在的 `.pytest_tmp` 在当前受限令牌下不可访问；不能声称原始命令已经全绿。详情见验证部分。

## 逐项裁决

### Bug #1：CRLF 二次膨胀——接受并修补

- 所有 Python `Path.write_text` 仓库文本写入明确 `newline="\n"`，或使用 bytes；包括 fixture seed/source、Worker 用例和受信 Runner 驱动。
- 保留原始 rename 两端授权用例，增加真实含 CRLF 的 Git blob 读取、重写、重命名测试：blob 字节必须完全相同，Git 必须识别 `R100`。
- 不依赖宿主 Git `autocrlf` 的隐式行为。

### 1：SliceRegistry / 人机身份 / 离线岗位——接受并修补

- `slices.py` 加载完整字段，验证文件名、路径边界、重复 Slice、重叠所有权、重复契约 Provider。注册表只能来自受信目录或受保护 Git 基线。
- `workers` 保存机器 ID、用户 ID、endpoint、skills、expires。`POST /hub/workers/heartbeat` 的用户/机器绑定取自认证 Principal，body 不能改 owner；同机器不得重绑另一用户。
- 支持一人多 Slice、多台机器；仍按机器限制一个 working/submitted 执行槽。
- 缺少满足技能要求的在线 owner 实例时设 `WAITING_WORKER`，事务 Outbox 写 `worker.offline` 和 `mention_user_id`；上线自动唤醒。飞书消息生成真实 `<at user_id="...">`，可通过 Adapter 的 `mention_ids` 映射内部用户 ID 到飞书 ID。
- 过期检查在领取前、显式 refresh 和创建/唤醒时进行；常驻调度器仍需定期调用 refresh。`backup_user_id` 不等于默认接管授权。
- 兼容旧单用户嵌入式调用的 `Store(registry=None)` 被明确保留；生产 factory 有 worker 凭据时不允许没有注册表。没有宣称该旧模式解决多人授权。

### 2：依赖路由 + 契约增量演进——接受核心问题，收紧“兼容”的含义

- 严格 `dependency-request/v1`：消费者必须声明 consumes，Hub 从 provides 定位唯一 Provider；期望变更存入补丁工单及 Outbox，不仅留在内存。
- 按规范化 contract + expected_change + Epic 计算去重键。补丁创建/复用、DAG 环检测、依赖边及原 attempt 状态在同一个写事务内完成。并发相同请求只建一单，环路导致整个事务回滚。
- 补丁仅授权目标契约文件；合入还不唤醒，必须独立 QA ACCEPTED。全部上游满足后取消旧 input-required attempt，写入已验收 integration_sha 并唤醒；下游新 attempt 不能用早于该快照的基线提交。
- DEV/BUG 禁改 contracts，即使 allowed_paths 很宽也不例外。
- CONTRACT_PATCH 允许既有属性类型/约束不动、既有必填保留、新增可选属性或带 default 的新增必填属性。删除、改型、新增无默认必填等必须绑定 breaking_contract 签名审批。
- Breaking 审批精确绑定 item version、change_set_id、各授权契约 before/after SHA-256；换内容、换版本、过期或复用其他差异均不授权。审批不绕过路径保护、测试与语义审查；测试/审查结束后在 prepare_merge 写事务内再次检查版本与有效期，防止运行期间过期仍发布。
- **对任务书的反制**：JSON Schema 的 default 是注解，并不自动补值；“存在 default”不能证明任意旧客户端继续工作。这里实现的是受限演进政策，不是通用 Schema 蕴含证明。Provider 的补值逻辑必须由实现与独立 QA 验证。另外，旧响应对象 `additionalProperties: false` 的消费者可能拒绝新增字段，因此此情况仍硬拦截，不盲目叫作向后兼容。
- 创建/删除整个契约文件仍需受信基线晋升；不借一个 Breaking 卡片授权任意控制面改动。

### 3：GateRejected 永久卡死——接受并修补

- 候选路径/契约/测试/语义拒绝将当前批次所有已捕获版本 CAS 退回 REWORK，保存 blocked_reason，发 gate.rejected；与研发 rework_round 共用预算，第 3 次 BLOCKED。
- `InfrastructureError` 单独处理：Git 锁/进程不可用/超时、Runner 不可用/超时、未配置语义审查保留重试边界，不扣研发预算。
- 候选验证前的基础设施失败保留 GATE_PENDING；已进入 PREPARED 后的发布不确定性必须保留 GATING + intent 并走 recover，不能为了可领取而破坏双写恢复协议。
- 并发旧门禁结果不能回写更新版本。整批验证失败不会只退回第一个被检查的成员。

### 4：测试路径误杀与控制面漏保——接受并修补

- `tests/unit/<slice>/**` 在 Slice 授权内可写，并有真实 Git 合入测试。
- tests/acceptance、tests/qa_e2e、slices、migrations、.hub、.github、.agents 等受保护；任意层级 conftest.py / pytest.ini / pyproject.toml 等原保护名称继续受保护。
- 其他根级测试目录保持中央所有权，旧 `tests/test_acceptance.py` 不会因精确化而意外解锁。
- Worker 新增单测不自动成为权威门禁。Runner 仍从受信基线读取测试和 manifest；需要中央晋升后才作为 required 测试执行，不能用自行改写断言伪造验收。

### 5：Epic L0 / 发布收口 / 动态回归——接受并修补

- `epics` 保存 branch、skeleton_sha、state/version、release_sha。API 创建 Epic 验证骨架等于 integration head。
- L0 卡片签名包含 action、骨架与分支摘要、版本、有效期；认证 approver 或验签且映射成功的飞书操作人点击后，在事务内解锁子单。owner 离线的子单继续 WAITING_WORKER。
- Epic 分支审批前禁止借无 epic_id 的业务单绕过门禁；已发布就绪 Epic 不再接受新增业务单。
- QA ACCEPTED/CLOSED 晋升叶子 Slice 到持久回归集合。后续门禁自动加入这些 Slice 的 required 测试；旧 manifest accepted 列表仍兼容。
- 测试/审查期间回归集合变化会在 prepare_merge 时使门禁重试；PREPARED 期间暂停新的 QA 晋升，避免验证集与发布时序之间出现竞态。
- `check_release_ready` 要求：Epic ACTIVE、非空子单集合全部 ACCEPTED/CLOSED、没有 PREPARED intent、manifest 显式 `pending: []`、当前 head 是已 QA 的快照且包含所有子单已验收快照。成功记录 release_sha 并封口；不是执行部署。

### 6：QA 分类预算 / data Part / 飞书签名——接受并修补

- PASS / FAIL / ENV_ERROR / FLAKY 明确分支；FAIL 仍须结构化 BugReport 和精确快照。
- ENV_ERROR 首次故障后允许两次重试，第 3 次 BLOCKED_ENV，不扣开发返工次数。
- FLAKY 首报必须记录 observed=PASS|FAIL；只允许一次受控复跑，一致走正常 PASS/FAIL，不一致或再次 FLAKY 则 FLAKY_BLOCKED。熔断本身不扣研发次数。
- 环境/Flaky 人工签名恢复返回同一快照的 MERGED_PENDING_QA，不误派发开发，也不清空已有研发预算。
- A2A `kind=data` 支持 DependencyRequest 和 BugReport。前者只能由 owning Worker 操作，后者只能由独立 QA 提交；context、版本和 epoch 受约束。两个路径都有事务 inbox 幂等防重。
- 飞书直接 HTTP 路由验证原始 body 的 SHA-256（timestamp + nonce + encrypt_key + body）、时间窗、nonce/content；卡片还验证内部 HMAC、版本、action、到期时间与已注册 open-ID 对应的 approver 角色。
- 卡片审批变更、入站 receipt、Outbox 同事务。相同 nonce 不同 payload 不能在返回冲突前产生第二次审批副作用。
- **诚实边界**：当前接收签名明文 JSON；`encrypt` 密文 envelope 明确 422，尚无 AES 解密器。没有宣称真实租户/加密订阅已经部署闭环。旧 im_gateway 兼容路径仅允许读取/忽略事件，不提供卡片审批绕过。

### 7：宿主路径误用 safe_path——接受并修补

- config 的 database/repository/scratch 使用宿主 `Path.expanduser/resolve`；相对路径基于配置文件目录，不再借仓库相对路径校验器处理绝对路径。
- safe_path 仅用于仓库内 portable 路径与授权。
- load_app 使用实际临时绝对数据库、Git 仓库、scratch、注册表和环境凭据创建真实 FastAPI 应用，并实测 heartbeat。
- 本机实测 Windows 绝对路径；POSIX 分支没有在 Linux 宿主另跑，不冒充跨平台运行验收。

## 额外防御与兼容性

1. merge_intents 持久 `evidence_json`：测试节点结果、语义 verdict、回归 Slice 快照与候选 intent 一起预写，保留进程骤死后的审计依据。
2. v1 SQLite 升级原子重建带 CHECK 的业务表、保留旧数据/外键/journal，新增表和列；旧已验收叶子 Slice 回填回归集合。重启可重复执行，并发启动在写锁后再次检查。
3. QA 独立性使用认证用户 ID，不拿机器名称代替人的身份。
4. 收口检查真实 Git ancestry 与 head，不只看数据库业务状态。
5. 无新网络下载、无私有凭据写仓库、无远程服务变更、未启动其他 Agent。

## 实测与复现

### 原始指定命令

```powershell
$env:TEMP = "$PWD\.pytest_tmp"
$env:TMP = $env:TEMP
.\.venv\Scripts\pytest.exe --basetemp=.pytest_tmp -q
```

本轮开始实跑：14 passed、51 errors。主要错误为预存 `.pytest_tmp` 的 WinError 5；pytest 已经启动，不再是上一轮的进程创建失败。尝试仅在工作区内重命名保留旧 scratch 也被权限拒绝；没有修改 ACL 或绕开沙箱权限。

新目录进一步复现了 Python 3.13 mode=0700 创建目录后受限令牌不能访问的环境差异，故提供**仅供测试**的 `tests/run_windows_inherited_temp.py`：仅对新建且位于当前工作区的临时目录使用继承权限，不改变任何既有路径 ACL，不提升令牌、不跳过用例。

Git fixture 改为真实 pack-objects / index-pack / update-ref，避免为了本地装载对象而经 MSYS shell 创建 signal pipe。双进程领取用例改用两个独立 native Python subprocess 和文件屏障，仍实际争夺同一 SQLite 业务单；未用线程替代进程，也未 mock Git/SQLite。原先 `os._exit(73)` 发布中途骤死用例保留。

### 全量可运行验证命令

```powershell
$env:TEMP = "$PWD\.round2_tmp"
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe tests/run_windows_inherited_temp.py --basetemp=.round2_tmp/full-final-v2 -p no:cacheprovider -q --junitxml=.round2_tmp/full-final-v2.xml --tb=short
```

最终全量结果已写入 `docs/verification_round2.json`。早期针对性检查已得到 Round 2 新增用例 46 passed，随后扩充后的 Round 2 + Gatekeeper 组合 81 passed；这些中间结果不代替最终全量运行。

最终 120 用例版本按原始指定命令再次实跑：**34 passed、86 errors、3 warnings，5.73 秒**；错误仍为预存临时目录 WinError 5。因此“原命令全绿”这一验收项未满足。完整替代启动器运行没有 skip/xfail，120 个全部通过。

原始日志及 JUnit 留在被 Git 忽略的 `.round2_tmp/` 中，避免公开机器路径。飞书 HTTP、语义 Reviewer 是明确的测试替身；仓库、Git 合并、pytest 子进程、SQLite、并发与骤死恢复不是 mock。

## Round 3：给 Antigravity 的对抗挑战

请用可执行反例而非仅代码阅读作答；每项提交攻击输入、时序、预期/实际状态、Git SHA、Outbox/intent 证据。不要通过关闭安全检查、把环境异常算通过或跳过失败用例获得全绿。

1. **人机身份攻击**：同一用户两机器、不同用户伪报 owner、过期心跳、技能不足、机器 ID 重绑定；证明不能越权领取，且离线 @ 指向正确 IM 用户。
2. **依赖并发与环路**：两个不同下游同时请求同一契约变更，随后构造 A→B→A；证明只建一个补丁，环路事务全部回滚，旧 epoch 不能在唤醒后提交，旧 integration base 被拒绝。
3. **契约语义反例**：用 closed object、nested ref、oneOf、输入/输出方向差异以及“default 不会自动补值”攻击增量政策。请区分政策放行和真实客户端兼容；提出可执行 provider/consumer 测试，而非再次把 normalize 相等当作完整兼容实现。
4. **审批重放与 TOCTOU**：同卡双击、相同 nonce 不同 body、有效平台签名但未注册点击人、diff hash 修改、工作版本变化、审批在长测试期间到期。验证不能挪用授权；若发现窗口，应补绑定/重检，而非扩大通配权限。
5. **回归晋升竞态**：支付单测试运行时订单单 QA 接受；以及 Git CAS 成功后进程退出。证明回归集合变化会重试，恢复后 evidence_json 不丢，绝不把未记录候选当作正常验收。
6. **Epic 收口伪造**：pending 缺失/非空、子单未接受、外部非 QA head、无 Epic 单借用 L0 分支、封口后追加单；任何一个不满足都不能 READY_FOR_RELEASE。
7. **QA 故障注入**：ENV_ERROR 连续三次、FLAKY 后 PASS/FAIL/FLAKY、陈旧 QA 版本、人机 owner 换名；证明环境/抖动预算不侵占研发轮次，签名恢复不重置研发历史。
8. **真实接入与环境验收**：在原生 Windows 以指定 pytest 命令重跑并回传结果；在 Linux 跑绝对路径与迁移。再使用飞书测试租户验证真实头、真实卡片回调和加密 envelope；目前 AES envelope 未实现，不能把签名明文单测冒充加密订阅上线。

## 未宣称完成

- 原始 `.pytest_tmp` 权限问题未修复；测试启动器成功也不等于该命令已全绿。
- 没有 live 飞书租户/真人点击、加密 envelope、生产隔离 Runner、后台调度器/死信运维、完整 A2A conformance 或 Linux 宿主验收。
- 注册表是启动时可信快照，不是自动热更新/全局配置分发系统。注册表热更新和用户撤权必须另行设计活动租约 fencing。
- `READY_FOR_RELEASE` 是受约束状态，不是上线操作或用户体验验收。

