#!/usr/bin/env python3
"""交易日历模块(TradingCalendar): 统一交易日判断 + 交易时段判断。
优先使用交易日历文件(config.json/holidays.json); 失败才用行情数据推断。
包含: is_trading_day / is_open / session / minutes_from_open / minutes_to_close"""
import os, time, json
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))

# 内置中国A股常见节假日(节前休市/节后休市) — 可由外部 calendar.json 覆盖
_BUILT_IN_HOLIDAYS = {
    # 2024-2025 年示例(可扩展)
    "2026-01-01", "2026-01-28", "2026-01-29", "2026-01-30", "2026-01-31",
    "2026-02-01", "2026-02-02", "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-05-06", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
    # 周末自动处理
}

class TradingCalendar:
    def __init__(self, cfg_path=None):
        self.cfg_path = cfg_path or os.path.join(BASE, "config.json")
        self._holidays = set()
        self._load()

    def _load(self):
        # 1) 优先从外部 calendar.json / config.json 加载
        cal_path = os.path.join(BASE, "calendar.json")
        if os.path.exists(cal_path):
            try:
                with open(cal_path, encoding="utf-8") as f:
                    cal = json.load(f)
                for d in cal.get("holidays", []):
                    self._holidays.add(str(d))
                return
            except Exception:
                pass
        # 2) 从 config.json 读取
        try:
            with open(self.cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            for d in cfg.get("trading_holidays", []):
                self._holidays.add(str(d))
        except Exception:
            pass
        # 3) 使用内置节假日
        self._holidays.update(_BUILT_IN_HOLIDAYS)

    # ---- 核心判断 ----

    def is_trading_day(self, dt=None):
        """判断日期是否为交易日(周一~周五且非节假日)"""
        dt = dt or date.today()
        s = dt.isoformat()
        # 先查日历
        if s in self._holidays:
            return False
        # 周末直接否
        if dt.weekday() >= 5:
            return False
        # 日历没有命中 → 默认当日的交易日(盘前无K线时不会误判为休市)
        return True

    def is_open(self, dt=None, now=None):
        """是否处于开盘交易时段(含盘前 09:15 ~ 收盘 15:00)"""
        if not self.is_trading_day(dt):
            return False
        now = now or datetime.now()
        hm = now.hour * 100 + now.minute
        # 盘前 09:15 ~ 上午 11:30 / 下午 13:00 ~ 15:00
        # 也支持盘前 AI 报告 (09:15 前 30 分钟)
        return (915 <= hm < 1130) or (1300 <= hm <= 1500)

    def session(self, now=None):
        """当前时段: pre/AM/PM/closed"""
        now = now or datetime.now()
        if not self.is_trading_day():
            return "closed"
        hm = now.hour * 100 + now.minute
        if hm < 915:
            return "pre"
        if hm < 1130:
            return "AM"
        if hm < 1300:
            return "break"
        if hm <= 1500:
            return "PM"
        return "closed"

    def minutes_from_open(self, now=None):
        """距离开盘(09:30)的分钟数, 盘前为负"""
        now = now or datetime.now()
        if self.session(now) == "pre":
            return (now.hour * 60 + now.minute) - (9 * 60 + 30)
        if not self.is_open(now):
            return None
        if self.session(now) == "AM":
            return (now.hour * 60 + now.minute) - (9 * 60 + 30)
        if self.session(now) == "PM":
            return (now.hour * 60 + now.minute) - (13 * 60 + 30) + 120
        return None

    def minutes_to_close(self, now=None):
        """距离收盘(15:00)的分钟数"""
        now = now or datetime.now()
        if self.session(now) == "PM" and self.is_open(now):
            return (15 * 60 + 0) - (now.hour * 60 + now.minute)
        return None

    def is_trading_time(self, now=None):
        """兼容旧接口: 交易时段内(含盘前 15 分钟)"""
        return self.is_open(now) or (self.session(now) == "pre" and (now or datetime.now()).hour * 100 + (now or datetime.now()).minute >= 900)


# 全局实例(供 monitor 导入)
_CALENDAR = TradingCalendar()

def is_trading_day(dt=None):
    return _CALENDAR.is_trading_day(dt)

def is_trading_time(now=None):
    return _CALENDAR.is_trading_time(now)

def session(now=None):
    return _CALENDAR.session(now)

def minutes_from_open(now=None):
    return _CALENDAR.minutes_from_open(now)

def minutes_to_close(now=None):
    return _CALENDAR.minutes_to_close(now)
