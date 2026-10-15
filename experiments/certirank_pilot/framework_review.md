# 框架与算法复审及修复记录

日期：2026-10-15。范围：当前未提交的离线框架；基底提交 `ddb3ecf`。
保留进入本轮前的工作，未修改外部原始数据或 manifest；未提交、推送或调用模型。

结论：发现并修复执行状态、计费、评价与数据契约的边界缺陷。
扩大验证后，Tournament 核心在已测试范围内与独立重算参考一致；没有一般性正确性证明。
当前 129 项测试、22 项历史审计和 7 份可精确生成的分析产物全部通过。

## 缺陷、反例与处理

| ID / 优先级 | 修复前可复现问题 | 修复 | 回归证据 |
|---|---|---|---|
| R1 / P1 | 无效预算在基准响应已读取后才报错；`review_budget(5, 0.6)` 因浮点表示得到 2 | 首次读取前检查配置；预算只接受非负整数调用基数与精确字符串；`"0.6"` 得到 3 | `test_invalid_budget_is_rejected_before_reading_any_response`、`test_budget_rejects_noncanonical_inputs` |
| R2 / P1 | 新 session 接受已有缓存的 ledger，基准执行可免费复用旧结果 | session 要求全新 ledger 和三个预留槽位；不足槽位在读取前拒绝 | `test_review_session_requires_pristine_ledger_and_three_slots` |
| R3 / P1 | Vote3 后仍可启动普通复核，槽位序列 0/1/2/1/2；`False/True/False` 的多数票可被错误改成 True | ledger 全执行固定 single/Vote3 模式；失败 ledger 终止；槽位和 pair 严格校验 | `test_vote3_ledger_cannot_reuse_review_slots_or_mix_modes` 等 |
| R4 / P1 | Base/Vote3 或策略的初始执行缺响应时直接抛出异常，已花费调用不进入结果行；中止输出缺 pending pair | 缺初始输出记 failed、topk=null，保留全部账目和 failure pair；修复中止显式导出 pending pair 与最后完整输出 | 五方法失败用例、partial Vote3、`test_aborted_review_exports_pending_pair_and_last_complete_output` |
| R5 / P1 | 不同 query/响应源可合并为一次分歧估计；空/重复/越界输出也可产生“稳定”统计 | 绑定 source/query/N/K/算子及方法配置；拒绝不合法输出；基础失败阻止整组分歧估计，aborted 有效回退仍纳入分母 | cross-query/source、malformed-output、failed/aborted denominator 用例 |
| R6 / P2 | 字符串 `"False"` 被按 True 计算，重复 pair/repetition 被覆盖；第二个 ranking 有重复元素仍通过 Kendall；Top-K 与完整排序不匹配未拒绝 | 严格布尔判断、拒绝重复记录、校验双侧排序和 Top-K 长度/前缀；naive 反事实校验 K | operator/naive/Kendall/query-metric 负例 |
| R7 / P2 | 重复 query ID 在 workload 中被折叠，contract_ok 仍可为 True；空模型/无时区时间、嵌套 True 与 1 的 drift 漏检；宽松 pool 将字符串转布尔 | 唯一查询与候选、总数约束、非空 provenance 与 UTC 时间、类型敏感的 pinned identity；pool 永不转换非布尔值；JSONL 必须为对象 | `test_duplicate_workload_queries_cannot_pass_contract`、provenance/pinned-identity/pool/JSONL 用例 |
| R8 / P2 | 旧完整输出搭配新判断或非布尔 observed 映射，可生成错误 conditional safe/impact | 分析前核对映射的类型、候选范围和与原轨迹的每个判断一致性 | `test_conditional_analysis_rejects_stale_decisions_and_nonboolean_values` |
| R9 / P2 | manifest 失败后审计仍继续消费数据；契约异常中断审计；预算脚本可直接使用未经哈希验证的改动副本；run roster 可被重复覆盖 | manifest/契约 Gate 失败立即返回非零且不写成功产物；审计与预算脚本独立验清单；run roster 校验唯一性和输出形状 | `tests/test_audit_gates.py` |
| R10 / P2 | 相同向量的传播报告缺失 cancelling 等字段；测试 fixture 共享查询/文档列表，突变会污染其他记录并掩盖负例 | 恒定报告字段；测试 fixture 复制可变列表，验证单条突变隔离 | identical-vector 报告、`test_mutating_one_record_does_not_mutate_shared_fixtures` |

R1–R8 的新增负例在修复前先复现失败，随后通过。R9 的输入 Gate 与 R10 的缺字段也先复现，
随后修复；fixture 别名在编写负例时发现并隔离。测试通过数量不代表独立 bug 数量。

## 算法检查

1. N=4 的全部 64 张二元反对称比较表，K=1/2/3/4，每表六个单请求更新：1,536 次更新对照。
2. N=1/3/6/8 固定 seed 随机表：960 轮链式多更新。
3. N=5/7/16/31/64、不同 K：750 轮链式多更新，覆盖非二次幂叶数。
4. N=4、K=1/2/3 的 conditional safe/impact 分类逐一检查所有未观察边的补全。
   unresolved 保持未知，不通过增加假设给出安全结论。
5. 三种在线策略在相同响应组/seed/预算下，对照 incremental 与 cached_replay 的
   选边、输出、状态和逐请求账目；四元素新分支与预算中止另有手工反例。

初始树保存全部候选的晋级状态；变更命中已使用的初始比较后，节点与祖先被重新计算。
随后按固定移除规则提取 Top-K，新出现比较从当前执行账目获取，未变化判断合法复用。
在当前实现假设下，这些不变量与对照测试一致；未发现已测试范围内的核心排序输出不一致。

## 对已有结果的影响

修复后历史 420 条记录仍通过，naive 15/15 重放与 22 项历史数字不变。
Tournament 三查询集合 D_EM 仍为 0.60/0.90/0.00；159 个单请求反事实中 134 个 unresolved。
48 行单组三响应 smoke 均完成；其 D_EM 仍为 null，不能扩充成独立效果证据。
重新生成产物加入执行来源、维度与失败状态字段，并更新代码/配置来源哈希。

## 仍未验证的研究事项

- TraceSensitivity 是未冻结启发式。当前排序没有可靠概率校准或恢复成本预测，尚未证明复核预算收益。
- 同一复核顺序、相同完整请求缓存与响应下，incremental 和 cached_replay 的新响应调用一致符合当前语义。
  后续 H3 应先实测完整 CPU 开销；节点求值数没有计入所有扫描、复制、策略分析开销，不能替代 CPU/延迟收益。
- Python API 层隔离防止常规误用，不提供敌对代码沙盒。未知比较、服务时间独立性、真实成本和 HTTP 行为仍有各自边界。
- source_id 绑定内容及调用方提供的 query/request context。真实数据构造必须提供正确 context，不能把默认 synthetic 命名空间当作生产来源证明。
- 正式独立查询/12 响应数据、方法冻结、质量 Gate 和 live 验证仍待完成；本轮未扩大科学结论。

## 复现

```bash
uv run --no-project --with pytest python scripts/check_offline.py
```

输入 Gate 失败时返回非零并保留上次成功产物；它们不代表本次失败输入获得通过。
原始数据完整性可另行用 `python scripts/make_data_manifest.py --check` 核验。
