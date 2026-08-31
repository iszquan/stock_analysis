#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股实时行情 + 技术指标分析工具（纯标准库，无需安装任何依赖）

用法:
  python3 stock.py 600519              # 单只股票完整分析（日线）
  python3 stock.py 600519 000001       # 多只股票
  python3 stock.py sh600519            # 显式指定市场前缀也可
  python3 stock.py -i                  # 大盘指数概览
  python3 stock.py 600519 --trend      # 附当日分时走势
  python3 stock.py 600519 --period 60  # 以60分钟K线为主周期分析 (可选: d/60/30/15)
  python3 stock.py --find 茅台          # 按名称/拼音搜索代码
  python3 stock.py 600519 --watch 10   # 盯盘模式，每10秒刷新报价

数据源: 东方财富公开行情接口(免费无key)，腾讯行情作为备用。
声明: 输出仅为技术面参考信息，不构成投资建议。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import pandas as pd

UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
UT = "fa5fd1943c7b386f172d6893dbfba10b"


# ---------------------------------------------------------------- HTTP ----

def http_get(url, timeout=10, encoding="utf-8", headers=None, retries=3):
    last = None
    for attempt in range(retries):
        try:
            hdrs = dict(UA)
            if headers:
                hdrs.update(headers)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(encoding, errors="replace")
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(0.6 * (attempt + 1))
    raise last


def http_json(url, timeout=10, retries=3):
    return json.loads(http_get(url, timeout=timeout, encoding="utf-8", retries=retries))


# 东财 push2 主机对高频请求敏感, 用编号镜像轮询最稳
PUSH2_HOSTS = ["2.push2.eastmoney.com", "90.push2.eastmoney.com",
               "1.push2.eastmoney.com", "push2.eastmoney.com"]
PUSH2HIS_HOSTS = ["1.push2his.eastmoney.com", "2.push2his.eastmoney.com",
                  "90.push2his.eastmoney.com", "push2his.eastmoney.com"]

# 全部主机失败后的整体冷却期(指数退避): 避免重试风暴让服务器持续限流
_cooldown_until = [0]
_fail_streak = [0]
# 全局持续限速: 东财请求最小间隔, 健康时也保持礼貌速率
_last_push2 = [0.0]
_MIN_GAP = 2.0


def multi_host_get(hosts, path_and_query, timeout=8, rounds=2):
    """跨镜像主机轮询请求; 每台只试一次保证快速返回"""
    if time.time() < _cooldown_until[0]:
        raise IOError("eastmoney接口冷却中(%d秒后自动重试)"
                      % int(_cooldown_until[0] - time.time()))
    last = None
    for attempt in range(rounds):
        for h in hosts:
            gap = time.time() - _last_push2[0]
            if gap < _MIN_GAP:                 # 维持全局最小间隔
                time.sleep(_MIN_GAP - gap)
            _last_push2[0] = time.time()
            try:
                r = http_json("https://%s%s" % (h, path_and_query),
                              timeout, retries=1)
                _fail_streak[0] = 0
                _cooldown_until[0] = 0
                return r
            except Exception as e:
                last = e
        time.sleep(0.4 * (attempt + 1))
    _fail_streak[0] += 1
    cd = min(60 * (2 ** min(_fail_streak[0], 4)), 960)   # 60/120/240/480/960秒
    _cooldown_until[0] = time.time() + cd
    raise last


def push2_get(path_and_query, timeout=8):
    return multi_host_get(PUSH2_HOSTS, path_and_query, timeout)


def fetch_rt_batch(codes, max_workers=3, timeout=8):
    """批量并发获取实时行情 (3线程并发, 各自独立限速).
    codes: list of str; 返回 {code: rt_dict} 其中 rt_dict 包含 code/name/price/pct 等.
    限流说明: 东财 push2 单机限速 ~2s/请求, 3worker 并发理论上可 ~1.3s/标的.
    """
    if not codes:
        return {}
    # 过滤黑名单前缀
    filtered = [c for c in codes if not c.lower().startswith("ths.")]
    ths_codes = [c for c in codes if c.lower().startswith("ths.")]
    results = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    # 每 worker 独立限速器
    _worker_gap = [0.0]
    _lock = threading.Lock()

    def _fetch_one(code):
        try:
            rt = fetch_rt(code)
            return code, rt
        except Exception:
            return code, None

    # THS 标的同时发
    for c in ths_codes:
        try:
            rt = fetch_rt(c)
            results[c] = rt
        except Exception:
            results[c] = None

    # 其余用线程池并发 (max_workers 并发数)
    remaining = [(c, fetch_rt(c)) for c in filtered]
    # 先用 ThreadPoolExecutor 跑, 让限速器自己生效
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_rt, c): c for c in filtered}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                rt = fut.result()
                results[c] = rt
            except Exception:
                results[c] = None
    return results


