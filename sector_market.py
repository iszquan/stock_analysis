#!/usr/bin/env python3
"""SectorMarket: 全市场板块数据模块.
去除对"自选股分组"的依赖，直接从东财板块指数/板块行情获取真实市场数据。
支持行业板块(industry) / 概念板块(concept) 分开处理。
"""
import os, time, json, re
BASE = os.path.dirname(os.path.abspath(__file__))

# 东财板块接口: 行业 t:2 / 概念 t:3; 字段: f12=代码 f14=名称 f3=涨跌幅 f4=价格 f5=成交量 f6=成交额 f20=量比 f23=涨跌家数
# 实际使用 push2 clist 接口
_SECTOR_URL = ("https://push2.eastmoney.com/api/qt/clist/get?"
               "cb=&pn=1&pz=100&po=1&np=1&ut=&fltt=2&invt=2"
               "&fid=f3&fs={fs}&fields=f12,f14,f3,f4,f5,f6,f20,f23,f28,f29,f30,f31")

class SectorMarket:
    def __init__(self):
        self.cache = {}
        self.cache_ts = 0
        self.cache_ttl = 60  # 60秒缓存

    def _fetch(self, fs, timeout=8):
        """fs: m:90+t:2 (行业) 或 m:90+t:3 (概念)"""
        import urllib.request
        url = _SECTOR_URL.format(fs=fs)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/"
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            return data.get("data", {}).get("diff", [])
        except Exception as e:
            return []

    def _parse_row(self, d):
        # d 是字典; 字段名 f12/f14... 对应代码/名称/涨跌幅/价格...
        # 根据东财实际返回: f12=代码, f14=名称, f3=涨跌幅%, f4=最新价, f5=成交量, f6=成交额, f20=量比
        # f23=涨家数(部份接口没有), f28/f29/f30/f31 可能是涨跌/流入/流出
        pct = float(d.get("f3") or 0)  # 涨跌幅%
        price = float(d.get("f4") or 0)
        vol = float(d.get("f5") or 0) / 100  # 手 -> 万手或保持原始
        amount = float(d.get("f6") or 0) / 10000  # 元 -> 亿元约
        vol_ratio = float(d.get("f20") or 0)
        # 上涨/下跌家数: 部分接口无, 设为估算(根据涨跌幅估算)
        up_raw = d.get("f23")
        if isinstance(up_raw, (int, float)):
            up_cnt = int(up_raw)
        elif isinstance(up_raw, str) and up_raw.isdigit():
            up_cnt = int(up_raw)
        else:
            up_cnt = int(pct > 0 and 10 or 0)
        # 板块代码: f12 是板块代码如 BK0429
        return {
            "code": d.get("f12"),
            "name": d.get("f14"),
            "pct": round(pct, 2),
            "price": price,
            "vol": vol,
            "amount": amount,
            "vol_ratio": vol_ratio,
            "up_cnt": up_cnt,
            "type": "industry" if "t:2" in d.get("fs", "") else "concept",
        }

    def fetch_all(self, force=False):
        now = time.time()
        if not force and self.cache and (now - self.cache_ts) < self.cache_ttl:
            return self.cache
        industries = self._fetch("m:90+t:2")
        concepts = self._fetch("m:90+t:3")
        result = {}
        for row in industries:
            sec = self._parse_row(row)
            sec["type"] = "industry"
            # 用代码作 key
            k = sec["code"] or sec["name"]
            if k: result[k] = sec
        for row in concepts:
            sec = self._parse_row(row)
            sec["type"] = "concept"
            k = sec["code"] or sec["name"]
            if k: result[k] = sec
        self.cache = result
        self.cache_ts = now
        return result

    def get_sectors(self, types=None):
        """获取板块列表; types: ["industry","concept"] 或全部"""
        data = self.fetch_all()
        if types is None:
            return list(data.values())
        return [v for v in data.values() if v.get("type") in types]

    def score_sectors(self, sectors=None):
        """计算板块综合评分(0-100)"""
        items = sectors or self.get_sectors()
        if not items:
            return []
        # 先计算每个板块的原始分: 涨幅 30% / 上涨家数 20% / 成交额变化 15% / 量能 15% / 领涨强度 10% / 突破数 5%
        # 由于没有完整的"成交额变化/突破数"数据, 用可用字段估算
        scores = []
        for s in items:
            pct = s.get("pct", 0)
            # 涨幅分
            score_pct = min(30, max(0, pct * 7.5)) if pct > 0 else max(0, pct * 3)
            # 上涨家数估算(根据涨跌幅)
            up_score = min(20, max(0, abs(pct) * 4))
            # 成交额: 简化为量比贡献
            vol_score = min(15, s.get("vol_ratio", 0) * 3)
            # 量能
            amount_score = min(15, s.get("amount", 0) / 100 * 2)
            # 领涨强度(涨幅绝对值)
            leader_score = min(10, abs(pct) * 2.5)
            # 突破数: 无直接数据, 用涨幅+量比估算
            break_score = min(5, (abs(pct) > 2 and s.get("vol_ratio", 0) > 1.5 and 3 or 0))
            total = int(score_pct + up_score + vol_score + amount_score + leader_score + break_score)
            scores.append({
                "code": s.get("code"),
                "name": s.get("name"),
                "type": s.get("type"),
                "pct": pct,
                "price": s.get("price"),
                "up_cnt": s.get("up_cnt"),
                "vol_ratio": s.get("vol_ratio"),
                "amount": s.get("amount"),
                "score": max(0, min(100, total)),
            })
        scores.sort(key=lambda x: x["score"], reverse=True)
        return scores

# 全局实例
_MARKET = SectorMarket()

def get_market_sectors():
    return _MARKET.get_sectors()

def score_market_sectors():
    return _MARKET.score_sectors()
