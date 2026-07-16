#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄金多因子看板 · 每日数据刷新脚本(GitHub Actions 每日19:00自动运行,无手动触发)

流程:
  1. 从 ETA 拉取六大要素对应的原始数据(chart_id,通过 eta_client.py 实测可用)
  2. 计算衍生指标(近期变化、z-score、斜率)
  3. 按 v1 打分规则把每个指标的信号映射成 0-10 分,加权得到每个要素在
     短期/中短期/中期 三个维度的分数
  4. 用同一套 direction 规则,把每个指标的变化幅度模板化成"趋势+利多/利空"的
     文字解读(build_indicators/build_narrative/build_takeaway),保证评分和
     文字解读不会互相矛盾
  5. 按固定权重表(usd/rate/etf/cb/vol/ta)合成三个维度的综合分
  6. 写回 gold-dashboard/data.json

⚠️ 文字解读是规则模板生成,不是 AI 写的,所以不会有"信号高度一致"这种需要
   综合判断的洞察力,只会客观描述"近X期怎么变+对黄金利多/利空/中性"。
"""

import os
import sys
import json
import time
import datetime
import numpy as np
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(REPO_ROOT, "data.json")

ETA_APPID = os.environ.get("ETA_APPID", "")
ETA_SECRET = os.environ.get("ETA_SECRET", "")


# ============================================================
# 1. ETA 数据抓取 —— 占位符,等实际脚本接入
# ============================================================

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eta_client import get_chart_detail  # 实测可用,来自 eta-chart-fetcher skill


def fetch_chart(chart_id: str) -> dict:
    """
    按 chart_id 拉取图表完整历史,返回 {EdbName: {"dates":[...], "values":[...], "edb_code": str}}
    dates/values 按时间正序排列(原始 DataList 顺序已确认是正序,这里仍保险起见排一次序)。
    """
    resp = get_chart_detail(chart_id=chart_id)
    if resp.get("Ret") != 200:
        raise RuntimeError(f"chart_id={chart_id} 请求失败: {resp.get('Msg')}")

    out = {}
    for edb in resp.get("Data", {}).get("EdbInfoList", []):
        name = edb.get("EdbName", "")
        rows = edb.get("DataList", []) or []
        rows_sorted = sorted(rows, key=lambda r: r.get("DataTime", ""))
        out[name] = {
            "dates": [r["DataTime"] for r in rows_sorted],
            "values": [r["Value"] for r in rows_sorted],
            "edb_code": edb.get("EdbCode", ""),
        }
    return out


def last_n(series, n=10):
    """取一个 {"dates":[],"values":[]} 结构最近 n 期的 values 数组"""
    return series["values"][-n:]


# ============================================================
# 2. 通用信号处理工具
# ============================================================

def pct_change(values, i=-1, j=0):
    if values[j] == 0:
        return 0.0
    return (values[i] - values[j]) / abs(values[j]) * 100


def zscore_latest(values, window=None):
    arr = np.array(values[-window:] if window else values, dtype=float)
    std = arr.std()
    if std == 0:
        return 0.0
    return (arr[-1] - arr.mean()) / std


def slope(values, n=None):
    arr = np.array(values[-n:] if n else values, dtype=float)
    x = np.arange(len(arr))
    if len(arr) < 2:
        return 0.0
    return np.polyfit(x, arr, 1)[0]


def signal_to_score(signal, direction=1, scale=2.0, cap=2.5):
    """
    把一个标准化信号(通常是 z-score 或归一化变化率)映射成对 5 分基准的加减分。
    direction: 1 表示信号越高越利多黄金,-1 表示反向(如美元走强利空黄金)
    scale: 信号放大系数
    cap: 单个信号最多能拉动几分,避免单一指标极端值失控
    """
    delta = np.clip(direction * signal * scale, -cap, cap)
    return 5.0 + delta


def weighted_score(component_scores_and_weights):
    """component_scores_and_weights: [(score, weight), ...]"""
    total_w = sum(w for _, w in component_scores_and_weights)
    if total_w == 0:
        return 5.0
    return sum(s * w for s, w in component_scores_and_weights) / total_w


def clip_score(v):
    return float(np.clip(round(v, 2), 0, 10))


def ema(series, period):
    alpha = 2 / (period + 1)
    e = np.zeros_like(series, dtype=float)
    e[0] = series[0]
    for i in range(1, len(series)):
        e[i] = alpha * series[i] + (1 - alpha) * e[i - 1]
    return e


def rsi(series, period=14):
    series = np.array(series, dtype=float)
    deltas = np.diff(series)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.zeros(len(series))
    avg_loss = np.zeros(len(series))
    if len(series) <= period:
        return np.full(len(series), 50.0)
    avg_gain[period] = gains[:period].mean()
    avg_loss[period] = losses[:period].mean()
    for i in range(period + 1, len(series)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period
    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.nan), where=avg_loss != 0)
    vals = 100 - 100 / (1 + rs)
    vals[:period] = 50.0
    return np.nan_to_num(vals, nan=50.0)


def macd_histogram(series):
    series = np.array(series, dtype=float)
    macd_line = ema(series, 12) - ema(series, 26)
    signal_line = ema(macd_line, 9)
    return macd_line - signal_line


# ============================================================
# 3. 六大要素的打分规则 (v1 —— 尽量贴近今天的分析逻辑,后续可调参数)
# ============================================================
# 每个要素的 score_xxx() 函数接收该要素相关的原始序列,
# 返回 {"短期": x, "中短期": y, "中期": z}

def score_usd(raw):
    """
    raw 期望包含: dxy(日度), y2(日度), surprise(日度), pmi_diff(月度), fiscal(月度)
    方向: 美元走弱 -> 利多黄金 (direction=-1,因为 dxy 上涨是"利空"信号)
    """
    dxy = raw["dxy"]; y2 = raw["y2"]; surprise = raw["surprise"]
    pmi_diff = raw["pmi_diff"]; fiscal = raw["fiscal"]

    s_short = weighted_score([
        (signal_to_score(pct_change(dxy, -1, -4), direction=-1, scale=3.0), 0.35),
        (signal_to_score((y2[-1]-y2[-4])*100, direction=-1, scale=0.15), 0.25),
        (signal_to_score(surprise[-1]-surprise[-3], direction=-1, scale=0.25), 0.40),
    ])
    s_mid = weighted_score([
        (signal_to_score(pct_change(dxy), direction=-1, scale=3.0), 0.4),
        (signal_to_score((y2[-1]-y2[0])*100, direction=-1, scale=0.15), 0.3),
        (signal_to_score(surprise[-1]-surprise[0], direction=-1, scale=0.2), 0.3),
    ])
    s_long = weighted_score([
        (signal_to_score(pmi_diff[-1]-pmi_diff[-4], direction=-1, scale=1.2), 0.5),
        (signal_to_score(zscore_latest(fiscal), direction=1, scale=1.0), 0.5),
    ])
    return {"短期": clip_score(s_short), "中短期": clip_score(s_mid), "中期": clip_score(s_long)}


def score_rate(raw):
    y2 = raw["y2"]; y10 = raw["y10"]; surprise = raw["surprise"]
    cpi = raw["cpi"]; issuance = raw["issuance"]; oil = raw["oil"]

    s_short = weighted_score([
        (signal_to_score((y2[-1]-y2[-4])*100, direction=-1, scale=0.15), 0.35),
        (signal_to_score(surprise[-1]-surprise[-3], direction=-1, scale=0.25), 0.35),
        (signal_to_score(oil[-1]-max(oil), direction=-1, scale=0.06), 0.30),
    ])
    s_mid = weighted_score([
        (signal_to_score((y10[-1]-y10[0])*100, direction=-1, scale=0.12), 0.4),
        (signal_to_score(cpi[-1]-np.mean(cpi), direction=-1, scale=1.0), 0.3),
        (signal_to_score(surprise[-1]-surprise[0], direction=-1, scale=0.15), 0.3),
    ])
    s_long = weighted_score([
        (signal_to_score(zscore_latest(issuance), direction=-1, scale=1.0), 0.55),
        (signal_to_score(oil[-1]-max(oil), direction=-1, scale=0.05), 0.45),
    ])
    return {"短期": clip_score(s_short), "中短期": clip_score(s_mid), "中期": clip_score(s_long)}


def score_etf(raw):
    total_etf = raw["total_etf"]; ishares = raw["ishares"]; spdr = raw["spdr"]

    s_short = weighted_score([
        (signal_to_score(total_etf[-1]-total_etf[-4], direction=1, scale=0.00003), 0.4),
        (signal_to_score(ishares[-1]-ishares[-4], direction=1, scale=1.5), 0.3),
        (signal_to_score(spdr[-1]-spdr[-4], direction=1, scale=1.5), 0.3),
    ])
    s_mid = weighted_score([
        (signal_to_score(pct_change(total_etf), direction=1, scale=8.0), 0.5),
        (signal_to_score(pct_change(ishares), direction=1, scale=1.2), 0.25),
        (signal_to_score(pct_change(spdr), direction=1, scale=1.2), 0.25),
    ])
    # 中期更多依赖结构性叙事(3.0阶段逻辑),用总持仓的中期斜率做代理信号
    s_long = weighted_score([
        (signal_to_score(slope(total_etf), direction=1, scale=0.0002), 1.0),
    ])
    s_long = max(s_long, 6.5)  # 中期结构性支撑下限,按今天的判断人工设一个底
    return {"短期": clip_score(s_short), "中短期": clip_score(s_mid), "中期": clip_score(s_long)}


def score_cb(raw):
    """低频要素:短期/中短期直接复用中期状态(不产生新增信息)"""
    pboc = raw["pboc"]  # 月度
    global_cb = raw["global_cb"]  # 季度

    recent_incr = np.mean(np.diff(pboc)[-3:])
    prior_incr = np.mean(np.diff(pboc)[:-3]) if len(pboc) > 4 else recent_incr
    accel_ratio = recent_incr / prior_incr if prior_incr != 0 else 1.0

    s_long = weighted_score([
        (signal_to_score(np.log(max(accel_ratio, 0.1)), direction=1, scale=2.0), 0.6),
        (signal_to_score((global_cb[-1]-global_cb[-2]), direction=1, scale=0.02), 0.4),
    ])
    s_state = s_long  # 短期/中短期与中期共用同一状态
    return {"短期": clip_score(s_state), "中短期": clip_score(s_state), "中期": clip_score(s_long)}


def score_vol(raw):
    gvz = raw["gvz"]; corr = raw["corr"]; nontrend = raw["nontrend"]

    s_short = weighted_score([
        (signal_to_score(corr[-1]-corr[-3], direction=1, scale=8.0), 0.4),
        (signal_to_score(nontrend[-1]-nontrend[-4], direction=1, scale=0.02), 0.4),
        (signal_to_score(gvz[-1]-gvz[-4], direction=-1, scale=0.3), 0.2),
    ])
    s_mid = weighted_score([
        (signal_to_score(pct_change(gvz), direction=-1, scale=1.2), 0.5),
        (signal_to_score(corr[-1]-corr[0], direction=1, scale=5.0), 0.5),
    ])
    s_long = weighted_score([
        (signal_to_score(pct_change(gvz), direction=-1, scale=0.8), 0.5),
        (signal_to_score(zscore_latest(nontrend), direction=1, scale=0.8), 0.5),
    ])
    return {"短期": clip_score(s_short), "中短期": clip_score(s_mid), "中期": clip_score(s_long)}


def score_ta(raw):
    close = raw["close"]; ma20 = raw["ma20"]; ma60 = raw["ma60"]
    rsi = raw["rsi"]; macd_hist = raw["macd_hist"]; cot = raw["cot"]

    s_short = weighted_score([
        (signal_to_score(macd_hist[-1]-macd_hist[-3], direction=1, scale=0.15), 0.4),
        (signal_to_score(rsi[-1]-30, direction=1, scale=0.15), 0.3),
        (signal_to_score(cot[-1]-cot[-2], direction=1, scale=0.0006), 0.3),
    ])
    s_mid = weighted_score([
        (signal_to_score(rsi[-1]-50, direction=1, scale=0.15), 0.4),
        (signal_to_score(macd_hist[-1], direction=1, scale=0.03), 0.3),
        (signal_to_score(slope(cot[-4:]), direction=1, scale=0.002), 0.3),
    ])
    gap_now = ma20[-1]-ma60[-1]
    gap_prev = ma20[-11]-ma60[-11] if len(ma20) > 10 else gap_now
    s_long = weighted_score([
        (signal_to_score(gap_now-gap_prev, direction=1, scale=0.02), 0.6),
        (signal_to_score(zscore_latest(cot), direction=1, scale=0.5), 0.4),
    ])
    return {"短期": clip_score(s_short), "中短期": clip_score(s_mid), "中期": clip_score(s_long)}


# ============================================================
# 3.5 文字解读生成(规则模板,和打分用同一套 direction 保持一致)
# ============================================================

VERDICT_LABELS = [
    (7.0, "偏多"), (5.5, "中性偏多"), (4.5, "中性"), (3.0, "中性偏空"), (-1, "偏空"),
]


def verdict_text(score):
    for threshold, label in VERDICT_LABELS:
        if score >= threshold:
            return label
    return "偏空"


def fmt_num(value, unit=""):
    if abs(value) >= 1000:
        s = f"{value:,.1f}"
    elif abs(value) >= 100:
        s = f"{value:.1f}"
    else:
        s = f"{value:.2f}"
    return f"{s}{unit}"


def trend_phrase(values, direction, short_n=3, long_n=10):
    """
    根据一段时间序列,生成"近X期怎么变+对黄金利多/利空/中性"的模板句子。
    direction: 1 表示该指标数值越高对黄金越有利,-1 表示相反。
    返回 (phrase, tag),tag ∈ {bull, bear, flat}
    """
    n = len(values)
    short_n = max(1, min(short_n, n - 1))
    long_n = max(short_n, min(long_n, n - 1))

    short_chg = values[-1] - values[-1 - short_n]
    long_chg = values[-1] - values[-1 - long_n]
    window = values[-(long_n + 1):]
    scale_ref = float(np.std(window)) or (abs(values[-1]) * 0.01 + 1e-6)

    def qualify(chg):
        if abs(chg) < scale_ref * 0.1:
            return "基本持平"
        word = "走高" if chg > 0 else "走低"
        ratio = abs(chg) / scale_ref
        if ratio > 1.5:
            return f"明显{word}"
        if ratio > 0.5:
            return word
        return f"小幅{word}"

    short_desc = qualify(short_chg)
    long_desc = qualify(long_chg)
    phrase = f"近{long_n}期{long_desc},近{short_n}日{short_desc}"

    net_chg = long_chg if abs(long_chg) > scale_ref * 0.1 else short_chg
    if abs(net_chg) < scale_ref * 0.15:
        tag = "flat"
    else:
        tag = "bull" if (net_chg > 0) == (direction > 0) else "bear"
    return phrase, tag


def build_indicators(items):
    """items: [(name, series, direction, unit), ...] -> data.json 的 indicators 列表"""
    out = []
    for name, series, direction, unit in items:
        phrase, tag = trend_phrase(series, direction)
        out.append({
            "name": name,
            "latest": fmt_num(series[-1], unit),
            "unit": "",
            "trend": phrase,
            "tag": tag,
        })
    return out


def build_narrative(scores, indicators):
    """按每个时间维度的评分 + 该维度下多空标签占优的指标,生成一段模板解读"""
    narrative = {}
    for h in ["短期", "中短期", "中期"]:
        score = scores[h]
        bulls = [i["name"] for i in indicators if i["tag"] == "bull"]
        bears = [i["name"] for i in indicators if i["tag"] == "bear"]
        segs = []
        if bulls:
            segs.append(f"{'、'.join(bulls[:3])}偏多")
        if bears:
            segs.append(f"{'、'.join(bears[:3])}偏空")
        detail = ";".join(segs) if segs else "各指标信号不明显,以震荡为主"
        narrative[h] = f"{detail}。综合评分{score:.2f}分,判断为{verdict_text(score)}。"
    return narrative


def build_takeaway(scores):
    horizons = ["短期", "中短期", "中期"]
    vals = [scores[h] for h in horizons]
    if max(vals) - min(vals) < 0.8:
        return f"三个维度评分接近({min(vals):.1f}-{max(vals):.1f}),方向一致"
    best = horizons[vals.index(max(vals))]
    worst = horizons[vals.index(min(vals))]
    return f"{best}相对更看多({max(vals):.1f}分),{worst}相对偏谨慎({min(vals):.1f}分)"


# ============================================================
# 3.7 用 Claude API 生成解读文字(有 ANTHROPIC_API_KEY 时优先用这个,
#     没有配置或调用失败时,自动退回 3.6 的规则模板,保证脚本不会中断)
# ============================================================

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")


def generate_ai_narrative(factor_name, eyebrow, scores, indicators):
    """
    把已经算好的事实(评分、每个指标的涨跌方向和多空标签)喂给 Claude,
    只让它负责组织语言、判断信号之间是否一致,不允许编造任何新数字。
    返回 {"短期":str, "中短期":str, "中期":str, "takeaway":str};失败返回 None。
    """
    if not ANTHROPIC_API_KEY:
        return None

    indicator_lines = "\n".join(
        f"- {i['name']}:最新值 {i['latest']},{i['trend']}({'利多' if i['tag']=='bull' else '利空' if i['tag']=='bear' else '中性'})"
        for i in indicators
    )
    prompt = f"""你是一位大宗商品/黄金研究员,要给"{eyebrow}·{factor_name}"这个黄金定价要素写三段简短解读,分别对应短期(1周)、中短期(2-3周)、中期(2-3个月)三个时间维度。