# ---------------------------------------------------------------- 代码 ----

def secid(code):
    """把用户输入的代码转成东财 secid (市场.代码)"""
    s = str(code).strip().replace(" ", "")
    if "." in s:
        m, _, cc = s.partition(".")
        if m.isdigit():                    # 显式市场码: 原样透传(保留大小写)
            return "%s.%s" % (m, cc)
        if m in ("sh", "sz", "bj"):
            return {"sh": "1", "sz": "0", "bj": "0"}[m] + "." + cc
    c = s.lower()
    if c[:1] in ("6", "5", "9"):          # 沪: 60股票 68科创 5基金 9B股
        return "1." + c
    return "0." + c                        # 深: 00/30/12/15/16/18 及北交 4/8


def tencent_code(code):
    """把各种格式代码转成腾讯格式: sh600519 / sz000001"""
    s = str(code).strip()
    if "." in s:
        m, _, cc = s.partition(".")
        if m in ("1", "sh"):
            return "sh" + cc
        if m in ("0", "sz"):
            return "sz" + cc
        return ("sh" if m == "1" else "sz") + cc
    sl = s.lower()
    if sl.startswith("sh"):
        return "sh" + sl[2:]
    if sl.startswith("sz"):
        return "sz" + sl[2:]
    if sl[:1] in ("6", "5", "9"):
        return "sh" + sl
    return "sz" + sl


# --------------------------------------------------- 同花顺独有指数 ----

# 同花顺板块码 -> 东财等价板块(用于K线历史/分时等上下文数据, 以及THS故障备源)
THS_EM_BOARD = {
    "884286": "90.BK1621",   # 锂概念 -> 锂
    "886033": "90.BK1128",   # 共封装光学 -> CPO概念
    "886108": "90.BK1629",   # AI应用
    "886078": "90.BK0963",   # 商业航天
    "885710": "90.BK0574",   # 锂电池概念
}

# 昨收缓存文件: THS当日接口不含昨收, 跨日自维护以计算涨跌幅
_THS_PREV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ths_prev.json")
_ths_prev = None


def _ths_prev_load():
    global _ths_prev
    if _ths_prev is None:
        try:
            with open(_THS_PREV_FILE, encoding="utf-8") as f:
                _ths_prev = json.load(f)
        except Exception:
            _ths_prev = {}
    return _ths_prev


def ths_board_today(num):
    """同花顺板块当日快照(明文JSONP): 开高低最新+成交量额+名称"""
    raw = http_get("https://d.10jqka.com.cn/v4/line/bk_%s/01/today.js" % num,
                   headers={"User-Agent": UA["User-Agent"],
                            "Referer": "https://quote.10jqka.com.cn/"})
    m = re.search(r"\((\{.*\})\)", raw)
    if not m:
        raise IOError("同花顺返回格式异常: %s" % raw[:60])
    d = json.loads(m.group(1))["bk_" + num]
    return {"date": str(d.get("1", "")), "open": float(d["7"]),
            "high": float(d["8"]), "low": float(d["9"]),
            "price": float(d["11"]), "vol": float(d.get("13") or 0),
            "amount": float(d.get("19") or 0), "time": str(d.get("dt", "")),
            "name": d.get("name", "")}


def _rt_ths(num):
    """同花顺板块实时: 主源=THS当日快照; 涨跌幅用自维护昨收;
    THS失败时降级到东财等价板块"""
    try:
        d = ths_board_today(num)
    except Exception:
        em = THS_EM_BOARD.get(num)
        if not em:
            raise
        rt = fetch_rt(em)          # 备源: 东财等价板块
        rt["src"] = "eastmoney(等价板块)"
        return rt

    prev = _ths_prev_load().get(num) or {}
    today = d["date"]
    prev_close = None
    if prev.get("last_date") == today:
        prev_close = prev.get("prev_close")
    elif prev.get("last_date"):
        prev_close = prev.get("last_close")     # 上一次抓取即昨日收盘
    # 更新缓存: 记录今日最新价与对应昨收
    _ths_prev[num] = {"last_date": today, "last_close": d["price"],
                      "prev_close": prev_close}
    try:
        with open(_THS_PREV_FILE, "w", encoding="utf-8") as f:
            json.dump(_ths_prev, f, ensure_ascii=False)
    except Exception:
        pass
    pct = (d["price"] / prev_close - 1) * 100 if prev_close else None
    return {
        "code": "ths." + num, "name": d.get("name") or ("THS" + num),
        "price": d["price"], "open": d["open"], "high": d["high"],
        "low": d["low"], "prev_close": prev_close,
        "pct": pct, "chg": (d["price"] - prev_close) if prev_close else None,
        "vol": d["vol"] / 100 if d["vol"] else None,
        "amount": d["amount"], "update": "%s %s" % (today[4:6], d["time"]),
        "date": today, "src": "同花顺",
    }


