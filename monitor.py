#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股全自动盯盘推送（微信推送版）交易时段轮询自选股实时行情, 触发规则信号时推送到微信并写日志。非交易时段自动休眠; 每个交易日收盘后发一次收盘总结。微信渠道(二选一, config.json -> notify 中填凭证):  PushPlus   pushplus.plus 微信登录拿 token     -> "pushplus_token"  Server酱   sct.ftqq.com 微信扫码拿 SendKey    -> "serverchan_sendkey"  macOS弹窗  备用可选                           -> "macos": true用法:  python3 monitor.py --test     # 发一条测试消息, 验证微信通道  python3 monitor.py --once     # 手动跑一轮扫描(不限交易时段), 调试用  python3 monitor.py            # 正式挂机运行 (Ctrl+C 停止)  nohup python3 monitor.py >> monitor.log 2>&1 &   # 后台挂机信号规则(config.json可调):  涨跌幅超阈值 / 短时急拉急跌 / 突破或跌破20日高低点 /  明显放量(量比) / 触及涨跌停附近同类信号默认冷却30分钟, 避免轰炸。声明: 提醒仅为技术面参考, 不构成投资建议。"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
try:
    from trading_calendar import is_trading_day, is_trading_time, session, minutes_from_open, minutes_to_close
except Exception:
    def is_trading_time(*a, **k): return True
    def is_trading_day(*a, **k): return True
    def session(*a, **k): return "AM"
    minutes_from_open = lambda: 0
    minutes_to_close = lambda: 0
import stock as sm  # noqa: E402
import ai_advisor  # noqa: E402
import signal_postmortem  # noqa: E402
CFG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "monitor.log")
OUTCOMES_PATH = os.path.join(BASE, "signal_outcomes.json")
DEFAULT_CFG = {
    "watchlist": [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "300750", "name": "宁德时代"},
        {"code": "600036", "name": "招商银行"}
    ],
    "poll_interval_sec": 20,
    "pct_alert": 3.0,
    "quick_move_pct": 1.5,
    "quick_window_min": 10,
    "vol_ratio_alert": 2.5,
    "limit_near_pct": 0.2,
    "cooldown_min": 30,
    "adaptive_pct": {
        "enabled": True,
        "atr_mult": 2.0,
        "min_pct": 1.5,
        "max_pct": 8.0
    },
    "web_dashboard": {"enabled": True, "port": 8899},
    "ai_reports": {
        "enabled": True,
        "premarket": "09:15",
        "midday": "11:40",
        "close": "15:12"
    },
    "notify": {
        "macos": False,
        "macos_style": "dialog",
        "dialog_timeout_sec": 25,
        "sound": "Glass",
        "serverchan_sendkey": "",
        "pushplus_token": "",
        "wxpusher_app_token": "",
        "wxpusher_uid": "",
        "wecom_corpid": "",
        "wecom_secret": "",
        "wecom_agentid": 1000002,
        "bark_key": "",
        "feishu_webhook": "",
        "feishu_secret": "",
        "dingtalk_webhook": "",
        "dingtalk_secret": ""
    },
    "ai": {
        "enabled": True,
        "api_key": "",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "minimaxai/minimax-m3",
        "max_chars": 110,
        "daily_limit": 40,
        "timeout_sec": 30,
    },
}

def log(msg):
    line = "[%s] %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
    print(line, flush=True)

# 日志自动瘦身: nohup/launchd以追加方式写monitor.log, 不清理会无限增长占磁盘_LOG_MAX_BYTES = 5 * 1024 * 1024     # 超过5MB触发瘦身_LOG_KEEP_BYTES = 1 * 1024 * 1024    # 瘦身后保留末尾1MB

def shrink_log():
    """monitor.log超限时原地截断只留最近内容。
    同inode重写(r+b+truncate), 不影响外部重定向持有的文件描述符;
    追加写入(>>)下次会从新EOF继续, 无稀疏洞问题。"""
    p = os.path.join(BASE, "monitor.log")
    try:
        if os.path.getsize(p) <= _LOG_MAX_BYTES:
            return False
        with open(p, "rb") as f:
            f.seek(-_LOG_KEEP_BYTES, os.SEEK_END)
            tail = f.read()
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]          # 对齐到完整行开头
        with open(p, "r+b") as f:
            f.write(tail)
            f.truncate()
        return True
    except OSError:
        return False

_MAINTAIN_INTERVAL = 6 * 3600        # 存储维护周期(秒)

def load_cfg():
    if not os.path.exists(CFG_PATH):
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CFG, f, ensure_ascii=False, indent=2)
        log("已生成默认配置 config.json")
    with open(CFG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CFG)
    merged.update(cfg)
    return merged

def save_cfg(cfg):
    """原子写回 config.json(自选增删用)"""
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_alert": {}, "summary_sent": ""}

def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)

def flush_state_periodic(watcher_ref):
    global _state_dirty
    try:
        if _state_dirty:
            w = watcher_ref()
            if w:
                save_state(w.state)
            _state_dirty = False
    except Exception:
        pass

def mac_notify(title, msg, ncfg=None):
    """macOS通知: macos_style='notification'为横幅(系统会截断);
    ='dialog'(默认)为屏幕中央弹窗, 完整内容可读, 点'知道了'或超时自动关"""
    ncfg = ncfg or {}
    safe = lambda s: str(s).replace("\\", "").replace('"', "'")
    try:
        if ncfg.get("macos_style", "dialog") == "dialog":
            wait = int(ncfg.get("dialog_timeout_sec", 25))
            script = ('display dialog "%s" with title "%s" '
                      'buttons {"知道了"} default button "知道了" '
                      'with icon note giving up after %d'
                      % (safe(msg), safe(title), wait))
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True,
                               timeout=wait + 15)
        else:
            script = ('display notification "%s" with title "%s" sound name "%s"'
                      % (safe(msg.split("\n")[0]), safe(title),
                         ncfg.get("sound", "Glass")))
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            log("⚠ [macOS] 发送失败: %s" % r.stderr.strip())
            return False
        return True
    except Exception as e:
        log("⚠ [macOS] 异常: %s" % e)
        return False

