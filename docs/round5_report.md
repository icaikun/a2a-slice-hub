# Round 5：对抗反例、深度修复与收敛审计

日期：2026-11-13。版本：v0.1.0。范围：当前仓库的代码、协议、状态机、测试和公开文档。
没有部署生产服务、调用真实 IM 租户、修改代理、安装依赖或提交 Git commit。

## 1. 结论与证据入口

**最终全量：447 passed in 368.70s；0 failures、0 errors、0 skips、0 warnings。**

- `tests/test_round5.py`：本轮 175 个回归实例；包含用户五项反例、完整角色差分、额外审计反例、schema 正反样本、队列 aging 与 DLQ 预算。
- `tests/test_publication_hygiene.py`：20 个实例；扫描现行源码/测试/文档/示例/锁文件，并以动态构造的假秘密和私有路径检验规则确实生效。
- `.codex_round5_tmp/full-results.xml`、`.codex_round5_tmp/full.log`：最终全量 JUnit 与控制台证据，保留在本地忽略目录。
- `docs/round5_test_summary.json`：测试计数、命令、文件 SHA-256、历史报告未变更校验和验收边界。
- `README.md`、`SECURITY.md`、`docs/state_transitions.md`、`docs/architecture_blueprint.md`：当前规范。

用户提供的 `redteam_round5_exploits.py` 原文件不在本工作区；本轮按给定 JSON 和时序重建反例，**没有声称执行过该原文件**。仓库源码原本就是未跟踪状态，本轮不 git add/commit，不覆盖 Round 1–4 报告及历史测试摘要。

## 2. 要求 2.1–2.7 的实现与证据

| 要求 | 修复与直接验证 | 结果 |
|---|---|---|
| 5-1 独立 gatekeeper A2A | message/send 外层加入 gatekeeper；文本 claim 只允许 worker/coordinator；7 类 send 命令 × 6 角色、get/cancel × 6 角色 × 同名/不同名；真实 ASGI，允许的 gate-merge 经真实 Git/测试 runner | PASS |
| 5-2 签名恢复 | 新 generation 清零当前仲裁/协商/重做计数，历史保留；父单 REWORK，叶子按 L0→依赖→在线 worker→REWORK 计算；环境/FLAKY 恢复仍独立恢复原 QA 快照 | PASS |
| 恢复后依赖唤醒 | 独立叶子和聚合子单覆盖四种 dispatchability；CONTRACT_PATCH 通过 QA 后得到 SHA、READY、dependency.ready；另有真实两人争议→签名恢复→补丁 QA→合并的完整时序 | PASS |
| 5-3 添加依赖 | add_dependency 重算 readiness/pin，WAITING 可继续加第二条边，L0/WAITING 状态不变也递增版本；已 ACCEPTED/CLOSED 的依赖立即参与快照约束 | PASS |
| 5-4 多依赖 | 用 COMMITTED merge_intents.rowid 选最新快照，候选包含所有依赖及已有 pin，不再按最后 QA 到达覆盖；_ready 也统一计算，避免 L0 期间接受依赖后丢 pin | PASS |
| 多分支 ancestry | Gatekeeper 对每个依赖验证 accepted/closed 和 origin、目标 branch 双 ancestry；测试较老 origin、真实分叉 branch、未接受依赖、旧 scalar 元数据，以及合法最新基线实际合并 | PASS |
| 5-5 schema | 新增字段及本地引用实行 Positive Habitable Additive Profile；拒绝所有列举复杂关键词、布尔 schema、循环引用、空 enum、类型矛盾；使用构造见证检测 type/enum/const/ref 的组合；jsonschema 独立验证正样本 | PASS |
| 方向性 | 明确是 bounded producer-additive policy，不是双向 JSON Schema entailment；测试证明旧未知字段 ext=1 仍可能被新增 string 定义拒绝，禁止把此策略包装成通用兼容证明 | PASS |
| Outbox aging | urgent=10；普通 available 等待 >=300 秒提升到有效优先级 10，再按 available/id 排序；299/300/301 边界、10,000 新毒丸被 fresh urgent 越过 | PASS |
| DLQ budget | 默认 3 次；Store 可显式指定严格整数 1..10；HTTP 固定默认；重放保持 ID，reset/count/actor audit 原子提交；耗尽不再产生 outbox 事件 | PASS |
| 开源卫生/文档 | 加入 GitHub PAT/OAuth/App/refresh、Slack、Bearer、Windows/macOS/Linux home 规则及正控制；现行文档去除陈旧实现声明，蓝图改 SQLite WAL v4，补实际表和部署边界 | PASS |

