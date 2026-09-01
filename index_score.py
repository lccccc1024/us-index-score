#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股指数定投评分系统（纳斯达克100 & 标普500）

数据源：
- 行情价格/MA200/VIX：yfinance
- PE十年百分位：蛋卷基金公开接口（NDX/SP500 主值）
- SPX 辅助对比：Shiller 官方月度数据自算百分位

评分公式与等级边界严格遵循开发文档，禁止修改。
"""

import argparse
import json
import math
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timezone
from io import BytesIO

import requests

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

HTTP_TIMEOUT = 60
SHILLER_TIMEOUT = 180
RETRIES = 3

DANJUAN_URL = "https://danjuanfunds.com/djapi/index_eva/dj"
DANJUAN_REFERER = "https://danjuanfunds.com/valuation/dj"
SHILLER_PAGE = "https://shillerdata.com/"
SHILLER_YALE_MIRROR = "http://www.econ.yale.edu/~shiller/data/ie_data.xls"
SHILLER_YEARS = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

GRADE_STYLE = {
    "A": ("#1b5e20", "#ffffff"),
    "B": ("#c8e6c9", "#1a1a1a"),
    "C": ("#fff9c4", "#1a1a1a"),
    "D": ("#ffb74d", "#1a1a1a"),
    "E": ("#b71c1c", "#ffffff"),
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url, headers=None, timeout=HTTP_TIMEOUT, retries=RETRIES):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            log(f"请求失败(第{i + 1}次): {url} -> {e}")
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"请求最终失败: {url} -> {last}")


def http_get_json(url):
    return http_get(url).json()


def with_retry(fn, *args, retries=RETRIES, **kwargs):
    last = None
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            log(f"执行失败(第{i + 1}次): {fn.__name__} -> {e}")
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"执行最终失败: {fn.__name__} -> {last}")


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def compute_scores(pe_percentile, dev_pct, vix):
    """改进后评分公式：S型MA200 + 对数VIX + 线性PE。

    MA200: 40 / (1 + e^(dev%/5)) — 平滑S型曲线，消除线性断崖。
           dev%=0 → 20，dev%=10 → 4.8，dev%=-10 → 35.2
    VIX:   30 × log(VIX/8) / log(4) — 对数缩放，正常区间灵敏。
           VIX=8 → 0，VIX=15 → 13.6，VIX=30 → 28.6，VIX≥32 → 30
    PE:    30 × (1 − pe_percentile) — 线性映射，百分位越低越便宜得分越高。
    """
    pe_score = round(clamp(30 * (1 - pe_percentile), 0, 30), 2)
    ma_score = round(clamp(40 / (1 + math.exp(dev_pct / 5)), 0, 40), 2)
    if vix <= 8:
        vix_score = 0.0
    else:
        vix_score = round(clamp(30 * math.log(vix / 8) / math.log(4), 0, 30), 2)
    total_score = round(pe_score + ma_score + vix_score, 2)
    return pe_score, ma_score, vix_score, total_score


def level_and_advice(total):
    """等级边界严格按文档：A>=80, B>=60, C>=40, D>=6, E<6。"""
    if total >= 80:
        return "A", "极度低估，可以加大仓位"
    if total >= 60:
        return "B", "低估，加大定投"
    if total >= 40:
        return "C", "中性，正常定投"
    if total >= 6:
        return "D", "估值偏高，维持小额定投，不重仓"
    return "E", "极度高估，谨慎，减少加仓"


def quote_session_meta(ticker):
    """雅虎 chart meta: 最近完成会话的收盘价与时间戳。

    K线末根可能滞后（最新交易日K线未入库），而 meta.regularMarketPrice
    始终是最近一个完成会话的收盘价，带正确时间戳。
    """
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(ticker)}?range=1mo&interval=1d")
    d = http_get_json(url)
    meta = d["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    ts = meta.get("regularMarketTime")
    if price is None or ts is None:
        return None, None
    as_of = str(datetime.fromtimestamp(ts, timezone.utc).date())
    return float(price), as_of


def fetch_market(ticker):
    if yf is None:
        raise RuntimeError("缺少 yfinance 依赖")
    df = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=False)
    if df is None or len(df) < 200:
        raise RuntimeError(f"{ticker} 历史数据不足: {0 if df is None else len(df)} 条")
    close = df["Close"].dropna()
    price, as_of = quote_session_meta(ticker)
    if price is None:
        price = float(close.iloc[-1])
        as_of = str(close.index[-1].date())
        log(f"{ticker} 使用K线末根作为收盘价: {price:.2f} ({as_of})")
    else:
        bar_price = float(close.iloc[-1])
        if abs(bar_price - price) / price > 0.001:
            log(f"{ticker} K线末根({bar_price:.2f})与meta收盘({price:.2f})不一致，采用meta")
    ma200 = float(close.tail(200).mean())
    dev_pct = (price - ma200) / ma200 * 100
    return price, ma200, dev_pct, as_of


def fetch_vix():
    if yf is None:
        raise RuntimeError("缺少 yfinance 依赖")
    df = yf.Ticker("^VIX").history(period="5d", interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("VIX 数据获取失败")
    return float(df["Close"].dropna().iloc[-1])


def fetch_danjuan_percentiles():
    """蛋卷公开接口：一次返回全部指数估值，取 NDX/SP500 的 pe_percentile。"""
    headers = {**HEADERS, "Referer": DANJUAN_REFERER}
    r = http_get(DANJUAN_URL, headers=headers)
    try:
        data = r.json()
    except ValueError as e:
        raise RuntimeError(f"蛋卷接口返回非JSON: {e}")
    if not isinstance(data, dict):
        raise RuntimeError(f"蛋卷接口返回结构异常: {type(data).__name__}")
    items = (data.get("data") or {}).get("items") or []
    out = {}
    for it in items:
        code = it.get("index_code")
        if code in ("NDX", "SP500"):
            pct = it.get("pe_percentile")
            if pct is None:
                raise RuntimeError(f"蛋卷接口 {code} 缺少 pe_percentile")
            out[code] = {
                "pe": it.get("pe"),
                "pe_percentile": float(pct),
                "date": it.get("date"),
            }
    if "NDX" not in out or "SP500" not in out:
        raise RuntimeError("蛋卷接口缺失 NDX/SP500 数据")
    return out


def _shiller_download_url():
    page = http_get(SHILLER_PAGE, timeout=60).text
    m = re.search(r'href="([^"]*ie_data[^"]*)"', page)
    if not m:
        return SHILLER_YALE_MIRROR
    url = m.group(1)
    if url.startswith("//"):
        url = "https:" + url
    return url


def fetch_shiller_pe_series():
    """Shiller 官方月度数据：PE = S&P Composite Price / Earnings，返回含 dt/pe 的 DataFrame。"""
    if pd is None:
        raise RuntimeError("缺少 pandas 依赖")
    try:
        url = _shiller_download_url()
    except Exception as e:
        log(f"解析 shillerdata.com 下载链接失败，回退 Yale 镜像: {e}")
        url = SHILLER_YALE_MIRROR
    r = http_get(url, timeout=SHILLER_TIMEOUT)
    try:
        raw = pd.read_excel(BytesIO(r.content), header=None, sheet_name="Data")
    except Exception:
        raw = pd.read_excel(BytesIO(r.content), header=None, sheet_name=0)
    header_idx = None
    for i in range(min(20, len(raw))):
        row_vals = [str(x) for x in raw.iloc[i].tolist()]
        if (
            len(row_vals) > 3
            and row_vals[0].strip().upper() == "DATE"
            and row_vals[1].strip() == "P"
            and row_vals[3].strip() == "E"
        ):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("无法定位 Shiller 数据表头")
    df = raw.iloc[header_idx + 1:].copy()
    df = df[[0, 1, 3]]
    df.columns = ["date", "price", "earnings"]
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["earnings"] = pd.to_numeric(df["earnings"], errors="coerce")
    df = df.dropna(subset=["price", "earnings"])

    def to_month_start(v):
        try:
            s = f"{float(v):.2f}"
            y, m = s.split(".")
            return datetime(int(y), int(m), 1)
        except Exception:
            return None

    df["dt"] = df["date"].apply(to_month_start)
    df = df.dropna(subset=["dt"])
    df["pe"] = df["price"] / df["earnings"]
    latest = df["dt"].max()
    cutoff = latest.replace(year=latest.year - SHILLER_YEARS)
    df = df[df["dt"] >= cutoff].sort_values("dt")
    if len(df) < 60:
        raise RuntimeError(f"Shiller PE 序列不足10年: {len(df)} 个月")
    return df


def percentile_of_series(series, current):
    """按文档定义：历史10年PE序列中，小于 current 的数量 / 总样本数量。"""
    return float((series < current).mean())


def build_record(name, price, ma200, dev_pct, pe_percentile, vix, extra=None):
    pe_score, ma_score, vix_score, total_score = compute_scores(pe_percentile, dev_pct, vix)
    level, advice = level_and_advice(total_score)
    rec = {
        "name": name,
        "price": round(price, 2),
        "ma200": round(ma200, 2),
        "dev_pct": round(dev_pct, 2),
        "pe_percentile": round(pe_percentile, 4),
        "vix": round(vix, 2),
        "pe_score": pe_score,
        "ma_score": ma_score,
        "vix_score": vix_score,
        "total_score": total_score,
        "level": level,
        "advice": advice,
    }
    if extra:
        rec.update(extra)
    return rec


def render_html(date_str, ndx, spx, note_lines=None):
    def row(label, value):
        return ("<tr>"
                f"<th style='border:1px solid #e3e6ea;padding:8px 10px;font-size:14px;"
                f"background:#f0f2f5;text-align:left;width:46%'>{label}</th>"
                f"<td style='border:1px solid #e3e6ea;padding:8px 10px;font-size:14px;"
                f"text-align:right;font-variant-numeric:tabular-nums'>{value}</td>"
                "</tr>")

    def block(sym):
        grade_bg, grade_fg = GRADE_STYLE[sym["level"]]
        badge = (f"<span style='display:inline-block;min-width:28px;text-align:center;"
                 f"font-weight:700;border-radius:4px;padding:2px 8px;"
                 f"background:{grade_bg};color:{grade_fg}'>{sym['level']}</span>")
        return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
style="width:100%;max-width:960px;background:#ffffff;border:1px solid #e3e6ea;
border-radius:10px;margin:0 auto 24px"><tr><td style="padding:20px 24px">
  <h2 style="font-size:18px;margin:0 0 14px;border-left:4px solid #2f6fed;
padding-left:10px;color:#222">{sym['name']}</h2>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td width="50%" valign="top" style="padding-right:10px">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="font-size:13px;color:#444;font-weight:600;text-align:left;
padding-bottom:6px">原始输入数据</td></tr>
          {row("行情日期", sym.get("as_of", "-"))}
          {row("指数收盘价格", f"{sym['price']:,.2f}")}
          {row("MA200", f"{sym['ma200']:,.2f}")}
          {row("MA200偏离度(%)", f"{sym['dev_pct']:.2f}")}
          {row("PE十年百分位", f"{sym['pe_percentile']:.2f}")}
          {row("VIX恐慌指数", f"{sym['vix']:.2f}")}
        </table>
      </td>
      <td width="50%" valign="top" style="padding-left:10px">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="font-size:13px;color:#444;font-weight:600;text-align:left;
padding-bottom:6px">评分结果</td></tr>
          {row("PE得分(满分30)", f"{sym['pe_score']:.2f}")}
          {row("MA200得分(满分40)", f"{sym['ma_score']:.2f}")}
          {row("VIX得分(满分30)", f"{sym['vix_score']:.2f}")}
          {row("综合评分(满分100)", f"{sym['total_score']:.2f}")}
          {row("投资等级", badge)}
          {row("投资建议", f"<span style='text-align:left'>{sym['advice']}</span>")}
        </table>
      </td>
    </tr>
  </table>
</td></tr></table>"""

    note = "数据来源：Yahoo Finance（行情）、蛋卷基金（PE百分位）、Shiller（标普500辅助对比）。本页面仅供参考，不构成投资建议。"
    if note_lines:
        note += "<br>" + "<br>".join(note_lines)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股指数每日评分｜NDX &amp; SPX</title>
