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
# TG 推送（支持完整图文推送 + 纯文本回退）
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
        f"🖥 服务器ID: {SERVER_ID}\n"
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

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15):
            print("📨 TG文本推送成功")
    except Exception as e:
        print(f"⚠️ TG推送失败：{e}")


# ============================================================
# Invisible Turnstile：注入监听器 + Hook 捕获 Token
# ============================================================

INJECT_TOKEN_LISTENER_JS = """
(function() {
    if (window.__cf_token_listener_injected__) return;
    window.__cf_token_listener_injected__ = true;
    window.__cf_turnstile_token__ = '';
    window.__captured_auth_header__ = '';

    // 1. 监听 Cloudflare Turnstile postMessage 事件
    window.addEventListener('message', function(e) {
        if (!e.origin || e.origin.indexOf('cloudflare.com') === -1) return;
        var d = e.data;
        if (!d) return;
        if (d.token && d.token.length > 30) {
            console.log('[TokenCapture] postMessage token length:', d.token.length);
            window.__cf_turnstile_token__ = d.token;
        }
    });

    // 2. Hook window.fetch，捕获网页自身发出的 turnstile_token 和 Authorization
    var origFetch = window.fetch;
    window.fetch = function() {
        var args = arguments;
        try {
            if (args[1]) {
                if (args[1].headers) {
                    var auth = args[1].headers['Authorization'] || args[1].headers['authorization'];
                    if (auth) window.__captured_auth_header__ = auth;
                }
                if (args[1].body && typeof args[1].body === 'string') {
                    var parsed = JSON.parse(args[1].body);
                    if (parsed.turnstile_token) {
                        window.__cf_turnstile_token__ = parsed.turnstile_token;
                    }
                }
            }
        } catch(e) {}
        return origFetch.apply(this, args);
    };

    // 3. 定时轮询页面隐藏的 turnstile input 与全局对象
    setInterval(function() {
        try {
            var inputs = document.querySelectorAll(
                'input[name="cf-turnstile-response"], input[name="cf_turnstile_response"], input[name*="turnstile"]'
            );
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


def inject_listener(sb):
    try:
        sb.execute_script(INJECT_TOKEN_LISTENER_JS)
        print("📡 Turnstile 监听器已注入")
    except Exception as e:
        print(f"⚠️ 监听器注入失败：{e}")


def wait_for_token(sb, timeout=25) -> str:
    print(f"⏳ 等待 Turnstile Token 自动生成（最多 {timeout} 秒）...")
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

    print("ℹ️ 未单独捕获到明文 Turnstile Token")
    return ''


# ============================================================
# 登录辅助：兼容新版 Vue/React 登录表单与多方式提交
# ============================================================

def fill_and_submit_login(sb) -> bool:
    print("✏️ 填写账号密码...")
    try:
        sb.wait_for_element_visible("input[type='password']", timeout=25)

        # 使用原生 setter 写入值并派发完整事件链，确保前端框架绑定生效
        fill_js = """
        (function(email, pwd) {
            var userInp = document.querySelector("input[placeholder*='utilisateur']")
                       || document.querySelector("input[name='pseudo']")
                       || document.querySelector("input[name='email']")
                       || document.querySelector("input[type='text']")
                       || document.querySelector("input[type='email']");
            var passInp = document.querySelector("input[type='password']");
            if (!userInp || !passInp) return false;

            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;

            nativeInputValueSetter.call(userInp, email);
            userInp.dispatchEvent(new Event('input', { bubbles: true }));
            userInp.dispatchEvent(new Event('change', { bubbles: true }));

            nativeInputValueSetter.call(passInp, pwd);
            passInp.dispatchEvent(new Event('input', { bubbles: true }));
            passInp.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        })(arguments[0], arguments[1]);
        """
        ok = sb.execute_script(fill_js, EMAIL, PASSWORD)
        if not ok:
            # 备用常规输入
            sb.type("input[placeholder*='utilisateur'], input[type='text']", EMAIL)
            sb.type("input[type='password']", PASSWORD)
        print("✅ 账号密码已填入并同步表单状态")
    except Exception as e:
        print(f"❌ 登录框加载或填写失败：{e}")
        return False

    time.sleep(2)
    print("📤 提交登录请求...")
    trigger_login_button(sb)
    return True


def trigger_login_button(sb):
    """尝试通过 Selenium 原生点击 + JS 点击触发 Se connecter 按钮"""
    try:
        if sb.is_element_visible("button:contains('Se connecter')"):
            sb.click("button:contains('Se connecter')")
            print("✅ 已点击 Se connecter 登录按钮")
            return
    except Exception:
        pass

    try:
        sb.execute_script("""
            (function(){
                var btns = Array.from(document.querySelectorAll('button'));
                var target = btns.find(function(b){
                    return b.textContent.indexOf('Se connecter') !== -1 || b.type === 'submit';
                });
                if (target) {
                    target.removeAttribute('disabled');
                    target.click();
                }
            })()
        """)
        print("✅ 已通过 JS 触发 Se connecter 登录按钮")
    except Exception as e:
        print(f"⚠️ 点击登录按钮异常：{e}")


# ============================================================
# 服务器电源控制：同时支持“离线开机 (Démarrer)”与“在线重启 (Restart)”
# ============================================================

def check_server_and_click_power(sb) -> dict:
    """
    识别当前服务器是离线 (Hors ligne / Offline) 还是在线 (En ligne / Online)：
    - 若已离线：自动点击绿色的 'Démarrer le serveur' 或 'Démarrer' 按钮开机
    - 若在线中：自动点击左上角三个并排按钮中的黄色重启按钮，刷新 4 小时倒计时
    """
    js_power = """
    (function() {
        var res = {
            status: 'unknown',
            clicked: false,
            action_type: '',
            detail: '',
            countdown: ''
        };

        var bodyText = document.body.innerText || '';

        // 1. 提取右上角 4 小时倒计时（如 S'éteint dans 3h 32m 50s）
        var mTimer = bodyText.match(/S'éteint dans\\s+([^\\n]+)/i)
                  || bodyText.match(/Shutting down in\\s+([^\\n]+)/i)
                  || bodyText.match(/\\d+h\\s+\\d+m\\s+\\d+s/);
        if (mTimer) {
            res.countdown = mTimer[0].trim();
        }

        // 2. 判断当前状态是 Hors ligne (Offline) 还是 En ligne (Online)
        if (/Hors ligne|Serveur hors ligne|Offline|Server offline/i.test(bodyText)) {
            res.status = 'Offline (已关机)';
        } else if (/En ligne|Online/i.test(bodyText)) {
            res.status = 'Online (运行中)';
        }

        var allBtns = Array.from(document.querySelectorAll('button'));

        // 3. 情况 A：服务器处于关机状态 -> 寻找绿色 Démarrer / Start 按钮并点击开机
        for (var i = 0; i < allBtns.length; i++) {
            var b = allBtns[i];
            if (b.offsetParent === null || b.disabled) continue;
            var txt = (b.textContent || '').trim();
            if (/Démarrer le serveur|Démarrer|Start server|^Start$/i.test(txt)) {
                b.click();
                res.clicked = true;
                res.action_type = 'start';
                res.detail = '已点击绿色启动按钮 (' + txt + ')';
                return res;
            }
        }

        // 4. 情况 B：服务器处于运行中状态 -> 寻找左上角并排的三个控制按钮（蓝、黄、红），点击中间黄色重启按钮
        for (var j = 0; j < allBtns.length; j++) {
            var parent = allBtns[j].parentElement;
            if (!parent) continue;
            var siblingBtns = parent.querySelectorAll(':scope > button');
            if (siblingBtns.length === 3) {
                var blueBtn   = siblingBtns[0];
                var yellowBtn = siblingBtns[1]; // 中间黄色按钮为重启 (Restart)
                if (yellowBtn && !yellowBtn.disabled) {
                    yellowBtn.click();
                    res.clicked = true;
                    res.action_type = 'restart';
                    res.detail = '已点击左上角黄色重启按钮 (Restart)';
                    return res;
                } else if (blueBtn && !blueBtn.disabled) {
                    blueBtn.click();
                    res.clicked = true;
                    res.action_type = 'start';
                    res.detail = '已点击左上角蓝色启动按钮';
                    return res;
                }
            }
        }

        return res;
    })()
    """
    try:
        data = sb.execute_script(js_power) or {}
        print(f"🔍 页面状态检测：{data}")
        return data
    except Exception as e:
        print(f"⚠️ 页面电源按钮操作异常：{e}")
        return {}


def get_countdown_text(sb) -> str:
    """获取刷新后的 4 小时倒计时文本"""
    try:
        txt = sb.execute_script(r"""
            (function(){
                var bodyText = document.body.innerText || '';
                var m = bodyText.match(/S'éteint dans\s+([^\n]+)/i)
                     || bodyText.match(/Shutting down in\s+([^\n]+)/i)
                     || bodyText.match(/\d+h\s+\d+m\s+\d+s/);
                if (m) return m[0].trim();

                var spans = document.querySelectorAll('[data-slot="base"] span');
                var parts = [];
                for (var i = 0; i < spans.length; i++) {
                    var t = spans[i].textContent.trim();
                    if (/^\d+[hms]$/.test(t)) parts.push(t);
                }
                return parts.length ? parts.join(' ') : '';
            })()
        """)
        return txt or ""
    except Exception:
        return ""


# ============================================================
# API：通过浏览器 fetch 发送重启/启动指令（携带登录 Cookie）
# ============================================================

def send_power_api(sb, token: str, action: str = "restart") -> bool:
    if not token:
        return False
    token_js = json.dumps(token)
    auth_js = json.dumps(AUTH_TOKEN)
    script = (
        "var done = arguments[0];"
        "var authHeader = window.__captured_auth_header__ || " + auth_js + ";"
        'fetch("' + API_URL + '", {'
        '  method: "PUT",'
        '  headers: {'
        '    "Authorization": authHeader,'
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
            print(f"✅ API ({action}) 指令已成功送达！")
            return True
        print(f"⚠️ API ({action}) 返回非 200：{result}")
        return False
    except Exception as e:
        print(f"⚠️ API 请求异常：{e}")
        return False


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

        # ── 1. IP 验证 ───────────────────────────────────────
        print("🌐 验证出口IP...")
        try:
            sb.open("https://api.ipify.org/?format=json")
            ip_text = re.sub(r'(\d+\.\d+\.\d+\.)\d+', r'\1xx', sb.get_text('body'))
            print(f"✅ 出口IP确认：{ip_text}")
        except Exception:
            print("⚠️ IP验证超时，跳过")

        # ── 2. 登录 ──────────────────────────────────────────
        print("🔑 打开登录页面...")
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=6)
        time.sleep(4)

        if not fill_and_submit_login(sb):
            sb.save_screenshot("login_fail.png")
            send_tg("❌ 登录框加载失败", "未能找到或填入账号密码输入框", photo_path="login_fail.png")
            return

        print("⏳ 等待登录跳转...")
        for sec in range(45):
            try:
                cur_url = sb.get_current_url()
                if "/connexion" not in cur_url:
                    print(f"✅ 登录成功！当前页：{cur_url}")
                    break
                # 若第 10 秒或第 22 秒仍未跳转，自动补点一次验证与登录按钮
                if sec in (10, 22):
                    print(f"🔄 等待 {sec}s 未跳转，尝试补充点击登录按钮...")
                    try:
                        sb.uc_gui_click_captcha()
                    except Exception:
                        pass
                    trigger_login_button(sb)
            except Exception:
                pass
            time.sleep(1)
        else:
            print("❌ 登录等待超时")
            sb.save_screenshot("login_timeout.png")
            send_tg("❌ 登录等待超时", "提交账号密码后页面未跳转，请查看截图", photo_path="login_timeout.png")
            return

        # ── 3. 跳转服务器管理页并注入监听器 ──────────────────
        print(f"🔃 跳转至服务器管理页：{SERVER_URL}")
        sb.open(SERVER_URL)
        time.sleep(5)
        print(f"📄 当前页面：{sb.get_current_url()}")
        inject_listener(sb)
        sb.save_screenshot("server_page.png")

        # ── 4. 执行页面按钮点击（离线点 Démarrer 开机 / 在线点黄色按钮重启）
        power_res = check_server_and_click_power(sb)
        ui_clicked = power_res.get("clicked", False)
        action_type = power_res.get("action_type") or "restart"
        action_detail = power_res.get("detail") or "未匹配到按钮"
        before_status = power_res.get("status", "unknown")

        # 点击按钮后会触发网页自身的 Turnstile 验证，等待几秒检查是否有弹窗或 Token
        time.sleep(4)
        try:
            sb.uc_gui_click_captcha()
        except Exception:
            pass

        # ── 5. 尝试获取 Token 并调用 API 双重保障 ────────────
        token = wait_for_token(sb, timeout=15)
        api_ok = False
        if token:
            api_ok = send_power_api(sb, token, action=action_type)
            if not api_ok and action_type == "restart":
                api_ok = send_power_api(sb, token, action="start")

        if not ui_clicked and not api_ok:
            sb.save_screenshot("api_fail.png")
            send_tg(
                "❌ 重启/开机操作失败",
                f"当前状态: {before_status}\nUI按钮与API均未能成功触发",
                photo_path="api_fail.png"
            )
            return

        # ── 6. 等待服务器启动/重启并刷新页面读取最新 4 小时倒计时 ──
        print("🔄 等待 12 秒让服务器完成开机/重启状态刷新...")
        time.sleep(12)
        sb.open(SERVER_URL)
        time.sleep(6)
        sb.save_screenshot("restart_success.png")

        remaining = get_countdown_text(sb)
        after_check = sb.execute_script("""
            (function(){
                var t = document.body.innerText || '';
                if (/En ligne|Online/i.test(t)) return 'Online (运行中)';
                if (/Hors ligne|Offline/i.test(t)) return 'Offline (关机/启动中)';
                return '未知';
            })()
        """)

        countdown_line = f"⏰ 关机倒计时: {remaining}" if remaining else "⏰ 关机倒计时: 正在初始化（请查看截图）"
        summary = (
            f"📌 操作前状态: {before_status}\n"
            f"🛠 执行动作: {action_detail}\n"
            f"🟢 当前状态: {after_check}\n"
            f"{countdown_line}"
        )
        print(f"⏱️ 执行完成：\n{summary}")
        send_tg("✅ 4小时重启/开机保活成功！", summary, photo_path="restart_success.png")


if __name__ == "__main__":
    run_script()