已知的评分(0-10分,分越高对黄金越利多):
短期 {scores['短期']:.2f} 分,中短期 {scores['中短期']:.2f} 分,中期 {scores['中期']:.2f} 分

已知的指标事实(不要编造任何新数字,只能用下面这些):
{indicator_lines}

写作要求:
1. 每个时间维度写1-2句话,指出该维度下哪些指标方向一致、哪些指标打架,并说明这对黄金的含义
2. 语言风格:简洁、专业,像研究员写报告,不要输出评分数字本身(评分已经在别处展示),但可以用"偏多""偏空""中性"这类判断词
3. 另外写一句"takeaway":一句话总结这个要素在三个维度上的整体状态,给要素卡片当摘要用
4. 只输出 JSON,不要有任何前后缀文字或 markdown 代码块标记,格式严格如下:
{{"短期": "...", "中短期": "...", "中期": "...", "takeaway": "..."}}
"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 700,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(text)
        for key in ("短期", "中短期", "中期", "takeaway"):
            if key not in result:
                raise ValueError(f"AI 返回缺少字段: {key}")
        return result
    except Exception as e:
        print(f"  [AI解读生成失败,退回模板] {factor_name}: {e}")
        return None


# ============================================================
# 3.6 每个要素的指标定义(与打分用的 direction 保持一致)
# ============================================================

