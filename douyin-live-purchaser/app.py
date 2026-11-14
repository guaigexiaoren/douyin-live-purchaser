"""抖音直播自动购买 — 桌面悬浮窗 (pywebview)"""
import asyncio, ctypes, json, os, re, sys, threading, time, webview
from datetime import datetime
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright.async_api import async_playwright
from taobao_buy import check_and_buy

# ==================== 路径 ====================
if getattr(sys, 'frozen', False):
    ROOT = os.path.dirname(sys.executable)
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = os.path.join(sys._MEIPASS, 'playwright-browsers')
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
AUTH = os.path.join(ROOT, "auth.json")
WHITELIST_JSON = os.path.join(ROOT, "whitelist.json")
PURCHASED_FILE = os.path.join(ROOT, "purchased.json")
HISTORY_FILE = os.path.join(ROOT, "purchase_history.json")

# ==================== 窗口位置 ====================
# 监控浏览器常显在屏幕右侧，主控制面板贴在它左边，两窗并排不重叠
_SCREEN_W = ctypes.windll.user32.GetSystemMetrics(0)
MONITOR_X = max(0, _SCREEN_W - 470)   # 监控浏览器 450 宽，右边留 20
PANEL_X = max(0, _SCREEN_W - 880)     # 主控制面板 400 宽 + 20 间距

# ==================== 业务逻辑 ====================
NOISE = ['政府补贴','爆款','气质','时尚','冬季保暖','新款','热卖','清仓','秒杀','新品','限时','抢购','运费险','7天无理由退货','去抢购','已抢光','衣长','|']
UNPRICED_WORDS = ('待开价', '未开价', '等待开价', '即将开价', '马上开价', '开价中', '待定价')

def clean(t):
    for n in NOISE: t = t.replace(n, '')
    return re.sub(r'\s+', ' ', t).strip()

def load_json(path, default):
    if not os.path.exists(path): return default
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def douyin_login_required(page):
    """Check only login controls that are actually visible in the current viewport."""
    return await page.evaluate("""()=>{
        function visible(el,minWidth,minHeight){
            var r=el.getBoundingClientRect(),s=window.getComputedStyle(el);
            return r.width>=minWidth&&r.height>=minHeight&&
                r.bottom>0&&r.right>0&&r.top<window.innerHeight&&r.left<window.innerWidth&&
                s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';
        }
        var loginTexts=new Set(['请登录','手机号登录','密码登录','短信登录','扫码登录','登录']);
        // 遍历文本节点而不是所有元素：避免每个 div 都拼接整棵子树的 textContent
        var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null,false);
        while(w.nextNode()){
            var t=(w.currentNode.nodeValue||'').trim();
            if(!t||!loginTexts.has(t))continue;
            var el=w.currentNode.parentElement;
            if(el&&visible(el,20,16))return true;
        }
        var inputs=document.querySelectorAll('input[placeholder]');
        for(var i=0;i<inputs.length;i++){
            var placeholder=(inputs[i].getAttribute('placeholder')||'').trim();
            if(placeholder.indexOf('手机号')!==-1&&visible(inputs[i],20,16))return true;
        }
        var qr=document.querySelectorAll('[class*="qrcode" i], [class*="QR"], [class*="扫码"]');
        for(var j=0;j<qr.length;j++){
            if(visible(qr[j],60,60))return true;
        }
        var iframes=document.querySelectorAll('iframe[src*="login"]');
        for(var k=0;k<iframes.length;k++){
            if(visible(iframes[k],200,200))return true;
        }
        return false;
    }""")

