# -*- coding: utf-8 -*-
"""用电速率估算与「还能撑多久」预测。

思路（用户确认的方案）：把每次查到的 fj_surplus 全量记录下来，
- 主策略：取最近 48h 窗口内、最后一次「充值跳变」之后的首尾两个样本，
  速率 = 电量差 / 时间差；
- 退化策略：窗口内电表读数没变化时，用最近 7 天的日均耗电 ÷ 24；
- 冷启动（样本不足或无正速率）：返回 None，由调用方决定是否按保守阈值告警。

直接运行本文件会跑一遍合成数据自测：python estimator.py
"""

from datetime import datetime, timedelta

RECHARGE_JUMP_KWH = 0.5      # 超过该增幅视为充值跳变
RECENT_WINDOW_HOURS = 48
FALLBACK_WINDOW_DAYS = 7


def _parse_ts(s):
    return datetime.fromisoformat(s)


def _cut_after_recharge(samples):
    """返回最后一次明显上涨（充值）之后的子序列。"""
    start = 0
    for i in range(1, len(samples)):
        if samples[i]["surplus"] > samples[i - 1]["surplus"] + RECHARGE_JUMP_KWH:
            start = i
    return samples[start:]


def _rate_from_segment(seg):
    if len(seg) < 2:
        return None
    dt_h = (_parse_ts(seg[-1]["ts"]) - _parse_ts(seg[0]["ts"])).total_seconds() / 3600.0
    drop = seg[0]["surplus"] - seg[-1]["surplus"]
    if dt_h <= 0.1 or drop <= 0:
        return None
    return drop / dt_h


def estimate_rate_kwh_per_hour(history, now=None):
    """返回每小时耗电速率（正数）；估不出来返回 None。"""
    now = now or datetime.now()
    if not history or len(history) < 2:
        return None
    hist = sorted(history, key=lambda s: s["ts"])

    recent = [s for s in hist
              if _parse_ts(s["ts"]) >= now - timedelta(hours=RECENT_WINDOW_HOURS)]
    rate = _rate_from_segment(_cut_after_recharge(recent))
    if rate:
        return rate

    week = [s for s in hist
            if _parse_ts(s["ts"]) >= now - timedelta(days=FALLBACK_WINDOW_DAYS)]
    return _rate_from_segment(_cut_after_recharge(week))


def hours_left(surplus, rate_kwh_per_hour):
    """剩余电量还能撑几小时；无法预测返回 None。"""
    if rate_kwh_per_hour is None or rate_kwh_per_hour <= 0:
        return None
    if surplus is None or surplus <= 0:
        return 0.0
    return surplus / rate_kwh_per_hour


def _parse_d(s):
    return datetime.strptime(str(s), "%Y-%m-%d").date()


def rate_from_daily_usage(daily, now=None, window_days=7, half_life_days=2.0):
    """按「数据中心每日用电量」估算速率，**指数加权**自适应：

    - 最近几天的权重高（半衰期默认 2 天）：入冬不开空调、放假回家等行为变化，
      速率会在 2~3 天内自动跟下来，而不是被旧数据拖住；
    - 缺记录的天按 0 度计重（学校只记非零天是常态）。
    daily: [{"date": "YYYY-MM-DD", "kwh": float}, ...]
    """
    if not daily:
        return None
    now = now or datetime.now()
    lookup = {}
    for item in daily:
        try:
            lookup[_parse_d(item["date"])] = float(item.get("kwh") or 0)
        except (KeyError, ValueError):
            continue
    w_sum = 0.0
    kwh_w = 0.0
    for age in range(window_days):
        d = now.date() - timedelta(days=age)
        w = 0.5 ** (age / half_life_days)
        kwh_w += lookup.get(d, 0.0) * w
        w_sum += w
    if w_sum <= 0:
        return None
    return (kwh_w / w_sum) / 24.0