# ---------------------------------------------------------------- 行情 ----

RT_FIELDS = ("f43,f44,f45,f46,f47,f48,f50,f51,f52,f57,f58,f60,"
             "f116,f117,f162,f163,f164,f167,f168,f169,f170,f171,f86")


def _num(x, scale=1.0):
    try:
        v = float(x)
        return None if v <= -999999 else v / scale
    except (TypeError, ValueError):
        return None


def _tencent_quote(tc):
    """腾讯单只实时报价解析(A股/港股指数通用, 可选字段容错)"""
    txt = http_get("https://qt.gtimg.cn/q=" + tc, encoding="gbk")
    p = txt.split("~")

    def sf(i):
        try:
            return float(p[i]) if len(p) > i and p[i] else None
        except (ValueError, TypeError):
            return None

    try:
        price = float(p[3]); prev = float(p[4])
    except (IndexError, ValueError):
        raise IOError("腾讯行情格式异常: %s" % tc)
    ts = (p[30].split(" ")[-1] if len(p) > 30 else "")
    if len(ts) == 14 and ts.isdigit():
        ts = "%s:%s:%s" % (ts[8:10], ts[10:12], ts[12:14])
    return {
        "code": p[2] if len(p) > 2 else tc, "name": p[1] if len(p) > 1 else tc,
        "price": price, "prev_close": prev,
        "open": sf(5), "vol": sf(36), "amount": sf(37) * 1e4 if sf(37) else None,
        "high": sf(33), "low": sf(34),
        "chg": price - prev, "pct": (price - prev) / prev * 100 if prev else None,
        "turnover": sf(38), "pb": sf(46),
        "update": ts, "src": "tencent",
    }


def _sina_quote(syms):
    """新浪批量行情: 返回 {sym: [字段...]}"""
    txt = http_get("https://hq.sinajs.cn/list=" + ",".join(syms),
                   headers={"Referer": "https://finance.sina.com.cn"},
                   encoding="gbk")
    out = {}
    for line in txt.strip().splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)";?\s*$', line.strip())
        if m:
            out[m.group(1)] = [x for x in m.group(2).split(",") if x != ""]
    return out


def sina_xau():
    """伦敦金现货 (新浪 hf_XAU)"""
    f = _sina_quote(["hf_XAU"])["hf_XAU"]
    price, prev = float(f[0]), float(f[7])
    return {
        "code": "XAU", "name": "伦敦金现", "price": price,
        "prev_close": prev, "open": float(f[8]),
        "high": float(f[4]), "low": float(f[5]),
        "pct": (price / prev - 1) * 100 if prev else None,
        "chg": price - prev if prev else None,
        "update": f[6], "date": f[12], "src": "sina",
    }


def sina_sge(sym="AU9999"):
    """上海黄金交易所现货 (新浪 SGE_前缀)"""
    f = _sina_quote(["SGE_" + sym])["SGE_" + sym]
    price = float(f[3])
    prev = float(f[4]) if len(f) > 4 and f[4] else None
    return {
        "code": sym, "name": ("上金所" + str(f[2])) if len(f) > 2 else sym,
        "price": price, "prev_close": prev,
        "open": float(f[5]) if len(f) > 5 and f[5] else None,
        "high": float(f[6]) if len(f) > 6 and f[6] else None,
        "low": float(f[7]) if len(f) > 7 and f[7] else None,
        "pct": (price / prev - 1) * 100 if prev else None,
        "chg": price - prev if prev else None,
        "update": "", "src": "sina",
    }


def _em_rt(sid):
    """东财实时报价(单市场)"""
    q = ("/api/qt/stock/get?ut=%s&invt=2&fltt=2&fields=%s&secid=%s"
         % (UT, RT_FIELDS, sid))
    d = push2_get(q)["data"]
    if not d or d.get("f43") in ("-", None):
        raise ValueError("empty")
    upd = _num(d.get("f86")) or _num(d.get("f124"))
    return {
        "code": d.get("f57", ""), "name": d.get("f58", ""),
        "price": _num(d.get("f43")), "high": _num(d.get("f44")),
        "low": _num(d.get("f45")), "open": _num(d.get("f46")),
        "vol": _num(d.get("f47")),           # 手
        "amount": _num(d.get("f48")),        # 元
        "vol_ratio": _num(d.get("f50")),     # 量比
        "limit_up": _num(d.get("f51")), "limit_down": _num(d.get("f52")),
        "prev_close": _num(d.get("f60")),
        "mktcap": _num(d.get("f116")), "float_cap": _num(d.get("f117")),
        "pe_dyn": _num(d.get("f162")), "pe_static": _num(d.get("f163")),
        "pe_ttm": _num(d.get("f164")), "pb": _num(d.get("f167")),
        "turnover": _num(d.get("f168")),      # %
        "chg": _num(d.get("f169")), "pct": _num(d.get("f170")),
        "update": time.strftime("%H:%M:%S", time.localtime(upd)) if upd else "",
        "src": "eastmoney",
    }


