import os
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

def _get_proxy_server_from_env() -> str | None:
    """
    GitHub Actions / 本地通用：按常见优先级读取代理环境变量。
    支持：
      - SOCKS5_PROXY
      - ALL_PROXY
      - HTTPS_PROXY / https_proxy
      - HTTP_PROXY / http_proxy
    返回形如：
      socks5h://user:pass@host:port
      socks5://host:port
      http://user:pass@host:port
    """
    keys = [
        "SOCKS5_PROXY",
        "ALL_PROXY",
        "HTTPS_PROXY", "https_proxy",
        "HTTP_PROXY", "http_proxy",
    ]
    for k in keys:
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return None

def _playwright_proxy_config(proxy_url: str | None) -> dict | None:
    """
    将环境变量里的代理 URL 转成 Playwright proxy 配置。
    Playwright proxy 支持：
      {"server": "...", "username": "...", "password": "..."}
    但 server 里也可以直接带 user:pass@，两种都能工作。
    这里做一次解析，把 username/password 分离出来更稳。
    """
    if not proxy_url:
        return None

    # 允许用户写 host:port（无 scheme），默认当作 socks5h
    if "://" not in proxy_url:
        proxy_url = f"socks5h://{proxy_url}"

    u = urlparse(proxy_url)
    if not u.scheme or not u.hostname or not u.port:
        raise ValueError(f"代理地址格式不正确: {proxy_url}")

    server = f"{u.scheme}://{u.hostname}:{u.port}"
    cfg = {"server": server}

    # 账号密码（如果提供）
    if u.username:
        cfg["username"] = u.username
    if u.password:
        cfg["password"] = u.password

    return cfg