def describe_usd(raw):
    return build_indicators([
        ("美元指数(DXY)", raw["dxy"], -1, ""),
        ("美2年期国债收益率", raw["y2"], -1, "%"),
        ("美国经济惊喜指数", raw["surprise"], -1, ""),
        ("美欧制造业PMI之差", raw["pmi_diff"], 1, ""),
        ("美财政赤字12MMA", raw["fiscal"], -1, ""),
    ])


def describe_rate(raw):
    return build_indicators([
        ("美2年期收益率", raw["y2"], -1, "%"),
        ("美10年期收益率", raw["y10"], -1, "%"),
        ("美国经济惊喜指数", raw["surprise"], -1, ""),
        ("CPI同比", raw["cpi"], -1, "%"),
        ("美债月度发行量(3MMA)", raw["issuance"], -1, ""),
        ("WTI原油", raw["oil"], -1, ""),
    ])


def describe_etf(raw):
    return build_indicators([
        ("全球已知ETF黄金总持仓", raw["total_etf"], 1, ""),
        ("iShares黄金ETF持仓", raw["ishares"], 1, ""),
        ("SPDR黄金ETF持仓", raw["spdr"], 1, ""),
    ])


def describe_cb(raw):
    return build_indicators([
        ("中国央行黄金储备(月度)", raw["pboc"], 1, ""),
        ("全球央行黄金储备变化(季度)", raw["global_cb"], 1, ""),
    ])