def fetch_rt(code):
    """实时报价多源调度:
    ths.*     -> 同花顺直连(主) -> 东财等价板块(备)
    118./122. -> 东财 -> 新浪(上金所/伦敦金)
    124.      -> 东财 -> 腾讯hk
    0./1.     -> 东财 -> 腾讯
    """
    s = str(code).strip()
    if s.lower().startswith("ths."):
        return _rt_ths(s.split(".", 1)[1])
    sid = secid(s)
    em_err = None
    try:
        return _em_rt(sid)
    except Exception as e:
        em_err = e
    cc = sid.split(".", 1)[1]
    mkt = sid.split(".", 1)[0]
    fallbacks = []
    if mkt == "122":
        fallbacks.append(sina_xau)
    if mkt == "118":
        fallbacks.append(lambda: sina_sge(cc.upper()))
    if mkt == "124":
        fallbacks.append(lambda: _tencent_quote("hk" + cc))
    if mkt in ("0", "1"):
        fallbacks.append(lambda: _tencent_quote(tencent_code(s)))
    for fn in fallbacks:
        try:
            return fn()
        except Exception:
            continue
    raise em_err


def _em_kline(code, klt, lmt, fqt):
    q = ("/api/qt/stock/kline/get?ut=%s"
         "&secid=%s&klt=%d&fqt=%d&lmt=%d&end=20500101"
         "&fields1=f1,f2,f3,f4,f5,f6"
         "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
         % (UT, secid(code), klt, fqt, lmt))
    data = multi_host_get(PUSH2HIS_HOSTS, q)["data"]
    rows = []
    for line in data["klines"]:
        f = line.split(",")
        rows.append({
            "date": f[0], "open": float(f[1]), "close": float(f[2]),
            "high": float(f[3]), "low": float(f[4]), "vol": float(f[5]),
            "amount": float(f[6]), "amp": float(f[7]), "pct": float(f[8]),
            "chg": float(f[9]), "turnover": float(f[10]),
        })
    df = pd.DataFrame(rows)
    df["name"] = data.get("name", "")
    return df


def _kline_tencent(code, lmt):
    """腾讯日K备用源 (ifzq 前复权)"""
    tc = tencent_code(code)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=%s,day,,,%d,qfq" % (tc, lmt))
    d = http_json(url)
    if d.get("code") != 0:
        raise IOError("腾讯K线返回错误: %s" % d)
    tcd = d["data"].get(tc)
    if not tcd:
        raise IOError("腾讯K线无数据: %s" % tc)
    rows0 = tcd.get("qfqday") or tcd.get("day") or []
    rows = []
    for r in rows0:
        try:
            o, c, h, l = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            v = float(r[5]) if len(r) > 5 and r[5] else 0.0
        except Exception:
            continue
        rows.append({
            "date": r[0], "open": o, "close": c, "high": h, "low": l,
            "vol": v, "amount": 0.0, "amp": 0.0,
            "pct": (c / o - 1) * 100 if o else 0.0,
            "chg": c - o, "turnover": 0.0
        })
    df = pd.DataFrame(rows[-lmt:])
    df["name"] = tc
    return df


def _kline_sina(code, lmt):
    """新浪日K备用源 (scale=240)"""
    sym = tencent_code(code)
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
           "CN_MarketDataService.getKLineData?symbol=%s&scale=240"
           "&ma=no&datalen=%d" % (sym, lmt))
    arr = json.loads(http_get(url, encoding="utf-8"))
    rows = []
    for r in arr:
        try:
            o, c, h, l = float(r["open"]), float(r["close"]), float(r["high"]), float(r["low"])
            v = float(r.get("volume") or 0)
        except Exception:
            continue
        rows.append({
            "date": r["day"], "open": o, "close": c, "high": h, "low": l,
            "vol": v, "amount": 0.0, "amp": 0.0,
            "pct": (c / o - 1) * 100 if o else 0.0,
            "chg": c - o, "turnover": 0.0
        })
    df = pd.DataFrame(rows[-lmt:])
    df["name"] = sym
    return df


