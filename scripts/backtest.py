#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建评分分布，复核 A/B/C/D/E 五个等级阈值的分位标定。

为什么需要这个脚本：`doc.md` 声称等级边界按 1990–2026 的历史分数分布标定
（A≈P90、B≈P75 …），但仓库里原本没有任何可复现的材料。这个脚本用公开数据
把那条断言变成可验证的。

数据源
------
- S&P 月度价格与盈利：Shiller `ie_data.xls`（shillerdata.com 主站，HTTPS）
- VIX 日线：CBOE `VIX_History.csv`，按月取最后一个交易日
- 评分函数：直接调用 `index_score.compute_scores`，与线上完全一致

与线上流程的差异（结论按"量级参考"使用，不是精确复算）
------------------------------------------------------
1. 月度粒度：200 个交易日用 10 个月均线近似
2. PE 用 Shiller 的 S&P Composite P/E；线上 NDX/SPX 用的是蛋卷的滚动十年百分位
3. VIX 取月末收盘，而非月末当天
4. 样本从 1999-12 起（PE 十年百分位需要 120 个月预热），只覆盖 SPX

用法
----
    python scripts/backtest.py
"""

import io
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_score as m  # noqa: E402

SHILLER_PAGE = "https://shillerdata.com/"
VIX_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
START = "1990-10-01"      # 与文档一致的回测起点
PE_WINDOW = 120           # PE 十年百分位窗口（月）
MA_WINDOW = 10            # 10 个月 ≈ 200 个交易日
CLAIMED = {"A": 10, "B": 15, "C": 25, "D": 30, "E": 18}

HEADERS = {**m.HEADERS}


def http_get(url, timeout=180, retries=3):
    last = None
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[backtest] 请求失败(第{attempt + 1}次): {url} -> {exc}", flush=True)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"请求最终失败: {url} -> {last}")


def shiller_download_url():
    page = http_get(SHILLER_PAGE, timeout=60).text
    match = re.search(r'href="([^"]*ie_data[^"]*)"', page)
    if not match:
        raise RuntimeError("shillerdata.com 页面上找不到 ie_data 下载链接")
    url = match.group(1)
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://shillerdata.com" + url
    if not url.startswith("https://"):
        raise RuntimeError(f"Shiller 下载地址不是 HTTPS，拒绝使用: {url}")
    return url


def load_shiller_monthly():
    url = shiller_download_url()
    content = http_get(url, timeout=m.SHILLER_TIMEOUT).content
    raw = pd.read_excel(io.BytesIO(content), header=None, sheet_name="Data")

    header_idx = None
    for i in range(min(30, len(raw))):
        values = [str(x) for x in raw.iloc[i].tolist()]
        if (len(values) > 3 and values[0].strip().upper() == "DATE"
                and values[1].strip() == "P" and values[3].strip() == "E"):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("无法定位 Shiller 数据表头")

    frame = raw.iloc[header_idx + 1:][[0, 1, 3]].copy()
    frame.columns = ["date", "price", "earnings"]
    for column in ("price", "earnings"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["price", "earnings"])
    frame = frame[frame["earnings"] > 0].copy()

    def to_month(value):
        text = f"{float(value):.2f}"
        year, month = text.split(".")
        return pd.Timestamp(year=int(year), month=int(month), day=1)

    frame["month"] = frame["date"].apply(to_month)
    frame = frame.sort_values("month").reset_index(drop=True)
    frame["pe"] = frame["price"] / frame["earnings"]
    return frame[["month", "price", "pe"]]


def load_vix_monthly():
    text = http_get(VIX_CSV, timeout=60).text
    frame = pd.read_csv(io.StringIO(text))
    frame["DATE"] = pd.to_datetime(frame["DATE"], format="%m/%d/%Y")
    frame["month"] = frame["DATE"].dt.to_period("M").dt.to_timestamp()
    return frame.groupby("month")["CLOSE"].last()


def build_series():
    shiller = load_shiller_monthly()
    vix_monthly = load_vix_monthly()

    data = shiller.copy()
    data["vix"] = data["month"].map(vix_monthly)
    data = data.dropna(subset=["vix"]).reset_index(drop=True)

    # PE 十年百分位：滚动 120 个月内严格小于当前值的比例（与线上定义一致）
    data["pe_pct"] = [
        (data["pe"].iloc[max(0, i - PE_WINDOW + 1):i + 1] < data["pe"].iloc[i]).mean()
        if i >= PE_WINDOW - 1 else float("nan")
        for i in range(len(data))
    ]
    data["ma"] = data["price"].rolling(MA_WINDOW).mean()
    data["dev"] = (data["price"] / data["ma"] - 1) * 100

    scored = data[(data["month"] >= START) & data["pe_pct"].notna() & data["ma"].notna()].copy()
    scored["score"] = [
        m.compute_scores(pe, dev, vix)[3]
        for pe, dev, vix in zip(scored["pe_pct"], scored["dev"], scored["vix"])
    ]
    scored["level"] = [m.level_and_advice(score)[0] for score in scored["score"]]
    return scored


def main():
    scored = build_series()
    scores = scored["score"]
    print(f"可评分区间：{scored['month'].min():%Y-%m} .. {scored['month'].max():%Y-%m}，"
          f"{len(scored)} 个月\n")

    print("=== 总分分位数 ===")
    for q in (0.05, 0.15, 0.25, 0.50, 0.75, 0.90, 0.95):
        print(f"  P{q * 100:>5.1f} = {scores.quantile(q):6.2f}")
    print(f"  min={scores.min():.2f}  max={scores.max():.2f}  mean={scores.mean():.2f}")

    print("\n=== 当前阈值下的等级占比 vs 文档声称 ===")
    shares = scored["level"].value_counts(normalize=True)
    for level in ("A", "B", "C", "D", "E"):
        actual = shares.get(level, 0.0) * 100
        print(f"  {level}: 实测 {actual:5.1f}%   文档声称 {CLAIMED[level]:>2}%")

    print("\n=== 阈值实际落在哪个分位 ===")
    for threshold, level in ((72, "A"), (53, "B"), (36, "C"), (22, "D")):
        share_at_or_above = (scores >= threshold).mean() * 100
        print(f"  {level} 阈值 {threshold:>2} → 该分位以上占 {share_at_or_above:5.1f}%"
              f"（即 ≈P{100 - share_at_or_above:.0f}）")

    print("\n=== 结构性约束 ===")
    above = scored[scored["dev"] > 4.24]
    if len(above):
        print(f"  dev > +4.24% 的月份占 {len(above) / len(scored) * 100:.1f}%，"
              f"其中最高分仅 {above['score'].max():.2f}（A 需 72，故不可达）")

    print("\n=== 若要让占比接近 A10/B15/C25/D30/E20 ===")
    for level, q in (("A", 0.90), ("B", 0.75), ("C", 0.50), ("D", 0.20)):
        print(f"  {level} 阈值应取 {scores.quantile(q):6.2f}（P{q * 100:.0f}）")
    print("\n注意：本脚本为月度近似，与线上日线流程存在差异，结论只作量级参考。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