没有引入新数据库、新服务、通用命令框架、外部下载或新增运行依赖。修改集中于 api、approvals、workflow、store、gatekeeper、review 六个现有模块。

### Schema 的保守性边界

只接受正向结构、有限 enum/const、无环本地引用及注释；enum 元素限 JSON 基元/对象。每个新增 schema（包括可选嵌套属性、items、schema 型 additionalProperties 和引用定义）均需构造出有效值。缓存重复子问题并限制剖面深度，无法证明的合法组合也会拒绝；这是一项保守策略，不是完备可满足性求解器。format/default 只作为元数据，不验证格式或填充运行时缺省值。已有定义不允许借新增字段顺便改写；复杂变化进入精确 diff/version/change-set 的 breaking_contract 审批。

## 3. 扩展审计：本轮另外证实并修复的缺陷

以下不是靠全套绿灯推测出来的结论，均有明确的旧实现失败证据和新增回归。

### 3.1 同名非 Worker 越权读取任务（协议权限）

复现：创建 worker-a 的 attempt；配置名字也叫 worker-a、角色却为 approver 或 im_gateway 的 principal；调用 tasks/get。旧 authorize_attempt 仅比较名字，错误返回任务。私有 `audit-before.log` 记录两个失败用例。

修复：非 coordinator/gatekeeper/qa 的读取者必须同时是 worker 角色且匹配 owning worker。共 24 项任务方法差分验证读取与取消权限，不借修复放宽写权限。

### 3.2 Python 布尔/数值相等导致契约变更漏检

复现：旧字段的 const=true 改 const=1，或 enum=[true] 改 enum=[1]，default 同理；Python 字典相等误认为未变化，schema_compatible 返回 True。`audit-before.log` 另有三个失败实例。

修复：规范化后使用类型敏感的 JSON 序列化比较；habitable 见证匹配也区分布尔和数值。未新增的既有字段不能绕过 breaking 审批。

### 3.3 聚合父单依赖绕过及原子组死锁

真实 Git 复现：建 BUG 父单与两个不同 owner/slice 子单；将未完成依赖挂父单；两人争议冻结；签名 resume；提交两个子单。修复前实际观察到：父依赖仍 READY，父单已 MERGED_PENDING_QA，merge_succeeded=true。原因是恢复父单 REWORK 后，子单 dispatch/merge 只看自己的依赖。

修复：父依赖由叶子继承，在 _ready、claim、失败回退、依赖 QA 唤醒、快照选择及 Gatekeeper 全部生效。修改父依赖前检查每个子单仍未派发，并提升子单版本；遗留错误状态不能绕开门禁。

对应 DAG 按 change_set_id 折叠为原子验收单元，拒绝父→子、子→父、同组兄弟单向边及跨组传递环。否则即使显式节点图没有环，也会出现必须整组合并才能满足自身依赖的死锁。38 项依赖相关定向回归通过，最终仍以全套为准。

### 3.4 聚合父子 Epic/分支绑定缺口

复现：ACTIVE Epic 的 BUG 父单可以接受 epic_id=None 或另一个 Epic 的子单；旧 prepare_merge 只检查叶子的 Epic，父单可被不相关 branch 的 intent 冻结。`epic-before.log` 中三个负向测试全部旧实现失败。

