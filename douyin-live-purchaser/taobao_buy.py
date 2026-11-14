"""淘宝自动化：打开链接 → 等待价格同步 → 选择 SKU → 下单支付"""
import asyncio, json, os, random, re, sys, time

if sys.platform == 'win32' and sys.stdout is not None:
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass

def tprint(*args, **kwargs):
    try: print(*args, **kwargs)
    except: pass

TAOBAO_AUTH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'taobao_auth.json')
SS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'screenshots')
UNPRICED_WORDS = ('待开价', '未开价', '等待开价', '即将开价', '马上开价', '开价中', '待定价')
CAPTCHA_WORDS = ('安全验证', '验证码', '拖动滑块', '访问过于频繁', '访问受限', '请完成验证')
SIZE_TARGETS = ('M', '160', 'S', '均码', 'F', 'L', 'XS', 'XL')
PRICE_WAIT_MAX_SECONDS = 300
PRICE_REFRESH_BASE_DELAYS = (20, 40, 60)
PRICE_REFRESH_JITTER_SECONDS = 3
PRICE_MATCH_TOLERANCE = 0.01


async def check_and_buy(page, url, product_name, min_sales=0, expected_price=None, should_stop=None, status_callback=None):
    """
    Open Taobao product page, check sales (if min_sales > 0), buy.
    page: Playwright Page (already in a working context)
    url: Taobao product URL
    product_name: Product name for logging
    min_sales: minimum sales required, 0 to skip sales check
    expected_price: Douyin price; wait for Taobao to match or go below it
    should_stop: optional callback checked during price waiting
    status_callback: optional callback for user-visible price-wait logs
    Returns: True if purchased, False otherwise
    """
    try:
        tprint(f'\n[Taobao] Opening: {url}')
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(5000)

        safe_name = product_name.replace('/', '_')[:20]

        # === Login check ===
        # 先看页面有没有可见的登录入口；有就是未登录。
        # 不能先看“我的淘宝”，因为未登录的淘宝页面也有这个导航文字。
        has_login_prompt = await page.evaluate("""()=>{
            function vis(el){
                var r=el.getBoundingClientRect(),s=getComputedStyle(el);
                return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';
            }
            var btns=document.querySelectorAll('button,span,div,a');
            for(var i=0;i<btns.length;i++){
                var t=(btns[i].textContent||'').trim();
                if((t==='登录/注册'||t==='请登录'||t==='手机号登录'||t==='密码登录'||t==='短信登录')&&vis(btns[i]))return true;
            }
            var iframes=document.querySelectorAll('iframe[src*="login"]');
            for(var j=0;j<iframes.length;j++){if(vis(iframes[j]))return true;}
            // 只在登录面板内的二维码才算，避免商品页的店铺/客服二维码误判
            var qr=document.querySelectorAll('[class*="qrcode"], [class*="QR"]');
            for(var k=0;k<qr.length;k++){
                if(vis(qr[k])&&qr[k].closest('[class*="login" i]'))return true;
            }
            return false;
        }""")
        if has_login_prompt:
            need_login = True
        else:
            already_logged = await page.evaluate("""()=>{
                var nick=document.querySelector('[class*="nick"], [class*="Nick"], [class*="avatar"]');
                if(nick&&nick.textContent.trim().length>0&&nick.textContent.trim().length<20)return true;
                var t=document.body.textContent||'';
                return t.indexOf('我的淘宝')!==-1;
            }""")
            need_login = not already_logged

        if need_login:
            tprint('[Taobao] Login required - please scan QR code in browser...')
            if status_callback:
                try: status_callback('淘宝需要登录，请在浏览器中扫码')
                except Exception: pass
            logged_in = False
            for i in range(120):
                if should_stop and should_stop():
                    tprint('[Taobao] Stop requested during login wait')
                    return False
                await asyncio.sleep(2)
                try:
                    still_login = await page.evaluate("""()=>{
                        function vis(el){
                            var r=el.getBoundingClientRect(),s=getComputedStyle(el);
                            return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';
                        }
                        var btns=document.querySelectorAll('button,span,div,a');
                        for(var i=0;i<btns.length;i++){
                            var tt=(btns[i].textContent||'').trim();
                            if((tt==='登录/注册'||tt==='请登录'||tt==='手机号登录'||tt==='密码登录'||tt==='短信登录')&&vis(btns[i]))return true;
                        }
                        var qr=document.querySelectorAll('[class*="qrcode"], [class*="QR"]');
                        for(var j=0;j<qr.length;j++){
                            if(vis(qr[j])&&qr[j].closest('[class*="login" i]'))return true;
                        }
                        var iframes=document.querySelectorAll('iframe[src*="login"]');
                        for(var k=0;k<iframes.length;k++){if(vis(iframes[k]))return true;}
                        return false;
                    }""")
                    if not still_login:
                        logged_in = True
                        break
                except:
                    # Navigation happened - likely login redirect, check URL
                    try: await page.wait_for_load_state('domcontentloaded')
                    except: pass
                    await page.wait_for_timeout(1000)
                    # If we're on a product page now, login succeeded
                    try:
                        still = await page.evaluate("""()=>{
                            function vis(el){
                                var r=el.getBoundingClientRect(),s=getComputedStyle(el);
                                return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';
                            }
                            var btns=document.querySelectorAll('button,span,div,a');
                            for(var i=0;i<btns.length;i++){
                                var tt=(btns[i].textContent||'').trim();
                                if((tt==='登录/注册'||tt==='请登录'||tt==='手机号登录'||tt==='密码登录'||tt==='短信登录')&&vis(btns[i]))return true;
                            }
                            return false;
                        }""")
                        if not still:
                            logged_in = True
                            break
                    except:
                        pass
                if i % 15 == 0 and i > 0:
                    tprint(f'  Waiting for login... ({120-i*2}s)')

            if not logged_in:
                tprint('[Taobao] Login timeout, skipping')
                return False

            tprint('[Taobao] Logged in, saving session...')
            await page.wait_for_timeout(3000)
            try:
                await page.context.storage_state(path=TAOBAO_AUTH)
                tprint('[Taobao] Session saved')
            except Exception as e:
                tprint(f'[Taobao] Failed to save session: {e}')

        if not await wait_for_taobao_price(
            page,
            expected_price,
            should_stop=should_stop,
            status_callback=status_callback,
        ):
            return False

        def stopped():
            return bool(should_stop and should_stop())

        if stopped():
            tprint('[Taobao] Stop requested, aborting before buy')
            return False

        await page.screenshot(path=f'{SS}/taobao_product_{safe_name}.png')

        # === Extract sales count (skip if min_sales is 0) ===
        if min_sales > 0:
            sales = await extract_sales(page)
            tprint(f'[Taobao] Sales: {sales}')

            if sales is None:
                tprint('[Taobao] Could not detect sales count')
                return False

            if sales <= min_sales:
                tprint(f'[Taobao] Sales {sales} <= {min_sales}, skipping')
                return False

            tprint(f'[Taobao] Sales {sales} > {min_sales}, proceeding to buy!')
        else:
            tprint('[Taobao] Skipping sales check, proceeding to buy!')

        # === Select color + size ===
        if stopped():
            tprint('[Taobao] Stop requested, aborting before SKU selection')
            return False
        sku_selected = await select_sku(page)
        if not sku_selected:
            tprint('[Taobao] SKU selection incomplete, stopping before order')
            return False
        await page.wait_for_timeout(2000)
        await page.screenshot(path=f'{SS}/taobao_aftersku_{safe_name}.png')

        # === Click "立即购买" ===
        if stopped():
            tprint('[Taobao] Stop requested, aborting before buy click')
            return False
        buy_clicked = await click_buy_now(page)
        if not buy_clicked:
            buy_clicked = await click_any_buy_button(page)
        if not buy_clicked:
            tprint('[Taobao] Could not find buy button')
            return False

        await page.wait_for_timeout(3000)
        await page.screenshot(path=f'{SS}/taobao_order_{safe_name}.png')
        if not await wait_for_order_page(page):
            tprint('[Taobao] Order page not reached, stopping')
            return False

        # === Submit order ===
        if stopped():
            tprint('[Taobao] Stop requested, aborting before order submit')
            return False
        submitted = await submit_order(page)
        if submitted:
            tprint(f'[Taobao] >>> ORDER SUBMITTED for {product_name}!')
            try: await page.wait_for_timeout(3000)
            except: pass
            try: await page.screenshot(path=f'{SS}/taobao_done_{safe_name}.png')
            except: pass
            return True
        else:
            tprint('[Taobao] Could not submit order')
            return False

    except Exception as e:
        tprint(f'[Taobao] Error: {e}')
        return False


