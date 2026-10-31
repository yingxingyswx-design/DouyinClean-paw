"""Chrome/CDP 连接、页面采集与页面状态检查。

只通过 Playwright 的公开 API 和正常页面 DOM 操作，不调用抖音私有接口。
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright

from config import Settings, TEMP_DIR, validate_cdp_url
from matcher import Account, normalize_username

LOGGER = logging.getLogger("douyin_clean.browser")

LOGIN_MARKERS = ("扫码登录", "手机号登录", "密码登录", "登录后查看")
CHALLENGE_MARKERS = (
    "请完成安全验证",
    "拖动滑块",
    "滑块验证",
    "验证码校验",
    "访问过于频繁",
    "操作频繁，请稍后再试",
)

# 抖音改版时优先在这里补充选择器；找不到入口时程序会停止，而不是乱点。
MORE_BUTTON_SELECTORS = (
    "#tooltip",
    '[aria-haspopup="true"][data-popupid]',
    '[aria-haspopup="true"]',
    '[data-e2e="user-more"]',
    '[data-e2e="user-detail-more"]',
    '[data-e2e="user-more-btn"]',
    'button[aria-label="更多"]',
    '[role="button"][aria-label="更多"]',
)


class BrowserError(RuntimeError):
    """浏览器连接或页面操作失败。"""


class NotLoggedInError(BrowserError):
    """页面要求重新登录。"""


class PageChallengeError(BrowserError):
    """页面出现验证码/风控，需要人工处理。"""


@dataclass(frozen=True, slots=True)
class CdpInfo:
    ok: bool
    browser: str = ""
    detail: str = ""


@dataclass(slots=True)
class Session:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    created_page: bool

    def close(self, keep_page: bool = False) -> None:
        """断开 CDP，不关闭用户正在使用的浏览器。"""
        try:
            if self.created_page and not keep_page and not self.page.is_closed():
                self.page.close()
        except Exception:
            LOGGER.debug("关闭自动化标签页失败（可忽略）", exc_info=True)
        try:
            # 对 connect_over_cdp 而言，close 只断开连接，不会退出 Chrome。
            self.browser.close()
        except Exception:
            LOGGER.debug("断开 CDP 连接失败（可忽略）", exc_info=True)
        try:
            self.playwright.stop()
        except Exception:
            LOGGER.debug("停止 Playwright 失败（可忽略）", exc_info=True)


def is_douyin_url(url: str) -> bool:
    """严格判断是否为抖音主域名，防止误访问仿冒域名。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host == "douyin.com" or host.endswith(".douyin.com")


def probe_cdp(cdp_url: str, timeout: float = 2.0) -> CdpInfo:
    """探测本机 CDP 端口；返回值不包含 webSocketDebuggerUrl。"""
    endpoint = cdp_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return CdpInfo(False, detail=f"{type(exc).__name__}: {exc}")
    browser = str(payload.get("Browser") or "未知版本")
    return CdpInfo(True, browser=browser)


def chrome_process_count() -> int:
    """统计 chrome.exe 进程数；无法查询时返回 -1。"""
    if os.name != "nt":
        return -1
    try:
        completed = subprocess.run(
            ["tasklist", "/NH", "/FI", "IMAGENAME eq chrome.exe"],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    return completed.stdout.lower().count(b"chrome.exe")


def chrome_version(chrome_path: Path | None) -> str:
    """读取 Chrome 版本；读取失败返回“未知”。"""
    if chrome_path is None or not chrome_path.is_file():
        return "未知"
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"
            ) as key:
                value, _ = winreg.QueryValueEx(key, "version")
                return str(value)
        except OSError:
            pass
    return "未知"


def kill_debug_chrome(port: int = 9229) -> int:
    """只结束本工具使用的调试 Chrome（带 9229 端口或 chrome-profile）。"""
    if os.name != "nt":
        return 0

    script = f"""
$targets = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {{
  $_.CommandLine -and $_.CommandLine -notlike '*--type=*' -and (
    $_.CommandLine -like '*--remote-debugging-port={port}*' -or
    $_.CommandLine -like '*chrome-profile*'
  )
}}
$killed = 0
foreach ($p in $targets) {{
  & taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {{ $killed++ }}
}}
Write-Output $killed
"""
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creationflags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BrowserError(f"结束 Chrome 失败：{exc}") from exc

    output = (completed.stdout or "").strip().splitlines()
    if output:
        try:
            return int(output[-1].strip())
        except ValueError:
            pass
    return 0


