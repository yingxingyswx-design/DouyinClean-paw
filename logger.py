"""中文日志与本地历史记录。

历史记录只保存公开的账号 ID、用户名、操作状态和原因，不保存任何认证信息。
"""

from __future__ import annotations

import csv
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from matcher import Account

STATUS_BLOCKED = "blocked"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_DRY_RUN = "dry_run"

STATUS_LABELS = {
    STATUS_BLOCKED: "成功",
    STATUS_SKIPPED: "跳过",
    STATUS_FAILED: "失败",
    STATUS_DRY_RUN: "预览",
}

HISTORY_FIELDS = ("timestamp", "account_id", "username", "status", "reason", "url")

# 即使调用方误把敏感字符串传给日志，也做一层兜底脱敏。
_SENSITIVE_RE = re.compile(
    r"(?i)\b(cookie|set-cookie|sessionid|session_id|token|authorization|"
    r"passport_csrf_token|csrf_token|s_v_web_id)\b(\s*[:=]\s*)([^\s,;]+)"
)


class HistoryError(RuntimeError):
    """历史记录读写失败。"""


def redact(text: str) -> str:
    """把常见的 Cookie/Token 键值替换为 ***。"""
    return _SENSITIVE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class RedactFilter(logging.Filter):
    """在日志落盘前统一脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 格式化异常不应阻塞日志
            return True
        record.msg = redact(message)
        record.args = ()
        return True


def setup_logger(
    log_file: Path, verbose: bool = False, console: bool = True
) -> logging.Logger:
    """控制台 + 文件双输出；静默模式只写文件，不弹任何控制台内容。"""
    logger = logging.getLogger("douyin_clean")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    redactor = RedactFilter()

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(logging.DEBUG if verbose else logging.INFO)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        logger.addHandler(stream)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as exc:
        if console:
            logger.warning("无法写入日志文件 %s：%s（仅输出到控制台）", log_file, exc)
        else:
            logger.addHandler(logging.NullHandler())
    else:
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)

    # 降低第三方库噪音。
    logging.getLogger("playwright").setLevel(logging.WARNING)
    return logger


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    timestamp: str
    account_id: str
    username: str
    status: str
    reason: str
    url: str


class HistoryStore:
    """追加式 CSV 历史记录；成功记录用于断点续跑。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._write_header()
        else:
            self._check_header()

    def _write_header(self) -> None:
        with self.path.open("w", newline="", encoding="utf-8-sig") as handle:
            csv.DictWriter(handle, fieldnames=HISTORY_FIELDS).writeheader()

    def _check_header(self) -> None:
        try:
            with self.path.open("r", newline="", encoding="utf-8-sig") as handle:
                header = next(csv.reader(handle), [])
        except OSError as exc:
            raise HistoryError(f"无法读取历史记录 {self.path}：{exc}") from exc
        if tuple(header) != HISTORY_FIELDS:
            raise HistoryError(
                f"历史记录表头异常：{self.path}。"
                f"期望 {','.join(HISTORY_FIELDS)}，实际 {','.join(header)}。"
                "请备份后删除该文件，程序会重新生成。"
            )

    def append(self, account: Account, status: str, reason: str) -> None:
        record = HistoryRecord(
            timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
            account_id=account.account_id,
            username=account.username,
            status=status,
            reason=redact(reason)[:300],
            url=account.url,
        )
        try:
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
                writer.writerow(
                    {
                        "timestamp": record.timestamp,
                        "account_id": record.account_id,
                        "username": record.username,
                        "status": record.status,
                        "reason": record.reason,
                        "url": record.url,
                    }
                )
        except OSError as exc:
            raise HistoryError(f"写入历史记录失败 {self.path}：{exc}") from exc

    def blocked_ids(self) -> set[str]:
        """返回历史上已确认拉黑成功的账号 ID。"""
        if not self.path.exists():
            return set()
        result: set[str] = set()
        try:
            with self.path.open("r", newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    if row.get("status") == STATUS_BLOCKED and row.get("account_id"):
                        result.add(row["account_id"].strip())
        except OSError as exc:
            raise HistoryError(f"读取历史记录失败 {self.path}：{exc}") from exc
        except csv.Error as exc:
            raise HistoryError(f"历史记录格式错误 {self.path}：{exc}") from exc
        return result

    def counts(self) -> dict[str, int]:
        """统计各状态数量，供环境检查展示。"""
        counts = {status: 0 for status in STATUS_LABELS}
        if not self.path.exists():
            return counts
        try:
            with self.path.open("r", newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    status = (row.get("status") or "").strip()
                    if status in counts:
                        counts[status] += 1
        except (OSError, csv.Error):
            return counts
        return counts
