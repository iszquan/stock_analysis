#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 解读模块: NVIDIA NIM (OpenAI 兼容接口, 纯标准库实现)

信号触发时, 把实时行情+技术指标快照发给大模型, 生成简短中文解读,
附在盯盘通知里。也支持单独使用:

  python3 ai_advisor.py 600519              # 直接对某只股做一次 AI 解读
  python3 ai_advisor.py 600519 "大涨2.7%"   # 模拟"触发信号"

API Key 三种给法(优先级从高到低):
  1. config.json -> "ai": {"api_key": "nvapi-..."}
  2. 环境变量 export NVIDIA_API_KEY=nvapi-...
  3. 运行时 --key 参数 (仅测试用)

模型默认 meta/llama-3.3-70b-instruct, 可在 config.json 改 model 字段,
NIM 上常用的还有: qwen/qwen2.5-72b-instruct, deepseek-ai/deepseek-r1 等。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import stock as sm  # noqa: E402

CFG_PATH = os.path.join(BASE, "config.json")

DEFAULT_AI = {
    "enabled": True,
    "api_key": "",
    "base_url": "https://integrate.api.nvidia.com/v1",
    "model": "minimaxai/minimax-m3",
    "max_chars": 110,
    "daily_limit": 40,
    "timeout_sec": 30,
}

SYSTEM_PROMPT = (
    "你是一名严谨的A股短线技术分析师。根据用户给出的行情与指标数据, 用不超过%d个汉字"
    "输出一段分析, 依次包含: ①对触发信号的解读(结合指标互相印证或矛盾) ②短线倾向"
    "(偏多/偏空/震荡, 给一句话理由) ③关键价位(支撑/压力) ④一句风险提示。"
    "要求: 直接给结论不客套; 不用markdown和序号; 数字保留1位小数; 结尾注明'仅供参考'。"
)


# ---------------------------------------------------------------- 配置 --

def load_ai_cfg():
    ai = dict(DEFAULT_AI)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            file_cfg = (json.load(f).get("ai") or {})
        ai.update(file_cfg)
    except Exception:
        pass
    if not ai.get("api_key"):
        ai["api_key"] = (os.environ.get("NVIDIA_API_KEY")
                         or os.environ.get("NVIDIA_NIM_API_KEY") or "")
    return ai


# ---------------------------------------------------------------- NIM --

def chat(ai_cfg, messages, max_tokens=500):
    url = ai_cfg["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": ai_cfg["model"],
        "messages": messages,
        "temperature": 0.3,
        "top_p": 0.9,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer %s" % ai_cfg["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=ai_cfg.get("timeout_sec", 30)) as r:
                data = json.loads(r.read().decode("utf-8"))
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            # 推理型模型(nemotron/gpt-oss等)优先取content, 空则回退reasoning_content
            text = msg.get("content") or msg.get("reasoning_content") or ""
            return strip_think((text or "").strip())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < 2:
                time.sleep(25)      # NIM每分钟限频, 退避后重试
                continue
            break
        except Exception as e:
            last = e
            time.sleep(0.8)
    raise last


def strip_think(text):
    """兼容 deepseek-r1 等思考型模型, 去掉 <think> 段"""
    t = text
    while "<think>" in t and "</think>" in t:
        s = t.index("<think>")
        e = t.index("</think>") + len("</think>")
        t = t[:s] + t[e:]
    return t.replace("<think>", "").strip()


# ---------------------------------------------------------------- 快照 --