def launch_chrome(settings: Settings) -> CdpInfo:
    """用现有配置目录启动 Chrome，并临时开启本机调试端口。

    前提：Chrome 必须已经完全退出。程序不会结束或杀死任何浏览器进程，
    也不会修改 Chrome 的首选项/配置文件。
    """
    if settings.silent and not settings.allow_window:
        raise BrowserError(
            "静默模式拒绝启动 Chrome：这会创建可见窗口。"
            "如确认允许弹窗，请显式加上 --allow-window；否则请先手动启动 Chrome。"
        )

    info = probe_cdp(settings.cdp_url)
    if info.ok:
        LOGGER.info("检测到 Chrome 调试端口已就绪：%s", info.browser)
        return info

    if settings.chrome_path is None or not settings.chrome_path.is_file():
        raise BrowserError(
            "未找到 Chrome。请通过 --chrome-path 指定 chrome.exe 的完整路径。"
        )
    if settings.profile_dir is None or not settings.profile_dir.is_dir():
        raise BrowserError(
            f"未找到 Chrome 用户数据目录：{settings.profile_dir}。"
            "请通过 --user-data-dir 指定。"
        )

    running = chrome_process_count()
    if running != 0:
        detail = "无法确认" if running < 0 else str(running)
        raise BrowserError(
            f"检测到 Chrome 仍在运行（进程数：{detail}）。"
            "请先完全退出 Chrome（包括托盘/后台进程），再重新运行 --launch-chrome。"
            "本工具不会主动结束你的浏览器进程。"
        )

    parsed = urlparse(validate_cdp_url(settings.cdp_url))
    port = parsed.port or 9222
    command = [
        str(settings.chrome_path),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={settings.profile_dir}",
        f"--profile-directory={settings.profile_directory}",
        "--restore-last-session",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    LOGGER.warning(
        "即将启动 Chrome：复用现有配置目录，仅本次开启本机调试端口 %d；"
        "不会修改浏览器设置。调试端口开启期间，本机其他程序可控制该浏览器，"
        "使用完毕后请关闭 Chrome。",
        port,
    )
    LOGGER.debug("启动命令：%s", " ".join(command))

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise BrowserError(f"启动 Chrome 失败：{exc}") from exc

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        info = probe_cdp(settings.cdp_url, timeout=1.0)
        if info.ok:
            LOGGER.info("Chrome 调试端口已就绪：%s", info.browser)
            return info
        time.sleep(0.5)
    raise BrowserError(
        "Chrome 已启动，但 25 秒内无法连接调试端口。"
        "请检查是否有安全软件拦截，或手动确认端口 9222 未被占用。"
    )


def _choose_context(browser: Browser) -> BrowserContext:
    """优先选择已经打开抖音页面的持久化上下文。"""
    contexts = browser.contexts
    if not contexts:
        raise BrowserError(
            "已连接 Chrome，但没有可用的浏览器上下文。"
            "请确认使用的是正常登录的 Chrome，而不是无痕窗口。"
        )
    for context in contexts:
        if any(is_douyin_url(page.url) for page in context.pages):
            return context
    return contexts[0]


def _prepare_driver_temp() -> None:
    """把 Playwright/Node 的临时目录放到项目内，规避 %TEMP% 权限问题。"""
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(TEMP_DIR)


def _install_popup_guards(page: Page) -> None:
    """静默运行期间不显示原生弹窗；遇到弹窗/新窗口直接关闭并记录。"""

    def on_dialog(dialog) -> None:
        LOGGER.warning(
            "页面触发浏览器原生弹窗（%s），已自动关闭；"
            "涉及确认的操作会因无法确认而失败。",
            getattr(dialog, "type", "unknown"),
        )
        try:
            dialog.dismiss()
        except Exception:
            LOGGER.debug("关闭原生弹窗失败", exc_info=True)

    def on_popup(popup) -> None:
        LOGGER.warning("页面尝试打开新窗口，已在后台关闭（静默模式不弹窗）。")
        try:
            popup.close()
        except Exception:
            LOGGER.debug("关闭新窗口失败", exc_info=True)

    page.on("dialog", on_dialog)
    page.on("popup", on_popup)


def open_session(settings: Settings, create_tab: bool) -> Session:
    """连接已登录的 Chrome，复用现有 Cookie 会话。"""
    from playwright.sync_api import sync_playwright

    _prepare_driver_temp()
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(
            settings.cdp_url, timeout=15_000
        )
    except Exception as exc:
        playwright.stop()
        raise BrowserError(
            "无法连接 Chrome 调试端口。请确认 Chrome 已使用 "
            "--remote-debugging-port=9222 启动，且端口未被占用。"
            f"（{type(exc).__name__}: {exc}）"
        ) from exc

    try:
        context = _choose_context(browser)
        pages = [page for page in context.pages if not page.is_closed()]

        if create_tab:
            page = context.new_page()
            created_page = True
            LOGGER.info("已在现有 Chrome 会话中新建标签页。")
        else:
            douyin_pages = [page for page in pages if is_douyin_url(page.url)]
            if not douyin_pages:
                hint = (
                    "静默模式不会新建标签页；请先在 Chrome 中打开抖音页面，"
                    "或显式加上 --new-tab。"
                    if settings.silent
                    else "请先手动打开抖音目标页面（如关注列表或用户搜索结果），"
                    "或使用 --url 指定要访问的抖音页面，或加上 --new-tab。"
                )
                raise BrowserError(f"当前 Chrome 没有已打开的抖音页面。{hint}")
            page = douyin_pages[-1]
            created_page = False
            try:
                title = page.title()
            except Exception:
                title = ""
            LOGGER.info("使用现有抖音标签页：%s %s", title or "(无标题)", page.url)
            if settings.silent:
                LOGGER.info("静默模式：不新建窗口/标签页，也不主动聚焦浏览器。")
            if len(douyin_pages) > 1:
                LOGGER.info(
                    "检测到 %d 个抖音标签页，默认使用最后一个；"
                    "如需精确指定，请改用 --url。",
                    len(douyin_pages),
                )

        _install_popup_guards(page)
        return Session(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            created_page=created_page,
        )
    except Exception:
        try:
            browser.close()
        finally:
            playwright.stop()
        raise


def ensure_page_ready(page: Page) -> None:
    """识别登录失效和验证码/风控页面；不做任何绕过。"""
    url = page.url
    if "passport" in url or "/login" in url:
        raise NotLoggedInError("页面跳转到了登录页，请先在 Chrome 中重新登录抖音。")

    try:
        body = page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 6000)"
        )
    except Exception:
        body = ""
    text = str(body or "")

    for marker in CHALLENGE_MARKERS:
        if marker in text:
            raise PageChallengeError(
                f"页面出现“{marker}”，需要人工验证。"
                "本工具不会绕过验证码或风控，已停止操作。"
            )

    # 登录弹层偶尔会出现在已登录页面上；以“未登录”作为硬判断，
    # 避免把可正常采集的页面误判为登录失效。
    if "未登录" in text:
        raise NotLoggedInError(
            "页面显示“未登录”，登录状态可能已失效。请先在 Chrome 中登录抖音。"
        )

    login_marker = next((marker for marker in LOGIN_MARKERS if marker in text), "")
    if login_marker:
        page.wait_for_timeout(1200)
        try:
            text2 = page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 6000)"
            )
        except Exception:
            text2 = ""
        if "未登录" in str(text2 or ""):
            raise NotLoggedInError(
                f"页面提示“{login_marker}”，登录状态可能已失效。"
                "请先在 Chrome 中登录抖音。"
            )


