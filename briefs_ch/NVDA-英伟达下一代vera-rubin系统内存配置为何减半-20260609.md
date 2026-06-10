---
ticker: NVDA
question: 英伟达下一代Vera Rubin系统内存配置为何减半？
date: 2026-06-09
model: kimi-k2.6
---

# NVDA: 英伟达下一代Vera Rubin系统内存配置为何减半？

## Summary
英伟达Vera Rubin NVL72机架系统的CPU侧SOCAMM内存配置从预期的55TB降至28TB（降幅约50%），核心原因是**2026年全球LPDDR5X供应极度紧张**，而非需求萎缩。此举是英伟达为确保Rubin平台按时交付而采取的供应链弹性管理策略。GPU侧的HBM4内存（每颗Rubin GPU 288GB）完全不受影响。此外，Vera Rubin采用模块化SOCAMM2插槽设计，客户可后续升级至更高容量模块。

## Sub-questions

### 1. "内存减半"具体指什么？GPU侧的HBM4是否受影响？

所谓"内存减半"仅指**CPU侧的系统内存（SOCAMM LPDDR5X）**，与GPU算力核心完全无关。

根据SemiAnalysis报告，Vera Rubin NVL72每柜含72颗Rubin GPU和36颗Vera CPU。GPU侧每颗Rubin搭载**288GB HBM4**，整柜约**20.7TB**，这一数字**保持不变**[1][2]。变化发生在CPU侧：每颗Vera CPU有8个SOCAMM插槽，原本市场预期采用192GB模块满配（每CPU 1.5TB，整柜约54-55TB），但实际出货多数将采用**96GB模块**（每CPU 768GB，整柜约28TB）[1][2]。

### 2. 为何选择降低CPU侧内存配置？是需求不足还是供应短缺？

**根本原因是LPDDR5X供应短缺，而非需求不足。**

2026年全球LPDDR5X供应极度紧张。美光在5月底的Wolfe Research会议上明确表示，内存需求远超供应能力，且这一失衡预计将延续至2026年以后。美光FY2026全年HBM产能已售罄，DRAM均价同比上涨超110%，毛利率飙升至74%。三星和SK海力士 likewise 处于满产满销状态[2][3]。

在此背景下，英伟达面临的挑战是**"无法获得足够的LPDDR5X芯片来填满每个插槽"**。降低默认SOCAMM配置本质上是工程级的供应链管理决策：与其因内存短缺延误整柜交付，不如先以较低配置出货，让算力尽快上线[2][3]。

此外，每台机架物料与制造成本可从760万美元降至680万美元，单机架节省80万美元（约10%），这也是兼顾成本优化的战略选择[1]。

### 3. 这种"降配"是永久性的吗？客户能否后续升级？

**不是永久性降级。** Vera Rubin平台采用JEDEC标准化的**SOCAMM2模块化设计**，与Blackwell GB300主板上直接焊接LPDDR不同，SOCAMM2支持热插拔、现场更换和升级[2][3]。

英伟达在CES 2026上特别强调这一设计优势：整个计算托盘的组装时间从2小时缩短至5分钟。客户可以先安装96GB模块，后续如需更多内存，可直接更换为192GB甚至256GB模块，类似于更换标准内存条[2][3]。

SK海力士已于2026年2月宣布开始量产192GB SOCAMM2模块，专为NVIDIA Vera Rubin平台设计，采用1cnm工艺LPDDR5X，带宽较传统RDIMM翻倍，功耗效率提升75%以上[4]。

### 4. 市场对这一消息的反应是否合理？

**市场存在明显误读。** SemiAnalysis报告本身准确，但市场将其解读为"内存需求腰斩"，导致美光股价单日暴跌超10%（市值蒸发超1000亿美元），SK海力士重挫9.92%，A股存储板块集体回调[1][2]。

市场的逻辑漏洞在于：
- **忽略模块化架构**：降配是阶段性弹性配置，非永久性硬件降级
- **混淆"每柜内存量"与"总需求量"**：在相同LPDDR5X供应约束下，英伟达可以组装更多机架。此前一批内存只够100柜，现在可支持近200柜。总LPDDR5X消耗并未下降，只是分布在更多机架上[2][3]
- **误伤标的**：美光在SOCAMM领域地位稳固（首个推出256GB SOCAMM2，是英伟达核心SOCAMM合作伙伴五年），其真正风险在于HBM4市场份额边缘化（Rubin平台HBM4订单SK海力士占70%、三星30%，美光预计仅18%）[2]

此外，6月4日Broadcom发布Q2财报后未上调全年AI芯片收入指引，引发半导体板块整体抛售，SemiAnalysis报告只是给已寻找卖点的交易者提供了"叙事弹药"[2][3]。

### 5. 推理工作负载是否需要满配CPU内存？

**并非所有工作负载都需要1.5TB LPDDR5X。** 虽然大模型训练对内存需求极高，但许多推理任务——尤其是agentic AI和长上下文推理——可以通过NVLink-C2C灵活地在HBM和LPDDR之间调度KV缓存。对不少客户而言，768GB的CPU侧内存已经足够[2][3]。

Vera Rubin平台定位为推理优化架构，Rubin GPU在可比HGX配置中可实现**高达3.5倍的推理性能提升**，专为agentic系统和长上下文工作流设计[5]。这意味着部分内存密集型任务可由GPU侧HBM4更高效地处理，降低了对CPU侧内存的绝对依赖。

## Sources

[1] 英伟达 Rubin 机架内存缩水一半？市场误读导致美光市值蒸发 — 凤凰网/IT之家 — 2026年6月6日 — https://h5.ifeng.com/c/vivoArticle/v0027r8K0G3oj1xLXgWvZri7W4cSh8V-_7H4jdzyvK1eMb68__?vivoBusiness=hiboardnews

[2] 55 TB Reduced to 28 TB? Rumors and Panic Behind Rubin's Memory Reduction — TechFlow Post — 2026年6月 — https://www.techflowpost.com/en-US/article/31919

[3] NVIDIA Rubin SOCAMM Memory Cut Triggers Market Panic and MU Decline — KuCoin News/Tide Research — 2026年6月 — https://www.kucoin.com/news/flash/nvidia-rubin-socamm-memory-cut-sparks-market-panic-and-mu-drop

[4] SK Hynix Begins Mass Production of 192 GB SOCAMM2 Memory With 2x Bandwidth, A Vital Piece For NVIDIA' Vera Rubin — Wccftech — 2026年2月 — https://wccftech.com/sk-hynix-mass-produces-192-gb-socamm2-memory-for-nvidia-vera-rubi-ai-datacenters

[5] NVIDIA lauches next-gen with Vera Rubin — 2CRSi — 2026年 — https://2crsi.com/nvidia-vera-rubin-generation-cpu-gpu

[6] Nvidia's memory costs soar 485%, latest AI systems now cost $7.8 million to build — Tom's Hardware — 2026年5月 — https://www.tomshardware.com/tech-industry/artificial-intelligence/nvidias-memory-costs-soar-485-percent-latest-ai-systems-now-cost-usd7-8-million-to-build-memory-now-comprises-25-percent-of-the-total-cost-rubin-gpus-a-mere-usd50-000-apiece

[7] 英伟达新品被指内存容量"缩水" 海内外存储股应声重挫 — 证券时报/搜狐 — 2026年6月5日 — https://www.sohu.com/a/1032824204_122014422