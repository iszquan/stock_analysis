# A股实时分析工具

> ## 📌 改动同步约定（重要）
> **每次修改本仓库代码（monitor.py / dashboard.html / stock.py / ai_advisor.py / 配置或信号规则等），
> 都必须同步更新本文档对应章节**，保证 README 始终是代码事实的准确镜像。
> 改动点自检清单：① 启动/部署方式 ② Web 仪表盘与 API ③ 信号规则与阈值 ④ config.json 字段
> ⑤ 通知/AI 渠道 ⑥ 模块清单。最后更新时间见文末。

配合 AI 会话使用的股票实时分析套件，覆盖「手动分析 → 全自动盯盘推送 → 本地 Web 仪表盘 → AI 解读/复盘」全链路。

## 模块总览

| 文件 | 作用 |
|---|---|
| `stock.py` | 行情+技术指标分析脚本（手动问 AI 用，支持单只/多只/大盘概览/搜索） |
| `monitor.py` | 全自动盯盘守护进程：行情轮询 + 信号预警推送 + 内置 Web 仪表盘（`ThreadingHTTPServer`，默认 `127.0.0.1:8899`） |
| `dashboard.html` | **前端仪表盘页面（独立文件）**，由 `monitor.py` 启动时读取并下发。⚠️ 部署/拷贝时必须与 `monitor.py` 在一起，否则首页只剩「文件缺失」提示页 |
| `ai_advisor.py` | NVIDIA NIM AI 解读模块（信号触发/AI 定时复盘/追问对话） |
| `sector_market.py` | 全市场板块数据（行业/概念板块涨跌、强弱股），供给仪表盘右栏大盘速览 |
| `stock_sectors.py` | 个股 → 真实板块映射（THS/东财板块号） |
| `trading_calendar.py` | 交易日/交易时段判断（含节假日、集合竞价、session、距开收盘分钟数） |
| `signal_postmortem.py` | 信号后验统计：记录每次触发信号的环境与评分，按时间步采样估胜率/收益（写 `signal_outcomes.json`） |
| `resolve_watch.py` | 把关注列表里的各种编码解析成东财可用 secid（板块/ETF/港股指换算） |
| `watch_monitor.sh` | 外部 supervisor：每 30 秒探测端口，无响应就 `kill -9` 并重启 `monitor.py`，防卡死/网络阻塞导致仪表盘挂掉 |

纯 Python 标准库 + pandas，无需安装额外依赖（解释器用 `/usr/bin/python3`，3.9，已装 pandas）。

## 快速开始

```bash
# —— 手动分析（stock.py）——
python3 stock.py 600519                # 单只完整分析（日线）
python3 stock.py 600519 000001 300750  # 多只一起
python3 stock.py -i                    # 大盘概览（上证/深成/创业板/科创50/沪深300/中证500）
python3 stock.py -f 茅台               # 按名称搜索代码

# —— 全自动盯盘（monitor.py）——
python3 monitor.py --test    # 发一条测试消息到通知渠道, 验证通道
python3 monitor.py --once    # 手动跑一轮扫描后退出, 看当前行情与触发情况
python3 monitor.py           # 常驻守护（见下方「运行方式」）
```

## 手动分析脚本（stock.py）

### 常用参数

| 参数 | 作用 |
|---|---|
| `-t` | 附当日分时走势（VWAP、早/午盘高低点） |
| `-p 60` | 切换主分析周期：`d`(默认) / `60` / `30` / `15` 分钟 |
| `-w 10` | 盯盘模式：每 N 秒刷新一次报价 |
| `-i` | 大盘概览 |
| `-f 名称` | 按名称搜索代码 |
| 代码格式 | 支持纯代码 `600519`，或显式前缀 `sh600519` / `sz000001` |

### 输出内容

- **实时报价**：最新价、涨跌幅、开高低收、量比、换手、成交额、PE/PB、涨跌停价
- **技术指标**（自动计算）：MA5/10/20/60、MACD(DIF/DEA/柱)、RSI(6/12/24)、KDJ、BOLL、ATR14、量能对比
- **关键位**：近 20 日/60 日高低点构成的支撑与压力
- **信号汇总**：金叉死叉、均线多空排列、超买超卖、放量缩量、突破破位、连涨连跌、长影线等，按 🐂看多 / 🐻看空 / •中性 分类

## 全自动盯盘推送（monitor.py）