def goto_douyin(page: Page, url: str, settings: Settings) -> None:
    """只导航到抖音域名，等待页面基本可用。"""
    if not is_douyin_url(url):
        raise BrowserError(f"拒绝访问非抖音域名：{url}")
    validate_cdp_url(settings.cdp_url)

    try:
        response = page.goto(
            url, wait_until="domcontentloaded", timeout=settings.timeout_ms
        )
    except Exception as exc:
        raise BrowserError(
            f"打开页面失败：{url}（{type(exc).__name__}: {exc}）"
        ) from exc

    if response is not None and response.status >= 400:
        raise BrowserError(f"页面返回 HTTP {response.status}：{url}")

    page.wait_for_timeout(2500)
    ensure_page_ready(page)


def _extract_accounts_js() -> str:
    """返回页面内提取账号的 JS。只读取 DOM 中可见的用户链接。"""
    return r"""
() => {
  const out = [];
  const seen = new Set();
  const navWords = new Set([
    "首页", "推荐", "关注", "朋友", "我的", "消息", "搜索", "登录",
    "抖音", "创作者中心", "更多", "私信", "已关注", "分享", "举报",
    "...", "···", "…"
  ]);
  const actionWords = new Set(["关注", "已关注", "私信", "更多", "...", "···", "…"]);
  const clean = (value) => (value || "")
    .replace(/[\u200b\xa0]/g, " ")
    .replace(/\s+/g, " ")
    .trim();

  const anchors = Array.from(document.querySelectorAll('a[href*="/user/"]'));
  for (const anchor of anchors) {
    const href = anchor.href || "";
    const match = href.match(/\/user\/([^/?#]+)/);
    if (!match) continue;
    let accountId = "";
    try {
      accountId = decodeURIComponent(match[1]);
    } catch (err) {
      accountId = match[1];
    }
    if (!accountId || accountId === "self" || seen.has(accountId)) continue;

    const rect = anchor.getBoundingClientRect();
    const style = window.getComputedStyle(anchor);
    if (rect.width < 2 || rect.height < 2) continue;
    if (style.display === "none" || style.visibility === "hidden") continue;

    const lines = (anchor.innerText || "").split(/\n/)
      .map(clean)
      .filter(Boolean);
    let name = lines.find((line) => !actionWords.has(line)) || "";

    if (!name) {
      const img = anchor.querySelector("img[alt]");
      if (img) name = clean(img.getAttribute("alt"));
    }
    if (!name) {
      const label = anchor.getAttribute("aria-label") || anchor.getAttribute("title");
      if (label) name = clean(label);
    }

    const card = anchor.closest(
      '[data-e2e*="user"], li, [class*="card"], [class*="Card"], ' +
      '[class*="user"], [class*="User"]'
    );
    if (!name && card) {
      const nameEl = card.querySelector(
        '[data-e2e*="user-name"], [data-e2e*="userName"], ' +
        '[class*="userName"], [class*="username"], [class*="nickname"], ' +
        '[class*="Name"]'
      );
      if (nameEl) name = clean(nameEl.innerText || nameEl.textContent);
    }
    if (!name && card) {
      name = clean((card.innerText || "").split(/\n/)[0] || "");
    }
    if (!name) continue;
    name = name.replace(/^@/, "").trim();
    if (name.length > 40) continue;
    if (navWords.has(name)) continue;

    seen.add(accountId);
    out.push({
      account_id: accountId,
      username: name,
      url: href.split("?")[0]
    });
  }
  return out;
}
"""