async def has_unpriced_state(page):
    """Return True when a visible status says the product has not opened its price."""
    return await page.evaluate("""(words)=>{
        var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null,false);
        while(w.nextNode()){
            var t=(w.currentNode.textContent||'').trim();
            if(!words.some(function(word){return t.includes(word);}))continue;
            var el=w.currentNode.parentElement;
            if(!el)continue;
            var s=window.getComputedStyle(el),r=el.getBoundingClientRect();
            if(s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&
               r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&
               r.top<window.innerHeight&&r.left<window.innerWidth)return true;
        }
        return false;
    }""", list(UNPRICED_WORDS))


async def find_taobao_price(page):
    """Read the currently displayed discounted product price, excluding the pre-discount price."""
    result = await page.evaluate("""()=>{
        var text=(document.body&&document.body.innerText)||'';
        var patterns=[
            /店铺优惠后\\s*[：:]?\\s*[¥￥]\\s*([\\d,]+(?:\\.\\d{1,2})?)/,
            /(?:券后价|券后约|到手价|活动价|售价)\\s*[：:]?\\s*[¥￥]\\s*([\\d,]+(?:\\.\\d{1,2})?)/
        ];
        for(var i=0;i<patterns.length;i++){
            var m=text.match(patterns[i]);
            if(m)return m[1].replace(/,/g,'');
        }
        return null;
    }""")
    try:
        return float(result) if result else None
    except (TypeError, ValueError):
        return None


