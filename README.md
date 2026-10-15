# CertiRank

研究固定语义 Top-K 算子中的运行间分歧：用执行时可见的结构选择复核，
并检验增量恢复相对合法缓存重放的价值。项目当前已完成离线执行基础，研究收益待验证。

研究目标与决策阈值以用户版文档为准：
[研究总纲 v0.1](docs/CertiRank_研究总纲_v0.1.md)、
[两周执行计划](docs/CertiRank_两周执行计划_2026-10-15至2026-10-28.md)。
最新状态见 [current_state](experiments/gate2c/current_state.md)，
执行语义见 [protocol](configs/protocol.yaml) 和 [机器配置](configs/offline_protocol.json)。

## 当前交付

- 历史 Gate 2：31 个文件不可变校验，420 条响应契约验证，naive 15/15 重放，22 项历史复算。
- 固定 Tournament Top-1/Top-3、执行轨迹、独立响应槽位、预算账目、多数票与增量修复。
- Base、Random、SimpleTopology、TraceSensitivity 开发候选、Vote3；Confidence unavailable。
- 四元素穷举与随机对照、修复中止边界、合法缓存重放对照。
- [真实历史响应的现象报告](experiments/certirank_pilot/analysis/phenomenon_report.md)、
  单组策略工程 smoke 与重叠子集敏感性，全部零模型调用。
- [成本情景估算](experiments/certirank_pilot/budget_estimate.md)；新付费运行的硬上限尚待确定。

## 复现

Python ≥3.10；运行核心仅需标准库，测试依赖见 `requirements-dev.txt`。
使用已安装 pytest 的环境：

```bash
python scripts/check_offline.py
```

也可以在 uv 的隔离依赖环境中运行：

```bash
uv run --no-project --with pytest python scripts/check_offline.py
```

产物生成命令（仅更新 `experiments/` 内的本项目分析文件）：

```bash
python scripts/audit_gate2.py
python scripts/pilot_budget.py
python scripts/analyze_offline.py
```

三个脚本均支持 `--check`；其中现象与预算脚本会精确比对已保存产物。
外部数据清单只用 `python scripts/make_data_manifest.py --check` 验证，不重新钉住漂移输入。

## 信息与证据边界

完整响应表只给评价器和事后反事实分析。在线策略只接收当前执行已观察信息；
当前执行首次访问新比较时，即使全局表中已有，也要扣逻辑调用预算。
跨执行禁用缓存；复核组三响应不重叠。历史每对 5 次响应只够一个完整三响应组，
无法支撑独立复核执行的一致性结论。

当前三个查询均已用于开发诊断。H0 只有历史小样本探索证据；H1/H2/H3 收益尚未验证。
无严格稳定性认证、无正式留出结果。`oracle.py` 目前只提供离线响应源和账目，
真实 API 适配器、当期计价验证与付费授权均未接入。

## 主要路径

- `src/certirank/operators/`：naive、Tournament、指标。
- `src/certirank/{trace,oracle,repair,policy,evaluation,diagnostics}.py`：执行与离线评价。
- `tests/`：契约、执行正确性、预算和历史数据回归。
- `data/external/lotus_gate2/`：不可变原始响应、生产方分析与只读脚本参照；本项目不执行参照脚本。
- `experiments/certirank_pilot/`：现象分析、策略说明、正确性覆盖和成本情景。

密钥与 `.env` 不入库。当前全部命令不访问 provider。

## 研究实验协议 v0.3

实际实验设置集中在 [experiment.json](configs/experiment.json)，
执行规则和证据边界见 [研究实验协议](docs/experiment_protocol_v0.3.md)。
历史分析新增真实复核漏斗、预算可行域、预算忠实的理想参考和均衡槽位敏感性。
ReviewValue 提供开发集统计拟合接口；当前历史数据不够，未报告真实策略收益。

```bash
python scripts/research_diagnostics.py  # 合成机制、独立响应组与价值代理检查
python scripts/benchmark_cpu.py         # 实测完整 CPU，不按节点数推断收益
```

[合成诊断](experiments/certirank_pilot/research_diagnostics/report.md)与
[CPU 微基准](experiments/certirank_pilot/cpu_benchmark/report.md)独立标记，不能替代真实 LLM 证据。
新数据用 `scripts/run_experiment.py` 统一开发/冻结/留出流程；
[清单模板](configs/pool_manifest.template.json)需要填入真实文件、哈希和独立标签后才能运行。