_KLINE_ALTS = (_kline_tencent, _kline_sina)


def fetch_kline(code, klt=101, lmt=160, fqt=1):
    """日/分钟K线多源调度: 东财(全市场) -> 腾讯/新浪(标准市场备用)。
    ths.* 代码自动映射到东财等价板块取历史上下文。"""
    s = str(code).strip()
    if s.lower().startswith("ths."):
        num = s.split(".", 1)[1]
        em = THS_EM_BOARD.get(num)
        if not em:
            raise ValueError("THS板块%s无等价K线源" % num)
        return fetch_kline(em, klt=klt, lmt=lmt, fqt=fqt)
    try:
        return _em_kline(s, klt, lmt, fqt)
    except Exception as em_err:
        if secid(s).startswith(("0.", "1.")) and klt == 101:
            for alt in _KLINE_ALTS:
                try:
                    return alt(s, lmt)
                except Exception:
                    continue
        raise em_err


def fetch_trends(code, ndays=1):
    """当日分时: 返回 (preClose, [(time,price,vol,amount,avg)])。
    ths.* 自动映射到东财等价板块。"""
    s = str(code).strip()
    if s.lower().startswith("ths."):
        num = s.split(".", 1)[1]
        em = THS_EM_BOARD.get(num)
        if not em:
            raise ValueError("THS板块%s无等价分时源" % num)
        code = em
    q = ("/api/qt/stock/trends2/get?ut=%s"
         "&secid=%s&fields1=f1,f2,f3,f7,f8&fields2=f51,f53,f56,f57,f58"
         "&iscr=0&ndays=%d" % (UT, secid(code), ndays))
    d = multi_host_get(PUSH2HIS_HOSTS, q)["data"]
    pre = d["preClose"]
    pts = []
    for line in d["trends"]:
        f = line.split(",")
        pts.append((f[0][11:], float(f[1]), float(f[2]), float(f[3]), float(f[4])))
    return pre, pts


def fetch_index_overview():
    secids = ",".join([
        "1.000001",  # 上证指数
        "0.399001",  # 深证成指
        "0.399006",  # 创业板指
        "1.000688",  # 科创50
        "1.000300",  # 沪深300
        "1.000905",  # 中证500
    ])
    q = ("/api/qt/ulist.np/get?ut=%s&fltt=2&invt=2"
         "&fields=f2,f3,f4,f12,f14&secids=%s" % (UT, secids))
    d = push2_get(q)["data"]["diff"]
    out = []
    for x in d:
        out.append({"name": x["f14"], "price": x["f2"], "pct": x["f3"], "chg": x["f4"]})
    return out


def search_code(kw):
    url = ("https://searchapi.eastmoney.com/api/suggest/get?input=%s"
           "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10"
           % urllib.parse.quote(kw))
    d = http_json(url)
    table = ((d.get("QuotationCodeTable") or {}).get("Data")) or []
    for x in table:
        print("%s\t%s\t%s" % (x.get("Code"), x.get("Name"),
                              "沪" if str(x.get("MktNum")) == "1" else "深/北"))


def search_instruments(kw, limit=8):
    """按名称/代码搜索可交易标的(仅沪深A股/基金/债券), 供自选添加。
    返回 [{code,name,market}], code为可直接用于fetch_rt的格式。"""
    kw = str(kw or "").strip()
    if not kw:
        return []
    url = ("https://searchapi.eastmoney.com/api/suggest/get?input=%s"
           "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=%d"
           % (urllib.parse.quote(kw), max(limit, 12)))
    d = http_json(url)
    table = ((d.get("QuotationCodeTable") or {}).get("Data")) or []
    out = []
    for x in table:
        mkt = str(x.get("MktNum") or "")
        code = str(x.get("Code") or "")
        name = str(x.get("Name") or "").strip()
        tname = str(x.get("SecurityTypeName") or "").strip()
        if not code or not name:
            continue
        if mkt == "1":                      # 沪
            pass
        elif mkt == "0":                    # 深/北
            pass
        else:                               # 港美/板块等暂不开放自选
            continue
        if any(k in tname for k in ("退",)):
            continue
        out.append({"code": code, "name": name,
                    "market": tname or ("沪" if mkt == "1" else "深")})
        if len(out) >= limit:
            break
    return out


# -------------------------------------------------------------- 指标 ------

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def sma_cn(s, n, m):
    """中国式SMA(Y=(M*Y'+X*(N-M))/N), KDJ用"""
    return s.ewm(alpha=m / n, adjust=False).mean()


