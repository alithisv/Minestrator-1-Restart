import os
import time
import json
import uuid
import urllib.request
import urllib.parse
import re
from seleniumbase import SB

_account = os.environ["MINESTRATOR_ACCOUNT"].split(",")
EMAIL      = _account[0].strip()
PASSWORD   = _account[1].strip()
SERVER_ID  = os.environ.get("MINESTRATOR_SERVER_ID", "").strip()
AUTH_TOKEN = os.environ.get("MINESTRATOR_AUTH", "").strip()

_proxy = os.environ.get("GOST_PROXY", "").strip()
LOCAL_PROXY = "127.0.0.1:8080" if _proxy else None

_tg = os.environ.get("TG_BOT", "").strip()
TG_CHAT_ID = _tg.split(",")[0].strip() if _tg else ""
TG_TOKEN   = _tg.split(",")[1].strip() if _tg and "," in _tg else ""

LOGIN_URL  = "https://minestrator.com/connexion"
SERVER_URL = f"https://minestrator.com/my/server/{SERVER_ID}"
API_URL    = f"https://mine.sttr.io/server/{SERVER_ID}/poweraction"

# ============================================================
# TG 推送（支持携带图片发送）
# ============================================================

def now_str():
    import datetime
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def send_tg(result, detail='', photo_path=None):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT，跳过推送")
        return
    msg = (
        f"🎮 Minestrator 重启通知\n"
        f"🕐 运行时间: {now_str()}\n"
        f"🖥 服务器: 🇫🇷 Minestrator-FR\n"
        f"📊 结果: {result}\n"
        f"{detail}"
    ).strip()

    if photo_path and os.path.exists(photo_path):
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
        boundary = uuid.uuid4().hex
        body = []
        
        body.append(f"--{boundary}\r\n".encode())
        body.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
        body.append(f"{TG_CHAT_ID}\r\n".encode())
        
        body.append(f"--{boundary}\r\n".encode())
        body.append(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
        body.append(f"{msg}\r\n".encode())
        
        filename = os.path.basename(photo_path)
        body.append(f"--{boundary}\r\n".encode())
        body.append(f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode())
        body.append(b"Content-Type: image/png\r\n\r\n")
        with open(photo_path, "rb") as f:
            body.append(f.read())
        body.append(b"\r\n")
        body.append(f"--{boundary}--\r\n".encode())
        
        payload = b"".join(body)
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(payload))
        }
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30):
                print(f"📨 TG图文推送成功 ({filename})")
            return
        except Exception as e:
            print(f"⚠️ TG图片推送失败，回退为纯文本推送：{e}")

    url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15):
            print("📨 TG文本推送成功")
    except Exception as e:
        print(f"⚠️ TG推送失败：{e}")


# ============================================================
# Invisible Turnstile：CDP 预注入 + Hook 拦截
# ============================================================

INJECT_TOKEN_LISTENER_JS = """
(function() {
    if (window.__cf_token_listener_injected__) return;
    window.__cf_token_listener_injected__ = true;
    window.__cf_turnstile_token__ = '';

    // 1. 监听 postMessage
    window.addEventListener('message', function(e) {
        if (!e.origin || e.origin.indexOf('cloudflare.com') === -1) return;
        var d = e.data;
        if (!d) return;
        if (d.token && d.token.length > 30) {
            window.__cf_turnstile_token__ = d.token;
        }
    });

    // 2. 拦截 fetch 请求，自动记录页面自身发出的 turnstile_token 或 Authorization
    var origFetch = window.fetch;
    window.fetch = function() {
        var args = arguments;
        try {
            if (args[1] && args[1].body && typeof args[1].body === 'string') {
                var parsed = JSON.parse(args[1].body);
                if (parsed.turnstile_token) {
                    window.__cf_turnstile_token__ = parsed.turnstile_token;
                }
            }
        } catch(e) {}
        return origFetch.apply(this, args);
    };

    // 3. 定时扫描页面所有隐藏 input 与 turnstile 对象
    setInterval(function() {
        try {
            var inputs = document.querySelectorAll('input[name*="turnstile"], input[name*="cf-"], textarea[name*="cf-"]');
            for (var i = 0; i < inputs.length; i++) {
                if (inputs[i].value && inputs[i].value.length > 30) {
                    window.__cf_turnstile_token__ = inputs[i].value;
                }
            }
            if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                var resp = window.turnstile.getResponse();
                if (resp && resp.length > 30) {
                    window.__cf_turnstile_token__ = resp;
                }
            }
        } catch(e) {}
    }, 500);
})();
"""

READ_TOKEN_JS = "(function(){ return window.__cf_turnstile_token__ || ''; })()"


def pre_inject_listener(sb):
    """在页面打开前通过 CDP 预注入脚本，确保不错过加载初期的 Turnstile Token"""
    try:
        sb.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": INJECT_TOKEN_LISTENER_JS}
        )
        print("📡 CDP 预注入 Turnstile 监听器已启用")
    except Exception as e:
        print(f"⚠️ CDP 预注入失败：{e}")

    try:
        sb.execute_script(INJECT_TOKEN_LISTENER_JS)
    except Exception:
        pass


