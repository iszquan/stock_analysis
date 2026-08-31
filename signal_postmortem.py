#!/usr/bin/env python3
"""信号后验统计: 记录每次触发信号的价格/环境/评分, 后续按时间步自动采样,
输出各时间点收益与胜率, 支持参数优化建议。"""
import json, time, os, math
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_outcomes.json")

def load():
    if not os.path.exists(PATH): return []
    try: return json.load(open(PATH, encoding="utf-8"))
    except: return []

def save(data):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data[-5000:], f, ensure_ascii=False, indent=None)

def log_signal(code, signal_type, score_level, price, pct, vr,
               market_regime, sector_score, signal_score, ai_score, ctx=""):
    """记录新触发信号"""
    data = load()
    entry = {
        "id": int(time.time()*1000),
        "ts": time.time(),
        "date": time.strftime("%Y-%m-%d"),
        "code": code,
        "signal_type": signal_type,
        "level": score_level,
        "price": round(price, 2) if price else None,
        "pct": round(pct, 2),
        "vol_ratio": round(vr, 2) if vr else None,
        "market_regime": market_regime,
        "sector_score": sector_score,
        "signal_score": signal_score,
        "ai_score": ai_score,
        "context": ctx,
        "outcomes": {},  # {"5m":price,"15m":price,"30m":price,"60m":price,"EOD":price}
    }
    data.append(entry)
    save(data)
    return entry["id"]

def record_outcome(signal_id, horizon, price_now):
    """5/15/30/60/EOD 后记录价格"""
    data = load()
    for e in data:
        if e.get("id") == signal_id:
            if not isinstance(price_now, (int, float)):
                try: price_now = float(price_now)
                except: price_now = None
            if price_now is not None:
                e.setdefault("outcomes", {})[horizon] = round(price_now, 2)
            save(data)
            return True
    return False

def compute_stats(signal_type_filter=None, min_samples=10):
    """计算各信号类型的后验胜率/收益"""
    data = load()
    stats = {}
    for e in data:
        st = e.get("signal_type", "unknown")
        if signal_type_filter and st != signal_type_filter:
            continue
        if not e.get("price") or not e.get("outcomes"):
            continue
        stats.setdefault(st, {"n":0,"5m":[],"15m":[],"30m":[],"60m":[],"EOD":[],"horizons":["5m","15m","30m","60m","EOD"]})
        stats[st]["n"] += 1
        for h in ["5m","15m","30m","60m","EOD"]:
            after = e["outcomes"].get(h)
            if after and e.get("price"):
                ret = (after / e["price"] - 1) * 100
                stats[st][h].append(ret)
    # 汇总
    result = {}
    for st, s in stats.items():
        if s["n"] < min_samples:
            continue
        entry = {"signal": st, "samples": s["n"]}
        for h in s["horizons"]:
            arr = s[h]
            if arr:
                entry[h + "_avg"] = round(sum(arr)/len(arr), 2)
                wins = sum(1 for r in arr if r > 0)
                entry[h + "_win"] = round(wins/len(arr)*100, 1)
            else:
                entry[h + "_avg"] = None
                entry[h + "_win"] = None
        result[st] = entry
    return result

def best_params_quick_move(current_window=1.5):
    """简化版: 通过历史数据估算 quick_move_pct 最佳区间"""
    # 实际应读取所有信号的 quick_move_pct 与 后验胜率; 这里提供框架
    stats = compute_stats(min_samples=5)
    best = None
    best_rate = 0
    for st, d in stats.items():
        w = d.get("15m_win")
        if w is not None and w > best_rate:
            best_rate = w
            best = (st, w, d.get("15m_avg", 0))
    return {"best_signal": best[0] if best else None,
            "best_win_rate_15m": best_rate,
            "suggested_quick_pct": 1.8,  # 默认建议
            "note": "建议以 1.5~2.2 区间为主测试区"}

if __name__ == "__main__":
    # 测试: 打印最近统计
    s = compute_stats()
    for k, v in list(s.items())[:3]:
        print(k, "n=", v["samples"], "15m_avg=", v.get("15m_avg"), "15m_win=", v.get("15m_win"))