def add_indicators(df):
    c, h, l, v = df["close"], df["high"], df["low"], df["vol"]
    for n in (5, 10, 20, 30, 60):
        df["ma%d" % n] = c.rolling(n).mean()
    dif = ema(c, 12) - ema(c, 26)
    dea = ema(dif, 9)
    df["dif"], df["dea"], df["macd"] = dif, dea, 2 * (dif - dea)

    for n in (6, 12, 24):
        delta = c.diff()
        up = delta.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
        dn = (-delta.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False).mean()
        df["rsi%d" % n] = 100 * up / (up + dn)

    ln = l.rolling(9).min()
    hn = h.rolling(9).max()
    rsv = (c - ln) / (hn - ln).replace(0, float("nan")) * 100
    df["kdj_k"] = sma_cn(rsv.fillna(50), 3, 1)
    df["kdj_d"] = sma_cn(df["kdj_k"], 3, 1)
    df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]

    df["boll_mid"] = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    df["boll_up"] = df["boll_mid"] + 2 * sd
    df["boll_low"] = df["boll_mid"] - 2 * sd

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    df["vma5"] = v.rolling(5).mean()
    df["vma10"] = v.rolling(10).mean()
    return df


# -------------------------------------------------------------- 信号 ------

def detect_signals(df):
    """基于最新两根K线给出技术信号列表 [(方向, 文本)]，方向: 1多 -1空 0中性"""
    sig = []
    if len(df) < 62:
        return sig
    cur, prv = df.iloc[-1], df.iloc[-2]
    px = cur["close"]

    # MACD 交叉
    cross = cur["dif"] - cur["dea"]
    pcross = prv["dif"] - prv["dea"]
    bars = 0
    i = len(df) - 1
    while i > 0 and (df.iloc[i]["dif"] - df.iloc[i]["dea"]) * cross > 0:
        bars += 1
        i -= 1
    if cross > 0 and pcross <= 0:
        sig.append((1, "MACD金叉(今日)"))
    elif cross < 0 and pcross >= 0:
        sig.append((-1, "MACD死叉(今日)"))
    elif cross > 0:
        sig.append((1, "MACD多头运行中(DIF>DEA第%d根)" % bars))
    else:
        sig.append((-1, "MACD空头运行中(DIF<DEA第%d根)" % bars))
    if abs(cur["macd"]) < abs(prv["macd"]) and bars >= 2:
        sig.append((cur["macd"] > 0 and -1 or 1,
                    "MACD柱收窄(动能减弱)"))

    # 均线排列
    mas = [cur["ma%d" % n] for n in (5, 10, 20, 60)]
    if all(mas[i] > mas[i + 1] for i in range(len(mas) - 1)) and px > mas[0]:
        sig.append((1, "均线多头排列(5>10>20>60)"))
    elif all(mas[i] < mas[i + 1] for i in range(len(mas) - 1)) and px < mas[0]:
        sig.append((-1, "均线空头排列(5<10<20<60)"))
    else:
        above = sum(px > m for m in mas)
        sig.append((0, "价格位于%d/4条均线上方" % above))

    # KDJ
    if cur["kdj_k"] > cur["kdj_d"] and prv["kdj_k"] <= prv["kdj_d"]:
        pos = "低位" if cur["kdj_k"] < 35 else ""
        sig.append((1, "KDJ金叉%s(K=%.0f)" % (pos, cur["kdj_k"])))
    elif cur["kdj_k"] < cur["kdj_d"] and prv["kdj_k"] >= prv["kdj_d"]:
        pos = "高位" if cur["kdj_k"] > 65 else ""
        sig.append((-1, "KDJ死叉%s(K=%.0f)" % (pos, cur["kdj_k"])))
    j = cur["kdj_j"]
    if j > 100:
        sig.append((-1, "KDJ-J超买(J=%.0f)" % j))
    elif j < 0:
        sig.append((1, "KDJ-J超卖(J=%.0f)" % j))

    # RSI
    r6 = cur["rsi6"]
    if r6 > 80:
        sig.append((-1, "RSI6超买(%.0f)" % r6))
    elif r6 < 20:
        sig.append((1, "RSI6超卖(%.0f)" % r6))

    # BOLL 位置
    if px >= cur["boll_up"]:
        sig.append((0, "触及布林上轨(%0.2f)注意回调压力" % cur["boll_up"]))
    elif px <= cur["boll_low"]:
        sig.append((0, "触及布林下轨(%0.2f)关注超跌反弹" % cur["boll_low"]))

    # 量能
    if cur["vma5"] and cur["vma5"] > 0:
        ratio = cur["vol"] / cur["vma5"]
        if ratio >= 1.8:
            tag = "显著放量(+%.0f%%)" % ((ratio - 1) * 100)
            sig.append((1 if cur["pct"] > 0 else -1, tag))
        elif ratio <= 0.6:
            sig.append((0, "明显缩量(-%.0f%%)" % ((1 - ratio) * 100)))

    # 突破/破位 (近20日、近60日极值, 排除当日)
    hi20 = df["high"].iloc[-21:-1].max()
    lo20 = df["low"].iloc[-21:-1].min()
    if px > hi20:
        sig.append((1, "突破近20日高点(%0.2f)" % hi20))
    if px < lo20:
        sig.append((-1, "跌破近20日低点(%0.2f)" % lo20))

    # 连续涨跌
    chg = df["pct"].iloc[-3:]
    if (chg > 0).all():
        sig.append((1, "连涨%d日" % len(chg)))
    elif (chg < 0).all():
        sig.append((-1, "连跌%d日" % len(chg)))

    # 长上/下影线
    body = abs(cur["close"] - cur["open"])
    rng = cur["high"] - cur["low"]
    if rng > 0 and body > 0:
        up_shadow = cur["high"] - max(cur["close"], cur["open"])
        dn_shadow = min(cur["close"], cur["open"]) - cur["low"]
        if up_shadow > 2 * body:
            sig.append((-1, "长上影线(上方抛压)"))
        elif dn_shadow > 2 * body:
            sig.append((1, "长下影线(下方承接)"))
    return sig