**交易时段**定义（见 `trading_calendar.py`）：`09:15–11:30` 与 `13:00–15:00`（含 09:15 盘前集合竞价）；
非交易时段自动休眠；每个交易日 **15:07 后**推送一次**收盘总结**。
扫描间隔由 `config.json → poll_interval_sec` 控制（默认 30 秒，当前配置 60 秒）。

### 运行方式（三选一）

1. **前台/后台直接跑**（单机最简单）：
   ```bash
   nohup python3 monitor.py >> monitor.log 2>&1 &
   ```
2. **开机自启（LaunchAgent，推荐常驻）**：见文末 plist 模板。`RunAtLoad`+`KeepAlive` 保证崩了自动拉起。
3. **崩溃自动重启（watch_monitor.sh）**：
   ```bash
   nohup bash watch_monitor.sh >> watch_monitor.log 2>&1 &
   ```
   每 30 秒探测 `127.0.0.1:8899`，端口无响应即 `kill -9` 并重启。

> ⚠️ 注意：**`dashboard.html` 是 `monitor.py` 启动时读入内存的**。修改前端页面后必须重启 `monitor.py` 才能生效（不要假设热重载）。

### 通知渠道配置（config.json → notify，可多选并存）

| 渠道 | 需填字段 | 说明 |
|---|---|---|
| _macos 弹窗_ | `macos: true` | 本机系统通知/对话弹窗（可与微信并存），`macos_style`/`dialog_timeout_sec`/`sound` 可调 |
| WxPusher（推荐，免费免实名） | `wxpusher_app_token` | `wxpusher_uid` 可留空（自动取首个关注者） |
| 企业微信自建应用 | `wecom_corpid` / `wecom_secret` / `wecom_agentid` | 直达个人微信，无条数限制 |
| Server酱 | `serverchan_sendkey` | 免费版每天 5 条 |
| PushPlus | `pushplus_token` | 需实名认证（未实名报 code 905） |
| Bark | `bark_key` | iOS 推送 |
| 飞书 | `feishu_webhook` / `feishu_secret` | 群机器人 |
| 钉钉 | `dingtalk_webhook` / `dingtalk_secret` | 群机器人 |

```bash
python3 monitor.py --test    # 发一条测试消息验证通道
```

### 监控信号规则

| 信号 | 默认阈值（config.json 可调） |
|---|---|
| ⚠️ 大涨/大跌 | 日涨跌幅 ≥ `pct_alert` (3.0%) |
| 🚀/💥 急拉/急跌 | `quick_window_min` (10) 分钟内变动 ≥ `quick_move_pct` (1.5%) |
| 🔥/🧊 突破/跌破关键位 | 穿越 20 日高点/低点瞬间 |
| 📢 明显放量 | 量比 ≥ `vol_ratio_alert` (2.5) |
| 🚨 触及涨停/跌停 | 距涨跌停价 `limit_near_pct` (0.2%) 以内 |

同类信号对同一只股默认**冷却 `cooldown_min` (30) 分钟**（防轰炸），触发状态存 `state.json`，重启不重复骚扰。
关键位支持磁盘缓存，行情接口偶发限流时用缓存兜底、自动重试，不阻塞盯盘。

### 智能提醒特性

- **波动率自适应阈值**：涨跌提醒线按各标的 ATR 动态计算（`阈值 = ATR占比 × atr_mult`，限制在 `adaptive_pct.min_pct`~`max_pct` 内）——高波板块放宽、低波指数收紧，弹窗标注 `(阈x.x%)`
- **聚合弹窗**：一轮扫描内的多条提醒合并为一张「盯盘汇总」弹窗（攒满即弹），杜绝轰炸
- **节假日自动识别**：用交易日历判断交易日，节假日不发总结/复盘

## 本地 Web 仪表盘

守护进程内置 HTTP 服务（默认 `http://127.0.0.1:8899`，端口由 `config.json → web_dashboard.port` 改；`web_dashboard.enabled:false` 可关闭）。

- **终端风三栏布局**：
  - 顶部：全标的滚动行情条（指数/板块/ETF/个股实时涨跌）
  - 左栏：自选列表（按 指数/行业板块/ETF/贵金属/海外 自动分组，支持名称过滤，可左右滑删除/重排）
  - **自选拖拽排序（同组内）**：在左栏拖动某标的即可调整同分组内的顺序，落下后调 `/api/wl_reorder` 持久化。仅同分组内可排（跨分组 `grpOf` 不同会忽略）。**关键修复**：拖拽进行中（10 秒自动刷新会重建列表 DOM、销毁正在拖拽的节点导致拖拽中断）已用 `_dragActive` 标志让 `renderList()` 在拖拽期间跳过重建，长距「从上往下拖」不再因刷新被打断；dragover 改为始终 `preventDefault` 并以稳定的整行 `.wrow` 作为落点容器，提升落点命中率。
  - 中栏：选中标的的大号分时图/日K线切换 + AI 结构化建议（多空对比/技术评分）+ 追问对话
  - 右栏：**实时信号流** + 大盘速览（板块涨跌/强弱股，来自 `sector_market.py`）