def add_server_time(server_url: str = "https://hub.weirdhost.xyz/server/a36fc168") -> bool:
    """
    尝试登录并点击 “시간 추가” 按钮。
    - 优先 REMEMBER_WEB_COOKIE 会话
    - 否则回退账号密码
    - 支持通过环境变量配置代理（本地/GitHub Actions 通用）
    """

    # --- 登录凭据 ---
    remember_web_cookie = os.environ.get("REMEMBER_WEB_COOKIE")
    remember_web_cookie_name = os.environ.get(
        "REMEMBER_WEB_COOKIE_NAME",
        "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
    )
    pterodactyl_email = os.environ.get("PTERODACTYL_EMAIL")
    pterodactyl_password = os.environ.get("PTERODACTYL_PASSWORD")

    if not (remember_web_cookie or (pterodactyl_email and pterodactyl_password)):
        print("错误: 缺少登录凭据。请设置 REMEMBER_WEB_COOKIE 或 PTERODACTYL_EMAIL/PTERODACTYL_PASSWORD。")
        return False

    # --- 代理（本地/GHA 通用）---
    proxy_url = _get_proxy_server_from_env()
    try:
        proxy_cfg = _playwright_proxy_config(proxy_url)
    except ValueError as e:
        print(f"错误: 代理配置无效：{e}")
        return False

    login_url = "https://hub.weirdhost.xyz/auth/login"

    def safe_goto(page, url, label):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            return True
        except PlaywrightTimeoutError:
            print(f"{label} 页面加载超时（90秒）：{url}")
            page.screenshot(path=f"{label}_goto_timeout.png")
            return False

    with sync_playwright() as p:
        launch_args = {"headless": True}
        if proxy_cfg:
            # Playwright 代理必须在 launch 时设置
            print(f"启用代理: {proxy_cfg.get('server')}")
            if proxy_cfg.get("username"):
                print("代理认证: 已提供 username（不会打印密码）")
            launch_args["proxy"] = proxy_cfg

        browser = p.chromium.launch(**launch_args)
        page = browser.new_page()
        page.set_default_timeout(90000)

        try:
            # --- 可选：代理生效自检（访问一个显示出口 IP 的页面）---
            # 不依赖第三方 JSON API，只用纯文本页；失败也不影响主流程，但会提示。
            try:
                if safe_goto(page, "https://api.ipify.org/", "proxy_ip_check"):
                    ip_txt = page.locator("body").inner_text(timeout=5000).strip()
                    print(f"出口 IP（用于确认是否走代理）: {ip_txt}")
            except Exception as e:
                print(f"代理自检跳过/失败（不影响主流程）：{e}")

            # ---------- 方案一：Cookie 登录 ----------
            if remember_web_cookie:
                print("检测到 REMEMBER_WEB_COOKIE，尝试 Cookie 登录...")

                # 先打开站点主页，确保 domain 上下文稳定
                if not safe_goto(page, "https://hub.weirdhost.xyz/", "home"):
                    return False

                page.context.add_cookies([{
                    "name": remember_web_cookie_name,
                    "value": remember_web_cookie,
                    "domain": "hub.weirdhost.xyz",
                    "path": "/",
                    "expires": int(time.time()) + 3600 * 24 * 365,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }])

                if not safe_goto(page, server_url, "server_cookie"):
                    return False

                # 更稳：检测登录页表单
                if "auth/login" in page.url or page.locator('input[name="username"]').count() > 0:
                    print("Cookie 登录失败/过期，回退账号密码登录。")
                    page.context.clear_cookies()
                    remember_web_cookie = None
                else:
                    print("Cookie 登录成功。")

            # ---------- 方案二：账号密码登录 ----------
            if not remember_web_cookie:
                if not (pterodactyl_email and pterodactyl_password):
                    print("错误: Cookie 无效且未提供账号密码。")
                    return False

                print(f"访问登录页: {login_url}")
                if not safe_goto(page, login_url, "login"):
                    return False

                email_selector = 'input[name="username"]'
                password_selector = 'input[name="password"]'
                login_button_selector = 'button[type="submit"]'

                page.wait_for_selector(email_selector, state="visible")
                page.wait_for_selector(password_selector, state="visible")
                page.wait_for_selector(login_button_selector, state="visible")

                page.fill(email_selector, pterodactyl_email)
                page.fill(password_selector, pterodactyl_password)

                print("点击登录按钮...")
                page.click(login_button_selector)

                # 不依赖导航：等待登录表单消失或 URL 改变
                try:
                    page.wait_for_selector(email_selector, state="detached", timeout=60000)
                except PlaywrightTimeoutError:
                    # 仍在登录页：读取错误提示
                    if "auth/login" in page.url:
                        err = "未知错误"
                        danger = page.locator(".alert.alert-danger")
                        if danger.count() > 0:
                            err = danger.first.inner_text().strip()
                        print(f"邮箱密码登录失败: {err}")
                        page.screenshot(path="login_fail_error.png")
                        return False

                print("邮箱密码登录成功（或已离开登录表单）。")

            # ---------- 确保到服务器页面 ----------
            if page.url != server_url:
                print(f"导航到目标服务器页面: {server_url}")
                if not safe_goto(page, server_url, "server_final"):
                    return False
                if "auth/login" in page.url:
                    print("导航后仍回到登录页：会话失效/权限不足。")
                    page.screenshot(path="server_page_nav_fail.png")
                    return False

            # ---------- 核心操作：点击“시간 추가” ----------
            add_button = page.locator('button:has-text("시간 추가")').first
            print("等待 '시간 추가' 按钮出现...")

            try:
                add_button.wait_for(state="visible", timeout=30000)
                add_button.scroll_into_view_if_needed()

                # 试探可点击（不会真的点）
                add_button.click(trial=True, timeout=5000)

                # 真点击
                add_button.click(timeout=10000)

                print("成功点击 '시간 추가' 按钮。")
                time.sleep(5)
                print("任务完成。")
                return True

            except PlaywrightTimeoutError:
                print("错误: 30秒内未找到按钮或按钮不可点击。")
                page.screenshot(path="add_time_button_not_clickable.png")
                # 额外调试：帮助判断是不是文案变了
                try:
                    html = page.content()
                    print(f"调试：页面是否包含 '시간 추가' 文本：{'시간 추가' in html}")
                except Exception:
                    pass
                return False

        except Exception as e:
            print(f"执行过程中发生未知错误: {e}")
            page.screenshot(path="general_error.png")
            return False
        finally:
            browser.close()

if __name__ == "__main__":
    print("开始执行添加服务器时间任务...")
    ok = add_server_time()
    print("任务执行成功。" if ok else "任务执行失败。")
    raise SystemExit(0 if ok else 1)
