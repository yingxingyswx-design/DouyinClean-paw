"""通过抖音网页正常 UI 执行拉黑，并在失败时立即停止。"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

from browser import (
    MORE_BUTTON_SELECTORS,
    NotLoggedInError,
    PageChallengeError,
    ensure_page_ready,
    is_douyin_url,
)
from config import ARTIFACT_DIR, Settings
from logger import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    HistoryStore,
)
from matcher import Account

LOGGER = logging.getLogger("douyin_clean.blocker")

BLOCK_MENU_PATTERNS = (
    re.compile(r"^拉黑$"),
    re.compile(r"^拉黑用户$"),
    re.compile(r"^加入黑名单$"),
)
BLOCK_WORDS = ("拉黑", "黑名单")
BLOCKED_BODY_MARKERS = (
    "你已将对方拉黑",
    "你已经拉黑对方",
    "无法查看其作品",
    "已加入黑名单",
)
CONFIRM_BUTTON_PATTERN = re.compile(r"^(确定|确认|拉黑|确认拉黑|确定拉黑)$")


class BlockError(RuntimeError):
    """拉黑流程中任何无法安全继续的情况。"""


@dataclass(frozen=True, slots=True)
class BlockResult:
    status: Literal["blocked", "skipped"]
    reason: str


@dataclass(slots=True)
class RunStats:
    matched: int = 0
    blocked: int = 0
    skipped: int = 0
    failed: int = 0


def _visible_first(locator: Locator, limit: int = 10) -> Locator | None:
    """返回第一个可见元素；定位器数量异常时不无限等待。"""
    try:
        count = min(locator.count(), limit)
    except Exception:
        return None
    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _inner_text(locator: Locator) -> str:
    try:
        return locator.inner_text(timeout=2000)
    except Exception:
        return ""


def _find_more_button(page: Page) -> Locator | None:
    try:
        page.locator("#tooltip").first.wait_for(state="visible", timeout=3000)
    except Exception:
        pass

    for selector in MORE_BUTTON_SELECTORS:
        item = _visible_first(page.locator(selector))
        if item is not None:
            return item

    candidates = (
        page.get_by_role("button", name="更多", exact=True),
        page.locator('button:has-text("更多")'),
        page.locator('[role="button"]:has-text("更多")'),
    )
    for locator in candidates:
        item = _visible_first(locator)
        if item is not None:
            return item
    return None


def _find_block_menu_item(page: Page) -> Locator | None:
    for pattern in BLOCK_MENU_PATTERNS:
        try:
            locator = page.get_by_text(pattern)
        except Exception:
            continue
        item = _visible_first(locator)
        if item is not None:
            return item
    return None


def _find_confirm_dialog(page: Page) -> Locator | None:
    selectors = (
        '[role="dialog"]',
        '[class*="modal"]',
        '[class*="Modal"]',
        '[class*="dialog"]',
        '[class*="Dialog"]',
    )
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 8)
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
            except Exception:
                continue
            text = _inner_text(item)
            if any(word in text for word in BLOCK_WORDS) and any(
                word in text for word in ("取消", "确定", "确认")
            ):
                return item
    return None


def _click_confirm(dialog: Locator) -> bool:
    locator = dialog.get_by_role("button", name=CONFIRM_BUTTON_PATTERN)
    try:
        count = min(locator.count(), 8)
    except Exception:
        count = 0
    for index in range(count):
        item = locator.nth(index)
        try:
            if not item.is_visible():
                continue
            text = _inner_text(item)
            if "取消" in text:
                continue
            item.click(timeout=5000)
            return True
        except Exception:
            continue

    # 某些弹窗按钮不是标准 button role，退回到文本定位。
    for text in ("确定", "确认", "拉黑"):
        fallback = dialog.get_by_text(text, exact=True)
        item = _visible_first(fallback)
        if item is not None:
            try:
                item.click(timeout=5000)
                return True
            except Exception:
                continue
    return False


def _has_blocked_state(page: Page) -> bool:
    """页面是否已经显示“已拉黑/解除拉黑”。"""
    for label in ("解除拉黑", "已拉黑"):
        if _visible_first(page.get_by_text(label, exact=True)) is not None:
            return True
    try:
        body = page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 4000)"
        )
    except Exception:
        body = ""
    text = str(body or "")
    return any(marker in text for marker in BLOCKED_BODY_MARKERS)


def _wait_block_success(page: Page, timeout_seconds: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            body = page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 4000)"
            )
        except Exception:
            body = ""
        text = str(body or "")
        if "拉黑成功" in text or "已拉黑" in text:
            return True
        if any(marker in text for marker in BLOCKED_BODY_MARKERS):
            return True
        if _has_blocked_state(page):
            return True
        page.wait_for_timeout(500)
    return False


def _capture_failure(page: Page, account: Account, settings: Settings) -> None:
    if not settings.screenshots:
        return
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", account.account_id)[:24]
        path = ARTIFACT_DIR / (
            f"fail-{time.strftime('%Y%m%d-%H%M%S')}-{safe_id or 'unknown'}.png"
        )
        page.screenshot(path=str(path), full_page=False)
        LOGGER.info("已保存失败截图（仅本地）：%s", path)
    except Exception:
        LOGGER.debug("保存失败截图失败", exc_info=True)


def block_account(page: Page, account: Account, settings: Settings) -> BlockResult:
    """打开账号主页，走官方“更多 -> 拉黑”UI；任一步失败则抛错。"""
    stage = "打开账号主页"
    try:
        if not is_douyin_url(account.url):
            raise BlockError("账号主页不是抖音域名，已拒绝操作。")

        try:
            response = page.goto(
                account.url, wait_until="domcontentloaded", timeout=settings.timeout_ms
            )
        except Exception as exc:
            raise BlockError(
                f"打开主页失败（{type(exc).__name__}: {exc}）"
            ) from exc
        if response is not None and response.status >= 400:
            raise BlockError(f"账号主页返回 HTTP {response.status}。")

        page.wait_for_timeout(2500)
        ensure_page_ready(page)

        stage = "检查当前拉黑状态"
        if _has_blocked_state(page):
            return BlockResult("skipped", "页面显示该账号已处于拉黑状态")

        stage = "打开“更多”菜单"
        more_button = _find_more_button(page)
        if more_button is None:
            raise BlockError(
                "未找到账号主页的“更多”入口。抖音页面结构可能已变化，"
                "请更新 browser.py 中的 MORE_BUTTON_SELECTORS 后重试。"
            )
        more_button.click(timeout=5000)
        page.wait_for_timeout(800)

        stage = "查找“拉黑”菜单项"
        block_item = _find_block_menu_item(page)
        if block_item is None:
            raise BlockError(
                "已打开“更多”菜单，但未找到“拉黑”选项。"
                "可能账号状态特殊或页面结构已变化，已停止，不会尝试其他按钮。"
            )
        block_item.click(timeout=5000)
        page.wait_for_timeout(800)

        stage = "确认拉黑"
        dialog = _find_confirm_dialog(page)
        if dialog is not None:
            dialog_text = _inner_text(dialog)
            if not any(word in dialog_text for word in BLOCK_WORDS):
                raise BlockError("确认弹窗内容与拉黑无关，已停止以避免误操作。")
            if not _click_confirm(dialog):
                raise BlockError("确认弹窗中未找到安全的确认按钮。")
            page.wait_for_timeout(1500)

        stage = "确认拉黑结果"
        if _wait_block_success(page):
            return BlockResult("blocked", "页面显示拉黑成功")

        raise BlockError(
            "点击后未出现成功提示，无法确认拉黑结果。"
            "为避免重复/误操作，已停止后续账号。"
        )

    except BlockError as exc:
        _capture_failure(page, account, settings)
        raise BlockError(f"{stage}：{exc}") from exc
    except (NotLoggedInError, PageChallengeError) as exc:
        _capture_failure(page, account, settings)
        raise BlockError(f"{stage}：{exc}") from exc
    except Exception as exc:
        _capture_failure(page, account, settings)
        raise BlockError(
            f"{stage}失败（{type(exc).__name__}: {exc}）"
        ) from exc


def _is_challenge_error(exc: BaseException) -> bool:
    text = str(exc)
    return any(
        marker in text
        for marker in ("验证码", "安全验证", "风控", "滑块", "访问过于频繁", "需要人工验证")
    )


def _sleep_with_stop(seconds: float, should_stop: Callable[[], bool] | None) -> bool:
    """可中断等待；返回 False 表示期间收到停止请求。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_stop is not None and should_stop():
            return False
        time.sleep(min(0.2, max(0.0, end - time.monotonic())))
    return True