def pessimistic_rate_kwh_per_hour(daily, now=None, window_days=7):
    """悲观速率 = max(EWMA 均值, 近 window_days 天最大单日 ÷ 24)。

    关机决策用它而不是均值：误关机的概率 = 某天用电超过近期最大单日的概率，
    很小；漏保护的代价由夜间提前窗口兜底。没有日数据时退化为均值。
    """
    now = now or datetime.now()
    avg = rate_from_daily_usage(daily, now=now, window_days=window_days)
    if not daily:
        return avg
    cutoff = now.date() - timedelta(days=window_days - 1)
    max_day = max((float(x.get("kwh") or 0) for x in daily
                   if x.get("date") and _parse_d(x["date"]) >= cutoff),
                  default=0.0)
    p = max(max_day / 24.0, avg or 0.0)
    return p or None


MAX_DECAY_HOURS = 48         # 余额长期不变时衰减上限，防止把历史陈账全算光


def last_balance_change_age_hours(history, now=None):
    """余额数值最后一次变化距今几小时（电表一天一更，该值代表数据陈旧度）。"""
    if not history:
        return None
    hist = sorted(history, key=lambda s: s["ts"])
    last_change = _parse_ts(hist[0]["ts"])
    for prev, cur in zip(hist, hist[1:]):
        if abs(cur["surplus"] - prev["surplus"]) > 1e-9:
            last_change = _parse_ts(cur["ts"])
    age = (now - last_change).total_seconds() / 3600.0
    return max(0.0, age)


def decayed_hours_left(history, surplus, rate, now=None):
    """把余额按「距上次电表刷新已消耗的量」折算后的可用小时（实时走表）。"""
    now = now or datetime.now()
    h = hours_left(surplus, rate)
    if h is None:
        return None
    age = last_balance_change_age_hours(history, now=now)
    if age:
        decay = min(age, MAX_DECAY_HOURS) * rate
        h = max(0.0, (surplus - decay) / rate)
    return h


def predict(history, current_surplus, daily_usage=None, now=None):
    """综合接口：返回 (rate, hours)。

    - 速率：本地余额差优先，退化为日用电 EWMA（自适应近期行为变化）；
    - 走表：余额按「上次变化至今」折算已用掉的电（实时衰减），倒计时随时间递减
      而不是干等电表一天一更。衰减上限 MAX_DECAY_HOURS。
    """
    now = now or datetime.now()
    rate = estimate_rate_kwh_per_hour(history, now=now)
    if not rate:
        rate = rate_from_daily_usage(daily_usage or [], now=now)
    return rate, decayed_hours_left(history, current_surplus, rate, now=now)


# ---------------- 自测 ----------------