def visible_accounts(page: Page) -> list[Account]:
    """读取当前 DOM 中可见的用户链接。"""
    try:
        rows: list[dict[str, Any]] = page.evaluate(_extract_accounts_js())
    except Exception as exc:
        raise BrowserError(
            f"读取页面账号失败，页面可能正在跳转（{type(exc).__name__}: {exc}）"
        ) from exc

    accounts: list[Account] = []
    seen: set[str] = set()
    for row in rows or []:
        account_id = str(row.get("account_id") or "").strip()
        username = normalize_username(str(row.get("username") or ""))
        url = str(row.get("url") or "").strip()
        if not account_id or not username or not is_douyin_url(url):
            continue
        if account_id in seen:
            continue
        seen.add(account_id)
        accounts.append(
            Account(
                account_id=account_id,
                username=username,
                url=url,
                source_url=page.url,
            )
        )
    return accounts


def _scroll_down(page: Page) -> tuple[int, int]:
    """向下滚动约一屏，返回 (滚动前, 滚动后) 位置。"""
    try:
        result = page.evaluate(
            """
            () => {
              const el = document.scrollingElement || document.documentElement;
              const before = el.scrollTop;
              const step = Math.max(window.innerHeight * 0.9, 600);
              el.scrollTop = Math.min(el.scrollHeight, before + step);
              return [before, el.scrollTop];
            }
            """
        )
    except Exception as exc:
        raise BrowserError(
            f"页面滚动失败（{type(exc).__name__}: {exc}）"
        ) from exc
    if isinstance(result, list) and len(result) == 2:
        return int(result[0]), int(result[1])
    return 0, 0


