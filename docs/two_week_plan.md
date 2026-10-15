# CertiRank 两周计划（2026-10-15 启动）

> **已让位**：日程与阶段编号以用户版 v0.1 计划为权威——
> [`CertiRank_两周执行计划_2026-10-15至2026-10-28.md`](CertiRank_两周执行计划_2026-10-15至2026-10-28.md)
> （P0–P7）。本文件是 P0 期间的工作草稿，编号体系不同（P0–P3），
> 映射关系见 [`experiments/gate2c/current_state.md`](../experiments/gate2c/current_state.md)。

> 状态标记：✅ 完成 · 🔨 进行中 · ⬜ 未开始 · ⏸ 冻结/缓办

## P0 — 整理仓库与恢复实验依据（10/15）✅

从原 LOTUS 项目取得 Gate 2 的原始响应、实验配置、分析报告与相关脚本；确认
420 次调用的原始记录完整；建立独立实验目录并记录数据来源、版本与校验信息。
**零新增 LLM 调用。**

- [x] 找回 `gate2_live_report.md`、`gate2_live_analysis.json`、
      `gate2_live_replay_analysis.json` 及其引用的原始 calls/runs JSONL、
      冻结 workload、生产方脚本（只读副本置于 `data/external/lotus_gate2/`）；
- [x] 建立数据契约（八类字段 + C1–C8 校验规则，见 `configs/protocol.yaml`）；
- [x] 实现最小审计程序 `scripts/audit_gate2.py`：一条命令从原始数据独立复算
      Gate 2 全部核心统计（22/22 通过），产物 `experiments/gate2c/results/`；
- [x] 数据清单 `data/manifests/gate2_external_data.json`（SHA-256 + 来源 + 上游 lineage）。

**P0 的一个正式发现（PROSE-46）**：Gate 2 报告正文的 "46 single-flip
high-impact pairs" 与其自身表格及存储分析矛盾；可复现值为 **37**
（= 14 交集 + 23 稳定但高影响）。详见 `results/gate2c_audit.md` §4.1。

## P1 — 复现 Gate 2 的关键统计（10/16）🔨

核验 Pair 翻转、Top-3 集合与顺序差异、影响分类及原始 Replay 结果；确认
29、37（原报告误作 46）、14、23 等数字的统计单位；确认实际可复用的
Pair Response Pool。

- [x] 统计单位核对（pair / run-pair / run 三级口径，见审计报告 §4）；
- [x] Pair Response Pool 可用性：84 slot × 5 响应、槽位内 request_hash 一致、
      支持任取 3/5 组合构造独立三次投票 run；
- [ ] 补充：把审计脚本的部分断言迁移为每周例行回归；对 5 响应子集做
      初步的 run 组合间方差描述（不改冻结协议）。

**交付**：可运行的审计脚本（已交付）、`gate2c_audit.md`（已交付）、补充分析。

## P2 — 构建最小 Tournament 执行器（10/17–18）⬜

固定执行规则、比较事件追踪、Top-1/Top-3 结果生成。

- [ ] 执行器核心：从 Pair Response Pool 取响应、按固定规则执行、逐比较记录轨迹；
- [ ] 合成比较表单元测试先行（正确性：已知答案的投票矩阵、平票、取消/联合涌现）；
- [ ] 用 Gate 2 兼容响应离线执行；分析单边翻转、新分支比较与结果敏感度；
- [ ] 信息边界落地：执行器只读执行路径上的槽位；表外比较 = 新分支请求计数。

**交付**：执行器、单元测试、控制流敏感度报告。

## P3 — 检验在线复核的价值并冻结策略（10/19–21）⬜

- [ ] Random、SimpleTopology、初步结构感知三档策略；
- [ ] 置信度信号仅在原始数据支持时使用；
- [ ] 在线可用信息 vs 事后 OracleImpact 的差距量化；
- [ ] 若出现可信收益信号 → 冻结协议，准备留出查询验证；否则记录负结果。

**交付**：`policy_spec.md`、`week1_decision.md`。

## 暂缓项 ⏸

- 完整 Incremental Repair：等 P3 结果再决定（`src/certirank/repair.py` 暂不建）；
- `oracle.py`：真实调用通道，待任何需要新调用的阶段再实现；
- 跨查询泛化、其他算子（join/filter/agg）、其他模型：超出 pilot 范围。

## 研究边界（冻结）

旧 Gate 2 对全部 28 个 pair 都调用了；CertiRank 的 Tournament 只观察实际执行
路径上的部分 pair。三层信息边界（完整响应表 / 在线执行轨迹 / 新分支请求）
自 P0 起冻结，规范见 `configs/protocol.yaml`。
