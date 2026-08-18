# 美股指数定投评分系统

对纳斯达克100（NDX）与标普500（SPX）两个指数进行每日自动化评分，用于辅助指数基金定投决策。

**每日评分报告（GitHub Pages）：** https://blog.950922.xyz/us-index-score/result.html

## 评分模型

三个维度加权评分，满分 100：

| 维度 | 满分 | 公式 |
|---|---|---|
| PE 估值（十年百分位） | 30 | `clamp(30 × (1 − pe_percentile), 0, 30)` |
| MA200 偏离度 | 40 | `clamp(20 − 偏离度% × 2, 0, 40)` |
| VIX 恐慌指数 | 30 | `clamp((VIX − 15) / 15 × 30, 0, 30)` |

> 偏离度% = (收盘价 / MA200 − 1) × 100。PE 百分位越低（越便宜）得分越高；价格低于 MA200 越多得分越高；VIX 高于 15 时开始加分（恐慌时便宜）。

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
4. 提交报告到仓库，GitHub Pages 自动发布

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
```

输出：控制台打印 JSON 结果，并写入 `result.json` / `result.html`。

## 目录结构

```
index_score.py                   主脚本（评分、HTML 渲染、自检）
requirements.txt                 依赖（yfinance / pandas / requests / xlrd）
.github/workflows/daily-run.yml  每日自动化工作流
index.html                       页面入口，跳转到最新报告
result.html / result.json        最新一期报告（由工作流自动更新）
```

## 免责声明

本项目仅供学习与研究参考，不构成任何投资建议。投资有风险，决策需谨慎。