- 点击左侧任一标的即加载其 AI 操作建议与图表；前端每 **10 秒**自动刷新（拉 `/api/status`）。
- **AI 建议「关键位/现价」阶梯**：中栏 AI 建议区右侧展示「现价 / 压力 / 支撑」阶梯（`renderLevels` 读取 `/api/aiadvice` 返回的 `ai.data.levels`）。**支撑位与压力位由本地计算注入**——近 20 日高/低点来自 `refresh_levels()` 写入 `watcher.levels[code]`（启动即刷新、交易时段实时刷新），**不依赖模型是否返回 `levels` 字段**；即使 AI 接口超时/失败，该阶梯仍显示本地关键位，不会像早期版本那样只剩「现价」一行。
- **实时信号流**：展示当日已触发信号，按 `kind`+文案分类为 🚨极强 / 🐂利好(涨) / 🐻利空(跌) / •提醒(放量) / 📊总结 / 🤖AI报告。
  信号历史写入 `state.json` 的 `alert_history` 并**在重启时从磁盘恢复**（修复过「重启后信号栏空白」的问题），只保留当日信号（旧日期自动剔除）。

### Web 仪表盘 API 参考

前端通过 `fetch('/api/...')` 取数，常用端点：

| 端点 | 说明 |
|---|---|
| `GET /api/status` | 全局状态：`time` `trading` `enabled` `mac_popup` `quotes` `watchlist` `sectors` `alerts` `history`(信号流) |
| `GET /api/detail?code=` | 标的详情：分时/趋势/技术指标/多空评分 |
| `GET /api/kline?code=&n=50` | 日K线（最近 n 根） |
| `GET /api/aiadvice?code=&force=1` | AI 结构化建议（强制重新生成加 `force=1`） |
| `GET /api/mainline` | 主线报告：板块排名 / 强势股 / 资金流向 / 大盘状态 |
| `GET /api/strategy_stats` | 策略统计 |
| `GET /api/search?q=` | 按名称/代码搜索可加标的 |
| `GET /api/wl_add?code=&name=` | 加入自选 |
| `GET /api/wl_del?code=` | 删除自选 |
| `GET /api/wl_reorder?order=` | 重排自选（逗号分隔代码） |
| `GET /api/toggle` | 暂停/恢复监控 |
| `GET /api/macpopup` | 切换本机弹窗开关 |
| `GET /api/aichat?code=&q=&h=` | AI 追问对话（带历史 `h`） |
| `GET /static/<file>` | 静态资源（仪表盘引用的图片等） |

## AI 定时复盘（三份报告）

配置 `ai.api_key` 且 `ai_reports.enabled:true` 后自动执行（时间由 `config.json → ai_reports` 改，默认
**09:15 盘前关注 / 11:40 午间复盘 / 15:12 收盘深度复盘**）。与信号触发的 AI 解读共用 `ai.daily_limit` 额度。
非交易日/非交易时段跳过。

## 信号触发时的 AI 解读（NVIDIA NIM）

配置 key（三选一，优先级从高到低）：

1. `config.json → "ai": {"api_key": "nvapi-..."}`（推荐）
2. 环境变量 `export NVIDIA_API_KEY=nvapi-...`
3. 临时测试 `python3 ai_advisor.py 600519 --key nvapi-xxx`

配置后，信号触发时会把**实时行情+全套日线指标快照**发给 NIM 模型，生成 ≤`ai.max_chars`(110) 字中文解读附在通知里：
信号解读 / 短线倾向 / 关键价位 / 风险提示。AI 还会交叉校验触发信号与指标是否矛盾，防止误报误导。

```bash
python3 ai_advisor.py 600519                  # 单独测试某只股的 AI 解读
python3 ai_advisor.py 600519 "突破20日高点"    # 模拟触发信号测试完整链路
```