# -------------------------------------------------------------- 展示 ------

def fmt(v, nd=2, dash="-"):
    return dash if v is None else ("%.*f" % (nd, v))


def yi(v):
    return "-" if v is None else "%.2f亿" % (v / 1e8)


def print_rt(rt):
    icon = "🔴" if (rt.get("pct") or 0) > 0 else ("🟢" if (rt.get("pct") or 0) < 0 else "⚪")
    print("%s [%s] %s  更新:%s (%s)" % (icon, rt["code"], rt["name"],
                                       rt.get("update") or "-", rt.get("src")))
    print("  最新 %-8s 涨跌 %+.2f (%+.2f%%)   开 %s  高 %s  低 %s  昨收 %s" % (
        fmt(rt.get("price")), rt.get("chg") or 0, rt.get("pct") or 0,
        fmt(rt.get("open")), fmt(rt.get("high")), fmt(rt.get("low")),
        fmt(rt.get("prev_close"))))
    extra = []
    if rt.get("vol_ratio") is not None:
        extra.append("量比 %.2f" % rt["vol_ratio"])
    if rt.get("turnover") is not None:
        extra.append("换手 %.2f%%" % rt["turnover"])
    if rt.get("amount"):
        extra.append("成交 %s" % yi(rt["amount"]))
    if rt.get("float_cap"):
        extra.append("流通值 %s" % yi(rt["float_cap"]))
    if rt.get("pe_ttm") is not None:
        extra.append("PE(TTM) %.1f" % rt["pe_ttm"])
    if rt.get("pb") is not None:
        extra.append("PB %.2f" % rt["pb"])
    if extra:
        print("  " + " | ".join(extra))
    if rt.get("limit_up"):
        print("  涨停 %s / 跌停 %s" % (fmt(rt["limit_up"]), fmt(rt["limit_down"])))