class Monitor:
    def __init__(self):
        self.running = False
        self.paused = False
        self.stop_flag = False
        self.status = 'idle'
        self.config = load_json(os.path.join(ROOT, 'config.json'), {
            'url': 'https://live.douyin.com/275002082267',
            'max_price': 2000,
            'shutdown_time': ''
        })
        if not isinstance(self.config, dict):
            self.config = {'url': 'https://live.douyin.com/275002082267', 'max_price': 2000, 'shutdown_time': ''}
        self.config.setdefault('url', 'https://live.douyin.com/275002082267')
        self.config.setdefault('shutdown_time', '')
        try:
            self.config['max_price'] = int(self.config.get('max_price', 2000))
        except (TypeError, ValueError):
            self.config['max_price'] = 2000
        taobao_profile = os.path.join(ROOT, 'taobao_profile')
        try:
            taobao_ready = os.path.isdir(taobao_profile) and len(os.listdir(taobao_profile)) >= 3
        except OSError:
            taobao_ready = False
        self.login_states = {
            'douyin': 'logged_in' if os.path.exists(AUTH) else 'logged_out',
            'taobao': 'logged_in' if taobao_ready else 'logged_out',
        }
        self.logs = []
        self.notifications = []  # 待消费的通知
        self.scan_stats = {'last_scan': '', 'cards': 0, 'cycles': 0}
        self._seen = set()  # 本次会话已扫描到的白名单商品标题
        self._shutdown_triggered = False
        self._thread = None
        self._on_before_retry = None
        self._reload_count = 0
        self._shutdown_date = None
        # 定时关机独立计时：即使未开始监控、暂停中或正在淘宝下单也能到点触发
        threading.Thread(target=self._shutdown_watch, daemon=True).start()

    def add_log(self, level, msg):
        self.logs.append({'time': datetime.now().strftime('%H:%M:%S'), 'level': level, 'msg': msg})
        if len(self.logs) > 300: self.logs = self.logs[-300:]

    def add_notification(self, kind, title, body):
        self.notifications.append({'kind': kind, 'title': title, 'body': body, 'time': datetime.now().isoformat()})
        if len(self.notifications) > 50: self.notifications = self.notifications[-50:]

    def drain_notifications(self):
        items = list(self.notifications)
        self.notifications.clear()
        return items

    def _show_popup(self, title, body):
        """弹出系统置顶提示框，独立线程执行，不阻塞监控。"""
        def run():
            try:
                # MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
                ctypes.windll.user32.MessageBoxW(0, body, title, 0x30 | 0x10000 | 0x40000)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def start(self):
        if self.running: return
        # 上一次监控线程可能还在收尾，此时再启动会出现两个浏览器抢同一登录态
        if self._thread and self._thread.is_alive():
            self.add_log('warn', '上一次监控正在停止，请稍后再试')
            return
        self.stop_flag = False
        self.paused = False
        self.running = True
        self.status = 'monitoring'
        self._seen = set()
        self.scan_stats = {'last_scan': '', 'cards': 0, 'cycles': 0}
        self._reload_count = 0
        save_json(os.path.join(ROOT, 'config.json'), self.config)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_flag = True
        self.paused = False
        self.running = False
        self.status = 'idle'
        if getattr(self, '_shutdown_triggered', False):
            os.system('shutdown /a')
            self._shutdown_triggered = False
            self.add_log('info', '已取消定时关机')

    def _shutdown_watch(self):
        """独立线程检查 shutdown_time，到点执行关机。"""
        while True:
            try:
                now = datetime.now()
                today = now.strftime('%Y-%m-%d')
                # 跨天后恢复可触发状态，否则定时关机只能用一次
                if self._shutdown_triggered and self._shutdown_date and self._shutdown_date != today:
                    self._shutdown_triggered = False
                sht = str(self.config.get('shutdown_time') or '').strip()
                if sht and not self._shutdown_triggered:
                    parts = sht.split(':')
                    hh = int(parts[0]); mm = int(parts[1])
                    if (0 <= hh < 24 and 0 <= mm < 60 and
                            now.hour == hh and now.minute == mm and
                            self._shutdown_date != today):
                        self._shutdown_date = today
                        self._trigger_shutdown(sht)
            except Exception:
                pass
            time.sleep(2)

    def _trigger_shutdown(self, sht):
        self._shutdown_triggered = True
        self.stop_flag = True
        self.paused = False
        self.add_log('warn', f'定时关机触发: {sht}，60秒后关机')
        self.add_notification('warn', '定时关机', '60秒后关机，执行 shutdown /a 可取消')
        os.system('shutdown /s /t 60')

    def pause(self):
        if self.running and not self.paused:
            self.paused = True
            self.status = 'paused'

    def resume(self):
        if self.running and self.paused:
            self.paused = False
            self.status = 'monitoring'

    def clear(self):
        self.logs.clear()
        self.notifications.clear()
        self.scan_stats = {'last_scan': '', 'cards': 0, 'cycles': 0}
        self._seen.clear()
        save_json(WHITELIST_JSON, [])
        save_json(PURCHASED_FILE, [])
        save_json(HISTORY_FILE, [])

    def _ensure_logins(self):
        """监控前检查登录态，未登录则弹出桌面可见浏览器扫码"""
        if not os.path.exists(AUTH):
            self.add_log('info', '抖音未登录，请在浏览器中扫码')
            self.add_notification('info', '抖音登录', '请扫码登录')
            self.login_douyin()
        if self.stop_flag: return
        tp = os.path.join(ROOT, 'taobao_profile')
        try:
            taobao_ready = os.path.isdir(tp) and len(os.listdir(tp)) >= 3
        except OSError:
            taobao_ready = False
        if not taobao_ready:
            self.add_log('info', '淘宝未登录，请在浏览器中扫码')
            self.add_notification('info', '淘宝登录', '请扫码登录')
            self.login_taobao()

    async def _wait_douyin_login(self, page, ctx, timeout_s=240):
        """等待用户扫码登录，成功后保存登录态。"""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            if self.stop_flag: return False
            try:
                if await douyin_login_required(page): continue
                await asyncio.sleep(0.3)
                if await douyin_login_required(page): continue
                self.login_states['douyin'] = 'logged_in'
                self.add_log('ok', '登录成功')
                try: await ctx.storage_state(path=AUTH)
                except Exception as e: self.add_log('warn', f'抖音登录态保存失败: {e}')
                return True
            except Exception:
                await asyncio.sleep(0.5)
        return False

    def _run(self):
        try:
            self._ensure_logins()
            if self.stop_flag:
                return
            if self.login_states.get('douyin') != 'logged_in':
                self.add_log('error', '抖音未登录，已停止监控')
                return

            max_retries = 3
            for attempt in range(max_retries):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._monitor_main())
                    break  # 正常退出
                except Exception as e:
                    self.add_log('error', str(e))
                    if attempt < max_retries - 1 and not self.stop_flag:
                        self.add_log('warn', f'浏览器异常，{10}秒后重试({attempt+2}/{max_retries})')
                        if self._on_before_retry:
                            try: self._on_before_retry(attempt + 2, max_retries)
                            except: pass
                        time.sleep(10)
                finally:
                    loop.close()
        except Exception as e:
            self.add_log('error', f'监控线程异常: {e}')
        finally:
            self.running = False
            self.status = 'idle'

    async def _monitor_main(self):
        whitelist_data = load_json(WHITELIST_JSON, [])
        db_items = [{'title': i.get('标题',''), 'url': i.get('商品链接',''), 'clean': clean(i.get('标题',''))}
                    for i in whitelist_data if isinstance(i, dict) and i.get('标题')]
        if not db_items:
            self.add_log('error', '白名单为空，请先导入')
            return

        purchased = set(load_json(PURCHASED_FILE, []))
        self.add_log('info', f'白名单: {len(db_items)} 件  |  已购: {len(purchased)} 件')
        self.add_log('info', f'单笔限额 ¥{self.config["max_price"]}  下单上限 {len(db_items)}件')

        async with async_playwright() as p:
            self._playwright = p
            browser = await p.chromium.launch(headless=False, args=[
                f'--window-position={MONITOR_X},0', '--window-size=1280,900',
                '--disable-blink-features=AutomationControlled',
                '--disable-background-networking', '--disable-sync',
                '--disable-default-apps', '--no-first-run', '--no-default-browser-check',
                '--disable-component-update', '--disable-breakpad',
                '--disable-features=Translate,MediaRouter',
            ])
            self._browser = browser
            ctx = None
            if os.path.exists(AUTH):
                try: ctx = await browser.new_context(viewport={'width':1280,'height':900}, storage_state=AUTH)
                except Exception as e: self.add_log('warn', f'抖音登录态读取失败: {e}')
            if not ctx: ctx = await browser.new_context(viewport={'width':1280,'height':900})
            # 抖音会检测 navigator.webdriver，隐藏自动化标记，降低运行中掉线概率
            await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = await ctx.new_page()

            self.add_log('info', f'直播间: {self.config["url"]}')
            await page.goto(self.config['url'], wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(8000)

            need = await douyin_login_required(page)
            if need:
                self.add_log('warn', '请扫码登录...')
                self.login_states['douyin'] = 'logged_out'
                if not await self._wait_douyin_login(page, ctx):
                    self.login_states['douyin'] = 'logged_out'
                    self.add_log('warn', '抖音登录等待超时，停止监控')
                    return
            else:
                self.login_states['douyin'] = 'logged_in'
                self.add_log('ok', '已登录')
                try: await ctx.storage_state(path=AUTH)
                except Exception as e: self.add_log('warn', f'抖音登录态保存失败: {e}')

            await page.wait_for_timeout(3000)

            async def scan():
                r = await page.evaluate("""()=>{
                    var cards=[],seen=new Set();
                    var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null,false);
                    while(w.nextNode()){
                        var n=w.currentNode,t=n.textContent.trim();
                        if(!t.includes('\\u00a5')&&!t.includes('\\uffe5'))continue;
                        if(t.length>30)continue;
                        var p=n.parentElement;
                        for(var i=0;i<8&&p&&p!==document.body;i++){if((p.tagName==='DIV'||p.tagName==='SECTION'||p.tagName==='LI')&&p.offsetWidth>100&&p.offsetHeight>60)break;p=p.parentElement;}
                        if(!p||seen.has(p))continue;seen.add(p);
                        var r=p.getBoundingClientRect();
                        if(r.left<window.innerWidth*0.3)continue;
                        if(r.top<100||r.bottom>window.innerHeight-50)continue;
                        cards.push({text:(p.textContent||'').trim(),x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)});
                    }
                    return JSON.stringify(cards);
                }""")
                return json.loads(r)

            self.add_log('ok', '=== 开始监控 ===')
            processed = set()
            pending_until = {}
            scan_fail = 0           # 连续扫描异常次数
            zero_card_since = 0.0   # 连续没扫描到任何卡片的起始时间
            zero_card_reload_after = 300  # 无卡片重载间隔，逐次翻倍避免频繁重载导致掉线
            last_login_check = time.time()
            last_state_save = time.time()

            while not self.stop_flag:
                # 暂停等待
                while self.paused and not self.stop_flag:
                    await asyncio.sleep(1)
                if self.stop_flag: break

                # 运行中登录态检测：每 5 分钟一次，连续两次检测到登录控件才判定掉线
                if time.time() - last_login_check >= 300:
                    last_login_check = time.time()
                    try:
                        if await douyin_login_required(page):
                            await asyncio.sleep(2)
                            if await douyin_login_required(page):
                                self.add_log('warn', '抖音登录已失效，请扫码重新登录')
                                self.add_notification('warn', '抖音掉线', '请扫码重新登录')
                                self._show_popup('抖音掉线', '抖音登录已失效，请在右侧监控浏览器中扫码重新登录。')
                                self.login_states['douyin'] = 'logged_out'
                                if not await self._wait_douyin_login(page, ctx):
                                    self.add_log('error', '抖音重新登录超时，停止监控')
                                    self.stop_flag = True
                                    break
                        else:
                            self.login_states['douyin'] = 'logged_in'
                            if time.time() - last_state_save >= 600:
                                last_state_save = time.time()
                                try: await ctx.storage_state(path=AUTH)
                                except Exception: pass
                    except Exception:
                        pass

                cfg = self.config
                try:
                    try:
                        cards = await scan()
                    except Exception:
                        scan_fail += 1
                        raise
                    scan_fail = 0
                    if cards:
                        zero_card_since = 0.0
                        zero_card_reload_after = 300
                    elif not zero_card_since:
                        zero_card_since = time.time()
                    self.scan_stats = {'last_scan': datetime.now().strftime('%H:%M:%S'), 'cards': len(cards), 'cycles': self.scan_stats.get('cycles', 0) + 1}
                    for c in cards:
                        if self.stop_flag: break
                        text = c['text']
                        m_code = re.search(r'([A-Z]{2,}\d{5,})', text) or re.search(r'(\d{4,}-\d{4,})', text)
                        code = m_code.group(1) if m_code else None
                        m_price = re.search(r'[¥￥]\s*(\d+(?:\.\d{1,2})?)', text)
                        price = m_price.group(1) if m_price else None
                        cname = clean(re.sub(r'[¥￥]\s*\d+(?:\.\d{1,2})?', '', text))

                        if '已抢光' in text or '已售罄' in text or any(w in text for w in UNPRICED_WORDS): continue
                        if not price or re.match(r'^\?+$', price) or re.match(r'^9{3,}', price): continue
                        try:
                            if not (0 < float(price) < 99999): continue
                        except: continue

                        matched = None
                        for item in db_items:
                            if SequenceMatcher(None, cname, item['clean']).ratio() >= 0.65:
                                matched = item; break
                        if not matched: continue

                        if matched['title'] in purchased:
                            continue
                        if cname in processed: continue
                        if pending_until.get(matched['title'], 0) > time.time(): continue

                        # 同一商品只提示一次，避免每轮扫描都刷日志/通知
                        first_seen = matched['title'] not in self._seen
                        self._seen.add(matched['title'])
                        if first_seen:
                            self.add_log('hit', f'命中! {code or "无货号"} ¥{price} — {cname[:50]}')
                            self.add_notification('hit', '命中白名单', f'{matched["title"][:30]} ¥{price}')

                        pf = float(price)
                        if 0 < cfg['max_price'] < pf:
                            if first_seen:
                                self.add_log('warn', f'价格超限: ¥{pf} > ¥{cfg["max_price"]}')
                                self.add_notification('warn', '价格超限', f'{matched["title"][:20]} ¥{pf} > ¥{cfg["max_price"]}')
                                self._save_history(cname, price, code, '价格超限')
                            pending_until[matched['title']] = time.time() + 300
                            continue

                        today = sum(1 for r in load_json(HISTORY_FILE,[]) if str(r.get('time',''))[:10]==datetime.now().strftime('%Y-%m-%d') and r.get('success'))
                        if today >= len(db_items):
                            if first_seen:
                                self.add_log('warn', f'下单上限已达: {today}/{len(db_items)}')
                            pending_until[matched['title']] = time.time() + 3600
                            continue

                        self.status = 'buying'

                        if not matched.get('url'):
                            if first_seen:
                                self.add_log('warn', f'白名单缺少淘宝商品链接，跳过: {matched["title"][:30]}')
                            pending_until[matched['title']] = time.time() + 600
                            self.status = 'monitoring'
                            continue

                        # 先打开直播间商品，确认已经开价后再去淘宝下单
                        await page.mouse.click(c['x'], c['y'])
                        await page.wait_for_timeout(4000)
                        pending = await page.evaluate("""(words)=>{
                            var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null,false);
                            while(w.nextNode()){
                                var t=(w.currentNode.textContent||'').trim();
                                if(!words.some(function(word){return t.includes(word);}))continue;
                                var el=w.currentNode.parentElement;
                                if(!el)continue;
                                var s=window.getComputedStyle(el),r=el.getBoundingClientRect();
                                if(s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0)return true;
                            }
                            return false;
                        }""", list(UNPRICED_WORDS))
                        if pending:
                            self.add_log('info', '商品尚未开价，跳过')
                            pending_until[matched['title']] = time.time() + 10
                            await page.keyboard.press('Escape')
                            await page.wait_for_timeout(1000)
                            self.status = 'monitoring'
                            continue

                        taobao_ok = await self._buy_via_taobao(ctx, matched, price, cname, code, processed, purchased)
                        if not taobao_ok:
                            pending_until[matched['title']] = time.time() + 60
                        self.status = 'monitoring'
                        await page.goto(cfg['url'], wait_until='domcontentloaded', timeout=30000)
                        await page.wait_for_timeout(8000)

                except Exception as e:
                    self.add_log('error', str(e))

                # 只在页面确实出问题时才整页重载（原来的每分钟强制刷新已去掉）
                if scan_fail >= 3:
                    await self._reload_page(page, cfg, f'连续 {scan_fail} 轮扫描异常')
                    scan_fail = 0
                    zero_card_since = 0.0
                elif zero_card_since and time.time() - zero_card_since > zero_card_reload_after:
                    await self._reload_page(page, cfg, f'连续 {zero_card_reload_after // 60} 分钟没扫描到任何商品卡片')
                    zero_card_since = 0.0
                    zero_card_reload_after = min(zero_card_reload_after * 2, 1800)


                await asyncio.sleep(3)

        self.add_log('info', '监控已停止')

    async def _reload_page(self, page, cfg, reason):
        """整页重载直播间。只在页面异常时调用，避免每分钟无谓重载。"""
        self._reload_count += 1
        self.add_log('warn', f'重新加载直播间页面（{reason}，本次运行第 {self._reload_count} 次）')
        try:
            await page.goto(cfg['url'], wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(8000)
        except Exception as e:
            self.add_log('error', f'页面重载失败: {e}')

    def _save_history(self, product, price, code, reason=''):
        records = load_json(HISTORY_FILE, [])
        records.append({
            'time': datetime.now().isoformat(), 'product': product,
            'price': str(price or '0'), 'code': code,
            'success': not bool(reason), 'reason': reason
        })
        if len(records) > 200: records = records[-200:]
        save_json(HISTORY_FILE, records)

    async def _buy_via_taobao(self, ctx, matched, price, cname, code, processed, purchased):
        purchased_ok = False
        self.add_log('info', f'打开淘宝: {matched["url"][:60]}')
        taobao_profile = os.path.join(ROOT, 'taobao_profile')
        os.makedirs(taobao_profile, exist_ok=True)
        taobao_ctx = None
        try:
            taobao_ctx = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=taobao_profile,
                headless=False,
                viewport={'width': 950, 'height': 900},
                locale='zh-CN',
                timezone_id='Asia/Shanghai',
                args=['--window-position=0,0', '--window-size=950,900', '--disable-blink-features=AutomationControlled'],
            )
            await taobao_ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            taobao_page = await taobao_ctx.new_page()
            result = await check_and_buy(
                taobao_page,
                matched['url'],
                matched['title'],
                min_sales=0,
                expected_price=float(price),
                should_stop=lambda: self.stop_flag,
                status_callback=lambda message: self.add_log('info', message),
            )
            if result:
                purchased_ok = True
                processed.add(cname)
                purchased.add(matched['title'])
                save_json(PURCHASED_FILE, list(purchased))
                self.add_log('ok', f'淘宝下单完成! {matched["title"][:40]} ¥{price}')
                self.add_notification('ok', '淘宝下单成功', f'{matched["title"][:30]} ¥{price}')
                self._save_history(cname, price, code)
            else:
                self.add_log('warn', f'淘宝下单未完成: {matched["title"][:30]}')
                self._save_history(cname, price, code, '淘宝下单失败')
        except Exception as e:
            self.add_log('error', f'淘宝异常: {e}')
        finally:
            if taobao_ctx:
                await taobao_ctx.close()
        return purchased_ok

    # ==================== 登录 ====================

    def login_douyin(self):
        self.login_states['douyin'] = 'checking'
        self.add_log('info', '打开抖音登录...')
        self.add_notification('info', '抖音登录', '请在浏览器中扫码')
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = False
        try:
            success = loop.run_until_complete(self._login_douyin_async())
        except Exception as e:
            self.add_log('error', f'抖音登录异常: {e}')
        finally:
            loop.close()
            if self.login_states['douyin'] == 'checking':
                self.login_states['douyin'] = 'logged_in' if success else 'logged_out'
        return success

    async def _login_douyin_async(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=[
                '--window-position=200,50', '--window-size=450,900',
                '--disable-blink-features=AutomationControlled',
            ])
            ctx = None
            try:
                ctx = await browser.new_context(viewport={'width': 450, 'height': 900})
                await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                page = await ctx.new_page()
                await page.goto(self.config['url'], wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(5000)
                if not await douyin_login_required(page):
                    self.login_states['douyin'] = 'logged_in'
                    self.add_log('ok', '抖音已登录')
                    try: await ctx.storage_state(path=AUTH)
                    except Exception as e: self.add_log('warn', f'抖音登录态保存失败: {e}')
                    return True
                self.login_states['douyin'] = 'logged_out'
                self.add_log('warn', '请扫码登录抖音（4分钟超时）')
                if await self._wait_douyin_login(page, ctx):
                    return True
                self.login_states['douyin'] = 'logged_out'
                self.add_log('warn', '抖音登录等待超时')
                return False
            finally:
                await browser.close()

    def login_taobao(self):
        self.login_states['taobao'] = 'checking'
        self.add_log('info', '打开淘宝登录...')
        self.add_notification('info', '淘宝登录', '请在浏览器中扫码')
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = False
        try:
            success = loop.run_until_complete(self._login_taobao_async())
        except Exception as e:
            self.add_log('error', f'淘宝登录异常: {e}')
        finally:
            loop.close()
            if self.login_states['taobao'] == 'checking':
                self.login_states['taobao'] = 'logged_in' if success else 'logged_out'
        return success

    async def _login_taobao_async(self):
        async with async_playwright() as p:
            taobao_profile = os.path.join(ROOT, 'taobao_profile')
            os.makedirs(taobao_profile, exist_ok=True)
            ctx = None
            try:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=taobao_profile,
                    headless=False,
                    viewport={'width': 950, 'height': 900},
                    locale='zh-CN', timezone_id='Asia/Shanghai',
                    args=['--window-position=200,50', '--window-size=950,900', '--disable-blink-features=AutomationControlled'],
                )
                await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                page = await ctx.new_page()
                await page.goto('https://www.taobao.com', wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(5000)
                need_login = await page.evaluate("""()=>{
                    function vis(el){
                        var r=el.getBoundingClientRect(),s=getComputedStyle(el);
                        return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';
                    }
                    var btns=document.querySelectorAll('button,span,div,a');
                    for(var i=0;i<btns.length;i++){
                        var t=(btns[i].textContent||'').trim();
                        if((t==='请登录'||t==='手机号登录'||t==='密码登录'||t==='短信登录'||t==='登录/注册')&&vis(btns[i]))return true;
                    }
                    // 只在登录面板内的二维码才算，避免首页其他二维码误判
                    var qr=document.querySelectorAll('[class*="qrcode"], [class*="QR"]');
                    for(var j=0;j<qr.length;j++){
                        if(vis(qr[j])&&qr[j].closest('[class*="login" i]'))return true;
                    }
                    var iframes=document.querySelectorAll('iframe[src*="login"]');
                    for(var k=0;k<iframes.length;k++){if(vis(iframes[k]))return true;}
                    return false;
                }""")
                if not need_login:
                    self.login_states['taobao'] = 'logged_in'
                    self.add_log('ok', '淘宝已登录')
                    return True
                self.login_states['taobao'] = 'logged_out'
                self.add_log('warn', '请扫码登录淘宝（4分钟超时）')
                for _ in range(120):
                    await asyncio.sleep(2)
                    if self.stop_flag: return False
                    try:
                        still = await page.evaluate("""()=>{
                            function vis(el){
                                var r=el.getBoundingClientRect(),s=getComputedStyle(el);
                                return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';
                            }
                            var btns=document.querySelectorAll('button,span,div,a');
                            for(var i=0;i<btns.length;i++){
                                var t=(btns[i].textContent||'').trim();
                                if((t==='请登录'||t==='手机号登录'||t==='密码登录'||t==='短信登录'||t==='登录/注册')&&vis(btns[i]))return true;
                            }
                            var qr=document.querySelectorAll('[class*="qrcode"], [class*="QR"]');
                            for(var j=0;j<qr.length;j++){
                                if(vis(qr[j])&&qr[j].closest('[class*="login" i]'))return true;
                            }
                            var iframes=document.querySelectorAll('iframe[src*="login"]');
                            for(var k=0;k<iframes.length;k++){if(vis(iframes[k]))return true;}
                            return false;
                        }""")
                        if not still:
                            self.login_states['taobao'] = 'logged_in'
                            self.add_log('ok', '淘宝登录成功')
                            return True
                    except Exception:
                        pass
                self.login_states['taobao'] = 'logged_out'
                self.add_log('warn', '淘宝登录等待超时')
                return False
            finally:
                if ctx: await ctx.close()


# ==================== pywebview API ====================

class Api:
    def __init__(self):
        self.monitor = Monitor()

    def get_state(self):
        m = self.monitor
        history = load_json(HISTORY_FILE, [])
        today = datetime.now().strftime('%Y-%m-%d')
        today_records = [r for r in history if str(r.get('time',''))[:10] == today]
        today_bought = sum(1 for r in today_records if r.get('success'))
        def price_num(r):
            try: return float(r.get('price') or 0)
            except (TypeError, ValueError): return 0
        today_spent = sum(price_num(r) for r in today_records if r.get('success'))
        total_purchased = len(set(r.get('product') for r in history if r.get('success')))
        purchased = set(load_json(PURCHASED_FILE, []))
        whitelist_data = [i for i in load_json(WHITELIST_JSON, []) if isinstance(i, dict) and i.get('标题')]
        return {
            'status': m.status,
            'running': m.running,
            'paused': m.paused,
            'url': m.config['url'],
            'max_price': m.config['max_price'],
            'shutdown_time': m.config.get('shutdown_time', ''),
            'login_states': dict(m.login_states),
            'logs': m.logs[-50:] if m.logs else [],
            'whitelist_count': len(whitelist_data),
            'history': history[-20:],
            'today_bought': today_bought,
            'today_spent': int(today_spent),
            'total_purchased': total_purchased,
            'scan_stats': m.scan_stats,
            'notifications': m.drain_notifications(),
            'whitelist_status': [{'title': i['标题'], 'url': i.get('商品链接', ''), 'purchased': i['标题'] in purchased, 'seen': i['标题'] in m._seen} for i in whitelist_data],
        }

    def start(self, url, max_price):
        url = (url or '').strip()
        if not url:
            self.monitor.add_log('error', '请先填写直播间地址')
            return
        m = self.monitor
        m.config['url'] = url
        m.config['max_price'] = int(max_price)
        m.start()

    def stop(self):
        self.monitor.stop()

    def pause(self):
        self.monitor.pause()

    def resume(self):
        self.monitor.resume()

    def clear_all(self):
        self.monitor.clear()

    def update_config(self, key, value):
        if key == 'max_price':
            try: value = int(value)
            except (TypeError, ValueError): return
        self.monitor.config[key] = value
        # 修改关机时间视为新计划：取消已触发的倒计时，允许新时间生效
        if key == 'shutdown_time':
            if self.monitor._shutdown_triggered:
                os.system('shutdown /a')
            self.monitor._shutdown_triggered = False
            self.monitor._shutdown_date = None
        save_json(os.path.join(ROOT, 'config.json'), self.monitor.config)

    def login_douyin(self):
        if self.monitor.running: return False
        return self.monitor.login_douyin()

    def login_taobao(self):
        if self.monitor.running: return False
        return self.monitor.login_taobao()

    def get_whitelist(self):
        items = [i for i in load_json(WHITELIST_JSON, []) if isinstance(i, dict) and i.get('标题')]
        return '\n'.join((i['标题'] + '\t' + i.get('商品链接', '')) for i in items)

    def save_whitelist(self, text):
        items = []
        for line in text.split('\n'):
            line = line.strip()
            if not line: continue
            if '\t' in line:
                parts = line.split('\t')
                items.append({'标题': parts[0].strip(), '商品链接': parts[1].strip() if len(parts) > 1 else ''})
            else:
                items.append({'标题': line, '商品链接': ''})
        save_json(WHITELIST_JSON, items)
        self.monitor.add_log('info', f'白名单已保存: {len(items)} 件')
        return len(items)

    def import_excel(self):
        try:
            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                directory=os.path.expanduser('~/Desktop'),
                file_types=('Excel Files (*.xls;*.xlsx)',)
            )
            if not result or len(result) == 0:
                return {'ok': False, 'error': '未选择文件'}
            path = result[0]
            import pandas as pd
            df = pd.read_excel(path)
            cols = df.columns.tolist()
            name_col = link_col = None
            for c in cols:
                cl = str(c).lower()
                if not name_col and any(k in cl for k in ['标题','商品','名称','name','product']): name_col = c
                if not link_col and any(k in cl for k in ['链接','url','地址','link']): link_col = c
            if not name_col and len(cols)>=1: name_col = cols[0]
            if not link_col and len(cols)>=2: link_col = cols[1]
            valid = df.dropna(subset=[name_col])
            items = []
            for _, row in valid.iterrows():
                title = str(row[name_col]).strip()
                url = str(row[link_col]).strip() if link_col and pd.notna(row.get(link_col)) else ''
                if title: items.append({'标题': title, '商品链接': url})
            save_json(WHITELIST_JSON, items)
            self.monitor.add_log('ok', f'从 Excel 导入了 {len(items)} 件商品')
            return {'ok': True, 'count': len(items)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}


# ==================== HTML UI ====================

HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
:root {
  --bg: #09090b;
  --surface: #13131a;
  --surface2: #181820;
  --surface3: #1c1c26;
  --border: #222231;
  --border-light: #2b2b3a;
  --text: #e4e4ec;
  --text2: #8b8b9e;
  --text3: #5a5a6e;
  --accent: #818cf8;
  --accent-soft: rgba(129,140,248,0.10);
  --accent-glow: rgba(129,140,248,0.20);
  --green: #4ade80;
  --green-soft: rgba(74,222,128,0.10);
  --green-glow: rgba(74,222,128,0.18);
  --red: #f87171;
  --red-soft: rgba(248,113,113,0.10);
  --amber: #fbbf24;
  --amber-soft: rgba(251,191,36,0.10);
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 6px;
  --radius-pill: 999px;
}

* { margin:0; padding:0; box-sizing:border-box; }

body {
  font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh; display: flex; flex-direction: column;
  overflow: hidden; user-select: none;
  /* allow log area to be selectable */ }

#log-list {
  user-select: text;
  letter-spacing: -0.01em;
  background-image:
    radial-gradient(ellipse 80% 50% at 50% -20%, rgba(129,140,248,0.06) 0%, transparent 100%),
    radial-gradient(ellipse 60% 40% at 50% 100%, rgba(248,113,113,0.03) 0%, transparent 100%);
}

