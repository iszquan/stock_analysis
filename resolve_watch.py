#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把用户关注列表里的各种编码解析成东财可用secid"""
import json
import sys

sys.path.insert(0, ".")
import stock as sm  # noqa: E402

TARGETS = [
    ("513880", "日经225ETF"),
    ("159819", "人工智能ETF"),
    ("884286", "锂概念"),
    ("886033", "共封装光学"),
    ("886108", "AI应用"),
    ("886078", "商业航天"),
    ("885710", "锂电池概念"),
    ("000941", "新能源"),
    ("HS2083", "恒生科技"),
    ("H30184", "半导体"),
    ("1B0688", "科创50"),
    ("1A0001", "上证指数"),
    ("au9999", "黄金9999"),
    ("AUUSDO", "伦敦金"),
]


def search(kw):
    import urllib.parse
    url = ("https://searchapi.eastmoney.com/api/suggest/get?input=%s"
           "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=8"
           % urllib.parse.quote(kw))
    d = sm.http_json(url)
    return ((d.get("QuotationCodeTable") or {}).get("Data")) or []


def main():
    for code, name in TARGETS:
        print("=" * 60)
        hits = []
        for kw in (code, name):
            try:
                hits = search(kw)
            except Exception as e:
                print("[%s %s] 搜索失败: %r" % (code, name, e))
                hits = []
            if hits:
                break
        print("[%s] 目标: %s" % (code, name))
        for h in hits[:4]:
            print("   候选: Code=%-10s Name=%-14s MktNum=%-3s 类型=%s"
                  % (h.get("Code"), h.get("Name"), h.get("MktNum"),
                     h.get("SecurityTypeName")))


if __name__ == "__main__":
    main()
