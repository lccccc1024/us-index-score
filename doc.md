# 美股指数定投评分系统 — 开发文档

## 1. 项目概述
### 1.1 目标
对纳斯达克100（NDX）与标普500（SPX）两个美股指数进行每日自动化评分，辅助指数基金定投决策。

### 1.2 技术栈约束
- Python 3.12（GitHub Actions runner）
- 数据源：yfinance（行情/MA200/VIX）、蛋卷基金公开接口（PE十年百分位）、Shiller官方月度数据（SPX辅助对比）
- 通知：dawidd6/action-send-mail@v3（Gmail SMTP）
- 发布：GitHub Pages（静态HTML）

## 2. 评分模型（改进版）

三个维度加权评分，满分 100：

| 维度 | 满分 | 公式 | 说明 |
|---|---|---|---|
| PE估值 | 30 | `30 × (1 − pe_percentile)` | PE百分位越低（越便宜）得分越高 |
| MA200偏离度 | 40 | `40 / (1 + e^(dev%/5))` | S型曲线，消除线性断崖 |
| VIX恐慌指数 | 30 | `30 × log(VIX/8) / log(4)` | 对数缩放，正常区间灵敏 |

> 偏离度% = (收盘价 / MA200 − 1) × 100
> VIX ≤ 8 时得0分（极度平静）
> VIX ≥ 32 时满分30（极度恐慌）

### 2.1 等级与建议

| 等级 | 综合评分 | 建议 |
|---|---|---|
| A | ≥ 80 | 极度低估，可以加大仓位 |
| B | ≥ 60 | 低估，加大定投 |
| C | ≥ 40 | 中性，正常定投 |
| D | ≥ 6 | 估值偏高，维持小额定投，不重仓 |
| E | < 6 | 极度高估，谨慎，减少加仓 |

## 3. 数据源
### 3.1 行情数据（yfinance）
- 指数收盘价：`yf.Ticker("^NDX").history(period="1y")` 最新有效收盘
- MA200：历史收盘序列最后200个有效值的均值
- VIX：`yf.Ticker("^VIX").history(period="5d")` 最新收盘

### 3.2 PE十年百分位
- **蛋卷基金公开接口**：`GET https://danjuanfunds.com/djapi/index_eva/dj`
  - 带 UA + Referer `https://danjuanfunds.com/valuation/dj`
  - 从返回列表中取 `NDX` 和 `SP500` 的 `pe_percentile`
  - 语义：当前PE比历史X%时间都贵（0-1，越低越便宜）
- **SPX辅助对比（Shiller）**：
  - 从 `https://shillerdata.com/` 解析CDN链接下载 `ie_data.xls`
  - 回退镜像：`http://www.econ.yale.edu/~shiller/data/ie_data.xls`
  - 自算PE百分位（近10年样本），写入 `shiller_pe_percentile` 字段
  - 获取失败仅告警，不阻断评分

### 3.3 异常容错规则
- 网络接口超时/返回空：脚本打印错误日志，终止运行；Action标记job失败。
- 蛋卷接口失败或缺失NDX/SP500：job直接报错，不允许继续计算。
- Shiller辅助数据获取失败：仅打印告警，不阻断评分（SPX主值仍来自蛋卷）。
- 价格、VIX获取失败：job失败退出。
- 行情日期（as_of）距今超过4个自然日：仅打印告警并在报告注明，不阻断评分。

## 4. 输出规范

### 4.1 文件
- `result.json`：UTF-8，格式化缩进
- `result.html`：UTF-8，纯静态HTML（内联样式，Gmail兼容）

### 4.2 JSON结构
```json
{
  "date": "YYYY-MM-DD",
  "ndx": { ... },
  "spx": { ... }
}
```

### 4.3 每个标的输出字段
```json
{
  "as_of": "YYYY-MM-DD（收盘价对应交易日）",
  "price": 收盘价格,
  "ma200": MA200价格,
  "dev_pct": MA200偏离度百分比,
  "pe_percentile": PE十年百分位(0‑1),
  "vix": VIX最新值,
  "pe_score": PE得分,
  "ma_score": MA200得分,
  "vix_score": VIX得分,
  "total_score": 综合得分,
  "level": "A/B/C/D/E",
  "advice": "投资建议文本"
}
```

## 5. HTML网页输出规范
### 5.1 页面整体要求
- 编码UTF-8，中文不乱码；纯静态HTML，无JS。
- 页面标题：`美股指数每日评分｜NDX & SPX`
- 页面顶部展示生成日期 `YYYY-MM-DD`
- 两个标的分块展示，每个标的包含两张表格：【原始输入数据】、【评分结果】
- 等级单元格背景色：A深绿、B浅绿、C黄、D橙、E深红
- 全内联样式（Gmail兼容）

### 5.2 每个标的表格字段
原始输入数据表格：
- 行情日期（收盘价对应交易日）
- 指数收盘价格
- MA200
- MA200偏离度(%)
- PE十年百分位
- VIX恐慌指数

评分结果表格：
- PE得分(满分30)
- MA200得分(满分40)
- VIX得分(满分30)
- 综合评分(满分100)
- 投资等级
- 投资建议

## 6. 自动化部署

### 6.1 GitHub Actions工作流
- 触发：cron `0 21 * * 1-5`（工作日21:00 UTC = 北京时间次日05:00）+ workflow_dispatch
- 环境：ubuntu-latest, Python 3.12

### 6.2 执行步骤
1. 拉取仓库代码
2. 安装Python 3.12 + 依赖
3. 执行 `python index_score.py` → 输出 result.json / result.html
4. 通过Gmail SMTP发送邮件（正文内嵌HTML报告 + 附件）
5. 提交产物到仓库（仅在有变更时提交）

### 6.3 所需Secrets
| Secret | 说明 |
|---|---|
| MAIL_USER | Gmail发件账号（完整邮箱地址） |
| MAIL_PWD | Gmail应用专用密码（App Password，非登录密码） |

## 7. 依赖要求
```
yfinance>=0.2.40
pandas>=2.0
requests>=2.31
xlrd>=2.0.1
```

## 8. 本地使用
```bash
pip install -r requirements.txt
python index_score.py            # 生成当日评分
python index_score.py --selftest # 公式自检（离线）
```

## 9. 验收清单
- [x] 公式与等级边界严格按本文档实现
- [x] 数据源：yfinance（行情）、蛋卷（PE百分位）、Shiller（SPX辅助）
- [x] 蛋卷失败job报错；Shiller失败仅告警
- [x] 输出字段完整（含as_of/shiller_pe_percentile）
- [x] HTML内联样式，Gmail兼容
- [x] 工作流cron + workflow_dispatch + 邮件 + 提交 + Pages
- [x] 无硬编码密钥（仅secrets引用）
- [x] selftest PASS（含MA200/VIX边界测试）
- [x] 完整运行成功，独立交叉验证通过