async def has_security_challenge(page):
    """Return True when Taobao shows a visible verification or anti-bot prompt."""
    if any(word in page.url.lower() for word in ('captcha', 'punish', 'verify')):
        return True
    return await page.evaluate("""(words)=>{
        var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null,false);
        while(w.nextNode()){
            var t=(w.currentNode.textContent||'').trim();
            if(!words.some(function(word){return t.includes(word);}))continue;
            var el=w.currentNode.parentElement;
            if(!el)continue;
            var s=getComputedStyle(el),r=el.getBoundingClientRect();
            if(s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&
               r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&
               r.top<window.innerHeight&&r.left<window.innerWidth)return true;
        }
        return false;
    }""", list(CAPTCHA_WORDS))


def next_price_refresh_delay(attempt):
    """Use 20s, 40s, then 60s refreshes with a small random offset."""
    base = PRICE_REFRESH_BASE_DELAYS[min(attempt, len(PRICE_REFRESH_BASE_DELAYS) - 1)]
    return max(1, base + random.uniform(-PRICE_REFRESH_JITTER_SECONDS, PRICE_REFRESH_JITTER_SECONDS))


async def interruptible_sleep(seconds, should_stop=None):
    """Sleep in short slices so Stop reacts without waiting for the full refresh delay."""
    deadline = time.monotonic() + seconds
    while True:
        if should_stop and should_stop():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(1, remaining))


