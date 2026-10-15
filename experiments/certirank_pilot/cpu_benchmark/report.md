# 合成数据 CPU 微基准

记录选边、复制、失效定位、节点更新、轨迹维护、提取和复核 CPU；包含中止工作。
计时有 instrumentation 开销，两种修复模式使用相同测量条件并交错运行。结果仅描述本机合成微基准。

| N | 策略 | incremental 平均 CPU ms | cached replay 平均 CPU ms | 相对降低 |
|---|---|---:|---:|---:|
| 8 | SimpleTopology | 0.068 | 0.072 | 5.8% |
| 8 | TraceSensitivity | 0.262 | 0.261 | -0.5% |
| 16 | SimpleTopology | 0.136 | 0.135 | -1.1% |
| 16 | TraceSensitivity | 1.143 | 1.144 | 0.0% |
| 64 | SimpleTopology | 0.675 | 0.684 | 1.4% |
| 64 | TraceSensitivity | 26.583 | 26.595 | 0.0% |

未测真实 API/端到端服务延迟，未据此宣布 H3 通过。节点求值数不作为 CPU 收益的替代指标。