def analyze(code, period="d", with_trend=False):
    klt = {"d": 101, "60": 60, "30": 30, "15": 15}[period]
    pname = {"d": "日线", "60": "60分钟", "30": "30分钟", "15": "15分钟"}[period]
    rt = fetch_rt(code)
    print("=" * 64)
    print_rt(rt)

    try:
        df = add_indicators(fetch_kline(code, klt=klt, lmt=160))
    except Exception as e:
        print("  ⚠️ K线获取失败: %s" % e)
        return
    cur = df.iloc[-1]
    px = cur["close"]

    print("-- %s技术面 (截至 %s) --" % (pname, cur["date"]))
    print("  K线: 开%0.2f 高%0.2f 低%0.2f 收%0.2f  涨跌%+.2f%%  量%.0f手" % (
        cur["open"], cur["high"], cur["low"], cur["close"], cur["pct"], cur["vol"]))
    ma_txt = "  ".join("MA%d=%s" % (n, fmt(cur["ma%d" % n])) for n in (5, 10, 20, 60))
    print("  均线: " + ma_txt)
    print("  MACD: DIF=%+.3f DEA=%+.3f 柱=%+.3f | RSI6/12/24=%.0f/%.0f/%.0f" % (
        cur["dif"], cur["dea"], cur["macd"], cur["rsi6"], cur["rsi12"], cur["rsi24"]))
    print("  KDJ: K=%.1f D=%.1f J=%.1f | ATR14=%s (%.1f%%)" % (
        cur["kdj_k"], cur["kdj_d"], cur["kdj_j"], fmt(cur["atr14"]),
        (cur["atr14"] / px * 100) if px else 0))
    print("  BOLL: 上%s 中%s 下%s | 量: 今日vs5日均量 %s" % (
        fmt(cur["boll_up"]), fmt(cur["boll_mid"]), fmt(cur["boll_low"]),
        ("%.0f%%" % (cur["vol"] / cur["vma5"] * 100)) if cur["vma5"] else "-"))

    # 关键位
    hi20 = df["high"].iloc[-21:-1].max() if len(df) > 21 else None
    lo20 = df["low"].iloc[-21:-1].min() if len(df) > 21 else None
    hi60 = df["high"].iloc[-61:-1].max() if len(df) > 61 else None
    lo60 = df["low"].iloc[-61:-1].min() if len(df) > 61 else None
    print("  关键位: 压力 %s(20日高)/%s(60日高)  支撑 %s(20日低)/%s(60日低)" % (
        fmt(hi20), fmt(hi60), fmt(lo20), fmt(lo60)))

    sigs = detect_signals(df)
    bull = [t for d, t in sigs if d == 1]
    bear = [t for d, t in sigs if d == -1]
    neut = [t for d, t in sigs if d == 0]
    print("  信号汇总: 🐂看多×%d  🐻看空×%d" % (len(bull), len(bear)))
    for t in bull:
        print("    ▲ " + t)
    for t in bear:
        print("    ▼ " + t)
    for t in neut:
        print("    • " + t)

    if with_trend:
        try:
            pre, pts = fetch_trends(code)
            if pts:
                last = pts[-1]
                avg = last[4]
                print("-- 当日分时 (%d笔) --" % len(pts))
                print("  最新 %s 价%0.2f 分时均价(VWAP)%0.2f  现价相对VWAP %+.2f%%" % (
                    last[0], last[1], avg, (last[1] / avg - 1) * 100 if avg else 0))
                am = [p for p in pts if p[0] < "11:30"]
                pm = [p for p in pts if p[0] >= "13:00"]
                seg = [("早盘", am), ("午盘", pm)]
                for nm, s in seg:
                    if s:
                        prices = [x[1] for x in s]
                        print("  %s: 高%0.2f 低%0.2f 收于%0.2f" % (
                            nm, max(prices), min(prices), prices[-1]))
        except Exception as e:
            print("  ⚠️ 分时获取失败: %s" % e)
    print()


def watch(codes, interval=10):
    print("盯盘模式: %s  每%ds刷新 (Ctrl+C退出)" % (codes, interval))
    try:
        while True:
            lines = []
            for c in codes:
                try:
                    rt = fetch_rt(c)
                    pct = rt.get("pct")
                    mark = "+" if (pct or 0) >= 0 else ""
                    lines.append("%s %s %s %s%s%%" % (
                        rt.get("name"), rt.get("code"), fmt(rt.get("price")),
                        mark, fmt(pct)))
                except Exception as e:
                    lines.append("%s 获取失败(%s)" % (c, e))
            print("[%s] %s" % (time.strftime("%H:%M:%S"), "  ||  ".join(lines)),
                  flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已退出盯盘模式")


def main():
    ap = argparse.ArgumentParser(description="A股实时行情+技术指标分析")
    ap.add_argument("codes", nargs="*", help="股票代码, 如 600519 / sh600519")
    ap.add_argument("-i", "--index", action="store_true", help="大盘指数概览")
    ap.add_argument("-t", "--trend", action="store_true", help="附当日分时走势")
    ap.add_argument("-p", "--period", default="d",
                    choices=["d", "60", "30", "15"], help="主分析周期")
    ap.add_argument("-w", "--watch", type=int, metavar="N",
                    help="盯盘模式, N秒刷新")
    ap.add_argument("-f", "--find", metavar="KEYWORD", help="搜索股票代码")
    args = ap.parse_args()

    try:
        if args.find:
            search_code(args.find)
        elif args.index:
            print("-- 大盘概览 %s --" % time.strftime("%Y-%m-%d %H:%M"))
            for x in fetch_index_overview():
                mark = "+" if x["pct"] >= 0 else ""
                print("  %-8s %10.2f  %s%.2f%%" % (x["name"], x["price"], mark, x["pct"]))
        elif args.watch and args.codes:
            watch(args.codes, args.watch)
        elif args.codes:
            for c in args.codes:
                analyze(c, period=args.period, with_trend=args.trend)
        else:
            ap.print_help()
    except Exception as e:
        print("❌ 出错: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