def execute_blocks(
    page: Page,
    accounts: list[Account],
    settings: Settings,
    history: HistoryStore,
    confirm_one: Callable[[Account], bool] | None = None,
    continue_on_error: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> RunStats:
    """逐个执行拉黑；默认出现失败立即停止，可显式改为继续。"""
    stats = RunStats(matched=len(accounts))

    for index, account in enumerate(accounts, 1):
        if should_stop is not None and should_stop():
            LOGGER.info("收到停止请求，结束当前批次。")
            break
        if confirm_one is not None and not confirm_one(account):
            stats.skipped += 1
            history.append(account, STATUS_SKIPPED, "用户在逐条确认时选择跳过")
            LOGGER.info("[%d/%d] 已跳过：%s", index, len(accounts), account.username)
            continue

        LOGGER.info(
            "[%d/%d] 正在处理：%s（%s）",
            index,
            len(accounts),
            account.username,
            account.url,
        )
        try:
            result = block_account(page, account, settings)
        except BlockError as exc:
            stats.failed += 1
            history.append(account, STATUS_FAILED, str(exc))
            LOGGER.error("[%d/%d] 失败：%s", index, len(accounts), exc)
            if continue_on_error and not _is_challenge_error(exc):
                LOGGER.warning("按 --keep-going 继续处理下一个账号。")
                if index < len(accounts):
                    delay = random.uniform(*settings.action_delay)
                    LOGGER.debug("等待 %.1f 秒后继续。", delay)
                    if not _sleep_with_stop(delay, should_stop):
                        break
                continue
            LOGGER.error(
                "已按安全策略停止后续操作。已成功的账号记录在 history.csv，"
                "重跑时会自动跳过；失败账号可排查后重试。"
            )
            break

        if result.status == "blocked":
            stats.blocked += 1
            history.append(account, STATUS_BLOCKED, result.reason)
            LOGGER.info("[%d/%d] 成功：%s", index, len(accounts), account.username)
        else:
            stats.skipped += 1
            history.append(account, STATUS_SKIPPED, result.reason)
            LOGGER.info("[%d/%d] 跳过：%s", index, len(accounts), result.reason)

        if index < len(accounts):
            delay = random.uniform(*settings.action_delay)
            LOGGER.debug("等待 %.1f 秒后处理下一个账号。", delay)
            if not _sleep_with_stop(delay, should_stop):
                break

    return stats