def click_power_button_ui(sb) -> bool:
    """
    直接在网页左上角控制面板点击重启（黄色按钮）或启动（蓝色按钮）。
    点击后不仅能直接触发网页原生重启，也会触发 Invisible Turnstile 刷新 Token。
    """
    print("🖱️ 尝试在网页面板直接点击重启/启动按钮...")
    js_click_power = """
    (function() {
        // 查找左上角 Online/Offline 状态区域下方的三个电源控制按钮（蓝=启动，黄=重启，红=停止）
        var allBtns = Array.from(document.querySelectorAll('button'));
        
        // 策略1：寻找包含 svg 图标且靠近 Online / Support code 区域的三个并排按钮
        for (var i = 0; i < allBtns.length; i++) {
            var parent = allBtns[i].parentElement;
            if (!parent) continue;
            var siblingBtns = parent.querySelectorAll(':scope > button');
            if (siblingBtns.length === 3) {
                var startBtn = siblingBtns[0];   // 蓝色：启动
                var restartBtn = siblingBtns[1]; // 黄色：重启
                if (!restartBtn.disabled) {
                    restartBtn.click();
                    return 'clicked_restart_btn';
                } else if (!startBtn.disabled) {
                    startBtn.click();
                    return 'clicked_start_btn';
                }
            }
        }

        // 策略2：根据按钮背景色/类名匹配（琥珀黄 warning / 蓝色 primary）
        var warnBtn = document.querySelector('button[class*="warning"], button[class*="yellow"], button[class*="amber"]');
        if (warnBtn && !warnBtn.disabled) {
            warnBtn.click();
            return 'clicked_warning_color_btn';
        }
        return '';
    })()
    """
    try:
        res = sb.execute_script(js_click_power)
        if res:
            print(f"✅ 成功触发面板电源按钮: {res}")
            return True
        else:
            print("⚠️ 未匹配到并排的三个电源按钮")
            return False
    except Exception as e:
        print(f"⚠️ UI 点击异常: {e}")
        return False