::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-light); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text3); }

/* ── HEADER ── */
#header {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 16px 12px;
  flex-shrink: 0;
}
#dot-wrap {
  position: relative; width: 14px; height: 14px; flex-shrink: 0;
}
#dot {
  width: 10px; height: 10px; border-radius: 50%;
  position: absolute; top: 2px; left: 2px; z-index: 2;
  transition: all 0.5s cubic-bezier(0.4,0,0.2,1);
}
#dot-ring {
  width: 14px; height: 14px; border-radius: 50%;
  position: absolute; top: 0; left: 0; z-index: 1;
  border: 2px solid transparent; transition: all 0.5s cubic-bezier(0.4,0,0.2,1);
}
#dot-wrap.idle #dot { background: #4a4a5a; }
#dot-wrap.idle #dot-ring { border-color: transparent; }
#dot-wrap.monitoring #dot { background: var(--green); box-shadow: 0 0 12px var(--green-glow); }
#dot-wrap.monitoring #dot-ring { border-color: rgba(74,222,128,0.2); animation: breathe 2s ease-in-out infinite; }
#dot-wrap.buying #dot { background: var(--red); box-shadow: 0 0 16px rgba(248,113,113,0.4); animation: pulse 0.7s ease-in-out infinite; }
#dot-wrap.buying #dot-ring { border-color: rgba(248,113,113,0.3); }
#dot-wrap.paused #dot { background: var(--amber); box-shadow: 0 0 10px rgba(251,191,36,0.25); }
#dot-wrap.paused #dot-ring { border-color: rgba(251,191,36,0.2); }