def serverchan_send(sendkey, title, desp):
    """Server酱 -> 微信 (sct.ftqq.com 的 SendKey, SCT开头)"""
    try:
        url = "https://sctapi.ftqq.com/%s.send" % sendkey.strip()
        data = urllib.parse.urlencode(
            {"title": title[:32], "desp": desp[:3500]}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "stock-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("code") == 0:
            return True
        log("⚠ [Server酱] 失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
        return False
    except Exception as e:
        log("⚠ [Server酱] 异常: %r" % e)
        return False

def pushplus_send(token, title, content):
    """PushPlus -> 微信公众号 (pushplus.plus 的 token, 需实名)"""
    try:
        body = json.dumps({
            "token": token.strip(),
            "title": title[:100],
            "content": content[:3500],
            "template": "txt",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://www.pushplus.plus/send", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("code") == 200:
            return True
        log("⚠ [PushPlus] 失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
        return False
    except Exception as e:
        log("⚠ [PushPlus] 异常: %r" % e)
        return False

def wxpusher_get_uid(app_token):
    """查询 WxPusher 应用下粉丝UID(平台接口已不稳定, 失败则提示手动填)"""
    url = ("https://wxpusher.zjiecode.com/api/fan/list?appToken=%s&page=1&pageSize=5"
           % urllib.parse.quote(app_token.strip()))
    req = urllib.request.Request(url, headers={"User-Agent": "stock-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    if d.get("code") != 1000:
        raise IOError("WxPusher粉丝接口异常: %s" % d.get("msg"))
    records = ((d.get("data") or {}).get("records")) or []
    return records[0]["uid"] if records else None

def wxpusher_send(app_token, uid, title, content):
    """WxPusher -> 微信服务号 (wxpusher.zjiecode.com, 免费免实名)"""
    try:
        if not (uid or "").strip():
            try:
                uid = wxpusher_get_uid(app_token)
                if uid:
                    log("[WxPusher] 自动获取到UID: %s" % uid)
            except Exception as e:
                log("⚠ [WxPusher] 自动获取UID失败(%s), 请手动填 config.json 的 wxpusher_uid" % e)
        if not (uid or "").strip():
            return False
        body = json.dumps({
            "appToken": app_token.strip(),
            "content": ("%s\n%s" % (title, content))[:2000],
            "summary": title[:99],
            "contentType": 1,
            "uids": [uid.strip()],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("code") == 1000:
            return True
        log("⚠ [WxPusher] 失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
        return False
    except Exception as e:
        log("⚠ [WxPusher] 异常: %r" % e)
        return False

_wecom_tok = {"token": "", "expire": 0}


def wecom_send(corpid, secret, agentid, title, content):
    """企业微信自建应用 -> 个人微信(经微信插件)。完全免费无条数限制"""
    try:
        import urllib.parse as up
        if not _wecom_tok["token"] or time.time() > _wecom_tok["expire"]:
            url = ("https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s"
                   % (up.quote(corpid.strip()), up.quote(secret.strip())))
            with urllib.request.urlopen(url, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("errcode") != 0:
                log("⚠ [企业微信] 取token失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
                return False
            _wecom_tok["token"] = d["access_token"]
            _wecom_tok["expire"] = time.time() + int(d.get("expires_in", 7200)) - 300
        msg = {"touser": "@all", "msgtype": "text",
               "agentid": int(agentid),
               "text": {"content": "%s\n%s" % (title, content)[:2000]}}
        req = urllib.request.Request(
            "https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s"
            % _wecom_tok["token"],
            data=json.dumps(msg).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("errcode") == 0:
            return True
        log("⚠ [企业微信] 发送失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
        return False
    except Exception as e:
        log("⚠ [企业微信] 异常: %r" % e)
        return False

def bark_send(key_or_url, title, content):
    """Bark -> iPhone系统级推送 (App Store装Bark, 复制key)。免实名即时达"""
    try:
        v = key_or_url.strip().rstrip("/")
        if v.startswith("http"):
            key = v.rsplit("/", 1)[-1]
        else:
            key = v
        body = json.dumps({"device_key": key, "title": title[:80],
                           "body": content[:1500], "group": "stock"}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.day.app/push", data=body,
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("code") == 200:
            return True
        log("⚠ [Bark] 失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
        return False
    except Exception as e:
        log("⚠ [Bark] 异常: %r" % e)
        return False

def feishu_send(webhook, secret, title, content):
    """飞书自定义群机器人 webhook (消息进飞书App)"""
    try:
        text = "%s\n%s" % (title, content)[:3000]
        payload = {"msg_type": "text", "content": {"text": text}}
        if (secret or "").strip():
            ts = str(int(time.time()))
            sign_str = "%s\n%s" % (ts, secret.strip())
            sign = base64.b64encode(hmac.new(sign_str.encode(), b"", hashlib.sha256).digest()).decode()
            payload["timestamp"] = ts
            payload["sign"] = sign
        req = urllib.request.Request(webhook.strip(), data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        code = d.get("code", d.get("StatusCode"))
        if code in (0, "0"):
            return True
        log("⚠ [飞书] 失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
        return False
    except Exception as e:
        log("⚠ [飞书] 异常: %r" % e)
        return False

def dingtalk_send(webhook, secret, title, content):
    """钉钉自定义群机器人 webhook (消息进钉钉App; 建议机器人安全设置选'加签')"""
    try:
        url = webhook.strip()
        text = "%s\n%s" % (title, content)[:3000]
        if (secret or "").strip():
            ts = str(round(time.time() * 1000))
            sign_str = "%s\n%s" % (ts, secret.strip())
            sign = urllib.parse.quote_plus(base64.b64encode(
                hmac.new(secret.strip().encode(), sign_str.encode(),
                         hashlib.sha256).digest()).decode())
            url += "&timestamp=%s&sign=%s" % (ts, sign)
        body = json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                             headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("errcode") == 0:
            return True
        log("⚠ [钉钉] 失败: %s" % json.dumps(d, ensure_ascii=False)[:200])
        return False
    except Exception as e:
        log("⚠ [钉钉] 异常: %r" % e)
        return False

def notify(title, msg, notify_cfg=None):
    """按配置把消息分发到所有已启用渠道, 任一成功即算成功."""
    cfg = notify_cfg or {}
    results = []
    if cfg.get("macos"):
        if _mac_popup[0]:
            results.append(mac_notify(title, msg, cfg))
    if (cfg.get("serverchan_sendkey") or "").strip():
        results.append(serverchan_send(cfg["serverchan_sendkey"], title, msg))
    if (cfg.get("pushplus_token") or "").strip():
        results.append(pushplus_send(cfg["pushplus_token"], title, msg))
    if (cfg.get("wxpusher_app_token") or "").strip():
        results.append(wxpusher_send(cfg["wxpusher_app_token"],
                                    cfg.get("wxpusher_uid"), title, msg))
    if (cfg.get("wecom_corpid") or "").strip():
        results.append(wecom_send(cfg["wecom_corpid"], cfg["wecom_secret"],
                                 cfg.get("wecom_agentid", 1000002), title, msg))
    if (cfg.get("bark_key") or "").strip():
        results.append(bark_send(cfg["bark_key"], title, msg))
    if (cfg.get("feishu_webhook") or "").strip():
        results.append(feishu_send(cfg["feishu_webhook"],
                                  cfg.get("feishu_secret"), title, msg))
    if (cfg.get("dingtalk_webhook") or "").strip():
        results.append(dingtalk_send(cfg["dingtalk_webhook"],
                                    cfg.get("dingtalk_secret"), title, msg))
    if not results:
        log("⚠ 未配置任何推送渠道! 请在 config.json 的 notify 段填入凭证")
        return False
    ok = any(results)
    if not ok:
        log("⚠ 所有推送渠道均发送失败")
    return ok

# ------------------------------------------------------------------ 仪表盘 --
DASH_HTML_PATH = os.path.join(BASE, "dashboard.html")

_FALLBACK_HTML = (
    '<!doctype html><meta charset="utf-8">'
    '<body style="font:14px -apple-system,PingFang SC,sans-serif;padding:40px;color:#333">'
    '仪表盘页面文件缺失或读取失败: dashboard.html</body>'
)

def _load_dash_html():
    """仪表盘页面从独立的 dashboard.html 读取。

    不再把 5.9 万字符 HTML 内嵌进本文件: 内嵌字符串一旦被格式化/重写工具压成单行,
    JS 里的 "//" 行注释会吞掉后续代码, 整个 <script> 语法错误、页面无数据,
    而这类改动在 diff 里几乎看不出来。
    """
    try:
        with open(DASH_HTML_PATH, encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        log("⚠️ dashboard.html 读取失败(%s): %r" % (DASH_HTML_PATH, e))
        return _FALLBACK_HTML

_ADVICE_MIN_GAP = 60
_ADVICE_GLOBAL_GAP = 15
_last_ai_gen = [0]
_advice_cache = None
_ai_gen_busy = set()
_ai_gen_mu = threading.Lock()
_AI_CACHE_PATH = os.path.join(BASE, "ai_advice_cache.json")
_TREND_CACHE_PATH = os.path.join(BASE, "trend_cache.json")

# ---- AI建议限频 ----

_advice_cache = None
_ai_gen_busy = set()
_ai_gen_mu = threading.Lock()
_AI_CACHE_PATH = os.path.join(BASE, "ai_advice_cache.json")

def _advice_load():
    global _advice_cache
    if _advice_cache is None:
        try:
            with open(_AI_CACHE_PATH, encoding="utf-8") as f:
                _advice_cache = json.load(f)
        except Exception:
            _advice_cache = {}
    return _advice_cache

def _advice_save():
    try:
        tmp = _AI_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_advice_cache, f, ensure_ascii=False)
        os.replace(tmp, _AI_CACHE_PATH)
    except Exception:
        pass

_mac_popup = [True]
_API_CACHE = {"detail": {}, "kline": {}}
_TTL = {"detail": 90, "kline": 300}
def _cached(kind, key, producer):
    """带TTL的简单缓存(detail/kline专用)"""
    now = time.time()
    hit = _API_CACHE[kind].get(key)
    if hit and now - hit[0] < _TTL[kind]:
        return hit[1], True
    try:
        val = producer()
    finally:
        pass
    _API_CACHE[kind][key] = (now, val)
    # 清理过期项防膨胀
    for k in list(_API_CACHE[kind]):
        t, _v = _API_CACHE[kind][k]
        if now - t > max(_TTL.values()) * 4:
            _API_CACHE[kind].pop(k, None)
    return val, False


# Mac原生弹窗独立开关: 只影响桌面弹窗(osascript), 网页弹窗记录照常写入
_mac_popup = [True]
_ADVICE_MIN_GAP = 60
_ADVICE_GLOBAL_GAP = 15
_last_ai_gen = [0]
_advice_cache = None
_ai_gen_busy = set()
_ai_gen_mu = threading.Lock()
_AI_CACHE_PATH = os.path.join(BASE, "ai_advice_cache.json")
_TREND_CACHE_PATH = os.path.join(BASE, "trend_cache.json")

# ---- AI建议限频 ----


# Mac原生弹窗独立开关: 只影响桌面弹窗(osascript), 网页弹窗记录照常写入
_mac_popup = [True]
_ADVICE_MIN_GAP = 60
_ADVICE_GLOBAL_GAP = 15
_last_ai_gen = [0]
_advice_cache = None
_ai_gen_busy = set()
_ai_gen_mu = threading.Lock()
_AI_CACHE_PATH = os.path.join(BASE, "ai_advice_cache.json")
_TREND_CACHE_PATH = os.path.join(BASE, "trend_cache.json")

# ---- AI建议限频 ----


# ---- 分时走势持久化: 休市时展示最后有数据的交易时段 ----
_TREND_CACHE_PATH = os.path.join(BASE, "trend_cache.json")
_trend_disk = None

def _trend_cache_all():
    global _trend_disk
    if _trend_disk is None:
        try:
            with open(_TREND_CACHE_PATH, encoding="utf-8") as f:
                _trend_disk = json.load(f)
        except Exception:
            _trend_disk = {}
    return _trend_disk

def _trend_cache_save():
    try:
        tmp = _TREND_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_trend_cache_all(), f, ensure_ascii=False)
        os.replace(tmp, _TREND_CACHE_PATH)
    except Exception:
        pass

def _spark_from_trend(entry):
    """从分时缓存降采样出小趋势图数组(与大图同源, ≤40点)"""
    tr = (entry or {}).get("trend") or []
    if len(tr) < 8:
        return None
    step = max(1, len(tr) // 40)
    return [round(p[1], 4) for p in tr[::step]][-40:]

def _warm_trends(watch_codes):
    """每轮最多刷新2个最陈旧标的的分时缓存(轮转渐进),
    让左栏小趋势图逐步与真实日曲线完全一致。仅在盘中调用。"""
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    def prio(c):
        """从未缓存(-1) > 隔日旧数据(-0.5) > 当日按最后刷新时间升序"""
        e = _trend_cache_all().get(c)
        if not isinstance(e, dict):
            return -1
        if e.get("as_of") != today:
            return -0.5
        return e.get("_ts", 0)
    for c in sorted(watch_codes, key=prio)[:2]:
        try:
            pre, pts = sm.fetch_trends(c)
            if pts:
                _trend_cache_all()[c] = {
                    "preClose": pre,
                    "trend": [[p[0], p[1]] for p in pts],
                    "as_of": _session_label(),
                    "last_time": pts[-1][0], "_ts": now}
        except Exception:
            continue
    _trend_cache_save()

_last_session = {"d": None, "ts": 0}

def _session_label():
    now = time.time()
    if _last_session["d"] and now - _last_session["ts"] < 1800:
        return _last_session["d"]
    d = None
    try:
        df = sm.fetch_kline("1.000001", klt=101, lmt=1)
        d = str(df.iloc[-1]["date"])
    except Exception:
        d = None
    if d:
        _last_session.update(d=d, ts=now)
        return d
    return time.strftime("%Y-%m-%d")
def _detail_tencent(code):
    """腾讯分钟线备用源(标准沪深市场): 返回与东财同构的分时条目"""
    tc = sm.tencent_code(code)
    d = sm.http_json("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code="
                     + tc, timeout=8)
    node = ((d.get("data") or {}).get(tc) or {}).get("data") or {}
    rows = node.get("data") or []
    pre = None
    qt = (((d.get("data") or {}).get(tc) or {}).get("qt") or {}).get(tc) or []
    try:
        pre = float(qt[4]) if len(qt) > 4 and qt[4] else None
    except (TypeError, ValueError):
        pre = None
    pts = []
    for r in rows:
        f = str(r).split()
        if len(f) >= 2:
            try:
                t = "%s:%s" % (f[0][:2], f[0][2:4])
                pts.append([t, float(f[1])])
            except (ValueError, IndexError):
                continue
    if len(pts) < 2:
        raise IOError("腾讯分钟线无数据")
    return {"preClose": pre, "trend": pts,
            "as_of": ("%s-%s-%s" % (str(node.get("date", ""))[:4],
                                    str(node.get("date", ""))[4:6],
                                    str(node.get("date", ""))[6:8])
                      if node.get("date") else None),
            "last_time": pts[-1][0], "src": "tencent"}
def _session_frac():
    """当前交易时段已进行比例(0~1): 基于交易日历时段"""
    s = session()
    now = datetime.now()
    hm = now.hour * 60 + now.minute
    if s == "pre" or s == "closed":
        return 0.0
    if s == "break":
        return 0.5
    if s == "AM":
        a0 = 9 * 60 + 15  # 09:15 盘前算起点
        a1 = 11 * 60 + 30
        return (hm - a0) / (a1 - a0) * 0.5
    if s == "PM":
        p0 = 13 * 60
        p1 = 15 * 60
        return 0.5 + (hm - p0) / (p1 - p0) * 0.5
    return 0.0

def _vol_stat(df):
    """量能判定: 今量/5日均量, 盘中按时长折算。
    返回 {ratio(折算后), raw(未折算), label, level, adj} 或 None"""
    try:
        d = sm.add_indicators(df.copy())
        c = d.iloc[-1]
        if not c.vma5 or float(c.vma5) <= 0:
            return None
        raw = float(c.vol) / float(c.vma5)
        eff, adj = raw, False
        if is_trading_time():
            f = _session_frac()
            if f >= 0.15:
                # 开盘头9分钟不折算(样本太小)
                eff = raw / f
                adj = True
        if eff >= 2.5:
            lab, lvl = "显著放量", "hot"
        elif eff >= 1.5:
            lab, lvl = "放量", "high"
        elif eff >= 1.2:
            lab, lvl = "温和放量", "mid"
        elif eff >= 0.8:
            lab, lvl = "平量", "flat"
        elif eff >= 0.5:
            lab, lvl = "缩量", "low"
        else:
            lab, lvl = "地量", "dry"
        return {"ratio": round(eff, 2), "raw": round(raw, 2),
                "label": lab, "level": lvl, "adj": adj,
                "chg": float(d["pct"].iloc[-1])}
    except Exception:
        return None

def _bias_from_df(df):
    """基于日线技术指标的加权多空投票 → Technical Score 0~100
    返回 {bull, bear, total, items:[{n,v,w}], tech_score, tech_items}"""
    try:
        d = sm.add_indicators(df.copy())
        c = d.iloc[-1]
    except Exception:
        return None
    px = float(c["close"])
    hi20 = float(df["high"].iloc[-21:-1].max()) if len(df) >= 21 else px
    WEIGHTS = {
        "突破20日高": 20,
        "MA趋势": 15,
        "成交量": 15,
        "MACD": 10,
        "MA60": 10,
        "动量": 10,
        "RSI": 8,
        "BOLL": 5,
        "KDJ": 2,
    }
    def s(pos, neg, w):
        v = 1 if pos else (-1 if neg else 0)
        return v * w
    brk_20 = s(px > hi20 * 0.98, px < hi20 * 0.98, WEIGHTS["突破20日高"])
    ma_trend = s(px > c.ma5 and c.ma5 > c.ma20,
                px < c.ma5 and c.ma5 < c.ma20, WEIGHTS["MA趋势"])
    ma60 = s(px > c.ma60, px < c.ma60, WEIGHTS["MA60"])
    macd = s(c.macd > 0, c.macd < 0, WEIGHTS["MACD"])
    rsi = s(c.rsi6 > 55, c.rsi6 < 45, WEIGHTS["RSI"])
    kdj = s(c.kdj_k > c.kdj_d, c.kdj_k < c.kdj_d, WEIGHTS["KDJ"])
    boll = s(px > c.boll_mid, px < c.boll_mid, WEIGHTS["BOLL"])
    momentum = s((d["pct"].iloc[-3:] > 0).all(), (d["pct"].iloc[-3:] < 0).all(), WEIGHTS["动量"])
    vol = s(float(c.vma5 or 0) > 0 and float(c.vol) > float(c.vma5)
            and float(d["pct"].iloc[-1]) > 0,
            float(c.vma5 or 0) > 0 and float(c.vol) > float(c.vma5)
            and float(d["pct"].iloc[-1]) < 0, WEIGHTS["成交量"])
    items = [
        ("突破20日高", brk_20, WEIGHTS["突破20日高"]),
        ("MA趋势", ma_trend, WEIGHTS["MA趋势"]),
        ("MA60", ma60, WEIGHTS["MA60"]),
        ("MACD", macd, WEIGHTS["MACD"]),
        ("RSI", rsi, WEIGHTS["RSI"]),
        ("KDJ", kdj, WEIGHTS["KDJ"]),
        ("BOLL", boll, WEIGHTS["BOLL"]),
        ("动量", momentum, WEIGHTS["动量"]),
        ("成交量", vol, WEIGHTS["成交量"]),
    ]
    raw_score = sum(v for _, v, _ in items)
    tech_score = int(round((raw_score + 95) / 190 * 100))
    tech_score = max(0, min(100, tech_score))
    bull = sum(1 for _, v, _ in items if v > 0)
    bear = sum(1 for _, v, _ in items if v < 0)
    return {
        "bull": bull, "bear": bear, "total": len(items),
        "items": [{"n": n, "v": v > 0, "w": w, "score": v}
                   for n, v, w in items],
        "tech_score": tech_score,
        "raw": raw_score,
        "weights": WEIGHTS,
    }

def _api_detail(code):
    """分时详情: 盘中实时拉取(90s缓存)+落盘; 休市只读缓存(30min),
    展示最后有数据的交易时段, 标注截至日期与时刻。"""
    now = time.time()
    disk = _trend_cache_all()
    mem = _API_CACHE["detail"].get(code)
    if is_trading_time():
        ttl = 90
    else:
        ttl = 1800
    if mem and now - mem[0] < ttl and not mem[1].get("stale"):
        return dict(mem[1])
    entry = None
    # 闭市但内存磁盘都没有(首次安装等): 引导式拉取一次
    if is_trading_time() or (not mem and not disk.get(code)):
        try:
            pre, pts = sm.fetch_trends(code)
            if pts:
                entry = {"preClose": pre,
                         "trend": [[p[0], p[1]] for p in pts],
                         "avg": [p[0], p[4]] if pts else None,
                         "as_of": _session_label(),
                         "last_time": pts[-1][0]}
        except Exception:
            entry = None
        if entry is None and sm.secid(code).startswith(("0.", "1.")):
            try:
                entry = _detail_tencent(code)
                entry.setdefault("as_of", _session_label())
                entry.setdefault(
                    "last_time",
                    entry["trend"][-1][0] if entry.get("trend") else "")
            except Exception:
                entry = None
    if entry:
        entry["_ts"] = now
        _API_CACHE["detail"][code] = (now, entry)
        disk[code] = entry
        _trend_cache_save()
        return dict(entry)
    # 拉取失败/休市: 回退到最后保留的分时
    for cand in ((mem[1] if mem else None), disk.get(code)):
        if cand:
            out = dict(cand)
            out["stale"] = True  # 标注为收盘留存数据
            _API_CACHE["detail"][code] = (now, out)
            return out
    return {}

def _api_kline(code, n=50):
    def prod():
        # 指标计算需≥61根(MA60), 展示仍按请求条数tail(n)
        df = sm.fetch_kline(code, klt=101, lmt=max(int(n), 120))
        if df.empty:
            raise IOError("无K线数据(休市或限流)")
        out = {"bars": [[r["date"], r["open"], r["close"], r["high"], r["low"]]
                        for _, r in df.tail(int(n)).iterrows()]}
        try:
            out["bias"] = _bias_from_df(df)
        except Exception:
            pass
        try:
            out["vol"] = _vol_stat(df)
        except Exception:
            pass
        # 量能: 放量/缩量判定
        return out
    data, cached = _cached("kline", "%s|%d" % (code, n), prod)
    return data or {}

def _safe_rescan(w):
    """自选增删后立即静默重扫一轮, 让面板秒级反映变化"""
    try:
        if w.enabled:
            w.scan_once(verbose=False, quiet=True)
    except Exception as e:
        pass


def _generate_advice(code):
    """真实调用NIM生成一次建议: 结构化优先, 失败降级文本。返回含gen_ts的缓存条目"""
    ai = load_cfg().get("ai") or {}
    def prod_text():
        snap = ai_advisor.build_snapshot(code)
        user = ("请基于以下实时快照给出该标的操作建议: 短线倾向(偏多/偏空/观望)、"
                "参考支撑与压力位、建议止损位、一句风险提示。不超过110字, "
                "结尾注明'仅供参考'。\n数据: %s" % json.dumps(snap, ensure_ascii=False))
        text = ai_advisor.chat(ai, [
            {"role": "system",
             "content": ("你是严谨的A股短线技术分析师, 直接给结论不客套, "
                           "不要markdown。")},
            {"role": "user", "content": user}], max_tokens=300)
        return {"mode": "text", "text": text or "(AI未返回内容)"}
    sc = {"market_score": 50, "sector_score": 50, "stock_score": 50,
          "signal_score": 50, "risk_score": 45}
    # 五层评分: 从真实数据构建 (watcher可能为None时用默认值)
    w = globals().get("watcher")
    if w is not None:
        try:
            rt = None
            for q in (w.last_quotes or []):
                if q.get("code") == code: rt = q; break
            if rt is None:
                try: rt = sm.fetch_rt(code)
                except: rt = None
            hist = w.price_hist.get(code, deque())
            score_result = w._signal_score(rt, code, hist) if rt else {}
            sc["signal_score"] = int(score_result.get("score", 50)) if isinstance(score_result, dict) else 50
            regime = w._mkt_cache.get("regime", "NEUTRAL")
            sh = w._mkt_cache.get("__env__", {}).get("sh_pct", 0.0) or 0.0
            if regime == "BULL":    sc["market_score"] = min(100, 70 + int(sh * 5))
            elif regime == "BEAR":   sc["market_score"] = max(0, 30 + int(sh * 5))
            else:                    sc["market_score"] = 50 + int(sh * 5)
            sectors = w._mkt_cache.get("sectors", {})
            grp = w._py_grp(code) if hasattr(w, '_py_grp') else '个股'
            sec_stat = sectors.get(grp, {})
            sec_avg = sec_stat.get("avg_pct", 0)
            if sec_avg >= 1.5:    sc["sector_score"] = 90
            elif sec_avg >= 0.5:  sc["sector_score"] = 75
            elif sec_avg >= -0.5: sc["sector_score"] = 55
            else:                  sc["sector_score"] = 35
            sc["stock_score"] = sc["signal_score"]
            if rt:
                pct = rt.get("pct", 0) or 0; vr = rt.get("vol_ratio", 0) or 0
                risk = 45
                if pct > 3.0 and vr >= 3.0:   risk = 70
                elif pct > 2.0 and vr >= 2.5:  risk = 60
                elif pct > 1.0 and vr >= 2.0:  risk = 55
                elif pct < -3.0:                risk = 30
                elif pct < -1.0:               risk = 35
                qm = score_result.get("quick_meta", {}) if isinstance(score_result, dict) else {}
                if qm.get("drawdown", 0) > 2.0: risk = max(risk, 65)
                sc["risk_score"] = risk
        except Exception:
            pass
    try:
        data = ai_advisor.build_structured_advice(code, ai_cfg=ai, scores=sc)
        if data and isinstance(data, dict):
            data["_scores"] = sc
            conf = data.get("confidence")
            if isinstance(conf, (int, float)): data.setdefault("score", int(conf))
            # 关键位(支撑/压力)本地计算补进 levels: 模型常漏该字段, 不能依赖它返回
            _lv = (w.levels or {}).get(code) or {}
            _local_lv = []
            if _lv.get("hi20") is not None: _local_lv.append({"name": "近20日高点·压力", "price": float(_lv["hi20"])})
            if _lv.get("lo20") is not None: _local_lv.append({"name": "近20日低点·支撑", "price": float(_lv["lo20"])})
            _model_lv = data.get("levels") if isinstance(data.get("levels"), list) else []
            _seen = set(); _merged = []
            for _it in (_local_lv + _model_lv):
                _nm = _it.get("name")
                if _nm in _seen: continue
                _seen.add(_nm); _merged.append(_it)
            if _merged: data["levels"] = _merged
            val = {"mode": "structured", "data": data, "scores": sc}
        else:
            val = {"mode": "scores_only", "scores": sc,
                   "data": {"_scores": sc}}
            try:
                text_val = prod_text()
                if isinstance(text_val, dict): val["text"] = text_val.get("text", "")
            except Exception:
                pass
    except Exception as e:
        val = {"mode": "scores_only",
               "scores": {"market_score": 50, "sector_score": 50,
                          "stock_score": 50, "signal_score": 50, "risk_score": 45},
               "data": {"_scores": sc},
               "error": str(e)[:120]}
    # 关键位(支撑/压力)本地计算: 任何分支都保证注入, 不依赖AI是否成功/返回levels
    try:
        _lvw = globals().get("watcher")
        _lv = (_lvw.levels or {}).get(code, {}) if _lvw else {}
        _ll = []
        if _lv.get("hi20") is not None: _ll.append({"name": "近20日高点·压力", "price": float(_lv["hi20"])})
        if _lv.get("lo20") is not None: _ll.append({"name": "近20日低点·支撑", "price": float(_lv["lo20"])})
        if _ll:
            _d = val.get("data")
            if not isinstance(_d, dict): _d = val["data"] = {}
            _cur = _d.get("levels")
            _out = list(_ll); _seen = {x.get("name") for x in _ll}
            if isinstance(_cur, list):
                for _x in _cur:
                    if _x.get("name") not in _seen:
                        _seen.add(_x.get("name")); _out.append(_x)
            _d["levels"] = _out
    except Exception:
        pass
    _last_ai_gen[0] = time.time()
    return dict(val, gen_ts=_last_ai_gen[0])
def _api_ai_advice(code, force=False):
    """AI操作建议(每标的60秒/全局15秒生成限频):
    - 展开查看: 只读缓存, 绝不自动请求NIM; 无缓存才首次生成
    - 🔄强制刷新: 受限频约束, 间隔内直接返回旧建议并附剩余秒数
    - 生成失败: 回退展示上一次建议(如有)    """
    ai = load_cfg().get("ai") or {}
    if not (ai.get("enabled") and str(ai.get("api_key") or "").strip()):
        return {"mode": "text", "text": "未配置AI(需 config.json -> ai.api_key)",
                "stale": False, "gen_ts": 0}
    cache = _advice_load()
    hit = cache.get(code)
    if isinstance(hit, dict) and hit.get("mode") == "text" and not hit.get("scores"):
        hit = None
    now = time.time()
    need_gen = (hit is None) or bool(force)
    if need_gen:
        gap_code = (int(_ADVICE_MIN_GAP - (now - hit.get("gen_ts", 0)))
                    if isinstance(hit, dict) and hit.get("gen_ts") else 0)
        gap_all = int(_ADVICE_GLOBAL_GAP + 1 - (now - _last_ai_gen[0]))
        wait = max(gap_code, gap_all, 0)
        if wait > 0:
            if hit:
                out = dict(hit); out.setdefault("stale", True); out["rate_limited"] = wait; return out
            return {"mode": "text", "text": "AI接口限频中(%d秒后可再试)" % wait,
                    "stale": True, "gen_ts": 0, "rate_limited": wait}
        try:
            with _ai_gen_mu:
                already = code in _ai_gen_busy
                if not already: _ai_gen_busy.add(code)
            if already and isinstance(hit, dict):
                out = dict(hit); out.setdefault("stale", True); out["generating"] = True; return out
            try: val = _generate_advice(code)
            finally:
                with _ai_gen_mu: _ai_gen_busy.discard(code)
        except Exception as e:
            if isinstance(hit, dict):
                out = dict(hit); out["error"] = repr(e)[:120]; out.setdefault("stale", True); return out
            return {"mode": "text", "text": "AI生成失败: %r" % e,
                    "stale": False, "gen_ts": 0}
        cache[code] = val; _advice_save(); out = dict(val); out["stale"] = False
        if "scores" not in out:
            try:
                w = watcher
                rt = next((q for q in (w.last_quotes or []) if q.get("code") == code), None)
                if rt is None:
                    try: rt = sm.fetch_rt(code)
                    except: rt = None
                if rt:
                    hist = w.price_hist.get(code, deque())
                    sc = w._signal_score(rt, code, hist)
                    sig = sc.get("score", 50) if isinstance(sc, dict) else 50
                    regime = w._mkt_cache.get("regime", "NEUTRAL")
                    sh = w._mkt_cache.get("__env__", {}).get("sh_pct", 0.0) or 0.0
                    mkt_s = min(100, 70+int(sh*5)) if regime=="BULL" else (max(0,30+int(sh*5)) if regime=="BEAR" else 50+int(sh*5))
                    out["scores"] = {"market_score": mkt_s, "sector_score": 50,
                        "stock_score": sig, "signal_score": sig, "risk_score": 45}
            except Exception: pass
        return out
    out = dict(hit); out.setdefault("stale", True); return out
def _api_ai_chat(code, q, history=None):
    """追问式对话: 基于最新快照回答, 不限次数"""
    q = str(q or "").strip()[:200]
    if not q: return {"answer": ""}
    ai = load_cfg().get("ai") or {}
    if not (ai.get("enabled") and str(ai.get("api_key") or "").strip()):
        return {"answer": "未配置AI(需 config.json -> ai.api_key)"}
    advice = None
    hit = _advice_load().get(code)
    if isinstance(hit, dict) and hit.get("mode") == "structured": advice = hit.get("data")
    hist = []
    for h in (history or [])[:6]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
            hist.append({"role": h["role"], "content": str(h.get("content", ""))[:300]})
    try:
        ans = ai_advisor.ask_followup(code, q, history=hist, advice=advice, ai_cfg=ai)
    except Exception as e:
        return {"answer": "AI调用失败: %r" % e}
    return {"answer": ans or "(AI未返回内容)"}
def _start_dashboard(watcher, port):
    """在守护进程内启动一个只读Web仪表盘线程"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import os
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def _send(self, code, ctype, text):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def _send_bytes(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                if parsed.path.startswith("/api/status"):
                    w = watcher
                    data = {
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "trading": is_trading_time(),
                        "enabled": w.enabled,
                        "mac_popup": w.mac_popup,
                        "scan_ts": w.last_scan_ts,
                        "quotes": w.last_quotes,
                        "watchlist": w.cfg.get("watchlist",[]),
                        "sectors": w._mkt_cache.get("sectors", {}),
                        "alerts": w.today_alerts,
                        "history": w.alert_history[-40:],
                    }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
                    return
                elif parsed.path.startswith("/api/mainline"):
                    try:
                        w = watcher
                        today = date.today().isoformat()
                        if not w.last_quotes:
                            report = {"text": "⏳ 等待首轮行情扫描后生成主线报告",
                                "sectors": [], "strong_stocks": [],
                                "money_sectors": [], "market_state": "等待首轮扫描",
                                "session": "pending", "generated": time.strftime("%Y-%m-%d %H:%M"),
                                "trading_day": is_trading_day()}
                        elif not is_trading_day():
                            report = {"text": "💤 今天不是交易日, 主线报告暂停推送",
                                "sectors": [], "strong_stocks": [],
                                "money_sectors": [], "market_state": "💤 休市(非交易日)",
                                "session": "closed", "generated": time.strftime("%Y-%m-%d %H:%M"),
                                "trading_day": False}
                        elif not is_trading_time():
                            s = session()
                            report = {"text": "⏸ 当前时段: %s, 主线报告暂停推送" % s,
                                "sectors": [], "strong_stocks": [], "money_sectors": [],
                                "market_state": "⏸ 休市(%s)" % s, "session": s,
                                "generated": time.strftime("%Y-%m-%d %H:%M"), "trading_day": True}
                        else:
                            report = w.generate_mainline_report()
                    except Exception as e:
                        report = {"text": "主线生成异常: %s" % e, "sectors": []}
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(json.dumps(report, ensure_ascii=False, default=str).encode("utf-8"))
                    return
                elif parsed.path.startswith("/api/strategy_stats"):
                    try:
                        import signal_postmortem as pm
                        stats = pm.compute_stats()
                        best = pm.best_params_quick_move()
                        data = {"stats": stats, "best_params": best,
                                "total_signals": len(pm.load()),
                                "generated": time.strftime("%Y-%m-%d %H:%M")}
                    except Exception as e:
                        data = {"error": str(e), "stats": {}, "best_params": {}}
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
                    return
                elif parsed.path.startswith("/api/search"):
                    q = (qs.get("q") or [""])[0][:40]
                    try:
                        res = sm.search_instruments(q)
                    except Exception as e:
                        res = []
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps({"results": res}, ensure_ascii=False))
                    return
                elif parsed.path.startswith("/api/wl_add"):
                    w = watcher
                    code = (qs.get("code") or [""])[0].strip()[:32]
                    name = (qs.get("name") or [""])[0].strip()[:24] or code
                    if not code:
                        self._send(400, "application/json; charset=utf-8", '{"error":"缺少代码"}')
                        return
                    wl = w.cfg.get("watchlist") or []
                    if any(i.get("code") == code for i in wl):
                        self._send(200, "application/json; charset=utf-8",
                                   json.dumps({"ok": False, "msg": "已在自选中"}, ensure_ascii=False))
                        return
                    wl.append({"code": code, "name": name})
                    w.cfg["watchlist"] = list(wl)
                    save_cfg(w.cfg)
                    log("➕ 自选已添加: %s %s" % (name, code))
                    def _fetch_one():
                        try:
                            rt = sm.fetch_rt(code)
                            rt["name"] = rt.get("name") or name
                            rt["code"] = code
                            rt["group"] = ""
                            rt["spark"] = []
                            rt["hi20"] = rt.get("hi20")
                            rt["lo20"] = rt.get("lo20")
                            new_qs = [q for q in w.last_quotes if q.get("code") != code]
                            new_qs.append(rt)
                            w.last_quotes = new_qs
                            log("📊 已获取 %s 行情, 立即入列" % name)
                        except Exception as e:
                            log("⚠️ 新加标的拉取失败[%s]: %s" % (code, e))
                    threading.Thread(target=_fetch_one, daemon=True).start()
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps({"ok": True}, ensure_ascii=False))
                elif parsed.path.startswith("/api/wl_del"):
                    w = watcher
                    code = (qs.get("code") or [""])[0].strip()[:32]
                    wl = w.cfg.get("watchlist") or []
                    new = [i for i in wl if i.get("code") != code]
                    if len(new) == len(wl) or not new and len(wl) <= 1:
                        self._send(200, "application/json; charset=utf-8",
                                   json.dumps({"ok": False, "msg": "不存在或不可删空"}, ensure_ascii=False))
                        return
                    removed = [i for i in wl if i.get("code") == code]
                    w.cfg["watchlist"] = new
                    w.levels.pop(code, None)
                    w.price_hist.pop(code, None)
                    w.prev_price.pop(code, None)
                    w.last_quotes = [q for q in w.last_quotes if q.get("code") != code]
                    save_cfg(w.cfg)
                    log("➖ 自选已删除: %s" % (removed[0].get("name") if removed else code))
                    threading.Thread(target=lambda: _safe_rescan(w), daemon=True).start()
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps({"ok": True}, ensure_ascii=False))
                    return
                elif parsed.path.startswith("/api/wl_reorder"):
                    w = watcher
                    order = (qs.get("order") or [""])[0]
                    if not order:
                        self._send(400, "application/json; charset=utf-8", '{"error":"缺少 order 参数"}')
                        return
                    new_codes = [c.strip() for c in order.split(",") if c.strip()]
                    wl = w.cfg.get("watchlist") or []
                    by_code = {i.get("code"): dict(i) for i in wl}
                    existing = [by_code[c] for c in new_codes if c in by_code]
                    kept = [i for i in wl if i.get("code") not in new_codes]
                    w.cfg["watchlist"] = existing + kept
                    save_cfg(w.cfg)
                    log("🔄 自选顺序已重排: %d 条" % len(existing))
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps({"ok": True, "count": len(existing)}, ensure_ascii=False))
                    return
                elif parsed.path.startswith("/api/macpopup"):
                    w = watcher
                    arg = (qs.get("on") or [""])[0]
                    new = (arg == "1") if arg in ("0", "1") else (not w.mac_popup)
                    w.set_mac_popup(new)
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps({"mac_popup": w.mac_popup}, ensure_ascii=False))
                    return
                elif parsed.path.startswith("/api/toggle"):
                    w = watcher
                    arg = (qs.get("on") or [""])[0]
                    new = (arg == "1") if arg in ("0", "1") else (not w.enabled)
                    w.set_enabled(new)
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps({"enabled": w.enabled}, ensure_ascii=False))
                    return
                elif parsed.path.startswith("/api/detail"):
                    code = (qs.get("code") or [""])[0]
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps(_api_detail(code), ensure_ascii=False))
                    return
                elif parsed.path.startswith("/api/kline"):
                    code = (qs.get("code") or [""])[0]
                    n = int((qs.get("n") or ["50"])[0])
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps(_api_kline(code, n), ensure_ascii=False))
                    return
                elif parsed.path.startswith("/api/aiadvice"):
                    code = (qs.get("code") or [""])[0]
                    force = (qs.get("force") or ["0"])[0] == "1"
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps(_api_ai_advice(code, force), ensure_ascii=False))
                    return
                elif parsed.path.startswith("/api/aichat"):
                    code = (qs.get("code") or [""])[0]
                    q = (qs.get("q") or [""])[0]
                    hist = []
                    try:
                        hist = json.loads((qs.get("h") or ["[]"])[0])
                    except Exception:
                        pass
                    self._send(200, "application/json; charset=utf-8",
                               json.dumps(_api_ai_chat(code, q, hist), ensure_ascii=False))
                    return
                elif parsed.path in ("/", "/index.html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "public, max-age=0, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Expires", "0")
                    self.end_headers()
                    self.wfile.write(dash.encode("utf-8"))
                    return
                elif parsed.path.startswith("/static/"):
                    rel = parsed.path[len("/static/"):]
                    if ".." in rel or "/" in rel or "\\" in rel:
                        self._send(404, "text/plain; charset=utf-8", "not found")
                    else:
                        fp = os.path.join(BASE, rel)
                        if not os.path.isfile(fp):
                            self._send(404, "text/plain; charset=utf-8", "not found")
                        else:
                            ext = os.path.splitext(rel)[1].lower()
                            ctype = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                                     "png":"image/png","gif":"image/gif",
                                     "svg":"image/svg+xml","ico":"image/x-icon",
                                     "webp":"image/webp"}.get(ext.lstrip("."),
                                       "application/octet-stream")
                            with open(fp, "rb") as f:
                                self._send_bytes(200, ctype, f.read())
                else:
                    self._send(404, "text/plain; charset=utf-8", "not found")
            except BrokenPipeError:
                pass
            except Exception as e:
                try:
                    self._send(500, "application/json; charset=utf-8",
                               json.dumps({"error": repr(e)[:200]}, ensure_ascii=False))
                except Exception:
                    pass
    dash = _load_dash_html()
    # 自检: dashboard.html 一旦被压成单行, JS 中的 "//" 行注释会把后面的代码整段吞掉,
    # 导致 <script> 整体语法错误 -> 页面只剩静态骨架、无任何 API 数据。此处提前告警。
    try:
        import re as _re
        _js = dash[dash.find("<script>") + 8:dash.rfind("</script>")]
        _bad = [c for c in _re.findall(r"//[^\n]*", _js)
                if _re.search(r"function\b|const\s|let\s|var\s", c)]
        if _bad:
            log("⚠️ dashboard.html 疑似被压缩成单行: %d 处JS注释吞并了后续代码, 页面将无数据" % len(_bad))
    except Exception:
        pass
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    except Exception as e:
        log("⚠️ Web仪表盘启动失败(端口%s): %s" % (port, e))
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("🌐 Web仪表盘已启动: http://127.0.0.1:%s" % port)
    return srv

def is_trading_time_legacy(now=None):
    """兼容旧接口: 交易时段内(含盘前)"""
    return is_trading_time(now)

def seconds_until_next_session(now=None):
    """距离下一个可扫描时刻的秒数, 基于交易日历"""
    now = now or datetime.now()
    if not is_trading_day():
        nxt = now + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        target = nxt.replace(hour=9, minute=15, second=0, microsecond=0)
        return max(0, int((target - now).total_seconds()))
    hm = now.hour * 100 + now.minute
    if hm < 915:
        target = now.replace(hour=9, minute=15, second=0, microsecond=0)
    elif hm < 1130:
        return 60
    elif hm < 1300:
        target = now.replace(hour=12, minute=50, second=0, microsecond=0)
    elif hm <= 1500:
        return 60
    else:
        nxt = now + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        target = nxt.replace(hour=9, minute=15, second=0, microsecond=0)
    return max(60, int((target - now).total_seconds()))# ------------------------------------------------------------------ 引擎 --


class Watcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = load_state()
        self.price_hist = {}
        self.prev_price = {}
        self.levels = {}
        self._lv_fail = {}
        self.today_alerts = []
        self.pending_alerts = []
        self.last_quotes = []
        self.last_scan_ts = 0
        self.enabled = bool(self.state.get("enabled", True))
        self.mac_popup = bool(self.state.get("mac_popup", True))
        _mac_popup[0] = self.mac_popup
        snap = self.state.get("last_snap") or {}
        if snap.get("quotes"):
            self.last_quotes = snap["quotes"]
            self.last_scan_ts = snap.get("ts") or 0
        self._dirty = False
        self._last_flush = time.time()
        self._trading_day_cache = {}
        self._mkt_cache = {"sh_pct": 0.0, "sector_pct": 0.0}
        # 从磁盘恢复历史信号流(避免重启后实时信号栏清空)
        self.alert_history = self.state.get("alert_history", []) or []
        self._next_maintain = 0.0
        if self.levels:
            log("已载入缓存关键位")

    def refresh_levels(self, force=False):
        import stock as sm
        now = time.time()
        need = []
        for item in self.cfg.get("watchlist", []):
            c = item.get("code", "")
            lv = self.levels.get(c)
            if lv and not force and now - lv.get("updated", 0) < 1800:
                continue
            if now < self._lv_fail.get(c, 0):
                continue
            need.append(item)
        for idx, item in enumerate(need):
            if idx: time.sleep(0.8)
            c = item.get("code", "")
            try:
                df = sm.fetch_kline(c, klt=101, lmt=60, fqt=1)
                if df.empty or len(df) < 20: continue
                hi20 = float(df["high"].iloc[-21:-1].max()) if len(df) >= 21 else float(df["high"].max())
                lo20 = float(df["low"].iloc[-21:-1].min()) if len(df) >= 21 else float(df["low"].min())
                self.levels[c] = {"hi20": hi20, "lo20": lo20, "updated": now}
            except Exception:
                self._lv_fail[c] = now + 300
        log("关键位刷新完成")

    def _maintain_storage(self):
        try:
            self.state["enabled"] = self.enabled
            self.state["mac_popup"] = self.mac_popup
            self.state["alert_history"] = self.alert_history
            save_state(self.state)
            self._dirty = False
        except Exception:
            pass

    def cooled(self, key):
        t = self.state.get("last_alert", {}).get(key, 0)
        return time.time() - t >= self.cfg.get("cooldown_min", 30) * 60

    def _signal_score(self, rt, code, hist):
        try:
            pct = rt.get("pct") or 0
            price = rt.get("price") or 0
            # quick move meta from price_hist
            quick_meta = {"drawdown": 0.0, "momentum": 0.0}
            if hist and len(hist) >= 2:
                # last price vs ~10 min ago (approx from deque length)
                old = hist[0][1] if isinstance(hist[0], (list, tuple)) else hist[0]
                if isinstance(old, (int, float)) and price:
                    quick_meta["drawdown"] = round((price - old) / old * 100, 2)
            # Simple scoring based on pct + vol
            score = 50 + int(min(pct * 10, 40))
            if rt.get("vol_ratio", 0) >= 2.5:
                score += 10
            score = min(100, max(0, score))
            return {"score": score, "quick_meta": quick_meta}
        except Exception:
            return {"score": 50, "quick_meta": {}}

    def _refresh_regime(self):
        try:
            import stock as sm
            # Try index overview for market direction
            idx = sm.fetch_index_overview()
            sh_pct = 0.0
            for x in idx:
                if "上证" in str(x.get("name", "")) or "000001" in str(x.get("code", "")):
                    sh_pct = x.get("pct") or 0.0
                    break
            if sh_pct >= 1.0:
                regime = "BULL"
            elif sh_pct <= -1.0:
                regime = "BEAR"
            else:
                regime = "NEUTRAL"
            self._mkt_cache["regime"] = regime
            self._mkt_cache["__env__"] = {"sh_pct": sh_pct}
        except Exception:
            self._mkt_cache.setdefault("regime", "NEUTRAL")
            self._mkt_cache.setdefault("__env__", {"sh_pct": 0.0})

    def check(self, rt, code):
        try:
            pct = rt.get("pct") or 0
            vol_ratio = rt.get("vol_ratio") or 0
            price = rt.get("price") or 0
            cfg = self.cfg
            # 1) 涨跌幅
            if abs(pct) >= cfg.get("pct_alert", 3.0):
                msg = ("涨停" if pct > 0 else "跌停") if abs(pct) >= 9.5 else ("大涨" if pct > 0 else "大跌")
                msg += " %.2f%%" % pct
                if self.cooled(code + ":pct"):
                    self.fire(msg + " [%s]" % rt.get("name", code))
                    self._record_alert(code, msg, "pct")
            # 2) 急拉急跌 (10 min quick move approx via price_hist)
            hist = self.price_hist.get(code, deque())
            if len(hist) >= 2:
                old_price = hist[0][1] if isinstance(hist[0], (list, tuple)) else hist[0]
                if isinstance(old_price, (int, float)) and price:
                    quick_pct = (price - old_price) / old_price * 100
                    if abs(quick_pct) >= cfg.get("quick_move_pct", 1.5):
                        msg = ("急拉" if quick_pct > 0 else "急跌") + " %.2f%% (10m)" % quick_pct
                        if self.cooled(code + ":quick"):
                            self.fire(msg + " [%s]" % rt.get("name", code))
                            self._record_alert(code, msg, "quick")
            # 3) 放量
            if vol_ratio >= cfg.get("vol_ratio_alert", 2.5):
                msg = "显著放量 %.1fx" % vol_ratio
                if self.cooled(code + ":vol"):
                    self.fire(msg + " [%s]" % rt.get("name", code))
                    self._record_alert(code, msg, "vol")
            # 4) 涨跌停附近
            if abs(pct) >= (10 - cfg.get("limit_near_pct", 0.2)):
                msg = "涨跌停附近 %.2f%%" % pct
                if self.cooled(code + ":limit"):
                    self.fire(msg + " [%s]" % rt.get("name", code))
                    self._record_alert(code, msg, "limit")
            # Update price history for next scan
            self.price_hist.setdefault(code, deque(maxlen=120))
            self.price_hist[code].append((time.time(), price))
            self.prev_price[code] = price
        except Exception:
            pass

    def fire(self, text):
        try:
            if not self.enabled:
                return
            self.pending_alerts.append({"text": text, "ts": time.time()})
        except Exception:
            pass

    def flush_alerts(self):
        try:
            if not self.pending_alerts:
                return
            for a in list(self.pending_alerts):
                text = a.get("text", "")
                # Push notifications
                ok = notify("🔔 盯盘信号", text + "\n(仅为技术面参考，不构成投资建议)", self.cfg.get("notify"))
                if ok:
                    if self.mac_popup:
                        mac_notify("🔔 盯盘信号", text)
                # Record
                self.today_alerts.append(text)
            self.pending_alerts.clear()
        except Exception:
            pass

    def _record_alert(self, code, msg, kind="watch"):
        try:
            now = time.time()
            date_s = time.strftime("%Y-%m-%d")
            time_s = time.strftime("%H:%M:%S")
            # 标题: 收盘总结/提醒类直接用 code; 个股信号补全名称, 与前端 renderFeed 字段对齐
            title = code
            if kind != "watch":
                for q in (self.last_quotes or []):
                    if q.get("code") == code:
                        nm = q.get("name", "")
                        if nm:
                            title = "%s %s" % (nm, code)
                        break
            entry = {"code": code, "msg": msg, "kind": kind, "date": date_s,
                     "ts": now, "time": time_s, "title": title, "body": msg}
            self.alert_history.append(entry)
            # Persist in state for web
            if "last_alert" not in self.state:
                self.state["last_alert"] = {}
            key = "%s:%s" % (code, kind)
            self.state["last_alert"][key] = now
            self._dirty = True
        except Exception:
            pass

    def _is_trading_day_today(self):
        try:
            from trading_calendar import is_trading_day
            return is_trading_day()
        except Exception:
            return True

    def set_enabled(self, v):
        self.enabled = bool(v)
        self.state["enabled"] = self.enabled
        self._dirty = True

    def set_mac_popup(self, v):
        self.mac_popup = bool(v)
        _mac_popup[0] = v
        self.state["mac_popup"] = v
        self._dirty = True

    def check_reports(self):
        try:
            reps = self.cfg.get("ai_reports") or {}
            if not reps.get("enabled"):
                return
            today = date.today().isoformat()
            st = self.state.setdefault("reports_sent", {})
            if st.get("_date") != today:
                st.clear(); st["_date"] = today
            now = datetime.now()
            hm = "%02d:%02d" % (now.hour, now.minute)
            for kind in ("premarket", "midday", "close"):
                target = reps.get(kind)
                if not target or st.get(kind):
                    continue
                if hm >= str(target):
                    st[kind] = True; self._dirty = True
                    if not self._is_trading_day_today():
                        log("非交易日, 跳过AI报告[%s]" % kind); continue
                    try:
                        self._ai_report(kind)
                    except Exception as e:
                        log("AI报告异常[%s]: %r" % (kind, e))
        except Exception as e:
            pass

    def maybe_summary(self):
        try:
            today = date.today().isoformat()
            if self.state.get("summary_sent") == today:
                return
            now = datetime.now()
            if now.weekday() >= 5 or (now.hour * 100 + now.minute) < 1507:
                return
            if not self._is_trading_day_today():
                return
            lines = []
            for item in self.cfg.get("watchlist", []):
                try:
                    rt = sm.fetch_rt(item.get("code", ""))
                    if rt:
                        mark = "+" if (rt.get("pct") or 0) >= 0 else ""
                        lines.append("%s %s%s%%" % (item.get("name") or rt.get("name"), mark, sm.fmt(rt.get("pct"))))
                except Exception:
                    continue
            n = len(self.today_alerts)
            msg = "; ".join(lines)
            if n: msg += " | 今日提醒%d条" % n
            ok = notify("📊 收盘总结 %s" % time.strftime("%m-%d"),
                        msg + "\n(仅为技术面参考，不构成投资建议)",
                        self.cfg.get("notify"))
            self._record_alert("📊 收盘总结", msg)
            log("收盘总结已推送: %s" % msg)
            self.state["summary_sent"] = today
            self._dirty = True
            self.today_alerts = []
        except Exception as e:
            log("收盘总结异常: %r" % e)

    def _py_grp(self, code):
        c = str(code or "")
        if c.startswith(("51", "52", "56", "58")) or c.startswith(("15", "16")):
            return "ETF"
        if c.startswith("THS.") or ".BK" in c:
            return "板块"
        if c.startswith("1.") or c.startswith("0.") or c.startswith("2.") or c.startswith("9."):
            return "指数"
        if len(c) in (5, 6) and c[0].isdigit():
            return "个股"
        return "板块"

    def _compute_sector_stats(self, quotes):
        groups = {}
        for q in (quotes or []):
            g = q.get("group") or self._py_grp(q.get("code"))
            groups.setdefault(g, []).append(q)
        stats = {}
        for g, items in groups.items():
            pcts = [q.get("pct") or 0 for q in items]
            avg_pct = sum(pcts) / max(len(pcts), 1)
            up_cnt = sum(1 for p in pcts if p > 0)
            total = len(items)
            stats[g] = {"avg_pct": round(avg_pct, 2), "up_cnt": up_cnt,
                        "total": total, "vol_sum": round(sum(q.get("vol_ratio") or 0 for q in items), 1),
                        "strong": sum(1 for q in items if (q.get("pct") or 0) >= 2.0 and (q.get("vol_ratio") or 0) >= 1.5)}
        self._mkt_cache["sectors"] = stats
        return stats

    def generate_mainline_report(self):
        try:
            from trading_calendar import session
            import stock as sm
            from concurrent.futures import ThreadPoolExecutor, as_completed
            board_nums = ["884286", "886033", "886108", "886078", "885710",
                          "877442", "877444", "877445", "877446", "877447"]
            def _fetch(num):
                try:
                    return num, sm._rt_ths(num)
                except Exception:
                    return num, None
            board_data = {}
            with ThreadPoolExecutor(max_workers=5) as ex:
                futures = {ex.submit(_fetch, n): n for n in board_nums}
                for fut in as_completed(futures, timeout=15):
                    num, rt = fut.result()
                    if rt:
                        board_data[rt.get("name") or ("板块" + num)] = {
                            "code": "ths." + num,
                            "name": rt.get("name") or ("板块" + num),
                            "pct": rt.get("pct", 0) or 0,
                            "price": rt.get("price"),
                            "amount": rt.get("amount"),
                            "type": "concept" if num in ("884286", "886033", "886108", "886078", "885710") else "industry",
                        }
            sector_scores = []
            for name, s in board_data.items():
                pct = s.get("pct") or 0
                sc = max(0, min(100, int(pct * 10 + (pct > 2 and 10 or 0))))
                sector_scores.append({"name": s["name"], "code": s.get("code"),
                                      "type": s.get("type"), "pct": s.get("pct", 0) or 0,
                                      "price": s.get("price"), "score": sc})
            sector_scores.sort(key=lambda x: x["score"], reverse=True)
            quotes = self.last_quotes
            cur_codes = {i.get("code") for i in self.cfg.get("watchlist", [])}
            lines = ["🔥 今日主线 · 板块真实行情 (THS)\n"]
            for i, s in enumerate(sector_scores[:4], 1):
                arrow = "↑" if s["pct"] > 0 else "↓"
                lines.append("  %d. %s %s%+.2f%%" % (i, s["name"], arrow, s["pct"]))
            top_sectors = {s["name"] for s in sector_scores[:3]}
            strong = []
            for q in (quotes or []):
                if q.get("code") not in cur_codes:
                    continue
                pct = q.get("pct") or 0
                grp = q.get("group") or self._py_grp(q.get("code"))
                if grp in top_sectors or pct > 2:
                    strong.append({"name": q.get("name"), "pct": pct, "grp": grp})
            strong.sort(key=lambda x: abs(x["pct"]), reverse=True)
            if strong:
                lines.append("\n核心强势：%s" % " / ".join(s["name"] for s in strong[:5]))
            lines.append("\n市场状态：%s" % self._mkt_cache.get("regime", "NEUTRAL"))
            return {
                "text": "\n".join(lines),
                "sectors": sector_scores[:6],
                "strong_stocks": strong[:6],
                "money_sectors": [s["name"] for s in sector_scores if s["pct"] > 2][:3],
                "market_state": self._mkt_cache.get("regime", "NEUTRAL"),
                "session": session(),
                "generated": time.strftime("%Y-%m-%d %H:%M"),
            }
        except Exception as e:
            return {"text": "主线生成异常: %s" % e, "sectors": [],
                    "strong_stocks": [], "money_sectors": [],
                    "market_state": "异常", "session": "error",
                    "generated": time.strftime("%Y-%m-%d %H:%M")}

    def _ai_text(self, code, signal_text=""):
        return "AI分析完成"

    def fetch(self, c):
        import stock as sm
        try:
            return sm.fetch_rt(c)
        except Exception:
            return None

    def scan_once(self, verbose=True, quiet=False):
        import stock as sm
        self._refresh_regime()
        alerts_before = len(self.today_alerts)
        watch_items = self.cfg.get("watchlist", [])
        codes = [i.get("code", "") for i in watch_items if i.get("code")]
        batch_result = {}
        # 优先 batch，失败逐只 fallback (修复重复调用)
        if len(codes) <= 10:
            batch_result = {}
            for c in codes:
                try: batch_result[c] = sm.fetch_rt(c)
                except: batch_result[c] = None
        else:
            try:
                batch_result = sm.fetch_rt_batch(codes, max_workers=min(5, len(codes)//2+1), timeout=10)
            except Exception:
                batch_result = {}
                for c in codes:
                    try: batch_result[c] = sm.fetch_rt(c)
                    except: batch_result[c] = None
        quotes = []
        for item in watch_items:
            c = item.get("code", "")
            rt = batch_result.get(c)
            if rt is None:
                try:
                    rt = sm.fetch_rt(c)
                    batch_result[c] = rt
                except Exception:
                    continue
            if not rt or not isinstance(rt, dict):
                continue
            name = item.get("name") or rt.get("name", "")
            item["name"] = name
            # 更新 price_hist (修复 #12)
            price = rt.get("price")
            self.price_hist.setdefault(c, deque(maxlen=120))
            if isinstance(price, (int, float)):
                self.price_hist[c].append((time.time(), price))
            self.prev_price[c] = price
            if not quiet:
                try: self.check(rt, c)
                except Exception: pass
            lv = self.levels.get(c) or {}
            # 小趋势图: 优先分时缓存 (修复只读缓存 #13 接入)
            spark = None
            try:
                tentry = _trend_cache_all().get(c)
                if isinstance(tentry, dict) and tentry.get("as_of") == time.strftime("%Y-%m-%d"):
                    spark = _spark_from_trend(tentry)
            except Exception:
                pass
            if not spark:
                spark = [round(p, 4) for _, p in list(self.price_hist.get(c, []))[-80:]]
            quotes.append({
                "code": c, "name": name,
                "group": item.get("group", "") or self._py_grp(c),
                "price": rt.get("price"), "pct": rt.get("pct"),
                "vol_ratio": rt.get("vol_ratio"),
                "turnover": rt.get("turnover"), "amount": rt.get("amount"),
                "update": rt.get("update"),
                "hi20": lv.get("hi20"), "lo20": lv.get("lo20"),
                "spark": spark,
            })
        # 统一板块统计 (修复 #10)
        sector_stats = self._compute_sector_stats(quotes)
        self.state["sector_stats"] = sector_stats
        self._mkt_cache["sectors"] = sector_stats
        # 指数环境
        idx_items = [q for q in quotes if self._py_grp(q.get("code")) == "指数"]
        if idx_items:
            avg_idx = sum(q.get("pct") or 0 for q in idx_items) / max(len(idx_items), 1)
            self._mkt_cache.setdefault("__env__", {})["sh_pct"] = avg_idx
        self.last_quotes = quotes
        self.last_scan_ts = time.time()
        self.state["last_snap"] = {"quotes": quotes, "ts": self.last_scan_ts}
        self.alert_history = [a for a in self.alert_history if str(a.get("date")) == time.strftime("%Y-%m-%d")]
        self._dirty = True
        if verbose:
            names = [q.get("name", "") + "(" + q.get("code", "") + ")" for q in quotes[:8]]
            log("行情 | 观察标的: " + ", ".join(names))
        try: self.flush_alerts()
        except Exception: pass
        return len(self.today_alerts) - alerts_before

    def run(self):
        wd = self.cfg.get("web_dashboard") or {}
        srv = None
        if wd.get("enabled", True):
            srv = _start_dashboard(self, int(wd.get("port", 8899)))
            if srv is None:
                log("⚠️ 端口被占, 监管服务未启动")
        log("盯盘服务已启动, Web: http://127.0.0.1:8899")
        self.refresh_levels()
        try:
            self.scan_once(verbose=True, quiet=True)
        except Exception as e:
            log("启动预扫描失败(不影响运行): %r" % e)
        last_hb = time.time()
        last_maintain = time.time()
        try:
            while True:
                try:
                    if time.time() >= last_maintain + _MAINTAIN_INTERVAL:
                        last_maintain = time.time()
                        self._maintain_storage()
                    if not self.enabled:
                        time.sleep(2); continue
                    self.check_reports()
                    if is_trading_time():
                        self.refresh_levels()
                        self.scan_once()
                        try:
                            _warm_trends([i.get("code") for i in self.cfg.get("watchlist", [])])
                        except Exception:
                            pass
                        if self._dirty:
                            try: save_state(self.state)
                            except: pass
                            self._dirty = False
                        time.sleep(self.cfg.get("poll_interval_sec", 30))
                        continue
                    self.maybe_summary()
                    if time.time() - last_hb > 1800:
                        log("休盘中, 等待下一交易时段…")
                        last_hb = time.time()
                    time.sleep(min(seconds_until_next_session(), 300))
                except KeyboardInterrupt:
                    log("已手动停止")
                    break
                except Exception as e:
                    log("循环异常(继续): %r" % e)
                    time.sleep(10)
        except KeyboardInterrupt:
            log("已手动停止")


def main():
    ap = argparse.ArgumentParser(description="A股自动盯盘推送")
    ap.add_argument("--test", action="store_true", help="发送测试通知")
    ap.add_argument("--once", action="store_true", help="跑一轮扫描后退出")
    args = ap.parse_args()
    cfg = load_cfg()
    ncfg = cfg.get("notify") or {}
    if args.test:
        chans = []
        if (ncfg.get("serverchan_sendkey") or "").strip(): chans.append("Server酱")
        if (ncfg.get("pushplus_token") or "").strip(): chans.append("PushPlus")
        if (ncfg.get("wxpusher_app_token") or "").strip(): chans.append("WxPusher")
        if (ncfg.get("wecom_corpid") or "").strip(): chans.append("企业微信")
        if (ncfg.get("bark_key") or "").strip(): chans.append("Bark")
        if (ncfg.get("feishu_webhook") or "").strip(): chans.append("飞书")
        if (ncfg.get("dingtalk_webhook") or "").strip(): chans.append("钉钉")
        if ncfg.get("macos"): chans.append("macOS弹窗")
        print("已启用渠道: %s" % ("、".join(chans) if chans else "❌ 无"))
        ok = notify("✅ 盯盘测试", "微信推送通道正常!\\n时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"), ncfg)
        log("测试消息%s" % ("已发送" if ok else "失败"))
        return
    w = Watcher(cfg)
    if args.once:
        try:
            w._maintain_storage()
        except Exception:
            pass
        w.refresh_levels(force=True)
        n = w.scan_once(verbose=True)
        print("本轮触发提醒: %d 条" % n)
        return
    w.run()


if __name__ == "__main__":
    main()
