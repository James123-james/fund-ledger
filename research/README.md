# 半自动双轨周报（research）

当前是 **Milestone 8：方向观察版周报跑通**。
只读台账、AKShare 行情、5 年估值/价格分位、宏观 6/6、催化登记表、持仓相关性、数据包和观察版周报都已打通。
催化登记表已确认 9 条高置信；科创50/消费/港股科技仍是 candidate；红利低波空白。
13 个方向与现有持仓的 60/120 日相关性已算，>0.7 自动标记为高重叠。

## 已实现

- 只读台账适配器：持仓、权重、汇总、流水/计划数量
- 观察池 YAML：当前是 13 个方向的草稿
- 生成带行情/宏观指标的 `data-pack.json`
- 一键脚本 `python3 -m research.weekly`（默认先抓宏观 + 行情）
- 宏观：美债 10Y 名义、布油、黄金、DR007；TIPS/DXY 可手填
- 催化登记表：日期/类型/来源校验，空表明确写 0 条
- 相关性：方向代理 vs 持仓净值，60/120 日，>0.7 降级
- AKShare 覆盖检查：`python3 -m research.check_source`
- 周报 skill 草稿：`research/skill/weekly-report.md`
- 第一份数据版周报：`reports/2026-W46/weekly.md`
- 不写 `data/fund.db`，不生成交易

## 运行

在仓库根目录：

```bash
python3 -m research.ledger_adapter          # 打印台账快照
python3 -m research.weekly --week 2026-W46 # 抓行情 + 生成数据包
python3 -m research.weekly --no-fetch      # 用已有行情缓存，快速重跑
python3 -m research.check_source            # 检查 AKShare 覆盖
python3 -m research.catalysts               # 校验催化登记表
python3 -m research.correlation             # 计算与持仓相关性
python3 -m research.review                  # 4 周复盘汇总
```

输出：

```text
research/data/2026-W46/data-pack.json
```

格式与 `docs/weekly-report-template/_template/data-pack.json` 对齐。

## 目录

```text
research/
├── __init__.py
├── ledger_adapter.py        # 只读台账
├── fetch_market.py          # AKShare 行情与指标
├── fetch_macro.py           # 宏观变量
├── catalysts.py             # 催化登记表校验
├── correlation.py           # 方向与持仓相关性
├── review.py                # 4 周复盘汇总
├── build_pack.py            # 数据包
├── check_source.py          # 数据源覆盖检查
├── weekly.py                # 一键入口
├── skill/weekly-report.md   # 周报 skill 草稿
└── config/
    ├── watchlist.draft.yml  # 13 个方向草稿
    ├── source_map.draft.yml # 方向→数据源映射
    ├── macro_manual.yml     # TIPS/DXY 手填
    ├── catalysts.draft.yml  # 30 日催化登记表
    ├── correlation_proxies.yml # 方向日线代理
    └── settings.example.yml # 台账地址、路径、报告上限
```

## 观察池草稿

`watchlist.draft.yml` 里 13 个方向：

- 宽基：沪深300、中证A500、科创50
- 风格：红利低波
- 行业/主题：半导体、机器人、创新药、化工、有色、电力、消费、港股科技
- 商品：黄金

`proxy` 是给数据源用的指数/ETF 映射；空的表示待映射。
`existing_overlap` 标了与现有基金的重叠，周报会用它做交叉检查。

**这是草稿**，确认后把 `status: draft` 改成 `active`，并删掉不需要的方向。

## AKShare 验证结果

已安装 AKShare 1.20.37，`python3 -m research.check_source` 的结论：

| 类型 | 方向 | 数据 |
| --- | --- | --- |
| 5 年 PE/PB | 沪深300 | Legulegu 月频，61 期 |
| 5 年 PE/PB（代理） | 红利低波 | 上证红利代理，61 期 |
| 行业 PE/PB 快照，5 年历史可拉 | 半导体(电子)、机器人(机械)、创新药(医药)、化工、有色、电力(公用)、消费(食品饮料) | 申万一级，月频约 60 次请求 |
| 只有价格分位 | 科创50、黄金、港股科技 | 新浪 ETF 历史，约 1210 个交易日 |
| 只有价格分位 | 中证A500 | 指数 sh000510 日线，无 PE/PB |

注意：

- 东方财富 push2 会限流，首次约 5 分钟，反复触发可能 40 分钟以上；ETF 价格改用新浪，避开它
- 申万行业 5 年前只有 27 个，现在是 31 个，行业分类会变；历史要用**指数代码**对齐，新行业没有完整 5 年
- 主题类（半导体/机器人/创新药/电力/消费）用的是行业代理，不是原主题指数
- 黄金/港股科技/科创50 只有价格分位，没有 PE/PB；报告要明确标注

## 周报 skill 已安装

DSH 技能名：`fund-ledger-weekly-report`

安装位置：

```text
~/.dsh/skills/fund-ledger-weekly-report/SKILL.md
```

重新安装：

```bash
mkdir -p ~/.dsh/skills/fund-ledger-weekly-report
cp research/skill/weekly-report.md ~/.dsh/skills/fund-ledger-weekly-report/SKILL.md
```

## 当前产出的边界

周报已是 **观察版**，不是买入版：

- 9 个方向有完整 PE/PB + 价格分位
- 4 个方向（中证A500/科创50/黄金/港股科技）只有价格分位
- 宏观 6/6：TIPS、美债 10Y 名义、布油、黄金、DR007、美元指数（截图手填）
- 催化 12 条：confirmed 9 条，candidate 3 条；红利低波空白
- 相关性 13/13 已算；9 个方向 >0.7 高重叠
- 本周观察：半导体（价值轨）、机器人（趋势轨）；不操作
- 评分模型未校准，所以暂不写分数

## 4 周复盘怎么看

- 每周五填 `reports/run-log.csv` 一行（耗时、数据问题、行为、清晰度）
- 11/25、12/11 更新 `reports/tracking.csv`
- 12/13 跑 `python3 -m research.review`，输出运行次数、耗时、触发、行为计数
- 详细字段见 `reports/review-guide.md`

## 下一步

1. 连续跑 4 周，按 `reports/index.md` 和 `reports/tracking.csv` 记录触发与否决
2. 每周更新美元指数手填值；超过 7 天会标 stale
3. 4 周后复盘命中率，再决定是否校准评分模型
4. 观察池去掉高重叠且不看的重复方向

## 约定

- 台账只读，不改正式库
- 报告不对比配置模板，不做再平衡建议
- 不给具体基金代码/买卖点/仓位比例
- 每周最多 3 个方向进入报告
- 没有合格方向就写“本周无合格方向”
- `research/data/` 与 `reports/` 不入库（含个人数据）