async def wait_for_taobao_price(page, expected_price, should_stop=None, status_callback=None):
    """Wait for Taobao's discounted price to catch up with the Douyin price."""
    try:
        expected = float(expected_price)
    except (TypeError, ValueError):
        return True
    if expected <= 0:
        return True

    def emit(message):
        tprint(f'[Taobao] {message}')
        if status_callback:
            try: status_callback(message)
            except Exception: pass

    deadline = time.monotonic() + PRICE_WAIT_MAX_SECONDS
    refresh_attempt = 0
    while True:
        if should_stop and should_stop():
            emit('监控已停止，结束淘宝价格等待')
            return False
        if 'login.taobao.com' in page.url:
            emit('淘宝登录已失效，停止价格等待')
            return False
        if await has_security_challenge(page):
            emit('淘宝出现安全验证，已停止刷新，请手动处理')
            return False

        pending = await has_unpriced_state(page)
        current_price = await find_taobao_price(page)
        if not pending and current_price is not None and current_price <= expected + PRICE_MATCH_TOLERANCE:
            emit(f'淘宝价格已同步：淘宝 ¥{current_price:g}，抖音 ¥{expected:g}')
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if pending:
                emit('淘宝等待开价超过 5 分钟，本次不下单')
            elif current_price is None:
                emit('超过 5 分钟仍未读取到淘宝优惠价，本次不下单')
            else:
                emit(f'淘宝价格等待超时：淘宝 ¥{current_price:g}，抖音 ¥{expected:g}，本次不下单')
            return False

        delay = min(next_price_refresh_delay(refresh_attempt), remaining)
        if pending:
            emit(f'淘宝尚未开价，约 {delay:.0f} 秒后刷新')
        elif current_price is None:
            emit(f'暂未读取到淘宝优惠价，约 {delay:.0f} 秒后刷新')
        else:
            emit(f'淘宝 ¥{current_price:g} 高于抖音 ¥{expected:g}，约 {delay:.0f} 秒后刷新')

        if not await interruptible_sleep(delay, should_stop):
            emit('监控已停止，结束淘宝价格等待')
            return False
        try:
            await page.reload(wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)
        except Exception as e:
            emit(f'淘宝页面刷新失败：{e}')
            return False
        refresh_attempt += 1


async def wait_for_order_page(page):
    """Wait until the page contains order-confirmation content."""
    for _ in range(6):
        try:
            state = await page.evaluate("""()=>{
                var text=(document.body&&document.body.textContent)||'';
                return location.href.indexOf('buy.taobao.com')!==-1||
                    text.indexOf('提交订单')!==-1||text.indexOf('确认订单')!==-1||
                    text.indexOf('免密支付')!==-1||text.indexOf('立即支付')!==-1||
                    (text.indexOf('实付款')!==-1&&text.indexOf('收货地址')!==-1);
            }""")
            if state:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return False


async def extract_sales(page):
    """Extract sales count from Taobao product page"""
    for attempt in range(5):
        await page.wait_for_timeout(1000)
        result = await page.evaluate("""()=>{
            var body=document.body.textContent||'';
            // Pattern 1: "月销量 XXX"
            var m=body.match(/月销量\\s*(\\d+[万千百+]?)/);
            if(m)return m[1];
            // Pattern 2: "已售 XXX"
            m=body.match(/已售\\s*(\\d+[万千百+]?)/);
            if(m)return m[1];
            // Pattern 3: "XXX人付款"
            m=body.match(/(\\d+[万+]?)\\s*人付款/);
            if(m)return m[1];
            // Pattern 4: "XXX+人已买"
            m=body.match(/(\\d+\\+?)\\s*人已买/);
            if(m)return m[1];
            // Pattern 5: search for elements with "销量" text
            var els=document.querySelectorAll('span,div,em');
            for(var i=0;i<els.length;i++){
                var t=(els[i].textContent||'').trim();
                m=t.match(/销量[：:]\\s*(\\d+[万+]?)/);
                if(m)return m[1];
                m=t.match(/(\\d+[万+]?)\\s*件/);
                if(m)return m[1];
            }
            return null;
        }""")
        if result:
            return parse_sales_number(result)
    return None


def parse_sales_number(s):
    """Parse sales string like '1万+' or '5000' to int"""
    s = s.replace('+', '').replace('件', '').strip()
    if '万' in s:
        try:
            return int(float(s.replace('万', '')) * 10000)
        except:
            return None
    try:
        return int(s)
    except:
        return None


