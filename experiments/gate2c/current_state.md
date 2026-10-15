# CertiRank 当前状态（2026-10-15，离线 P1/P2）

以用户版两周计划 P0–P7 为阶段编号。原 P0 记录位于 Git `ddb3ecf`；
本次修正了契约漏检、响应独立性、新分支计费与成本上限措辞。
源数据、外部报告与数据清单未修改。

## 已完成

| 阶段 | 当前状态 | 证据 |
|---|---|---|
| P0 状态恢复与离线协议 | 已完成；新付费运行 preflight 未完成 | 31 文件哈希、420 条记录、22 项审计；protocol v0.2 |
| P1 小例子与正确性边界 | 已完成实现与有限验证 | Top-1/Top-3、64 表穷举、随机多更新、预算与中止测试 |
| P2 历史配置现象与可观测性 | 已完成离线分析 | `certirank_pilot/analysis/phenomenon_report.md`、逐请求反事实与子集描述 |
| P3 开发策略 | 工程接口与单组 smoke 已完成；真实开发评价未完成 | Random、SimpleTopology、TraceSensitivity 候选、Vote3；D_EM=null |
| P4–P7 | 未开始正式验证 | 未冻结 Proposed/holdout，无真实调用、费用与端到端验证 |

## 核心结果

历史 source revision：LOTUS `136ae4f4a344a2f75d89f811e516dfcb0de30e46`，
run `live_pilot_20261014T132233Z`。更新密钥后的 420 次 pilot 成功，
早前鉴权失败作为独立 smoke 快照保留。当前无鉴权失败的证据推断。

- naive 重放 15/15，旧报告 `46` 笔误的可复现有序影响值为 37。
- 按无序集合重新统计，r0 高影响为 28，翻转交集 12，未观察到翻转的高影响 16。
- Tournament 集合 D_EM：636=0.60、327=0.90、51=0.00；宏平均 0.50。
- Tournament 每执行平均唯一请求约 10–11，Recall@3 均为 1.0。只有三个查询和单相关文档标签，质量证据有限。
- 159 个 run-request 单请求翻转反事实中，134 个 unresolved（84.3%）。
  它们需要当前轨迹外的信息，不能在在线分析中填零。
- 原始 slot 的时间独立性未经证明；每查询 10 个执行对相互依赖。
- 48 行单组工程 smoke 的增量与合法缓存重放输出、状态和调用账目一致。
  一个三响应组无法估计完整复核算法的独立执行 D_EM，结果保持 null。

## 契约修正与证据边界

候选、位置、condition、模型及参数均逐字段核对；原文重新解析后与两个判定字段核对。
重复响应拒绝装配，不再静默覆盖；entry 按 query_id 关联。
`configs/gate2_identity.json` 钉住历史预期元数据。
历史导出没有完整请求正文，因此无法在本仓库独立重建请求 hash；端点继承 manifest 证据。

响应池中的 10 个 3/5 组合相互重叠，只用于组合敏感性描述。
正式复核评价要求四个不重叠三槽位组；同请求 hash 不构成概率独立证明。
新分支按当前执行未观察定义，读取全局表也扣预算。

## 成本与阻塞

`budget_estimate.md` 是历史价格与 token 分布的情景外推，live_hard_cap=null。
核心 6,048 次采集在原缓存情景约 $0.14–0.38；去缓存高档情景约 $1.66。
新查询长度、输出、重试、价格和 live smoke 会改变金额；这些估算均未授权新运行。

继续正式开发/留出评价前缺少：

1. 新的独立 query-group 与标签、完整 prompt/request provenance 和实际候选。
2. 每对 12 次响应的非重叠槽位、采集随机/交错顺序与时间漂移检查。
3. 策略与最强 comparator 冻结、开发/留出隔离；三个历史查询均标记为已暴露开发数据。
4. 真实 API 适配器、当期价格、输入/输出/重试限额、累计金额与截止时间上限，以及明确付费授权。

H0 有探索性支持；H1/H2/H3 尚未检验真实收益。有限正确性验证不等于一般证明。

## 复现与交接

`uv run --no-project --with pytest python scripts/check_offline.py`。
核心运行与分析仅用标准库；pytest 只用于测试。
正确性覆盖见 `experiments/certirank_pilot/offline_correctness.md`，
策略接口见 `experiments/certirank_pilot/policy_spec.md`。

## 框架与算法复审（2026-10-15）

协议更新为 v0.2.1。已修复预算读取前校验、旧 ledger 复用、Vote3 槽位重复使用、
基础执行失败行丢失、跨查询/响应源汇总、非法输出与指标输入、契约 provenance 漏检、
旧轨迹与新判断混用、审计未在输入 Gate 失败时停止等问题。
完整反例、修复范围与剩余研究不确定性见 `experiments/certirank_pilot/framework_review.md`。
新增 N=4 全 K、非二次幂和 N=64 多更新、反事实所有补全对照；
正确性检查仍是有限验证，没有升级成一般证明。历史数据与原研究结论未改变。

## 研究目标对齐修正（v0.3）

已补齐统一配置、实际投票/修复漏斗、绝对预算开发网格、预算忠实的枚举理想参考、
开发统计价值估计器、分池加载/代码模型冻结/组级 Bootstrap 与完整 CPU 测量。
协议见 `docs/experiment_protocol_v0.3.md`。测试目前 143 项。

当前历史池仍不够拟合独立组价值，未打开真实留出集。合成机制和 CPU 报告分别位于
`certirank_pilot/research_diagnostics/` 与 `certirank_pilot/cpu_benchmark/`。
CPU 微基准未显示原定 25% 完整收益，暂缓扩展复杂修复器；策略主比较共用合法缓存重放。
真实质量饱和、独立新数据、当期服务计价、真实调用行为仍为待验证项。