def _self_test():
    base = datetime(2026, 10, 19, 12, 0, 0)

    def mk(hours_ago, surplus):
        t = base - timedelta(hours=hours_ago)
        return {"ts": t.isoformat(), "surplus": surplus, "update_time": None}

    # 1) 稳定速率：每 24h 用 8 度 → 1/3 度/小时
    hist = [mk(48, 30.0), mk(24, 22.0), mk(0, 14.0)]
    rate, h = predict(hist, 14.0, now=base)
    assert abs(rate - 8 / 24) < 1e-6, rate
    assert abs(h - 14 / (8 / 24)) < 1e-6, h

    # 2) 充值跳变：40h 前从 5 充到 40，之后正常消耗 → 速率只用充值后的段
    hist = [mk(48, 8.0), mk(40, 5.0), mk(39, 40.0), mk(15, 28.0), mk(0, 16.0)]
    rate, _ = predict(hist, 16.0, now=base)
    assert abs(rate - 24 / 39) < 1e-6, rate  # 充值后整段：40度→16度 / 39小时

    # 3) 冷启动：只有一个样本
    rate, h = predict([mk(0, 3.0)], 3.0, now=base)
    assert rate is None and h is None

    # 4) 近 48h 无变化（电表日更），退化为 7 天日均
    hist = [mk(120, 60.0), mk(72, 44.0), mk(30, 30.0), mk(1, 30.0)]
    rate, _ = predict(hist, 30.0, now=base)
    # 最近窗口 30h 内无下降 → 退化用 7 天窗口：(60-30)/ (120-1)h
    assert rate is not None and abs(rate - 30 / 119) < 1e-6, rate

    # 5) 用电量为 0 / 负预测
    assert hours_left(0.0, 0.5) == 0.0
    assert hours_left(2.0, None) is None

    # 6) 窗口内略有抖动（+0.1 度）不算充值：跨窗口平均
    hist = [mk(48, 20.0), mk(24, 10.05), mk(23.9, 10.15), mk(0, 0.15)]
    rate, _ = predict(hist, 0.15, now=base)
    assert rate is not None and rate > 0.3, rate

    # 7) 日用电 EWMA 种子：本地窗口无样本时退化使用；被旧的轻负载日拉低但为正
    daily = [{"date": "2026-10-13", "kwh": 6.0}, {"date": "2026-10-14", "kwh": 5.0},
             {"date": "2026-10-18", "kwh": 6.22}]
    rate = rate_from_daily_usage(daily, now=base)
    assert 0 < rate < 6.22 / 24, rate
    r2, h2 = predict([], 1.2, daily_usage=daily, now=base)
    assert r2 == rate and abs(h2 - 1.2 / rate) < 1e-6
    # 7b) 自适应：连续 7 天 8 度，最近 2 天降到 2 度 → 速率应明显回落（冬天关空调场景）
    hot = [{"date": (base - timedelta(days=a)).strftime("%Y-%m-%d"), "kwh": 8.0}
           for a in range(7)]
    cold = [{"date": d["date"], "kwh": (2.0 if a < 2 else 8.0)} for a, d in enumerate(hot)]
    r_hot = rate_from_daily_usage(hot, now=base)
    r_cold = rate_from_daily_usage(cold, now=base)
    assert abs(r_hot - 8 / 24) < 1e-9, r_hot
    assert r_cold < r_hot * 0.7, (r_hot, r_cold)  # 两天轻负载就明显回落（半衰期2天）
    # 7c) 悲观速率 = max(EWMA, 近 7 天最大单日/24)
    daily6 = [{"date": (base - timedelta(days=a)).strftime("%Y-%m-%d"), "kwh": 6.0}
              for a in range(7)]
    p1 = pessimistic_rate_kwh_per_hour(daily6, now=base)
    assert abs(p1 - 6 / 24) < 1e-9, p1
    mixed = [dict(d) for d in daily6]
    mixed[3]["kwh"] = 12.0
    p2 = pessimistic_rate_kwh_per_hour(mixed, now=base)
    assert abs(p2 - 12 / 24) < 1e-9, p2
    assert pessimistic_rate_kwh_per_hour(None, now=base) is None
    # 8) 本地样本优先于日历史
    r3, _ = predict(hist, 0.15, daily_usage=daily, now=base)
    assert r3 != rate

    # 9) 实时走表：余额 10 度停在 30 小时前，均匀 6 度/天 → 速率 0.25，已用 7.5，剩 2.5 度 = 10h
    stale = [mk(30, 10.0), mk(6, 10.0)]
    r4, h4 = predict(stale, 10.0, daily_usage=daily6, now=base)
    assert abs(r4 - 0.25) < 1e-9, r4
    assert abs(h4 - 10.0) < 1e-6, h4
    # 9b) 余额刚刷新（最新样本即变化点）→ 不衰减；本地速率优先：(10-9)/30h
    fresh = [mk(30, 10.0), mk(0.5, 10.0), mk(0, 9.0)]
    r5, h5 = predict(fresh, 9.0, daily_usage=daily6, now=base)
    assert abs(r5 - 1 / 30) < 1e-9, r5
    assert abs(h5 - 270.0) < 1e-6, h5
    # 9c) 衰减到负 → 归零（上限 48h × 0.25 = 12 度 > 10 度）
    dead = [mk(100, 10.0), mk(50, 10.0)]
    _, h6 = predict(dead, 10.0, daily_usage=daily6, now=base)
    assert h6 == 0.0, h6

    print("estimator self-test OK")


if __name__ == "__main__":
    _self_test()