async def _find_sku_option(page, labels, preferred=(), require_selected=False):
    """Find one visible option in a labelled SKU row and report its selected state."""
    return await page.evaluate("""(payload)=>{
        var labels=payload.labels,preferred=payload.preferred,requireSelected=payload.requireSelected;
        var stopLabels=['颜色分类','颜色','尺码','尺寸','规格','数量','配送','服务','发货'];
        function visible(el){
            var r=el.getBoundingClientRect(),s=getComputedStyle(el);
            return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';
        }
        function text(el){return ((el.textContent||'').replace(/\\s+/g,' ').trim());}
        var all=document.querySelectorAll('button,[role="option"],[role="radio"],li,a,span,div');
        var label=null,labelRect=null;
        for(var i=0;i<all.length;i++){
            var el=all[i],t=text(el);
            if(labels.indexOf(t)===-1||!visible(el))continue;
            var r=el.getBoundingClientRect();
            if(r.left<window.innerWidth*0.4)continue;
            if(!label||r.left>labelRect.left||r.top>labelRect.top){label=el;labelRect=r;}
        }
        if(!label)return{found:false,option:null};
        var rowTop=Math.max(0,labelRect.bottom-2),rowBottom=rowTop+120;
        for(var j=0;j<all.length;j++){
            var t=text(all[j]);
            if(stopLabels.indexOf(t)===-1||!visible(all[j]))continue;
            var r=all[j].getBoundingClientRect();
            if(r.top>rowTop+8&&r.left>=labelRect.left-2&&r.top<rowBottom)rowBottom=r.top-4;
        }
        function isSelected(el){
            var cur=el;
            for(var depth=0;cur&&depth<3;depth++,cur=cur.parentElement){
                var cls=String(cur.className||'');
                if(/(^|[-_\\s])(selected|active|checked|current|is-checked)([-_\\s]|$)/i.test(cls))return true;
                var a=cur.getAttribute('aria-selected')||cur.getAttribute('aria-checked')||cur.getAttribute('aria-pressed');
                if(a==='true')return true;
                var s=getComputedStyle(cur);
                var colors=[s.borderTopColor,s.borderRightColor,s.borderBottomColor,s.borderLeftColor];
                for(var k=0;k<colors.length;k++){
                    var m=(colors[k]||'').match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                    if(!m)continue;
                    var rv=parseInt(m[1]),gv=parseInt(m[2]),bv=parseInt(m[3]);
                    if(rv>190&&rv>gv*1.4&&rv>bv*1.4)return true;
                }
            }
            return false;
        }
        var best=null,bestScore=-1;
        for(var i=0;i<all.length;i++){
            var el=all[i],t=text(el),r=el.getBoundingClientRect();
            if(!t||t.length>80||!visible(el))continue;
            if(r.width<35||r.width>340||r.height<20||r.height>80)continue;
            if(r.left<labelRect.left||r.top<rowTop||r.top>rowBottom)continue;
            if(labels.indexOf(t)!==-1||t==='请选择'||t.indexOf('已选')!==-1)continue;
            var cls=String(el.className||'');
            if(/disabled|soldout|unavailable/i.test(cls))continue;
            var selected=isSelected(el);
            if(requireSelected&&!selected)continue;
            var targetIndex=-1;
            if(!requireSelected&&preferred.length){
                for(var p=0;p<preferred.length;p++){
                    var target=preferred[p];
                    if(t===target||t.indexOf(target+' ')===0||t.indexOf(target+'（')===0){targetIndex=p;break;}
                }
                if(targetIndex===-1)continue;
            }
            var s=getComputedStyle(el);
            var hasBorder=parseFloat(s.borderTopWidth||0)>0||parseFloat(s.borderLeftWidth||0)>0;
            var score=(preferred.length&&!requireSelected?(preferred.length-targetIndex)*10000:0)+(selected?1000:0)+(hasBorder?100:0)-(r.width*r.height/1000)-r.left/1000;
            if(score>bestScore){
                bestScore=score;
                best={text:t,x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),selected:selected};
            }
        }
        return{found:true,option:best};
    }""", {
        'labels': list(labels),
        'preferred': list(preferred),
        'requireSelected': require_selected,
    })


