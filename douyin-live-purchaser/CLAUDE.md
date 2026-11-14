# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

抖音直播间自动监控+下单系统。监控直播间浮动商品卡片，匹配白名单后自动购买。支持两种下单方式：抖音直接下单、打开淘宝链接下单。

## 与 boss（开发统筹）的协作关系

本项目是 [boss](C:\boss) 跟踪的 5 个项目之一。boss 维护飞书「开发进度」看板，统筹跨项目状态。

- **取任务**：`/我的任务` → 从飞书看板拉取分配给本项目的工单。
- **做任务**：选工单 → plan mode 出方案 → 实现。
- **报进度**：完成后告知用户"可让 boss 刷新进度"。**不要直接改飞书表**——状态由 boss 统筹更新。

## 编码规范

行为准则，减少 AI 编码常见错误。微小任务可灵活处理。

### 1. 先想再写
- 不确定就确认，不要自己猜
- 多种做法就列出来，不静默选
- 有更简单的方案就说

### 2. 简单优先
- 最小代码解决问题，不加没要求的功能
- 不写未来才用的抽象
- 200 行能压到 50 行就重写

### 3. 精准修改
- 只改有关的，不顺手优化旁边
- 保持现有代码风格一致
- 自己引入的无用 import/变量要删

### 4. 目标驱动
- 把任务变成可验证的目标
- 多步骤列出计划：每一步 → 验证点
- 做完自己验证，不等用户检查

## 入口与启动

```bash
python app.py              # 主程序（pywebview 桌面悬浮窗）
```

## 架构（app.py 主线）

```
app.py  ── pywebview 窗口 + Monitor 类 (daemon 线程跑 Playwright)
  ├── taobao_buy.py    # 淘宝自动化：check_and_buy(page, url, title, min_sales=0)
  ├── whitelist.json   # [{"标题":..., "商品链接":...}]
  ├── auth.json        # 抖音登录态 (Playwright storage_state)
  ├── taobao_profile/  # 淘宝持久化浏览器 profile (launch_persistent_context)
  ├── taobao_auth.json # 淘宝登录态 (storage_state，taobao_buy.py 内部保存)
  ├── config.json      # {url, max_price, shutdown_time}
  ├── purchased.json   # 已购商品名，跨会话去重
  └── purchase_history.json
```

Monitor 在 daemon 线程运行 Playwright，通过 `Api` 类暴露 JS 桥接给 pywebview。前端每 1.5s 调 `api().get_state()` 刷新界面。

## 浏览器策略

**抖音**：`p.chromium.launch(headless=False)` — Playwright 自带 Chromium，登录态用 `storage_state` 保存。
**淘宝**：`launch_persistent_context(user_data_dir=taobao_profile/)` — 另开一个 Playwright Chromium，登录态用完整浏览器 profile 保存（不能只用 storage_state JSON，淘宝会检测 IndexedDB/localStorage 指纹）。

两个浏览器引擎相同、实例独立，不会冲突。开发环境和打包后都无需系统 Chrome。

**窗口定位**：监控浏览器常显在屏幕右侧（`x=屏幕宽-470`，450×900），主控制面板贴在它左边（`x=屏幕宽-880`）两窗并排不重叠；命中淘宝时打开的窗口显示在屏幕左侧；登录时定位在 `x=200,y=50`。

## 登录流程（`_ensure_logins`）

点击"开始监控"后，先检查并完成所需登录，再启动隐藏窗口监控：

```
检查 auth.json + taobao_profile/ → 缺失则弹桌面浏览器扫码 → 保存 → 关闭
                               → 全部就绪 → 开桌面外窗口监控
```

检测逻辑：只检查当前屏幕内实际可见的登录按钮、扫码区域和登录弹窗；不使用 body 文字，避免聊天区“需先登录”等提示造成误判。扫码后每 0.5 秒检测一次。

登录按钮会显示“抖音登录/淘宝登录”，登录中显示“登录中…”，成功后显示“已登录”。

## 下单流程

固定通过白名单中的淘宝链接下单；缺链接时跳过，不在抖音直播间内直接购买。淘宝当前优惠价高于抖音命中价时，按 20/40/60 秒逐步放慢刷新并等待价格同步，最长等待 5 分钟。

## 关键实现细节

- **卡片定位**：`document.createTreeWalker` 遍历文字节点，找含 `¥`/`￥` 且长度 ≤30 的节点，向上找父容器，排除左侧 30% 和边缘
- **白名单匹配**：`difflib.SequenceMatcher` ≥65% 相似度（从 70% 下调，因为抖音卡片和淘宝标题遣词有差异）
- **颜色选择**：匹配"颜色分类"/"颜色"/"颜色："标签，选第一个可用色（最左侧），排除"已选""请选择"
- **尺码选择**：优先 M → 160 → S → 均码 → F → L → XS → XL，无匹配时兜底选第一个可用短文字选项
- **支付按钮**：确认订单页优先点击“免密支付”，其次“立即支付”，再回退到确认支付、去支付或提交订单
- **价格同步**：读取“店铺优惠后/券后价/到手价”等当前优惠价；价格未同步时低频刷新，遇到验证码立即停止
- **页面保活**：只在页面确实出问题时整页重载（连续 3 轮扫描异常，或连续 5 分钟没扫到任何商品卡片），不再每分钟强制刷新
- **登录检测**：直接查找页面上的登录按钮/弹窗/扫码元素，存在即未登录。不使用 body 文字匹配
- **定时关机**：`config.json` 中 `shutdown_time` 字段（HH:MM 格式）。监控循环中到点时执行 `shutdown /s /t 60`，停止监控。界面"停止"按钮自动 `shutdown /a` 取消。

## 打包

```bash
python -m PyInstaller DouyinBuyer.spec
# 输出: dist\DouyinBuyer\DouyinBuyer.exe （文件夹版 / onedir）
```

spec 的 `datas` 把本机 Playwright 实际使用的 `ms-playwright/chromium-1234/` 整包放进 `playwright-browsers/chromium-1234/`。frozen 模式下 `PLAYWRIGHT_BROWSERS_PATH` 指向 `sys._MEIPASS`，数据文件（config、白名单、登录态）生成在 exe 同级目录。

`hiddenimports` 需显式声明 `['pandas', 'playwright', 'taobao_buy']`。

文件夹版（onedir）零外部依赖，比单文件版启动更快、内存更稳，双击 `DouyinBuyer.exe` 运行。

## 淘宝模块（taobao_buy.py）

`check_and_buy(page, url, product_name, min_sales=0)`：
1. 打开商品页 → 检测登录 → 等扫码（最长 4 分钟），登录后保存 storage_state 到 `taobao_auth.json`
2. 比较淘宝当前优惠价与抖音命中价；偏高或未开价时低频刷新，最长等待 5 分钟
3. 选 SKU：颜色选第一个 → 尺码 160/S/M 优先级
4. 点击购买：`click_buy_now()` + `click_any_buy_button()` fallback（严格红色/橙色筛选+排除非购买文字）
5. 提交订单：`submit_order()` 最多 8 次重试，优先点击“免密支付”或“立即支付”主按钮，跳过支付方式选项行