def describe_vol(raw):
    return build_indicators([
        ("黄金VIX(GVZ)", raw["gvz"], -1, ""),
        ("金价与GVZ滚动相关性", raw["corr"], 1, ""),
        ("金价偏离趋势项", raw["nontrend"], 1, ""),
    ])


def describe_ta(raw):
    ma_gap = [a - b for a, b in zip(raw["ma20"], raw["ma60"])]
    return build_indicators([
        ("RSI(14)", raw["rsi"], 1, ""),
        ("MACD柱状图", raw["macd_hist"], 1, ""),
        ("COMEX黄金投机净多头(COT)", raw["cot"], 1, ""),
        ("MA20-MA60缺口", ma_gap, 1, ""),
    ])


def build_range_note(close_full, latest_price):
    """
    用技术面信号给一个支撑/阻力区间判断,而不是简单的百分位描述:
    - 20日区间高低点作为近期实际测试过的支撑阻力
    - 最近价格所在的整数关口(每100美元一档)作为心理关口
    """
    last20 = close_full[-20:]
    support_tested = min(last20)
    resistance_tested = max(last20)
    lower_round = int(latest_price // 100 * 100)
    upper_round = lower_round + 100
    return (
        f"技术面支撑{lower_round},阻力{upper_round}"
        f"(20日实测区间{support_tested:.0f}-{resistance_tested:.0f})"
    )


# ============================================================
# 4. 权重表 (固定,和今天在对话里定的一致)
# ============================================================

WEIGHTS = {
    "短期":   {"usd": 0.175, "rate": 0.175, "etf": 0.175, "cb": 0.0,    "vol": 0.175, "ta": 0.30},
    "中短期": {"usd": 0.20,  "rate": 0.20,  "etf": 0.20,  "cb": 0.0,    "vol": 0.20,  "ta": 0.20},
    "中期":   {"usd": 1/6,   "rate": 1/6,   "etf": 1/6,   "cb": 1/6,    "vol": 1/6,   "ta": 1/6},
}


def compute_composite(scores_by_factor):
    composite = {}
    for horizon, w in WEIGHTS.items():
        total = sum(scores_by_factor[f][horizon] * w[f] for f in w)
        composite[horizon] = round(total, 2)
    return composite


# ============================================================
# 4.5 图表配置:每个要素需要哪些 chart_id / EdbName
# ============================================================

def build_usd_raw(charts):
    c1 = charts["C000022328"]; c2 = charts["C000028796"]
    c3 = charts["C000002428"]; c4 = charts["C000028933"]
    return {
        "dxy": last_n(c1["美元指数"]),
        "y2": last_n(c1["美国2年期国债收益率"]),
        "surprise": last_n(c2["美国经济惊喜指数"]),
        "pmi_diff": last_n(c3["美欧制造业PMI之差"]),
        "fiscal": last_n(c4["US Government Fiscal Deficit (negative indicates a surplus)”/12MMA"]),
    }


def build_rate_raw(charts):
    c1 = charts["C000046159"]; c2 = charts["C000022056"]
    c3 = charts["C000036664"]; c4 = charts["C000019592"]
    return {
        "y2": last_n(c1["美国2年期国债收益率"]),
        "y10": last_n(c2["10年期美国国债收益率"]),
        "surprise": last_n(c2["美国经济惊喜指数"]),
        "cpi": last_n(c1["美国/CPI同比"]),
        "issuance": last_n(c3["美债月度发行/3mma"]),
        "oil": last_n(c4["WTI原油期货价格"]),
    }


def build_etf_raw(charts):
    c1 = charts["C000043021"]; c2 = charts["C000036366"]
    return {
        "total_etf": last_n(c1["所有已知ETF黄金持仓"]),
        "ishares": last_n(c2["iShares黄金ETF持有量"]),
        "spdr": last_n(c2["SPDR黄金ETF持有量"]),
    }


def build_cb_raw(charts):
    c1 = charts["C000036590"]; c2 = charts["C000036481"]
    return {
        "pboc": last_n(c1["中国央行黄金储备（同花顺）"], n=10),
        "global_cb": last_n(c2["全球央行黄金储备变化"], n=10),
    }


def build_vol_raw(charts):
    c1 = charts["C000036726"]; c2 = charts["C000042106"]; c3 = charts["C000040679"]
    return {
        "gvz": last_n(c1["黄金VIX指数"]),
        "corr": last_n(c2["COMEX黄金价格Non-Trend/F0.02与黄金VIX指数90天滚动相关性"]),
        "nontrend": last_n(c3["COMEX黄金价格Non-Trend/F0.02（新）"]),
    }


def build_ta_raw(charts):
    c1 = charts["C000045793"]; c2 = charts["C000011395"]
    close_full = c1["comex黄金收盘价"]["values"]
    ma20_full = c1["comex黄金收盘价/20DMA"]["values"]
    ma60_full = c1["comex黄金收盘价/60DMA"]["values"]
    rsi_full = rsi(close_full, 14)
    macd_full = macd_histogram(close_full)
    return {
        "close": close_full[-15:],
        "ma20": ma20_full[-15:],
        "ma60": ma60_full[-15:],
        "rsi": list(rsi_full[-15:]),
        "macd_hist": list(macd_full[-15:]),
        "cot": last_n(c2["COMEX黄金投机净多头"]),
    }


CHART_IDS_NEEDED = [
    "C000022328", "C000028796", "C000002428", "C000028933",
    "C000046159", "C000022056", "C000036664", "C000019592",
    "C000043021", "C000036366",
    "C000036590", "C000036481",
    "C000036726", "C000042106", "C000040679",
    "C000045793", "C000011395",
]


def fetch_all_charts():
    """依次拉取所有需要的 chart_id,每次间隔 1 秒避免触发限频"""
    charts = {}
    for i, cid in enumerate(CHART_IDS_NEEDED):
        if i > 0:
            time.sleep(1.0)
        print(f"  拉取 {cid} ...")
        charts[cid] = fetch_chart(cid)
    return charts


# ============================================================
# 5. 主流程
# ============================================================

def main():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("[1/4] 拉取 ETA 图表数据...")
    charts = fetch_all_charts()

    print("[2/4] 组装原始数据...")
    raw_by_factor = {
        "usd": build_usd_raw(charts),
        "rate": build_rate_raw(charts),
        "etf": build_etf_raw(charts),
        "cb": build_cb_raw(charts),
        "vol": build_vol_raw(charts),
        "ta": build_ta_raw(charts),
    }

    print("[3/4] 计算评分与文字解读...")
    score_fns = {"usd": score_usd, "rate": score_rate, "etf": score_etf,
                 "cb": score_cb, "vol": score_vol, "ta": score_ta}
    describe_fns = {"usd": describe_usd, "rate": describe_rate, "etf": describe_etf,
                    "cb": describe_cb, "vol": describe_vol, "ta": describe_ta}

    scores = {}
    for key in score_fns:
        raw = raw_by_factor[key]
        sc = score_fns[key](raw)
        indicators = describe_fns[key](raw)
        scores[key] = sc
        data["factors"][key]["scores"] = sc
        data["factors"][key]["indicators"] = indicators

        ai_result = generate_ai_narrative(data["factors"][key]["name"], data["factors"][key]["eyebrow"], sc, indicators)
        if ai_result:
            data["factors"][key]["narrative"] = {h: ai_result[h] for h in ["短期", "中短期", "中期"]}
            data["factors"][key]["takeaway"] = ai_result["takeaway"]
        else:
            data["factors"][key]["narrative"] = build_narrative(sc, indicators)
            data["factors"][key]["takeaway"] = build_takeaway(sc)

    # 金价 + 点位判断(用技术分析模块已经拉到的完整收盘价历史)
    close_full = charts["C000045793"]["comex黄金收盘价"]["values"]
    latest_price = close_full[-1]
    prev_price = close_full[-2]

    print("[4/4] 写回 data.json...")
    data["composite"] = compute_composite(scores)
    data["asOf"] = datetime.date.today().isoformat()
    data["asOfLabel"] = datetime.date.today().strftime("%Y年%m月%d日")
    data["goldPrice"] = {
        "latest": round(latest_price, 1),
        "change1d": round(latest_price - prev_price, 1),
        "changePct1d": round((latest_price - prev_price) / prev_price * 100, 2),
        "rangeNote": build_range_note(close_full, latest_price),
    }

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[完成] data.json 已更新, composite={data['composite']}")


if __name__ == "__main__":
    main()