async def select_sku(page):
    """Select one available option in every SKU row and fail if none is confirmed."""
    groups = (
        (('颜色分类', '颜色'), ()),
        (('尺码', '尺寸', '规格'), SIZE_TARGETS),
    )
    found_groups = 0
    for labels, preferred in groups:
        group = await _find_sku_option(page, labels, preferred)
        if not group.get('found'):
            continue
        found_groups += 1
        option = group.get('option')
        if not option:
            tprint(f'[Taobao] No available option in SKU row: {labels[0]}')
            return False
        if not option['selected']:
            await page.mouse.click(option['x'], option['y'])
            await page.wait_for_timeout(800)
            tprint(f'[Taobao] Selected {labels[0]}: {option["text"]}')
        confirmed = await _find_sku_option(page, labels, (), require_selected=True)
        if not confirmed.get('option'):
            tprint(f'[Taobao] SKU row is still unselected: {labels[0]}')
            return False

    if not found_groups:
        tprint('[Taobao] No SKU controls found, stopping to avoid an unselected order')
        return False
    return True


async def select_size(page):
    """Legacy wrapper - use select_sku instead."""
    await select_sku(page)


async def click_buy_now(page):
    """Click '立即购买' button on Taobao"""
    result = await page.evaluate("""()=>{
        var els=document.querySelectorAll('button,span,div,a');
        var kw=['立即购买','领券购买','马上抢','立即抢购','立刻购买','马上购买','立即下单','加入购物车'];
        for(var k=0;k<kw.length;k++){
            for(var i=0;i<els.length;i++){
                var el=els[i];
                var t=(el.textContent||'').trim();
                if(t.indexOf(kw[k])==-1)continue;
                var r=el.getBoundingClientRect();
                if(r.width<36||r.height<20||r.width>450)continue;
                if(r.top<100||r.top>window.innerHeight)continue;
                var s=window.getComputedStyle(el);
                var bg=s.backgroundColor||'';
                var m=bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                var isHighlighted=m&&parseInt(m[1])>180;
                if(isHighlighted||el.tagName==='BUTTON'||r.width<300){
                    return{x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),text:t.slice(0,30)};
                }
            }
        }
        // fallback: search for elements with buy-related classes
        var classPatterns=['buy','Buy','purchase','tb-btn-buy','J_LinkBuy','go-buy'];
        for(var c=0;c<classPatterns.length;c++){
            var matches=document.querySelectorAll('[class*="'+classPatterns[c]+'"]');
            for(var j=0;j<matches.length;j++){
                var el=matches[j];
                var t2=(el.textContent||'').trim();
                if(!t2||t2.length>20)continue;
                var r2=el.getBoundingClientRect();
                if(r2.width<30||r2.height<20||r2.width>400)continue;
                if(r2.top<100||r2.top>window.innerHeight)continue;
                return{x:Math.round(r2.left+r2.width/2),y:Math.round(r2.top+r2.height/2),text:t2.slice(0,30)};
            }
        }
        return null;
    }""")
    if result:
        await page.mouse.click(result['x'], result['y'])
        tprint(f'[Taobao] Clicked: {result["text"]}')
        return True
    return False


async def click_any_buy_button(page):
    """Fallback: find a red/orange button near the bottom of the page"""
    result = await page.evaluate("""()=>{
        var els=document.querySelectorAll('button,span,div,a');
        for(var i=0;i<els.length;i++){
            var el=els[i], r=el.getBoundingClientRect();
            if(r.width<60||r.height<30||r.width>500)continue;
            if(r.top<window.innerHeight*0.55||r.top>window.innerHeight-10)continue;
            var t=(el.textContent||'').trim();
            if(!t||t.length>20)continue;
            var s=window.getComputedStyle(el);
            var bg=s.backgroundColor||'';
            var m=bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
            if(!m)continue;
            var rv=parseInt(m[1]), gv=parseInt(m[2]), bv=parseInt(m[3]);
            // Very strict: must be red/orange-ish
            var isRed=rv>200&&gv<120&&bv<120;
            var isOrange=rv>220&&gv>80&&gv<160&&bv<60;
            var isPink=rv>220&&gv<100&&bv>100&&bv<200;
            if(!isRed&&!isOrange&&!isPink)continue;
            // Skip known non-buy elements
            var skip=['参数','详情','评价','收藏','分享','店铺','客服','购物车','加入','图集',
                      '视频','直播','领券','优惠券','满减','凑单','举报','复制','分类','首页'];
            var excluded=false;
            for(var k=0;k<skip.length;k++){if(t.indexOf(skip[k])!==-1){excluded=true;break;}}
            if(excluded)continue;
            return JSON.stringify({x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),text:t.slice(0,30)});
        }
        return null;
    }""")
    if result:
        btn = json.loads(result)
        tprint(f'[Taobao] Fallback clicked: {btn["text"]}')
        await page.mouse.click(btn['x'], btn['y'])
        return True
    return False