def wait_for_token(sb, timeout=25) -> str:
    print(f"⏳ 等待 Turnstile Token 捕获（最多 {timeout} 秒）...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            token = sb.execute_script(READ_TOKEN_JS)
            if token and len(token) > 30:
                print(f"✅ Token 已捕获（长度 {len(token)}）")
                return token
        except Exception:
            pass
        time.sleep(1)

    print("ℹ️ 未单独捕获到明文 Token")
    return ''


def send_restart(sb, token: str, action="restart") -> bool:
    token_js = json.dumps(token)
    script = (
        "var done = arguments[0];"
        'fetch("' + API_URL + '", {'
        '  method: "PUT",'
        '  headers: {'
        '    "Authorization": "' + AUTH_TOKEN + '",'
        '    "Content-Type": "application/json",'
        '    "Accept": "application/json",'
        '    "X-Requested-With": "XMLHttpRequest"'
        '  },'
        '  body: JSON.stringify({poweraction: "' + action + '", turnstile_token: ' + token_js + '})'
        '})'
        '.then(function(r){ return r.json(); })'
        '.then(function(data){ done({ok: true, data: data}); })'
        '.catch(function(err){ done({ok: false, error: err.toString()}); });'
    )
    try:
        result = sb.execute_async_script(script)
        print(f"📡 API ({action}) 响应：{result}")
        if result.get("ok") and result.get("data", {}).get("api", {}).get("code") == 200:
            print(f"✅ API {action} 指令已成功送达！")
            return True
        return False
    except Exception as e:
        print(f"⚠️ API请求异常：{e}")
        return False


def extract_remaining_info(sb) -> str:
    """从页面顶部横幅提取到期时间或利用期限"""
    try:
        info = sb.execute_script(r"""
            (function(){
                // 匹配顶部黄色试用期提示横幅文字
                var bodyText = document.body.innerText || '';
                var m1 = bodyText.match(/expires permanently on [^\.\n]+/i);
                if (m1) return m1[0];
                var m2 = bodyText.match(/202\d年\d+月\d+日[^\.\n，。]+/);
                if (m2) return m2[0];

                var spans = document.querySelectorAll('[data-slot="base"] span');
                var parts = [];
                for (var i = 0; i < spans.length; i++) {
                    var t = spans[i].textContent.trim();
                    if (/^\d+[hms]$/.test(t)) parts.push(t);
                }
                return parts.length ? parts.join(' ') : '';
            })()
        """)
        return info or ""
    except Exception:
        return ""


# ============================================================
# 主流程
# ============================================================

def run_script():
    print("🔧 启动浏览器...")

    sb_kwargs = dict(uc=True, test=True)
    if LOCAL_PROXY:
        sb_kwargs["proxy"] = LOCAL_PROXY
        print(f"🌐 使用代理：{LOCAL_PROXY}")
    else:
        print("ℹ️ 未配置代理，直连运行")

    with SB(**sb_kwargs) as sb:
        print("🚀 浏览器就绪！")

        # 启用 CDP 预注入，所有后续打开的页面都会在第一行 JS 执行前挂载 Token 拦截器
        pre_inject_listener(sb)

        # ── IP 验证 ──────────────────────────────────────────
        print("🌐 验证出口IP...")
        try:
            sb.open("https://api.ipify.org/?format=json")
            ip_text = re.sub(r'(\d+\.\d+\.\d+\.)\d+', r'\1xx', sb.get_text('body'))
            print(f"✅ 出口IP确认：{ip_text}")
        except Exception:
            print("⚠️ IP验证超时，跳过")

        # ── 登录 ─────────────────────────────────────────────
        print("🔑 打开登录页面...")
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5)
        time.sleep(3)

        print("✏️ 填写账号密码...")
        try:
            sb.wait_for_element_visible("input[type='password']", timeout=25)
            
            user_selectors = [
                "input[placeholder*='utilisateur']",
                "input[name='pseudo']",
                "input[name='email']",
                "input[type='text']",
                "input[type='email']",
                "input:not([type='password']):not([type='checkbox']):not([type='hidden'])"
            ]
            typed_user = False
            for sel in user_selectors:
                if sb.is_element_visible(sel):
                    sb.type(sel, EMAIL)
                    print(f"✅ 已使用选择器填写账号: {sel}")
                    typed_user = True
                    break
            if not typed_user:
                raise RuntimeError("未找到可见的账号输入框")

            sb.type("input[type='password']", PASSWORD)
        except Exception as e:
            print(f"❌ 登录框加载或填写失败：{e}")
            sb.save_screenshot("login_fail.png")
            send_tg("❌ 登录框加载失败", f"原因: {e}", photo_path="login_fail.png")
            return

        print("📤 提交登录请求...")
        try:
            if sb.is_element_visible("button:contains('Se connecter')"):
                sb.click("button:contains('Se connecter')")
                print("✅ 已点击 Se connecter 登录按钮")
            elif sb.is_element_visible("button[type='submit']"):
                sb.click("button[type='submit']")
                print("✅ 已点击 submit 登录按钮")
            else:
                sb.execute_script("""
                    (function(){
                        var btns = Array.from(document.querySelectorAll('button'));
                        var target = btns.find(function(b){
                            return b.textContent.indexOf('Se connecter') !== -1 || b.type === 'submit';
                        });
                        if (target) target.click();
                    })()
                """)
                print("✅ 已通过 JS 触发登录按钮点击")
        except Exception as e:
            print(f"❌ 登录按钮点击失败：{e}")
            sb.save_screenshot("login_submit_fail.png")
            send_tg("❌ 登录按钮不可用", f"原因: {e}", photo_path="login_submit_fail.png")
            return

        print("⏳ 等待登录跳转...")
        for _ in range(45):
            try:
                if "/connexion" not in sb.get_current_url():
                    print(f"✅ 登录成功！当前页：{sb.get_current_url()}")
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("❌ 登录等待超时")
            sb.save_screenshot("login_timeout.png")
            send_tg("❌ 登录等待超时", "提交账号密码后页面未跳转，请查看截图提示", photo_path="login_timeout.png")
            return

        # ── 跳转服务器管理页 ──────────────────────────────────
        print(f"🔃 跳转至服务器管理页：{SERVER_URL}")
        pre_inject_listener(sb)
        sb.open(SERVER_URL)
        time.sleep(5)
        print(f"📄 当前页面：{sb.get_current_url()}")
        sb.save_screenshot("server_page.png")

        # ── 方式1：直接点击网页左上角的黄色重启/蓝色启动按钮 ──
        ui_clicked = click_power_button_ui(sb)
        time.sleep(3)

        # ── 方式2：等待并检查是否捕获到 Turnstile Token ──
        token = wait_for_token(sb, timeout=20)
        api_ok = False
        if token:
            api_ok = send_restart(sb, token, action="restart")
            if not api_ok:
                api_ok = send_restart(sb, token, action="start")

        # 如果既没拿到 Token 调 API，UI 点击也没成功，才算真正失败
        if not ui_clicked and not api_ok:
            sb.save_screenshot("restart_fail.png")
            send_tg("❌ 重启操作未生效", "UI 按钮与 Token 均未触发成功", photo_path="restart_fail.png")
            return

        # ── 刷新页面，等待状态与剩余时间更新 ──────────────────
        print("🔄 等待 8 秒后刷新页面确认服务器状态...")
        time.sleep(8)
        sb.open(SERVER_URL)
        time.sleep(5)
        sb.save_screenshot("restart_success.png")

        remaining = extract_remaining_info(sb)
        mode_str = "UI面板按钮点击" if ui_clicked else "API指令发送"
        detail = f"🛠 触发方式：{mode_str}\n⏰ 状态信息：{remaining}" if remaining else f"🛠 触发方式：{mode_str}\n⏰ 状态信息：已执行重启（详见截图）"
        print(f"⏱️ {detail}")
        send_tg("✅ 重启指令已执行！", detail, photo_path="restart_success.png")


if __name__ == "__main__":
    run_script()