修复：创建子单要求与父单 Epic 相同；prepare_merge 对遗留不一致数据再次校验，并确认父 Epic 为 ACTIVE 且 branch 匹配。修复后同三例通过。

## 4. 测试执行与可追溯性

最终命令（原要求入口，附加 JUnit 输出）：

```powershell
.\.venv\Scripts\python.exe tests/run_windows_inherited_temp.py -W error -p no:cacheprovider --basetemp=.codex_round5_tmp/full -q --junitxml=.codex_round5_tmp/full-results.xml
```

- Windows 本机、现有 `.venv`；专用测试入口只调整工作区内新建临时目录的 mkdir 模式以保留继承 ACL，不修改已有目录权限，也不跳测。
- 五项首轮新增测试先得到 126 通过、4 个聚合夹具创建失败；修正父子 change_set_id 后，还修正了 L0 聚合夹具的组装顺序。这些是夹具失败，未当成产品验证通过。
- 扩展审计旧版负控制分别为 5 个协议/schema 失败与 3 个 Epic 失败；修复后对相应测试复跑为绿。
- 中间全套曾得到 437 passed in 345.64s，但聚合继承/Epic 审计修复在后，所以该结果**不是最终验收**；保存在 full-initial.log / full-initial-results.xml 与 full-initial 临时目录。
- 最终计数 447 passed in 368.70s；0 failures、0 errors、0 skips、0 warnings。。源码和测试哈希与最终运行一致；文档/报告补齐后另跑发布卫生检查。
- Round 4 三个 urgent backlog 用例保留，但将毒丸时间改为 fresh：旧测试要求 urgent 越过 available=0 的老事件，与 Round 5 明确要求的 aging 冲突；新的 300 秒边界及 10,000 fresh backlog 用例同时锁定新语义。
- Git 对象、CAS、SQLite 事务与 ASGI 路由是真实执行；semantic reviewer 和 IM HTTP 外部响应仍用明确 test double。没有把本地异常注入说成断电验收。

## 5. 外部部署前提（不混入仓库内修复结论）

以下仍须运营者独立提供和验收，仓库没有宣称自动完成：

1. **OS/container runner 隔离**：不可信代码禁止默认本地执行；独立身份、无控制面挂载/凭据、默认断网、资源限制、进程树终止与证据回传。
2. **真实飞书/钉钉租户**：应用与账号映射、凭据轮换、真实送达/供应商去重；DingTalk timestamp HMAC 不是正文认证，可信网关第一到达替换的边界不变。
3. **TLS 反向代理和 Git ref ACL**：单授权发布者、禁止外部 force-push、保留 in-flight 对象；不能防恶意管理员在检查后竞争修改 ref。
4. **单机运维合同**：SQLite 必须本地盘；持续调度/心跳/恢复、磁盘容量、备份恢复、审计导出由运营者负责。inbox/intents/disputes 不自动过期；outbox 仅有有界 delivered prune。不是 HA、无限存储或通知时延 SLA。

这些是显式支持范围与外部前提，不能由单元/集成测试替代。PostgreSQL 与远程容器 runner 是未来扩容选择，不是已实现后端。

## 6. 最终收敛判定

**本轮审计覆盖的仓库内设计与实现不变量已收敛；截至本报告，未发现任何已确认但尚未修复的 P0/P1/P2 代码、协议或状态机缺陷，也没有必须移交 Round 6 的已知未修复复现。**

这是上述支持范围、可执行反例与最终全量测试所支持的工程结论，不是对未知输入/所有并发交错的形式化无缺陷证明。OS/container 沙箱、真实 IM 租户凭据、TLS 反向代理和 Git 服务 ref ACL 仍为独立外部部署前提；**仓库内收敛不等于生产上线、租户接入或产品 UX 验收。**