async def _find_payment_action(page):
    """Find the main payment button without selecting a payment-method row."""
    return await page.evaluate("""()=>{
        var specs=[
            {text:'免密支付',rank:6},
            {text:'立即支付',rank:5},
            {text:'确认支付',rank:4},
            {text:'去支付',rank:3},
            {text:'提交订单',rank:2},
            {text:'确认下单',rank:1}
        ];
        function visible(el){
            var r=el.getBoundingClientRect(),s=window.getComputedStyle(el);
            return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&
                r.top<window.innerHeight&&r.left<window.innerWidth&&
                s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';
        }
        function isCta(el){
            var s=window.getComputedStyle(el),bg=(s.backgroundColor||'')+' '+(s.backgroundImage||'');
            var m=bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if(!m)return false;
            var r=parseInt(m[1]),g=parseInt(m[2]),b=parseInt(m[3]);
            return r>180&&r>g*1.35&&r>b*1.35;
        }
        function clickable(start){
            var cur=start,fallback=start;
            for(var depth=0;cur&&depth<5;depth++,cur=cur.parentElement){
                if(!visible(cur))continue;
                fallback=cur;
                var role=(cur.getAttribute('role')||'').toLowerCase();
                if(cur.tagName==='BUTTON'||cur.tagName==='A'||role==='button'||isCta(cur))return cur;
            }
            return fallback;
        }
        var els=document.querySelectorAll('button,a,[role="button"],span,div');
        for(var s=0;s<specs.length;s++){
            var spec=specs[s],best=null,bestScore=-1;
            for(var i=0;i<els.length;i++){
                var raw=(els[i].textContent||'').replace(/\\s+/g,' ').trim();
                if(!raw||raw.length>40||raw.indexOf(spec.text)===-1)continue;
                var hasAmount=/[¥￥]\\s*\\d/.test(raw);
                if(spec.text==='免密支付'&&raw.indexOf('支付宝免密支付')!==-1&&!hasAmount)continue;
                var el=clickable(els[i]),r=el.getBoundingClientRect();
                if(!visible(el)||r.width<80||r.height<24)continue;
                var role=(el.getAttribute('role')||'').toLowerCase();
                var buttonSemantic=el.tagName==='BUTTON'||el.tagName==='A'||role==='button';
                var cta=isCta(el);
                if(!buttonSemantic&&!cta&&!hasAmount)continue;
                var score=spec.rank*10000+(cta?1000:0)+(buttonSemantic?500:0)+(hasAmount?200:0)+(raw===spec.text?100:0)-r.width*r.height/1000;
                if(score>bestScore){
                    bestScore=score;
                    best={x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),text:raw};
                }
            }
            if(best)return best;
        }
        return null;
    }""")


async def submit_order(page):
    """Click the main payment action on the Taobao order-confirmation page."""
    for attempt in range(8):
        await page.wait_for_timeout(1500)
        result = await _find_payment_action(page)
        if result:
            await page.mouse.click(result['x'], result['y'])
            tprint(f'[Taobao] Payment action clicked: {result["text"]}')
            return True
        tprint(f'[Taobao] Waiting for payment button... (attempt {attempt+1})')
    return False
