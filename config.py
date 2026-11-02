"""配置加载与校验。

安全约定：
- 本模块不读取、不保存、不打印任何 Cookie / Session / Token。
- 只保存浏览器路径、CDP 地址、节奏参数等非敏感配置。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _app_dir() -> Path:
    """源码运行时用脚本目录；打包成 exe 后用 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_DIR = _app_dir()
KEYWORDS_FILE = PROJECT_DIR / "keywords.txt"
HISTORY_FILE = PROJECT_DIR / "history.csv"
LOG_FILE = PROJECT_DIR / "douyin_clean.log"
ARTIFACT_DIR = PROJECT_DIR / "artifacts"
TEMP_DIR = PROJECT_DIR / ".tmp"

DEFAULT_KEYWORDS = ("短剧", "剧场")
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_PROFILE_DIRECTORY = "Default"

APP_VERSION = "0.1.1"
APP_NAME = "DouyinClean"

DEFAULT_KEYWORDS_TEXT = """# 每行一个关键词，只匹配抖音“用户名”，不匹配抖音号。
# 以 # 开头的行为注释。
短剧
剧场
"""


class ConfigError(RuntimeError):
    """配置不合法时抛出。"""


def default_chrome_path() -> Path | None:
    """返回本机 Chrome 的常见安装路径；找不到返回 None。"""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def default_profile_dir() -> Path:
    """返回本机 Chrome 的用户数据目录（不是浏览器配置内容）。"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Google/Chrome/User Data"
    return Path.home() / "AppData/Local/Google/Chrome/User Data"


def load_keywords(path: Path = KEYWORDS_FILE) -> tuple[str, ...]:
    """从 keywords.txt 读取关键词，自动去重并忽略空行/注释。"""
    if not path.exists():
        path.write_text(DEFAULT_KEYWORDS_TEXT, encoding="utf-8")
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"无法读取关键词文件 {path}：{exc}") from exc

    keywords: list[str] = []
    for line in raw.splitlines():
        word = line.strip()
        if not word or word.startswith("#"):
            continue
        if word not in keywords:
            keywords.append(word)

    if not keywords:
        raise ConfigError(f"关键词文件 {path} 为空，请至少写入一个关键词。")
    return tuple(keywords)


def validate_cdp_url(cdp_url: str) -> str:
    """只允许连接本机回环地址，避免把浏览器控制端口暴露到局域网。"""
    parsed = urlparse(cdp_url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError("CDP 地址必须使用 http:// 或 https://。")
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("出于安全考虑，CDP 地址只允许本机回环地址（127.0.0.1/localhost）。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"CDP 端口不合法：{cdp_url}") from exc
    if port is None:
        raise ConfigError("CDP 地址必须包含端口，例如 http://127.0.0.1:9222。")
    return cdp_url.rstrip("/")


@dataclass(slots=True)
class Settings:
    """一次运行的全部非敏感设置。"""

    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    cdp_url: str = DEFAULT_CDP_URL
    chrome_path: Path | None = None
    profile_dir: Path | None = None
    profile_directory: str = DEFAULT_PROFILE_DIRECTORY
    dry_run: bool = True
    urls: list[str] = field(default_factory=list)
    following: bool = False
    search: bool = False
    user_tab: bool = False
    silent: bool = False
    allow_window: bool = False
    new_tab: bool = False
    max_scrolls: int = 8
    limit: int = 30
    scroll_pause: tuple[float, float] = (1.2, 2.4)
    action_delay: tuple[float, float] = (2.5, 5.0)
    confirm_each: bool = False
    continue_on_error: bool = False
    resume: bool = True
    keep_tab: bool = False
    screenshots: bool = False
    timeout_ms: int = 60_000
    log_file: Path = LOG_FILE
    history_file: Path = HISTORY_FILE

    def validate(self) -> None:
        """在执行任何浏览器操作前校验参数。"""
        if not self.keywords:
            raise ConfigError("关键词列表不能为空。")
        if not 1 <= self.limit <= 100:
            raise ConfigError("单次处理上限必须在 1 到 100 之间。")
        if not 0 <= self.max_scrolls <= 50:
            raise ConfigError("滚动次数必须在 0 到 50 之间。")
        if self.scroll_pause[0] < 1.0 or self.scroll_pause[0] > self.scroll_pause[1]:
            raise ConfigError("滚动间隔必须 >= 1 秒，且最小值不能大于最大值。")
        if self.action_delay[0] < 1.5 or self.action_delay[0] > self.action_delay[1]:
            raise ConfigError("拉黑间隔必须 >= 1.5 秒，且最小值不能大于最大值。")
        if self.timeout_ms < 10_000:
            raise ConfigError("页面超时时间不能小于 10 秒。")
        validate_cdp_url(self.cdp_url)

        for url in self.urls:
            host = (urlparse(url).hostname or "").lower()
            if host != "douyin.com" and not host.endswith(".douyin.com"):
                raise ConfigError(f"只允许访问抖音域名，拒绝：{url}")
