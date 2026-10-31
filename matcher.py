"""用户名清洗与关键词匹配。"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence
from urllib.parse import unquote

from config import DEFAULT_KEYWORDS

# 与需求文档保持一致：只匹配用户名，不匹配抖音号、简介或作品标题。
KEYWORDS: list[str] = list(DEFAULT_KEYWORDS)

_WHITESPACE_RE = re.compile(r"\s+")
_ACCOUNT_ID_RE = re.compile(r"/user/([^/?#]+)")

# 页面导航、按钮文字，避免被当成用户名。
NAVIGATION_NAMES = frozenset(
    {
        "首页",
        "推荐",
        "关注",
        "朋友",
        "我的",
        "消息",
        "搜索",
        "登录",
        "抖音",
        "创作者中心",
        "更多",
        "私信",
        "已关注",
        "拉黑",
        "解除拉黑",
        "分享",
        "举报",
    }
)


@dataclass(frozen=True, slots=True)
class Account:
    """页面上识别到的一个账号。account_id 来自主页 URL，不是登录凭证。"""

    account_id: str
    username: str
    url: str
    source_url: str = ""


@dataclass(slots=True)
class MatchResult:
    targets: list[Account]
    already_blocked: list[Account]
    duplicates: int
    invalid: int
    limit_reached: bool = False


def normalize_username(raw: str) -> str:
    """取第一行、压缩空白、去掉 @ 前缀和零宽字符。"""
    if not raw:
        return ""
    text = raw.replace("\u200b", "").replace("\xa0", " ")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    cleaned = _WHITESPACE_RE.sub(" ", first_line).strip()
    return cleaned.lstrip("@").strip()


def looks_like_username(name: str) -> bool:
    """过滤导航文字、抖音号行和明显不是用户名的内容。"""
    if not name or len(name) > 40:
        return False
    if name in NAVIGATION_NAMES:
        return False
    if name.startswith("抖音号") or name.startswith("粉丝") or name.startswith("获赞"):
        return False
    if name in {"...", "···", "…"}:
        return False
    return True


def should_block(username: str, keywords: Sequence[str] | None = None) -> bool:
    """用户名包含任意关键词即命中。"""
    words = KEYWORDS if keywords is None else keywords
    return any(keyword in username for keyword in words)


def extract_account_id(url: str) -> str:
    """从 https://www.douyin.com/user/<id> 提取账号 ID。"""
    match = _ACCOUNT_ID_RE.search(url or "")
    if not match:
        return ""
    return unquote(match.group(1)).strip()


def select_targets(
    accounts: Iterable[Account],
    keywords: Sequence[str],
    blocked_ids: frozenset[str] | set[str] = frozenset(),
    limit: int = 30,
) -> MatchResult:
    """清洗、去重、过滤已拉黑账号，并按关键词筛选。"""
    targets: list[Account] = []
    already_blocked: list[Account] = []
    seen: set[str] = set()
    duplicates = 0
    invalid = 0
    limit_reached = False

    for account in accounts:
        name = normalize_username(account.username)
        account_id = (account.account_id or extract_account_id(account.url)).strip()

        if not account_id or account_id == "self" or not looks_like_username(name):
            invalid += 1
            continue

        if account_id in seen:
            duplicates += 1
            continue
        seen.add(account_id)

        cleaned = replace(account, username=name, account_id=account_id)

        if account_id in blocked_ids:
            already_blocked.append(cleaned)
            continue

        if not should_block(name, keywords):
            continue

        if len(targets) >= limit:
            limit_reached = True
            continue

        targets.append(cleaned)

    return MatchResult(
        targets=targets,
        already_blocked=already_blocked,
        duplicates=duplicates,
        invalid=invalid,
        limit_reached=limit_reached,
    )