FOLLOWING_URL = "https://www.douyin.com/user/self"


def open_following_list(page: Page, settings: Settings) -> None:
    """在“我的”主页点击“关注”数字，打开关注列表弹层。"""
    goto_douyin(page, FOLLOWING_URL, settings)
    page.wait_for_timeout(1500)

    before = page.locator('a[href*="/user/"]').count()
    clicked = ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        clicked = page.evaluate(
            """
            () => {
              const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              const paragraphs = Array.from(document.querySelectorAll('p'));
              for (const p of paragraphs) {
                if (/^关注\\s*\\d+/.test(normalize(p.innerText))) {
                  const target = p.querySelector('span') || p;
                  target.click();
                  return normalize(p.innerText);
                }
              }
              const spans = Array.from(document.querySelectorAll('span'));
              for (const span of spans) {
                if (span.children.length === 0 && normalize(span.textContent) === '关注') {
                  const parent = span.parentElement;
                  if (parent && /\\d/.test(normalize(parent.innerText))) {
                    span.click();
                    return normalize(parent.innerText);
                  }
                }
              }
              return '';
            }
            """
        )
        if clicked:
            break
        page.wait_for_timeout(500)

    if not clicked:
        raise BrowserError(
            "未找到“关注”入口。请确认已登录，且当前账号主页可见关注数量。"
        )

    page.wait_for_timeout(2500)
    after = page.locator('a[href*="/user/"]').count()
    if after <= before:
        raise BrowserError(
            "点击“关注”后列表没有展开。抖音页面结构可能已变化，已停止。"
        )
    LOGGER.info("关注列表已打开：%s，用户链接 %d 个。", clicked, after)


def _scroll_following_list(page: Page) -> tuple[int, int] | None:
    """滚动关注列表弹层内部的滚动容器，而不是整个窗口。"""
    try:
        result = page.evaluate(
            """
            () => {
              const links = Array.from(document.querySelectorAll('a[href*="/user/"]'))
                .filter((a) => !(a.getAttribute('href') || '').includes('/user/self'));
              if (!links.length) return null;
              let node = links[0].parentElement;
              while (node && node !== document.body) {
                const style = getComputedStyle(node);
                if (
                  node.scrollHeight > node.clientHeight + 80 &&
                  (style.overflowY === 'auto' || style.overflowY === 'scroll')
                ) {
                  const before = node.scrollTop;
                  node.scrollTop = Math.min(
                    node.scrollHeight,
                    before + node.clientHeight * 0.9
                  );
                  return [before, node.scrollTop];
                }
                node = node.parentElement;
              }
              return null;
            }
            """
        )
    except Exception as exc:
        raise BrowserError(
            f"关注列表滚动失败（{type(exc).__name__}: {exc}）"
        ) from exc
    if isinstance(result, list) and len(result) == 2:
        return int(result[0]), int(result[1])
    return None


def collect_following_accounts(page: Page, settings: Settings) -> list[Account]:
    """打开关注列表并滚动采集其中的账号。"""
    open_following_list(page, settings)

    found: dict[str, Account] = {}
    stale_rounds = 0

    for attempt in range(settings.max_scrolls + 1):
        batch = visible_accounts(page)
        new_count = 0
        for account in batch:
            if account.account_id not in found:
                found[account.account_id] = account
                new_count += 1

        if attempt > 0:
            stale_rounds = stale_rounds + 1 if new_count == 0 else 0
            if stale_rounds >= 2:
                LOGGER.info("关注列表连续两轮没有新账号，停止滚动。")
                break

        if attempt >= settings.max_scrolls:
            break

        position = _scroll_following_list(page)
        if position is None:
            LOGGER.warning("未找到关注列表的可滚动容器，停止滚动。")
            break
        before, after = position
        if before == after:
            LOGGER.info("关注列表已到底部，停止滚动。")
            break

        delay_ms = int(random.uniform(*settings.scroll_pause) * 1000)
        page.wait_for_timeout(delay_ms)

    LOGGER.info("关注列表采集完成：共识别到 %d 个账号。", len(found))
    return list(found.values())