</head>
<body style="margin:0;padding:0;background:#f5f6f8">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#f5f6f8">
<tr><td align="center" style="padding:24px 8px">
  <h1 style="text-align:center;font-size:24px;margin:0 0 6px;color:#222;
font-family:'PingFang SC','Microsoft YaHei',-apple-system,sans-serif">
美股指数每日评分｜NDX &amp; SPX</h1>
  <div style="text-align:center;color:#666;margin:0 0 24px;font-size:14px">生成日期：{date_str}</div>
  {block(ndx)}
  {block(spx)}
  <div style="color:#888;font-size:12px;text-align:center;margin-top:20px;line-height:1.8;
font-family:'PingFang SC','Microsoft YaHei',-apple-system,sans-serif">{note}</div>
</td></tr>
</table>
</body>
</html>"""


def selftest():
    # 主样例（新公式期望值）
    pe, ma, vix, total = compute_scores(0.88, 7.96, 18.87)
    level, advice = level_and_advice(total)
    exp = (3.6, 6.76, 18.57, 28.93)
    assert (pe, ma, vix, total) == exp, f"样例不符: {(pe, ma, vix, total)} != {exp}"
    assert level == "D" and advice == "估值偏高，维持小额定投，不重仓", (level, advice)

    # 极端边界
    assert compute_scores(1.0, 20.0, 10.0) == (0.0, 0.72, 4.83, 5.55)
    assert compute_scores(0.0, -20.0, 60.0) == (30.0, 39.28, 30.0, 99.28)
    assert compute_scores(0.5, 0.0, 15.0) == (15.0, 20.0, 13.6, 48.6)

    # 等级边界
    assert level_and_advice(80.0)[0] == "A"
    assert level_and_advice(79.99)[0] == "B"
    assert level_and_advice(60.0)[0] == "B"
    assert level_and_advice(59.99)[0] == "C"
    assert level_and_advice(40.0)[0] == "C"
    assert level_and_advice(39.99)[0] == "D"
    assert level_and_advice(6.0)[0] == "D"
    assert level_and_advice(5.99)[0] == "E"

    # 百分位函数
    if pd is not None:
        s = pd.Series([10.0, 20.0, 30.0])
        assert percentile_of_series(s, 10.0) == 0.0
        assert percentile_of_series(s, 20.0) == 1.0 / 3.0
        assert percentile_of_series(s, 30.0) == 2.0 / 3.0
        assert percentile_of_series(s, 99.0) == 1.0

    # HTML渲染冒烟
    rec = build_record("测试指数", 10000.0, 9000.0, 5.0, 0.5, 18.0,
                       {"as_of": "2026-08-14"})
    pe2, ma2, vx2, tot2 = compute_scores(0.5, 5.0, 18.0)
    assert rec["pe_score"] == pe2 and rec["ma_score"] == ma2
    assert rec["vix_score"] == vx2 and rec["total_score"] == tot2
    assert rec["as_of"] == "2026-08-14"
    html = render_html("2026-01-01", rec, rec)
    assert "美股指数每日评分" in html
    assert "生成日期：2026-01-01" in html
    assert "10,000.00" in html
    assert "#fff9c4" in html
    assert "2026-08-14" in html

    print("selftest PASS: 改进公式(S型MA200+对数VIX)、等级边界、百分位、HTML渲染均与文档一致")
    return 0


def main():
    parser = argparse.ArgumentParser(description="美股指数定投评分系统")
    parser.add_argument("--selftest", action="store_true", help="运行公式自检后退出")
    args = parser.parse_args()
    if args.selftest:
        return selftest()

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"开始生成 {date_str} 评分报告")

    ndx_price, ndx_ma200, ndx_dev, ndx_asof = with_retry(fetch_market, "^NDX")
    log(f"NDX 价格={ndx_price:.2f} MA200={ndx_ma200:.2f} 偏离度={ndx_dev:.2f}% 行情日期={ndx_asof}")
    spx_price, spx_ma200, spx_dev, spx_asof = with_retry(fetch_market, "^GSPC")
    log(f"SPX 价格={spx_price:.2f} MA200={spx_ma200:.2f} 偏离度={spx_dev:.2f}% 行情日期={spx_asof}")
    vix = with_retry(fetch_vix)
    log(f"VIX={vix:.2f}")

    danjuan = fetch_danjuan_percentiles()
    ndx_pct = danjuan["NDX"]["pe_percentile"]
    spx_pct = danjuan["SP500"]["pe_percentile"]
    log(f"蛋卷百分位 NDX={ndx_pct:.4f} SP500={spx_pct:.4f}")

    spx_shiller_pct = None
    note_lines = []
    try:
        shiller = fetch_shiller_pe_series()
        current_pe = float(shiller["pe"].iloc[-1])
        spx_shiller_pct = percentile_of_series(shiller["pe"], current_pe)
        log(f"Shiller 辅助: 样本={len(shiller)} 当前PE={current_pe:.2f} 百分位={spx_shiller_pct:.4f}")
    except Exception as e:
        log(f"警告: Shiller 辅助数据获取失败，跳过（不阻断评分）: {e}")
        note_lines.append("Shiller 辅助数据本次获取失败，已跳过。")

    ndx = build_record("纳斯达克100（NDX）", ndx_price, ndx_ma200, ndx_dev, ndx_pct, vix,
                       {"pe_percentile_source": "danjuan", "as_of": ndx_asof})
    spx = build_record("标普500（SPX）", spx_price, spx_ma200, spx_dev, spx_pct, vix,
                       {"pe_percentile_source": "danjuan", "as_of": spx_asof,
                        "shiller_pe_percentile": round(spx_shiller_pct, 4) if spx_shiller_pct is not None else None})

    as_of_date = date.fromisoformat(min(ndx_asof, spx_asof))
    if (date.today() - as_of_date).days > 4:
        log(f"警告: 行情数据日期 {as_of_date} 距今超过4天，可能数据源滞后")
        note_lines.append(f"行情日期 {as_of_date} 距今超过4天，数据源可能滞后。")

    payload = {"date": date_str, "ndx": ndx, "spx": spx}
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    html = render_html(date_str, ndx, spx, note_lines)
    with open("result.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    log("完成: result.json / result.html 已生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())