- **模型选择**：默认 `ai.model = minimaxai/minimax-m3`（最快），`ai.base_url = https://integrate.api.nvidia.com/v1`。
  已实测可用且中文好：`minimaxai/minimax-m3`、`stepfun-ai/step-3.7-flash`、`nvidia/llama-3.3-nemotron-super-49b-v1.5`；
  `meta/llama-3.3-70b-instruct`、kimi 系列实测挂起或 404。
- 每日调用上限 `ai.daily_limit`（默认 40 次），超限自动跳过防刷爆额度
- AI 调用失败/超时（`ai.timeout_sec` 默认 30）→ 自动降级为纯规则文字提醒，盯盘永不中断
- key 只存本机 `config.json`，请勿分享该文件

## 配置（config.json 字段说明）

| 字段 | 默认/当前 | 说明 |
|---|---|---|
| `watchlist` | — | 自选股列表，每项 `{code, name, group?}`；`group` 自定义分组，不填按代码规则自动归类 |
| `poll_interval_sec` | 30（当前 60） | 扫描间隔秒，别低于 10 |
| `pct_alert` | 3.0 | 大涨/大跌阈值 % |
| `quick_move_pct` | 1.5 | 急拉/急跌阈值 % |
| `quick_window_min` | 10 | 急拉窗口（分钟） |
| `vol_ratio_alert` | 2.5 | 放量量比阈值 |
| `limit_near_pct` | 0.2 | 触及涨跌停附近 % |
| `cooldown_min` | 30 | 同类信号冷却分钟 |
| `adaptive_pct` | `{enabled:true, atr_mult:2.0, min_pct:1.5, max_pct:8.0}` | 波动率自适应阈值 |
| `web_dashboard` | `{enabled:true, port:8899}` | 内置仪表盘开关与端口 |
| `ai_reports` | `{enabled:true, premarket:"09:15", midday:"11:40", close:"15:12"}` | AI 定时复盘开关与时间 |
| `notify` | 见上「通知渠道」 | 各渠道凭据/开关 |
| `ai` | `{enabled, api_key, base_url, model, max_chars:110, daily_limit:40, timeout_sec:30}` | AI 解读配置 |

### 自选股高级写法

`watchlist` 的 `code` 支持任意东财市场码透传（板块/贵金属/港股指）：

```json
{"code": "90.BK1621",  "name": "锂概念"},
{"code": "118.AU9999", "name": "黄金9999"},
{"code": "122.XAU",    "name": "伦敦金现"}
```

可选 `"group"` 字段自定义仪表盘分组名（不填则按代码规则自动归入 指数/行业板块/ETF/贵金属/个股/海外）：

```json
{"code": "513880", "name": "日经225ETF", "group": "海外"}
```

同花顺板块码(884286 等)需换算成东财对应板块——用 `python3 resolve_watch.py` 或名称搜索查找。

## 与 AI 配合的使用方式

在交易时段直接对 AI 说：

> 「帮我看看 600519」
> 「分析一下宁德时代和比亚迪」
> 「大盘现在怎么样」

AI 会运行 `stock.py` 获取实时数据，并基于指标给出综合解读和操作参考（支撑压力位、趋势判断、风险提示）。

## 开机自启（LaunchAgent，可选）

创建 `~/Library/LaunchAgents/com.stock.monitor.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.stock.monitor</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>本目录绝对路径/monitor.py</string>
  </array>
  <key>WorkingDirectory</key><string>本目录绝对路径</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>本目录绝对路径/monitor.log</string>
  <key>StandardErrorPath</key><string>本目录绝对路径/monitor.log</string>
</dict></plist>
```

然后 `launchctl load ~/Library/LaunchAgents/com.stock.monitor.plist`。程序内部自带时段判断，挂一整天也不会在休市时打扰你。

## 数据源与稳定性

- 主源：东方财富公开行情接口（免费无 key），push2/push2his 均已做多镜像域名轮询 + 自动重试
- 备用：腾讯行情（实时报价主源异常时自动降级，输出会标注来源）
- K线为前复权；分钟级数据盘中即为实时；监控批量请求自带间隔防限流

## 免责声明

本工具输出的全部内容仅为公开行情数据的技术面统计参考，不构成任何投资建议。股市有风险，决策需独立判断。

---

_最后更新：2026-08-31（同步 monitor.py 前端拆出 dashboard.html、实时信号流字段契约与重启恢复、config.json 全字段、Web API 参考、交易时段常量修正；本轮新增 AI 建议「关键位/现价」阶梯由本地关键位注入的说明；本轮修复自选拖拽排序「从上往下拖」被 10 秒自动刷新打断的问题，并加固落点容器与 preventDefault）。_
