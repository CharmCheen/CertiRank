# CertiRank

CertiRank 是一个独立的小型研究原型：在 LLM 成对比较构成的对排序
（Tournament）中，用**在线可获得的执行轨迹信息**指导选择性复核，并用
可复现的实验方法证明（或否定）其价值。

研究背景与边界见用户版 v0.1 规划（权威）：
[研究总纲](docs/CertiRank_研究总纲_v0.1.md) ·
[两周执行计划（P0–P7）](docs/CertiRank_两周执行计划_2026-10-15至2026-10-28.md)；
当前状态见 [experiments/gate2c/current_state.md](experiments/gate2c/current_state.md)。
（`docs/research_overview.md` 与 `docs/two_week_plan.md` 为 P0 期间工作草稿，已标注让位。）

## 仓库原则

- **干净的研究仓库**：LOTUS 实验的代码与数据只以不可变外部数据形式接入
  （`data/external/`，manifest 记录 SHA-256 与来源），不搬运其代码。
- **可复现优先**：核心统计一条命令可复现；artifact 一律由脚本生成。
- **信息边界**（冻结）：完整响应表 → 仅离线评价/反事实上界；在线执行轨迹 →
  复核策略唯一输入；新分支请求 → 必须真实调用并计入预算。
  规范见 [configs/protocol.yaml](configs/protocol.yaml)。
- **密钥纪律**：任何密钥、.env、非公开查询内容不得入库；`provider_runtime.yaml`
  仅含环境变量名。

## 快速开始

依赖：Python ≥ 3.10，仅标准库；测试需要 `pytest`。

```bash
# 1. 验证外部数据未被改动（SHA-256 对照 manifest）
python scripts/make_data_manifest.py --check

# 2. Gate 2 审计：从原始响应独立复算全部核心统计（零 LLM 调用）
python scripts/audit_gate2.py            # 产出 experiments/gate2c/results/
python scripts/audit_gate2.py --check    # CI 模式，只返回码

# 3. 预算推算：按两周计划 §3.2 数据块计算金额上限（零 LLM 调用）
python scripts/pilot_budget.py          # 产出 experiments/certirank_pilot/

# 4. 单元测试（审计引擎的合成用例正确性）
python -m pytest tests -q
```

## 结构

```
CertiRank/
├── README.md
├── docs/
│   ├── CertiRank_研究总纲_v0.1.md          # 研究问题、假设、证据等级（权威）
│   ├── CertiRank_两周执行计划_...md         # P0–P7 日程与冻结阈值（权威）
│   ├── research_overview.md                # P0 工作草稿（已让位）
│   └── two_week_plan.md                    # P0 工作草稿（已让位）
├── configs/
│   └── protocol.yaml               # P0 冻结：数据契约、重放语义、审计期望
├── data/
│   ├── manifests/                  # gate2_external_data.json（SHA-256 + 来源）
│   └── external/lotus_gate2/       # 不可变副本：原始响应/报告/workload/脚本参照
├── src/certirank/
│   ├── operators/                  # naive sort 重放、影响分类、排序指标（纯函数）
│   └── pair_pool.py                # Pair Response Pool：契约校验、装配
├── scripts/
│   ├── make_data_manifest.py       # 生成/校验数据清单
│   ├── audit_gate2.py              # Gate 2 审计入口（一条命令复现核心统计）
│   └── pilot_budget.py             # 两周计划数据块的金额上限推算
├── tests/                          # 合成用例：重放语义、指标、契约
└── experiments/
    ├── gate2c/
    │   ├── current_state.md        # P0 状态（含总纲悬置问题核对、阶段映射）
    │   └── results/                # gate2c_audit.md / gate2c_audit.json（脚本生成）
    └── certirank_pilot/
        └── budget_estimate.{md,json}   # 预算推算（脚本生成）
```

尚未创建的模块（按阶段实现）：`src/certirank/oracle.py`（真实调用通道）、
`trace.py`（比较事件轨迹，P2）、`policy.py`（复核策略，P3）、`repair.py`
（增量修复，待 P3 决策）。

## 外部数据一句话说明

`data/external/lotus_gate2/` 保存 LOTUS Gate 2 实验（run
`live_pilot_20261014T132233Z`）的 420 条 pair 响应、15 条 run 记录、冻结
workload 与生产方报告/分析，全部为经哈希校验的不可变副本。审计证明：
3 query × 28 pair × 5 响应完整、重放 15/15 一致、报告数字可复现
（其中正文 "46" 为笔误，可复现值 37）。