def collect_user_tab_accounts(page: Page, settings: Settings) -> list[Account]:
    """在当前搜索结果页点击“用户”页签，并采集可见账号。"""
    tab = page.get_by_text("用户", exact=True).first
    try:
        tab.wait_for(state="visible", timeout=10_000)
    except Exception as exc:
        raise BrowserError(
            "当前页面没有“用户”结果页签。请先打开一次抖音搜索页面。"
        ) from exc

    if "/search/" not in page.url or "type=user" not in page.url:
        tab.click()
        page.wait_for_timeout(2500)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if visible_accounts(page):
            break
        page.wait_for_timeout(500)
    else:
        raise BrowserError("“用户”页签下没有加载出账号。")

    return collect_accounts(page, settings, page.url)


def _open_search_users(page: Page, keyword: str, settings: Settings) -> None:
    """通过抖音搜索框搜索关键词，并切到“用户”结果页。"""
    goto_douyin(page, "https://www.douyin.com/", settings)
    page.wait_for_timeout(1000)

    search_box = page.locator('input[placeholder*="搜索"]').first
    try:
        search_box.wait_for(state="visible", timeout=10_000)
    except Exception as exc:
        raise BrowserError(
            "未找到抖音搜索框。请确认页面已登录并正常加载。"
        ) from exc

    search_box.fill(keyword)
    search_box.press("Enter")
    page.wait_for_timeout(3500)

    user_tab = page.get_by_text("用户", exact=True).first
    try:
        user_tab.wait_for(state="visible", timeout=12_000)
    except Exception as exc:
        raise BrowserError(
            f"搜索“{keyword}”后未出现“用户”结果页签。"
            "可能是搜索被风控拦截，已停止。"
        ) from exc
    user_tab.click()
    page.wait_for_timeout(2500)

    # 等待用户卡片出现（排除页面自身的 /user/self 导航链接）。
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if visible_accounts(page):
            return
        page.wait_for_timeout(500)
    raise BrowserError(f"搜索“{keyword}”的用户结果为空或加载超时。")


def search_and_collect(page: Page, settings: Settings) -> list[Account]:
    """逐个关键词搜索，切换到“用户”页签并采集可见账号。"""
    found: dict[str, Account] = {}
    errors: list[str] = []

    for keyword in settings.keywords:
        LOGGER.info("搜索关键词：%s", keyword)
        try:
            _open_search_users(page, keyword, settings)
        except BrowserError as exc:
            LOGGER.warning("关键词“%s”搜索失败：%s", keyword, exc)
            errors.append(str(exc))
            continue

        for account in collect_accounts(page, settings, page.url):
            found.setdefault(account.account_id, account)

    if not found and errors:
        raise BrowserError("所有关键词搜索均失败：" + "；".join(errors[:3]))

    LOGGER.info("搜索采集完成：共识别到 %d 个账号。", len(found))
    return list(found.values())


def collect_accounts(
    page: Page, settings: Settings, source_url: str
) -> list[Account]:
    """滚动采集当前页面上的账号，按账号 ID 去重。"""
    found: dict[str, Account] = {}
    stale_rounds = 0
    previous_position = -1

    for attempt in range(settings.max_scrolls + 1):
        batch = visible_accounts(page)
        new_count = 0
        for account in batch:
            if account.account_id not in found:
                found[account.account_id] = Account(
                    account_id=account.account_id,
                    username=account.username,
                    url=account.url,
                    source_url=source_url,
                )
                new_count += 1

        if attempt > 0:
            stale_rounds = stale_rounds + 1 if new_count == 0 else 0
            if stale_rounds >= 2:
                LOGGER.info("连续两轮没有发现新账号，停止滚动。")
                break

        if attempt >= settings.max_scrolls:
            break

        before, after = _scroll_down(page)
        if after == before and before == previous_position:
            LOGGER.info("页面已到底部，停止滚动。")
            break
        previous_position = after
        if before == after:
            LOGGER.info("页面无法继续滚动，停止。")
            break

        delay_ms = int(random.uniform(*settings.scroll_pause) * 1000)
        page.wait_for_timeout(delay_ms)

    LOGGER.info("页面采集完成：共识别到 %d 个可见账号。", len(found))
    return list(found.values())
