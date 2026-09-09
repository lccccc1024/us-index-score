# 美股指数定投评分系统

对纳斯达克100（NDX）与标普500（SPX）两个指数进行每日自动化评分，用于辅助指数基金定投决策。

**每日评分报告（GitHub Pages）：** https://blog.950922.xyz/us-index-score/result.html

## 评分模型

三个维度加权评分，满分 100：

| 维度 | 满分 | 公式 | 说明 |
|---|---|---|---|
| PE 估值（十年百分位） | 30 | `30 × (1 − pe_percentile)` | PE百分位越低（越便宜）得分越高 |
| MA200 偏离度 | 40 | `40 / (1 + e^(dev%/5))` | S型曲线，平滑衰减，无断崖 |
| VIX 恐慌指数 | 30 | `30 × log(VIX/8) / log(4)` | 对数缩放，正常区间灵敏 |

> 偏离度% = (收盘价 / MA200 − 1) × 100。VIX ≤ 8 得0分（极度平静），VIX ≥ 32 满分30（极度恐慌）。

### 等级与建议

| 等级 | 综合评分 | 建议 |
|---|---|---|
| A | ≥ 80 | 极度低估，可以加大仓位 |
| B | ≥ 60 | 低估，加大定投 |
| C | ≥ 40 | 中性，正常定投 |
| D | ≥ 6 | 估值偏高，维持小额定投，不重仓 |
| E | < 6 | 极度高估，谨慎，减少加仓 |

## 数据来源

| 数据 | 来源 |
|---|---|
| 指数收盘价、MA200、VIX | Yahoo Finance（yfinance） |
| PE 十年百分位（NDX & SPX） | 蛋卷基金公开接口（danjuanfunds.com） |
| 标普500 辅助对比（Shiller 月度 PE 百分位） | Robert Shiller 官方数据集 |

Shiller 数据为辅助参考（仅 SPX 的 `shiller_pe_percentile` 字段），获取失败不阻断评分，仅提示跳过。

## 自动化流程

GitHub Actions 定时运行（工作日 21:00 UTC，即北京时间次日 05:00），也可手动触发：

1. 拉取行情、VIX、PE 百分位（含失败重试）
2. 计算评分并生成 `result.json` / `result.html`
3. 通过 Gmail SMTP 发送邮件（正文内嵌 HTML 报告，附件含 result.html / result.json）
4. 提交报告到仓库；独立任务显式部署 GitHub Pages，邮件失败不阻断发布

首次使用此工作流，请在仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**，并允许 `github-pages` 环境从运行分支部署。工作流仅发布 `index.html`、`result.html` 和 `result.json`。

输入拒绝 NaN、Infinity、非正价格/VIX 和超出 0–1 的 PE 百分位。MA200 使用合并最新收盘价后的最后 200 个有效交易日；盘中运行不使用当天未收盘日线。报告保留 PE、VIX 日期，并在日期不一致或超过四天时告警；蛋卷仅提供月日时，年份按最近一次该日期推定并注明。

### 所需 Secrets

| Secret | 说明 |
|---|---|
| `MAIL_USER` | Gmail 发件账号（完整邮箱地址） |
| `MAIL_PWD` | Gmail 应用专用密码（App Password，非登录密码） |

## 本地运行

```bash
pip install -r requirements.txt
python index_score.py            # 生成当日评分
python index_score.py --selftest # 公式自检（离线）
python -m unittest discover -s tests -v # 数据处理回归测试（离线）
```

输出：控制台打印 JSON 结果，并写入 `result.json` / `result.html`。

## 目录结构

```
index_score.py                   主脚本（评分、HTML 渲染、自检）
requirements.txt                 依赖（yfinance / pandas / requests / xlrd）
.github/workflows/daily-run.yml  每日自动化工作流
index.html                       页面入口，跳转到最新报告
result.html / result.json        最新一期报告（由工作流自动更新）
doc.md                           开发文档（公式、数据源、验收清单）
```

## 免责声明

本项目仅供学习与研究参考，不构成任何投资建议。投资有风险，决策需谨慎。