@keyframes breathe { 0%,100%{transform:scale(1)} 50%{transform:scale(1.15)} }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.5;transform:scale(0.85)} }

#status-text { font-size: 15px; font-weight: 600; flex: 1; color: var(--text); letter-spacing: -0.02em; }
#btn-clear {
  font-size: 12px; padding: 5px 14px; border-radius: var(--radius-pill);
  background: transparent; color: var(--text2); border: 1px solid var(--border);
  cursor: pointer; transition: all 0.2s; font-family: inherit; font-weight: 500;
}
#btn-clear:hover { color: var(--red); border-color: rgba(248,113,113,0.3); background: var(--red-soft); }

/* ── BUTTONS ── */
.btn-row { padding: 0 16px 10px; display: flex; gap: 8px; flex-shrink: 0; }
.btn {
  border: none; border-radius: var(--radius-pill); font-size: 13px; font-weight: 600;
  cursor: pointer; padding: 10px 0; color: #fff; transition: all 0.2s cubic-bezier(0.4,0,0.2,1);
  font-family: inherit; letter-spacing: 0.02em;
}
.btn-start { flex:1; background: var(--green); color: #042f14; font-weight: 700; }
.btn-start:hover { background: #5de894; box-shadow: 0 4px 20px var(--green-glow); transform: translateY(-1px); }
.btn-stop { flex:1; background: var(--red); font-weight: 700; }
.btn-stop:hover { background: #f98585; box-shadow: 0 4px 20px rgba(248,113,113,0.3); transform: translateY(-1px); }
.btn-pause { flex:1; background: var(--amber); color: #1a1500; font-weight: 700; }
.btn-pause:hover { background: #fcc84c; box-shadow: 0 4px 20px rgba(251,191,36,0.25); transform: translateY(-1px); }
.btn-resume { flex:1; background: var(--accent); font-weight: 700; }
.btn-resume:hover { background: #9ca3f9; box-shadow: 0 4px 20px var(--accent-glow); transform: translateY(-1px); }

/* ── SCAN STATUS ── */
#scan-bar {
  display: flex; gap: 6px; margin: 0 16px 8px; flex-shrink: 0;
}
.scan-chip {
  padding: 4px 12px; border-radius: var(--radius-pill);
  background: var(--surface2); border: 1px solid var(--border);
  font-size: 11px; color: var(--text2); font-weight: 500;
}
.scan-chip b { color: var(--accent); font-weight: 600; margin: 0 2px; }

/* ── STATS ── */
#stats-bar { display: flex; gap: 8px; margin: 0 16px 10px; flex-shrink: 0; }
.stat-card {
  flex: 1; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 8px 10px; text-align: center;
  transition: all 0.2s; cursor: default; position: relative; overflow: hidden;
}
.stat-card::after {
  content: ''; position: absolute; top: 0; left: 20%; right: 20%; height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  opacity: 0.3;
}
.stat-num { font-size: 26px; font-weight: 700; letter-spacing: -0.04em; color: var(--text); line-height: 1; }
.stat-num.accent { color: var(--accent); }
.stat-num.green { color: var(--green); }
.stat-label { font-size: 10px; color: var(--text3); margin-top: 4px; font-weight: 500; letter-spacing: 0.04em; text-transform: uppercase; }

/* ── SECTIONS ── */
.section {
  margin: 0 16px 8px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 12px 14px; flex-shrink: 0; transition: all 0.2s;
}
.section-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px;
}
.section-label {
  font-size: 11px; font-weight: 600; color: var(--text3);
  letter-spacing: 0.05em; text-transform: uppercase;
}

.row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.row:last-child { margin-bottom: 0; }
.row label { font-size: 12px; color: var(--text2); white-space: nowrap; min-width: 54px; font-weight: 500; }
.row input {
  flex: 1; background: var(--surface2); border: 1px solid var(--border); color: var(--text);
  padding: 7px 10px; border-radius: var(--radius-xs); font-size: 12px; outline: none;
  font-family: inherit; transition: all 0.2s;
}
.row input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.row input.short { max-width: 72px; flex: none; }

.login-actions { display: flex; gap: 6px; flex: 1; }

textarea {
  width: 100%; background: var(--surface2); border: 1px solid var(--border); color: var(--text);
  padding: 10px; border-radius: var(--radius-sm); font-size: 12px; resize: vertical;
  outline: none; font-family: "Cascadia Code", "Consolas", monospace; min-height: 56px;
  line-height: 1.5;
}
textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

.btn-sm {
  border: none; border-radius: var(--radius-pill); font-size: 12px; font-weight: 600; cursor: pointer;
  padding: 7px 16px; transition: all 0.2s; font-family: inherit; letter-spacing: 0.02em;
}
.btn-accent { background: var(--accent); color: #fff; }
.btn-accent:hover { background: #9ca3f9; box-shadow: 0 2px 12px var(--accent-glow); }
.btn-subtle { background: var(--surface3); color: var(--text2); border: 1px solid var(--border); }
.btn-subtle:hover { background: var(--border); color: var(--text); }
.btn-xs {
  border: 1px solid var(--border); border-radius: var(--radius-pill); font-size: 11px;
  cursor: pointer; padding: 4px 10px; color: var(--text2); background: transparent;
  font-family: inherit; font-weight: 500; transition: all 0.2s;
}
.btn-xs:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }

/* ── WHITELIST LIST ── */
#wl-status-list { max-height: 144px; overflow-y: auto; }
.wl-item {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 4px; font-size: 12px; border-radius: var(--radius-xs);
  transition: background 0.15s;
}
.wl-item:hover { background: var(--surface2); }
.wl-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  transition: all 0.3s;
}
.wl-dot.none { background: #2e2e3e; }
.wl-dot.seen { background: var(--amber); box-shadow: 0 0 6px rgba(251,191,36,0.3); }
.wl-dot.bought { background: var(--green); box-shadow: 0 0 6px rgba(74,222,128,0.3); }
.wl-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
.wl-tag {
  font-size: 10px; padding: 2px 8px; border-radius: var(--radius-pill);
  flex-shrink: 0; font-weight: 600; letter-spacing: 0.03em;
}
.wl-tag.bought { background: var(--green-soft); color: var(--green); }
.wl-tag.seen { background: var(--amber-soft); color: var(--amber); }

/* ── LOG ── */
#log-container {
  flex: 1; margin: 0 16px 6px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius);
  overflow: hidden; display: flex; flex-direction: column; min-height: 0;
}
#log-list {
  flex: 1; overflow-y: auto; padding: 8px 12px;
  font-family: "Cascadia Code", "Consolas", "Fira Code", monospace;
  font-size: 11px; line-height: 1.7;
}
.log-line { display: flex; gap: 10px; padding: 1px 0; }
.log-time { color: #3e3e52; flex-shrink: 0; font-weight: 500; }
.log-hit { color: var(--red); font-weight: 600; }
.log-ok { color: var(--green); font-weight: 600; }
.log-warn { color: var(--amber); }
.log-error { color: var(--red); }
.log-info { color: #71718a; }

/* ── HISTORY ── */
#history-wrap {
  margin: 0 16px 8px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 6px 10px; max-height: 88px; overflow-y: auto; flex-shrink: 0;
}
#history-table { width: 100%; font-size: 11px; border-collapse: collapse; }
#history-table th { color: var(--text3); text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border); font-weight: 500; font-size: 10px; letter-spacing: 0.04em; text-transform: uppercase; }
#history-table td { padding: 3px 6px; border-bottom: 1px solid rgba(255,255,255,0.015); color: var(--text2); font-weight: 500; }
#history-table tr.fail td { color: var(--red); }

/* ── TOAST ── */
#toast-container {
  position: fixed; top: 12px; right: 12px; z-index: 9999;
  display: flex; flex-direction: column; gap: 6px; max-width: 240px;
}
.toast {
  padding: 10px 14px; border-radius: var(--radius-sm); font-size: 12px; font-weight: 500;
  animation: toastIn 0.3s cubic-bezier(0.16,1,0.3,1);
  backdrop-filter: blur(12px); border: 1px solid; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  font-family: inherit;
}
.toast b { font-weight: 700; display: block; margin-bottom: 1px; }
.toast.hit { background: rgba(248,113,113,0.15); border-color: rgba(248,113,113,0.3); color: var(--red); }
.toast.ok { background: rgba(74,222,128,0.12); border-color: rgba(74,222,128,0.25); color: var(--green); }
.toast.warn { background: rgba(251,191,36,0.12); border-color: rgba(251,191,36,0.25); color: var(--amber); }
.toast.error { background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.25); color: var(--red); }
@keyframes toastIn { from { transform: translateX(120%) scale(0.95); opacity: 0; } to { transform: translateX(0) scale(1); opacity: 1; } }
</style>
</head>
<body>

<div id="header">
  <div id="dot-wrap" class="idle">
    <div id="dot-ring"></div>
    <div id="dot"></div>
  </div>
  <span id="status-text">就绪</span>
  <button id="btn-clear" onclick="clearAll()">清空</button>
</div>

<div id="toast-container"></div>

<div class="btn-row" id="btn-row">
  <button class="btn btn-start" onclick="toggle()">开始监控</button>
</div>

<div id="scan-bar">
  <div class="scan-chip">扫描 <b id="scan-cycles">0</b> 次</div>
  <div class="scan-chip">卡片 <b id="scan-cards">0</b> 个</div>
  <div class="scan-chip" id="scan-time">--</div>
</div>

<div id="stats-bar">
  <div class="stat-card">
    <div class="stat-num accent" id="stat-today">0</div>
    <div class="stat-label">今日已买</div>
  </div>
  <div class="stat-card">
    <div class="stat-num" id="stat-spent">0</div>
    <div class="stat-label">今日花费</div>
  </div>
  <div class="stat-card">
    <div class="stat-num green" id="stat-total">0</div>
    <div class="stat-label">累计品种</div>
  </div>
</div>

<div class="section">
  <div class="section-header">
    <span class="section-label">设置</span>
  </div>
  <div class="row">
    <label>直播间</label>
    <input type="text" id="cfg-url" placeholder="https://live.douyin.com/..." onchange="onSettingChange('url', this.value)">
  </div>
  <div class="row">
    <label>单笔限额</label>
    <input type="number" id="cfg-maxprice" class="short" value="2000" onchange="onSettingChange('max_price', this.value)">
  </div>
  <div class="row">
    <label>账号登录</label>
    <div class="login-actions">
      <button class="btn-sm btn-subtle login-btn" data-login="douyin" onclick="loginAccount('douyin')">抖音登录</button>
      <button class="btn-sm btn-subtle login-btn" data-login="taobao" onclick="loginAccount('taobao')">淘宝登录</button>
    </div>
  </div>
  <div class="row">
    <label>定时关机</label>
    <input type="time" id="cfg-shutdown" onchange="onSettingChange('shutdown_time', this.value)" style="flex:1;">
  </div>
</div>

<div class="section">
  <div class="section-header">
    <span class="section-label">白名单 <span style="color:var(--accent);">·</span> <span id="wl-count" style="color:var(--accent);font-weight:600;"></span></span>
    <button class="btn-xs" id="wl-edit-btn" onclick="toggleWhitelistMode()">编辑</button>
  </div>
  <div id="wl-status-list"></div>
  <div id="wl-edit" style="display:none;">
    <textarea id="wl-text" placeholder="每行一个商品名称..."></textarea>
    <div style="display:flex; gap:6px; margin-top:8px;">
      <button class="btn-sm btn-accent" onclick="saveWhitelist()">保存</button>
      <button class="btn-sm btn-subtle" onclick="importExcel()">导入 Excel</button>
    </div>
  </div>
</div>

<div id="log-container">
  <div class="section-header" style="padding:10px 12px 6px;margin:0;">
    <span class="section-label">日志</span>
  </div>
  <div id="log-list"></div>
</div>

<div class="section-header" style="padding:0 20px 4px;margin:0;">
  <span class="section-label">购买记录</span>
</div>
<div id="history-wrap">
  <table id="history-table">
    <thead><tr><th>时间</th><th>商品</th><th>金额</th><th>状态</th></tr></thead>
    <tbody id="history-body"></tbody>
  </table>
</div>

<script>
var running = false, paused = false, wlEditMode = false, lastToastTime = 0, loginBusy = false;
var _logHtml = '', _histHtml = '', _wlHtml = '', _btnState = '';
function api() { return window.pywebview.api; }

async function poll() {
  try { updateUI(await api().get_state()); } catch(e) {}
}

function updateUI(s) {
  running = s.running; paused = s.paused;

  // status dot
  var dc = s.status === 'paused' ? 'paused' : s.status;
  document.getElementById('dot-wrap').className = dc;
  var map = {idle:'就绪', monitoring:'监控中', buying:'抢购中', paused:'已暂停'};
  document.getElementById('status-text').textContent = map[s.status] || s.status;
  document.getElementById('btn-clear').style.display = s.running ? 'none' : 'inline-block';

  // buttons（只在状态变化时重建，避免每 1.5 秒重绘导致点击丢失）
  var row = document.getElementById('btn-row');
  var btnState = !s.running ? 'start' : (s.paused ? 'paused' : 'running');
  if (btnState !== _btnState) {
    _btnState = btnState;
    if (btnState === 'start') {
      row.innerHTML = '<button class="btn btn-start" onclick="toggle()">开始监控</button>';
    } else if (btnState === 'paused') {
      row.innerHTML = '<button class="btn btn-resume" onclick="resume()">继续监控</button><button class="btn btn-stop" onclick="stop()">停止</button>';
    } else {
      row.innerHTML = '<button class="btn btn-pause" onclick="pause()">暂停</button><button class="btn btn-stop" onclick="stop()">停止</button>';
    }
  }
  var loginStates = s.login_states || {};
  var loginNames = {douyin:'抖音',taobao:'淘宝'};
  document.querySelectorAll('.login-btn').forEach(function(b){
    var platform=b.dataset.login,state=loginStates[platform]||'logged_out';
    var suffix=state==='logged_in'?'已登录':(state==='checking'?'登录中…':'登录');
    b.textContent=loginNames[platform]+suffix;
    b.disabled=s.running||loginBusy||state==='checking';
    b.style.opacity=(s.running||loginBusy||state==='checking')?'0.5':'';
  });

  // scan
  var ss = s.scan_stats || {};
  document.getElementById('scan-cycles').textContent = ss.cycles || 0;
  document.getElementById('scan-cards').textContent = ss.cards || 0;
  document.getElementById('scan-time').textContent = ss.last_scan ? ss.last_scan : '--';

  // settings
  if (!s.running) {
    if (document.activeElement !== document.getElementById('cfg-url'))
      document.getElementById('cfg-url').value = s.url || '';
    if (document.activeElement !== document.getElementById('cfg-maxprice'))
      document.getElementById('cfg-maxprice').value = s.max_price || 2000;
    var st = s.shutdown_time || '';
    if (document.activeElement !== document.getElementById('cfg-shutdown'))
      document.getElementById('cfg-shutdown').value = st;
  }

  document.getElementById('wl-count').textContent = (s.whitelist_count||0)+' 件';

  // whitelist list
  if (!wlEditMode) {
    var items = s.whitelist_status || [];
    var h = '';
    for (var i=0; i<items.length; i++) {
      var it = items[i];
      var dc = it.purchased ? 'bought' : (it.seen ? 'seen' : 'none');
      var tag = it.purchased ? '已购' : (it.seen ? '扫描' : '');
      h += '<div class="wl-item"><div class="wl-dot '+dc+'"></div><span class="wl-title">'+esc(it.title)+'</span>';
      if (tag) h += '<span class="wl-tag '+dc+'">'+tag+'</span>';
      h += '</div>';
    }
    var wlHtml = h || '<div style="color:var(--text3);font-size:12px;padding:10px 0;text-align:center;">白名单为空</div>';
    if (wlHtml !== _wlHtml) { _wlHtml = wlHtml; document.getElementById('wl-status-list').innerHTML = wlHtml; }
  }

  // stats
  document.getElementById('stat-today').textContent = (s.today_bought||0)+'/'+(s.whitelist_count||0);
  document.getElementById('stat-spent').textContent = '¥'+(s.today_spent||0);
  document.getElementById('stat-total').textContent = s.total_purchased||0;

  // toasts
  (s.notifications||[]).forEach(function(n){ showToast(n); });

  // logs
  var logs = s.logs||[], lh='';
  for (var i=0; i<logs.length; i++) {
    var l=logs[i];
    lh+='<div class="log-line"><span class="log-time">'+esc(l.time)+'</span><span class="log-'+l.level+'">'+esc(l.msg)+'</span></div>';
  }
  if (lh !== _logHtml) {
    _logHtml = lh;
    var ld=document.getElementById('log-list');
    ld.innerHTML=lh; ld.scrollTop=ld.scrollHeight;
  }

  // history
  var hist=(s.history||[]).slice(-15).reverse(), rh='';
  for (var i=0; i<hist.length; i++) {
    var r=hist[i];
    rh+='<tr class="'+(r.success?'':'fail')+'"><td>'+esc((r.time||'').slice(5,16).replace('T',' '))+'</td><td>'+esc((r.product||'').slice(0,18))+'</td><td>¥'+(r.price||0)+'</td><td>'+(r.success?'已购':(r.reason||'失败'))+'</td></tr>';
  }
  if (rh !== _histHtml) { _histHtml = rh; document.getElementById('history-body').innerHTML = rh; }
}

function showToast(n) {
  var now=Date.now();
  if (now-lastToastTime<400&&document.getElementById('toast-container').children.length>2) return;
  lastToastTime=now;
  var c=document.getElementById('toast-container'),d=document.createElement('div');
  d.className='toast '+(n.kind||'info');
  d.innerHTML='<b>'+esc(n.title)+'</b>'+esc(n.body||'');
  c.appendChild(d);
  setTimeout(function(){d.style.opacity='0';d.style.transition='opacity 0.25s';setTimeout(function(){if(d.parentNode)d.parentNode.removeChild(d);},250);},3500);
}

function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

async function toggle(){if(running){await api().stop();}else{var u=document.getElementById('cfg-url').value.trim();if(!u){alert('请填写直播间地址');return;}var p=parseInt(document.getElementById('cfg-maxprice').value)||2000;await api().start(u,p);}}
async function pause(){await api().pause();}
async function resume(){await api().resume();}
async function stop(){await api().stop();}
async function clearAll(){
  if(!confirm('白名单、购买记录和日志将全部清除，确定继续？'))return;
  await api().clear_all();
  document.getElementById('wl-text').value='';
  wlEditMode=false;
  document.getElementById('wl-status-list').style.display='block';
  document.getElementById('wl-edit').style.display='none';
  document.getElementById('wl-edit-btn').textContent='编辑';
  await poll();
}
function onSettingChange(k,v){api().update_config(k,v);}
async function loginAccount(name){
  if(running||loginBusy)return;
  loginBusy=true;
  document.querySelectorAll('.login-btn').forEach(function(b){b.disabled=true;b.style.opacity='0.5';});
  try {
    if(name==='douyin')await api().login_douyin();
    else await api().login_taobao();
  } finally {
    loginBusy=false;
    await poll();
  }
}
function toggleWhitelistMode(){wlEditMode=!wlEditMode;document.getElementById('wl-status-list').style.display=wlEditMode?'none':'block';document.getElementById('wl-edit').style.display=wlEditMode?'block':'none';document.getElementById('wl-edit-btn').textContent=wlEditMode?'返回':'编辑';}

async function saveWhitelist(){var t=document.getElementById('wl-text').value;var c=await api().save_whitelist(t);toggleWhitelistMode();alert('已保存 '+c+' 件商品');}
async function importExcel(){var r=await api().import_excel();if(r.ok){document.getElementById('wl-text').value=await api().get_whitelist();toggleWhitelistMode();alert('已导入 '+r.count+' 件商品');}else{alert('导入失败: '+(r.error||'未知错误'));}}

async function init(){try{document.getElementById('wl-text').value=await api().get_whitelist();}catch(e){}poll();setInterval(poll,1500);}
window.onload=init;
</script>
</body>
</html>

"""


# ==================== 启动 ====================

if __name__ == '__main__':
    api = Api()

    window = webview.create_window(
        title='抖音直播自动购买',
        html=HTML,
        js_api=api,
        width=400,
        height=600,
        x=PANEL_X, y=50,
        on_top=True,
        resizable=True,
        frameless=False,
        easy_drag=False,
    )
    webview.start(debug=False)
