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
        f"🎮 Minestrator 保活巡检\n"
        f"🕐 运行时间: {now_str()}\n"
        f"🖥 服务器ID: {SERVER_ID}\n"
        f"📊 状态: {result}\n"
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
# Turnstile 预注入拦截器
# ============================================================

INJECT_TOKEN_LISTENER_JS = """
(function() {
    if (window.__cf_token_listener_injected__) return;
    window.__cf_token_listener_injected__ = true;
    window.__cf_turnstile_token__ = '';

    window.addEventListener('message', function(e) {
        if (!e.origin || e.origin.indexOf('cloudflare.com') === -1) return;
        var d = e.data;
        if (d && d.token && d.token.length > 30) {
            window.__cf_turnstile_token__ = d.token;
        }
    });

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
    try:
        sb.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": INJECT_TOKEN_LISTENER_JS}
        )
    except Exception:
        pass
    try:
        sb.execute_script(INJECT_TOKEN_LISTENER_JS)
    except Exception:
        pass


def check_and_click_renew(sb) -> dict:
    """
    检查页面上是否出现了 Renew / Renouveler / Prolonger（续期）按钮或链接。
    如果出现则自动点击；同时检查服务器是否 Offline，若离线则顺便点击蓝色启动按钮。
    """
    js_code = """
    (function() {
        var result = {
            renew_found: false,
            renew_clicked_text: '',
            server_status: 'unknown',
            start_clicked: false,
            banner_text: ''
        };

        // 1. 提取顶部试用期横幅文字
        var bodyText = document.body.innerText || '';
        var m = bodyText.match(/You are currently on a trial offer[^\\n]+/i) ||
                bodyText.match(/Vous profitez actuellement[^\\n]+/i) ||
                bodyText.match(/您目前正在享受试用优惠[^\\n]+/);
        if (m) result.banner_text = m[0].trim();

        // 2. 查找是否有 Renew / Renouveler / Prolonger 按钮或链接
        var keywords = ['renew', 'renouveler', 'prolonger', 'extend', '续订', '续期', '延长'];
        var clickables = Array.from(document.querySelectorAll('button, a, [role="button"]'));
        for (var i = 0; i < clickables.length; i++) {
            var el = clickables[i];
            if (el.offsetParent === null) continue; // 忽略不可见元素
            var txt = (el.textContent || '').trim().toLowerCase();
            // 排除顶部横幅纯文本容器，只匹配简短的按钮文字
            if (txt.length > 0 && txt.length < 40) {
                for (var k = 0; k < keywords.length; k++) {
                    if (txt.indexOf(keywords[k]) !== -1) {
                        el.click();
                        result.renew_found = true;
                        result.renew_clicked_text = (el.textContent || '').trim();
                        break;
                    }
                }
            }
            if (result.renew_found) break;
        }

        // 3. 检查左上角服务器在线状态（Online / En ligne / 离线）
        if (/Online|En ligne|在线/i.test(bodyText)) {
            result.server_status = 'Online';
        } else if (/Offline|Hors ligne|离线|Stopped|Arrêté/i.test(bodyText)) {
            result.server_status = 'Offline';
        }

        // 4. 如果服务器处于 Offline 关机状态，自动点击左上角第一个蓝色启动按钮
        if (result.server_status === 'Offline') {
            var allBtns = Array.from(document.querySelectorAll('button'));
            for (var j = 0; j < allBtns.length; j++) {
                var parent = allBtns[j].parentElement;
                if (!parent) continue;
                var siblingBtns = parent.querySelectorAll(':scope > button');
                if (siblingBtns.length === 3 && !siblingBtns[0].disabled) {
                    siblingBtns[0].click();
                    result.start_clicked = true;
                    break;
                }
            }
        }

        return result;
    })()
    """
    try:
        return sb.execute_script(js_code) or {}
    except Exception as e:
        print(f"⚠️ 检查续期按钮异常: {e}")
        return {}


def send_api_action(sb, token: str, action="restart") -> bool:
    if not AUTH_TOKEN or not token:
        return False
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
        return bool(result.get("ok") and result.get("data", {}).get("api", {}).get("code") == 200)
    except Exception:
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
            print(f"❌ 登录框加载失败：{e}")
            sb.save_screenshot("login_fail.png")
            send_tg("❌ 登录框加载失败", f"原因: {e}", photo_path="login_fail.png")
            return

        print("📤 提交登录请求...")
        try:
            if sb.is_element_visible("button:contains('Se connecter')"):
                sb.click("button:contains('Se connecter')")
            elif sb.is_element_visible("button[type='submit']"):
                sb.click("button[type='submit']")
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
            print("✅ 已点击登录按钮")
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
            send_tg("❌ 登录等待超时", "提交账号密码后页面未跳转", photo_path="login_timeout.png")
            return

        # ── 跳转服务器管理页 ──────────────────────────────────
        print(f"🔃 跳转至服务器管理页：{SERVER_URL}")
        pre_inject_listener(sb)
        sb.open(SERVER_URL)
        time.sleep(5)
        print(f"📄 当前页面：{sb.get_current_url()}")

        # ── 检查续期按钮与服务器状态 ──────────────────────────
        info = check_and_click_renew(sb)
        banner_text = info.get("banner_text") or "未读取到横幅提示"
        srv_status  = info.get("server_status", "unknown")
        renew_found = info.get("renew_found", False)
        start_clicked = info.get("start_clicked", False)

        print(f"📊 检测结果 -> 在线状态: {srv_status} | 发现续期按钮: {renew_found} | 横幅: {banner_text}")

        # 情况 A：发现了 Renew 按钮并已点击
        if renew_found:
            btn_txt = info.get("renew_clicked_text", "Renew")
            print(f"🎉 已点击续期按钮 [{btn_txt}]，等待可能弹出的验证码或弹窗...")
            time.sleep(4)
            # 如果点击 Renew 后弹出二次确认模态框或 Turnstile 验证，尝试自动点击处理
            try:
                sb.uc_gui_click_captcha()
            except Exception:
                pass
            time.sleep(5)
            sb.open(SERVER_URL)
            time.sleep(5)
            new_info = check_and_click_renew(sb)
            new_banner = new_info.get("banner_text") or banner_text
            sb.save_screenshot("renew_success.png")
            send_tg(
                "✅ 自动续期成功！",
                f"🔘 点击按钮: {btn_txt}\n🟢 服务器状态: {srv_status}\n⏰ 最新提示: {new_banner}",
                photo_path="renew_success.png"
            )
            return

        # 情况 B：还没到续期时间窗口（没有 Renew 按钮），但如果页面自带了 Token 则顺手调一下 API
        token = ""
        try:
            token = sb.execute_script(READ_TOKEN_JS) or ""
        except Exception:
            pass

        if token and len(token) > 30:
            print(f"✅ 捕获到页面 Token（长度 {len(token)}），发送保活指令...")
            send_api_action(sb, token, action="start" if srv_status == "Offline" else "restart")
            time.sleep(5)

        sb.save_screenshot("status_check.png")
        extra_note = "\n🚀 检测到服务器离线，已自动点击开机按钮！" if start_clicked else ""
        detail_msg = (
            f"🟢 服务器状态: {srv_status}\n"
            f"⏳ 续期按钮: 暂未出现（未到到期前续期窗口）\n"
            f"📢 面板提示: {banner_text}"
            f"{extra_note}"
        )
        print(f"ℹ️ 巡检完成：\n{detail_msg}")
        send_tg("ℹ️ 巡检正常（未到续期窗口）", detail_msg, photo_path="status_check.png")


if __name__ == "__main__":
    run_script()