def build_snapshot(code, rt=None):
    """汇总一只股票的当前技术面快照(dict), 尽量容错"""
    snap = {}
    try:
        rt = rt or sm.fetch_rt(code)
        keep = ("price", "pct", "chg", "open", "high", "low", "prev_close",
                "vol_ratio", "turnover", "pe_ttm", "pb", "limit_up", "limit_down")
        snap = {"代码": code, "名称": rt.get("name", ""),
                "更新时间": rt.get("update", "")}
        zh = {"price": "最新价", "pct": "涨跌幅%", "chg": "涨跌额", "open": "今开",
              "high": "最高", "low": "最低", "prev_close": "昨收",
              "vol_ratio": "量比", "turnover": "换手%",
              "pe_ttm": "PE_TTM", "pb": "PB",
              "limit_up": "涨停价", "limit_down": "跌停价"}
        for k in keep:
            if rt.get(k) is not None:
                snap[zh[k]] = rt[k]
    except Exception as e:
        snap["行情错误"] = str(e)
    # 日线指标(失败不影响主流程)
    try:
        df = sm.add_indicators(sm.fetch_kline(code, klt=101, lmt=80))
        c = df.iloc[-1]
        hi20 = float(df["high"].iloc[-21:-1].max())
        lo20 = float(df["low"].iloc[-21:-1].min())
        snap.update({
            "日线MA5/10/20/60": [round(c.ma5, 2), round(c.ma10, 2),
                                 round(c.ma20, 2), round(c.ma60, 2)],
            "MACD_DIF/DEA/柱": [round(c.dif, 3), round(c.dea, 3), round(c.macd, 3)],
            "KDJ_K/D/J": [round(c.kdj_k, 1), round(c.kdj_d, 1), round(c.kdj_j, 1)],
            "RSI6/12/24": [round(c.rsi6, 1), round(c.rsi12, 1), round(c.rsi24, 1)],
            "BOLL上/中/下": [round(c.boll_up, 2), round(c.boll_mid, 2), round(c.boll_low, 2)],
            "近20日高/低": [round(hi20, 2), round(lo20, 2)],
            "今日量vs5日均量%": round(float(c.vol) / float(c.vma5) * 100, 1) if c.vma5 else None,
            "连涨跌日数_正涨": int((df["pct"].iloc[-5:] > 0).sum()),
        })
    except Exception as e:
        snap["指标错误"] = str(e)
    return snap


def interpret(code, trigger_text="", ai_cfg=None, rt=None):
    """生成 AI 解读文本; 失败返回 None (调用方降级为纯规则文字)"""
    ai_cfg = ai_cfg or load_ai_cfg()
    if not ai_cfg.get("api_key"):
        return None
    try:
        snap = build_snapshot(code, rt=rt)
        user = "股票: %s %s\n触发信号: %s\n数据: %s" % (
            snap.get("名称", ""), code, trigger_text or "无(常规解读)",
            json.dumps(snap, ensure_ascii=False))
        text = chat(ai_cfg, [
            {"role": "system", "content": SYSTEM_PROMPT % ai_cfg.get("max_chars", 110)},
            {"role": "user", "content": user},
        ])
        limit = int(ai_cfg.get("max_chars", 110)) + 40
        if len(text) > limit:
            cut = text[:limit]
            if "。" in cut[-30:]:
                cut = cut[:cut.rfind("。") + 1]
            text = cut
        return text or None
    except Exception as e:
        sys.stderr.write("[ai] 解读失败: %r\n" % e)
        return None


