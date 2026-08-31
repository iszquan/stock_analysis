#!/usr/bin/env python3
"""股票 → 真实板块映射 (基于 THS 板块号 / 东财板块号)"""
# 真实板块数据源: THS 行业/概念板块接口
# 每个股票可以属于多个板块

STOCK_SECTORS = {
    # 指数类
    "1.000001": [{"code":"BK0001","name":"上证指数","type":"index"}],
    "1.000688": [{"code":"BK0996","name":"科创50","type":"index"}],
    "124.HSTECH": [{"code":"BK0731","name":"恒生科技","type":"index"}],
    # 个股 (示例映射 - 实际应由完整数据填充)
    "600519": [{"code":"877445","name":"白酒","type":"industry"},
               {"code":"883801","name":"食品饮料","type":"industry"}],
    "300750": [{"code":"877444","name":"新能源设备","type":"industry"},
                {"code":"886808","name":"锂电池概念","type":"concept"}],
    "688256": [{"code":"877442","name":"半导体","type":"industry"},
                {"code":"877459","name":"芯片","type":"industry"}],
    "300308": [{"code":"886033","name":"CPO概念","type":"concept"},
                {"code":"877442","name":"半导体","type":"industry"}],
    "688981": [{"code":"877442","name":"半导体","type":"industry"},
                {"code":"877459","name":"芯片","type":"industry"}],
    # THS 板块对应
    "ths.884286": [{"code":"884286","name":"锂概念","type":"concept"}],
    "ths.886033": [{"code":"886033","name":"CPO概念","type":"concept"}],
    "ths.886108": [{"code":"886108","name":"AI应用","type":"concept"}],
    "ths.886078": [{"code":"886078","name":"商业航天","type":"concept"}],
    "ths.885710": [{"code":"885710","name":"锂电池概念","type":"concept"}],
}

def get_stock_sectors(code):
    return STOCK_SECTORS.get(str(code), [{"code":"unknown","name":"个股","type":"unknown"}])