def parse_json_loose(text):
    """从模型输出中尽力解析JSON对象: 去围栏/截取首尾大括号"""
    if not text:
        return None
    t = strip_think(text).strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        obj = json.loads(t[s:e + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


ADVICE_SYSTEM = (
    "你是一名A股短线'信号裁判', 而非指标解释器。已有规则引擎算好四层分数, "
    "你的任务是判断'这个信号为什么值得/不值得交易'。"
    "只输出一个严格合法的JSON对象: 不用markdown、不解释、不加任何JSON以外的文字。"
)

# 三层 AI: 接收 score 包 → 输出 verdict 质量判断
_ADVICE_SPEC = (
    '{"verdict":"真突破|假突破|情绪推动|趋势启动|追高风险|弱势反弹 一项",'
    '"verdict_reason":"≤30字 解释为什么是这个判定",'
    '"confidence":0到100的整数,'
    '"doubt":"≤30字 当前信号最薄弱的一环, 缺什么就不成立",'
    '"invalidation":"≤30字 什么条件出现后该信号完全失效(具体价格或形态)",'
    '"best_action":"立即买入|等待回踩|观察一天|放弃|轻仓试探 一项",'
    '"position_pct":"建议仓位百分比如10%",'
    '"watch_levels":{"break_hold":"维持判定的最低价",'
    '"break_lose":"判失败的触发价",'
    '"take_profit":"目标价",'
    '"stop_loss":"止损价"},'
    '"trace":{"trend":"强趋势|震荡|走弱",'
    '"momentum":"加速|钝化|背离",'
    '"volume":"放量确认|缩量可疑|正常",'
    '"location":"突破位|高位|低位|超卖"}}'
)


def build_structured_advice(code, ai_cfg=None, rt=None, scores=None):
    """三层 AI: 接收 Signal/Market/Sector/Stock/Risk 分数 + 技术快照,
    判断信号是有效突破还是假突破、趋势还是情绪、何时失效。
    scores = {market_score:int, sector_score:int, stock_score:int,
              signal_score:int, risk_score:int}"""
    ai_cfg = dict(ai_cfg or load_ai_cfg())
    if not ai_cfg.get("api_key"):
        return None
    ai_cfg["timeout_sec"] = max(int(ai_cfg.get("timeout_sec") or 30), 60)
    snap = build_snapshot(code, rt=rt)
    sc = scores or {}
    # 四层分数摘要
    ms  = sc.get("market_score",  "-")
    ss  = sc.get("sector_score",  "-")
    sts = sc.get("stock_score",   "-")
    sig = sc.get("signal_score",  "-")
    rs  = sc.get("risk_score",    "-")
    # 提取技术关键数据供裁判参考
    ma_str   = snap.get("日线MA5/10/20/60", [])
    macd_str = snap.get("MACD_DIF/DEA/柱", [])
    kdj_str  = snap.get("KDJ_K/D/J", [])
    rsi_str  = snap.get("RSI6/12/24", [])
    boll_str = snap.get("BOLL上/中/下", [])
    hi_lo    = snap.get("近20日高/低", [])
    vol_ratio= snap.get("量比", "")
    pct_val  = snap.get("涨跌幅%", "")
    trend    = snap.get("连涨跌日数_正涨", "")
    name     = snap.get("名称", code)

    score_ctx = (
        "【三层评分(0-100)】\n"
        "  市场环境 score:  %s   (≥70强势 / 40-70震荡 / <40弱势)\n"
        "  板块强度 score:  %s   (≥85极强 / 70-85强 / 50-70一般 / <50弱)\n"
        "  个股综合 score:  %s   (≥70突破 / 50-70强势 / 30-50普通 / <30忽略)\n"
        "  信号总分 score:  %s   (≥85极强信号 / 70-85★重点 / 50-70强 / 30-50关注 / <30不通知)\n"
        "  风险评分 score:  %s   (≥70高风险 / 40-70中 / <40低风险)\n"
    ) % (ms, ss, sts, sig, rs)

    tech_ctx = (
        "【技术数据】\n"
        "  最新价: %(price)s  涨跌幅: %(pct)s%%  量比: %(vr)s\n"
        "  MA5/10/20/60: %(ma)s\n"
        "  MACD(DIF/DEA/柱): %(macd)s\n"
        "  KDJ(K/D/J): %(kdj)s\n"
        "  RSI(6/12/24): %(rsi)s\n"
        "  BOLL(上/中/下): %(boll)s\n"
        "  近20日高点/低点: %(hi_lo)s\n"
        "  今日量/5日均量: %(vol_ma)s%%\n"
        "  5日内上涨天数: %(trend)s天\n"
    ) % {
        "price": snap.get("最新价", ""),
        "pct":   pct_val,
        "vr":    vol_ratio,
        "ma":    ma_str,
        "macd":  macd_str,
        "kdj":   kdj_str,
        "rsi":   rsi_str,
        "boll":  boll_str,
        "hi_lo": hi_lo,
        "vol_ma": snap.get("今日量vs5日均量%", ""),
        "trend": trend,
    }

    user = (
        "标的: %s %s\n\n"
        "%s\n\n"
        "%s\n\n"
        "请根据以上四层评分和技术数据, 输出JSON:\n%s"
    ) % (name, code, score_ctx, tech_ctx, _ADVICE_SPEC)

    text = chat(ai_cfg, [
        {"role": "system", "content": ADVICE_SYSTEM},
        {"role": "user", "content": user},
    ], max_tokens=900)
    data = parse_json_loose(text)
    if not data:
        return None
    # 保留向后兼容字段(前端可能依赖这些键)
    data.setdefault("dims", [])
    data.setdefault("summary", data.get("verdict", "") + " | " + data.get("verdict_reason", ""))
    data.setdefault("holding", data.get("best_action", ""))
    data.setdefault("waiting", data.get("best_action", ""))
    data.setdefault("plan", [])
    data.setdefault("risk", {
        "position": data.get("position_pct", ""),
        "rr":       "",
        "stop_fix": "",
        "stop_tech": str(data.get("watch_levels", {}).get("stop_loss", "")),
        "stop_trail": "",
    })
    data["_scores"] = scores  # 前端显示用
    return data


def ask_followup(code, question, history=None, advice=None, ai_cfg=None):
    """追问式问答: 基于最新快照(+可选此前建议)回答用户问题。
    不做任何次数限制。返回文本或None。"""
    ai_cfg = dict(ai_cfg or load_ai_cfg())
    if not ai_cfg.get("api_key"):
        return None
    ai_cfg["timeout_sec"] = max(int(ai_cfg.get("timeout_sec") or 30), 60)
    snap = build_snapshot(code)
    msgs = [{"role": "system",
             "content": ("你是A股短线分析师助手, 基于提供的实时数据回答用户追问。"
                          "直接给结论不客套, ≤120字, 数字保留1-2位小数, "
                          "结尾注明'仅供参考'。不用markdown。")}]
    ctx = ("标的: %s %s\n最新数据: %s" %
           (snap.get("名称", ""), code, json.dumps(snap, ensure_ascii=False)))
    if advice:
        adv = json.dumps(advice, ensure_ascii=False)[:1500]
        ctx += "\n此前的操作建议(供参照): %s" % adv
    msgs.append({"role": "user", "content": ctx + "\n\n请记住以上数据, 回答我接下来的问题"})
    msgs.append({"role": "assistant", "content": "好的, 请问。"})
    for h in (history or [])[-4:]:
        role = h.get("role")
        txt = str(h.get("content", ""))[:300]
        if role in ("user", "assistant") and txt:
            msgs.append({"role": role, "content": txt})
    msgs.append({"role": "user", "content": str(question)[:200]})
    text = chat(ai_cfg, msgs, max_tokens=350)
    return strip_think(text).strip() or None


def main():
    ap = argparse.ArgumentParser(description="单次 AI 解读测试")
    ap.add_argument("code", help="股票代码")
    ap.add_argument("trigger", nargs="?", default="", help="模拟触发信号文本")
    ap.add_argument("--key", default="", help="直接传 nvapi key(仅测试)")
    args = ap.parse_args()

    ai_cfg = load_ai_cfg()
    if args.key:
        ai_cfg["api_key"] = args.key
    print("模型: %s | key: %s" % (ai_cfg["model"],
                                  "已配置(" + ai_cfg["api_key"][:9] + "...)" if ai_cfg["api_key"] else "❌未配置"))
    print("正在获取快照并请求模型…")
    out = interpret(args.code, args.trigger, ai_cfg)
    print("-" * 50)
    print(out if out else "❌ 未获得解读(未配key或调用失败, 见上方stderr)")


if __name__ == "__main__":
    main()
