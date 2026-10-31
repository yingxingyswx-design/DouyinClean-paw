"""抖音批量拉黑工具入口。

默认 DRY RUN：只采集、匹配并展示账号，不执行任何拉黑。
只有显式传入 --execute 并在终端确认后，才会走网页 UI 的“更多 -> 拉黑”。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

from config import (
    DEFAULT_CDP_URL,
    DEFAULT_PROFILE_DIRECTORY,
    HISTORY_FILE,
    KEYWORDS_FILE,
    LOG_FILE,
    PROJECT_DIR,
    ConfigError,
    Settings,
    default_chrome_path,
    default_profile_dir,
    load_keywords,
)
from logger import (
    STATUS_BLOCKED,
    HistoryError,
    HistoryStore,
    redact,
    setup_logger,
)
from matcher import (
    Account,
    normalize_username,
    select_targets,
    should_block,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="DouyinClean.py",
        description=(
            "抖音批量拉黑工具：复用本机已登录 Chrome，按用户名关键词筛选账号。"
            "默认 DRY RUN，不执行拉黑。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py --check\n"
            "  python main.py                      # 预览当前抖音页面上的匹配账号\n"
            "  python main.py --execute            # 确认后执行拉黑\n"
            "  python main.py --execute --limit 5 --confirm-each\n"
            "  python main.py --url \"https://www.douyin.com/search/短剧?type=user\"\n"
        ),
    )
    parser.add_argument("--check", action="store_true", help="检查 Chrome/CDP/Playwright 环境")
    parser.add_argument("--self-test", action="store_true", help="运行不依赖浏览器的自检")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正执行拉黑（默认只预览，不执行）",
    )
    parser.add_argument(
        "--launch-chrome",
        action="store_true",
        help="Chrome 完全退出后，用现有配置目录启动并开启本机调试端口",
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        metavar="URL",
        help="要采集的抖音页面，可重复；默认在当前抖音标签页中打开",
    )
    parser.add_argument(
        "--new-tab",
        action="store_true",
        help="允许新建标签页（默认复用当前抖音标签页，避免弹窗干扰）",
    )
    parser.add_argument(
        "--search",
        action="store_true",
        help="按 keywords.txt 中的关键词搜索抖音，并切换到“用户”结果页采集",
    )
    parser.add_argument(
        "--following",
        action="store_true",
        help="打开自己的“关注”列表并从中采集账号",
    )
    parser.add_argument(
        "--user-tab",
        action="store_true",
        help="在当前搜索页点击“用户”页签并采集账号",
    )
    parser.add_argument(
        "--silent",
        "--background",
        dest="silent",
        action="store_true",
        help=(
            "后台静默：不新建窗口/标签页、不弹确认框、不输出控制台；"
            "执行拉黑必须同时使用 --execute --yes"
        ),
    )
    parser.add_argument(
        "--allow-window",
        action="store_true",
        help="静默模式下仍允许 --launch-chrome 产生可见窗口（需明确确认）",
    )
    parser.add_argument(
        "--cdp-url",
        default=None,
        metavar="URL",
        help=f"CDP 地址，默认 {DEFAULT_CDP_URL}",
    )
    parser.add_argument("--chrome-path", default=None, help="chrome.exe 完整路径")
    parser.add_argument("--user-data-dir", default=None, help="Chrome 用户数据目录")
    parser.add_argument(
        "--profile-directory",
        default=None,
        help=f"Chrome 配置目录名，默认 {DEFAULT_PROFILE_DIRECTORY}",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=8,
        help="最多向下滚动次数，默认 8（0 表示不滚动）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="单次最多处理账号数，默认 30，最大 100",
    )
    parser.add_argument(
        "--confirm-each",
        action="store_true",
        help="每个账号执行前单独确认",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过批次确认（仍受 --confirm-each 约束；请确认列表无误后使用）",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="单个账号失败时继续处理下一个；验证码/风控仍会停止",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=2.5,
        help="账号间最小间隔秒数，默认 2.5",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=5.0,
        help="账号间最大间隔秒数，默认 5.0",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="不跳过 history.csv 中已成功的账号",
    )
    parser.add_argument(
        "--keep-tab",
        action="store_true",
        help="结束后保留自动化标签页，便于人工检查",
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="失败时保存本地截图到 artifacts/（默认关闭）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def build_settings(args: argparse.Namespace) -> Settings:
    keywords = load_keywords(KEYWORDS_FILE)
    chrome_path = (
        Path(args.chrome_path).expanduser()
        if args.chrome_path
        else default_chrome_path()
    )
    profile_dir = (
        Path(args.user_data_dir).expanduser()
        if args.user_data_dir
        else default_profile_dir()
    )
    settings = Settings(
        keywords=keywords,
        cdp_url=args.cdp_url
        or os.environ.get("DOUYIN_CDP_URL")
        or DEFAULT_CDP_URL,
        chrome_path=chrome_path,
        profile_dir=profile_dir,
        profile_directory=args.profile_directory
        or os.environ.get("DOUYIN_PROFILE_DIRECTORY")
        or DEFAULT_PROFILE_DIRECTORY,
        dry_run=not args.execute,
        urls=list(args.url or []),
        following=args.following,
        search=args.search,
        user_tab=args.user_tab,
        silent=args.silent,
        allow_window=args.allow_window,
        new_tab=args.new_tab,
        max_scrolls=args.max_scrolls,
        limit=args.limit,
        confirm_each=args.confirm_each,
        continue_on_error=args.keep_going,
        resume=not args.no_resume,
        keep_tab=args.keep_tab,
        screenshots=args.screenshots,
        action_delay=(args.min_delay, args.max_delay),
        log_file=LOG_FILE,
        history_file=HISTORY_FILE,
    )
    settings.validate()
    return settings


def run_check(settings: Settings) -> int:
    from browser import chrome_process_count, chrome_version, probe_cdp

    print("=" * 64)
    print("抖音批量拉黑工具 · 环境检查")
    print("=" * 64)
    print(f"Python          : {sys.version.split()[0]}")

    try:
        import playwright as _playwright  # noqa: F401
        from importlib.metadata import version

        playwright_version = version("playwright") or "已安装"
    except Exception:
        playwright_version = "未安装"
    print(f"Playwright      : {playwright_version}")

    chrome_path = settings.chrome_path
    chrome_exists = chrome_path is not None and chrome_path.is_file()
    print(f"Chrome          : {chrome_path if chrome_exists else '未找到'}")
    if chrome_exists:
        print(f"Chrome 版本     : {chrome_version(chrome_path)}")

    profile_dir = settings.profile_dir
    profile_exists = profile_dir is not None and profile_dir.is_dir()
    print(f"用户数据目录    : {profile_dir} [{('存在' if profile_exists else '不存在')}]")
    print(f"配置目录名      : {settings.profile_directory}")

    count = chrome_process_count()
    print(f"Chrome 进程数   : {count if count >= 0 else '无法查询'}")

    info = probe_cdp(settings.cdp_url)
    if info.ok:
        print(f"CDP 调试端口    : 已就绪（{info.browser}）")
    else:
        print(f"CDP 调试端口    : 未就绪（{info.detail}）")

    print(f"关键词          : {'、'.join(settings.keywords)}")
    print(f"关键词文件      : {KEYWORDS_FILE}")

    try:
        history = HistoryStore(settings.history_file)
        counts = history.counts()
        print(
            "历史记录        : "
            f"成功 {counts.get(STATUS_BLOCKED, 0)} / "
            f"跳过 {counts.get('skipped', 0)} / 失败 {counts.get('failed', 0)}"
        )
    except HistoryError as exc:
        print(f"历史记录        : 异常（{exc}）")

    print("-" * 64)
    if not playwright_version or playwright_version == "未安装":
        print("结论：缺少 Playwright。请先安装依赖（pip install -r requirements.txt）。")
        return 3
    if not chrome_exists:
        print("结论：未找到 Chrome，请用 --chrome-path 指定 chrome.exe。")
        return 3
    if not info.ok:
        if count > 0:
            print("结论：Chrome 正在运行，但没有开启调试端口。")
            print("      请完全退出 Chrome（含托盘/后台进程），然后运行：")
            print("      python main.py --launch-chrome --url <抖音页面>")
        else:
            print("结论：Chrome 未运行，调试端口不可用。可运行：")
            print("      python main.py --launch-chrome --url <抖音页面>")
        print("      说明：--launch-chrome 只复用现有配置目录并临时开启本机端口，")
        print("            不会修改浏览器设置；工具不会主动结束 Chrome 进程。")
        return 3

    print("结论：环境就绪。建议先运行 `python main.py` 做 DRY RUN 预览。")
    return 0


def run_self_test() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    check(should_block("短剧研究所", ("短剧", "剧场")), "应命中“短剧”")
    check(should_block("某某剧场", ("短剧", "剧场")), "应命中“剧场”")
    check(not should_block("日常生活记录", ("短剧", "剧场")), "不应误命中")
    check(not should_block("", ("短剧", "剧场")), "空用户名不应命中")
    check(
        normalize_username("  短剧\u200b社  \n第二行") == "短剧社",
        "用户名清洗不正确",
    )

    accounts = [
        Account("1", "短剧A", "https://www.douyin.com/user/1"),
        Account("1", "短剧A", "https://www.douyin.com/user/1?from=search"),
        Account("2", "普通用户", "https://www.douyin.com/user/2"),
        Account("3", "剧场B", "https://www.douyin.com/user/3"),
        Account("self", "我自己", "https://www.douyin.com/user/self"),
    ]
    result = select_targets(accounts, ("短剧", "剧场"), limit=10)
    check(len(result.targets) == 2, "关键词筛选数量不正确")
    check(result.duplicates == 1, "去重数量不正确")
    check(len(result.already_blocked) == 0, "已拉黑过滤不正确")

    limited = select_targets(accounts, ("短剧", "剧场"), limit=1)
    check(len(limited.targets) == 1, "limit 未生效")

    redacted = redact("cookie=secret-token; token=abc123")
    check("secret-token" not in redacted and "abc123" not in redacted, "日志脱敏失败")

    try:
        test_path = PROJECT_DIR / f".selftest-history-{os.getpid()}.csv"
        try:
            store = HistoryStore(test_path)
            store.append(accounts[0], STATUS_BLOCKED, "自检")
            check("1" in store.blocked_ids(), "历史记录断点续跑读取失败")
        finally:
            try:
                test_path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception as exc:  # 自检不应因临时文件问题崩溃
        failures.append(f"历史记录自检异常：{type(exc).__name__}: {exc}")

    if failures:
        print("自检失败：")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("自检通过：匹配、清洗、去重、限流、脱敏、历史记录均正常。")
    return 0


def _print_accounts(accounts: Iterable[Account], silent: bool = False) -> None:
    for index, account in enumerate(accounts, 1):
        short_id = account.account_id
        if len(short_id) > 18:
            short_id = short_id[:18] + "..."
        if silent:
            logging.getLogger("douyin_clean").info(
                "[%d] %s | ID: %s | %s",
                index,
                account.username,
                short_id,
                account.url,
            )
            continue
        print(f"[{index:>2}] {account.username}")
        print(f"     ID: {short_id}")
        print(f"     主页: {account.url}")


def _print_summary(stats: RunStats, silent: bool = False) -> None:
    line = (
        f"本次匹配 {stats.matched} 个 | "
        f"成功 {stats.blocked} | 跳过 {stats.skipped} | 失败 {stats.failed}"
    )
    if silent:
        logging.getLogger("douyin_clean").info(line)
    else:
        print("-" * 64)
        print(line)


def _confirm_batch(count: int) -> bool:
    try:
        answer = input(
            f"即将对 {count} 个账号执行拉黑。确认继续？输入 y 继续，其他取消："
        )
    except (EOFError, OSError):
        return False
    return answer.strip().lower() in {"y", "yes", "是"}


def _make_each_confirmer():
    def confirm(account: Account) -> bool:
        try:
            answer = input(f"拉黑“{account.username}”？输入 y 确认，其他跳过：")
        except (EOFError, OSError):
            return False
        return answer.strip().lower() in {"y", "yes", "是"}

    return confirm


def _configure_stdio() -> None:
    """重定向到管道/文件时统一用 UTF-8，避免中文日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # pythonw.exe 没有控制台
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _record_startup_error(message: str) -> None:
    """静默启动时控制台可能不可见，把配置错误也写进日志文件。"""
    if sys.stderr is not None:
        print(f"配置错误：{message}", file=sys.stderr)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} | ERROR   | 配置错误：{message}\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        settings = build_settings(args)
    except ConfigError as exc:
        _record_startup_error(str(exc))
        return 1

    logger = setup_logger(
        settings.log_file, verbose=args.verbose, console=not settings.silent
    )

    if args.self_test:
        return run_self_test()
    if args.check:
        return run_check(settings)

    if settings.silent and settings.confirm_each:
        logger.error("静默模式不能使用 --confirm-each；请改用 --yes 做一次性确认。")
        return 1
    if settings.silent and args.launch_chrome and not settings.allow_window:
        logger.error(
            "静默模式拒绝启动 Chrome（会产生可见窗口）。"
            "如确认允许弹窗，请显式加上 --allow-window；否则请先手动启动 Chrome。"
        )
        return 1
    if settings.silent and not settings.dry_run and not args.yes:
        logger.error("静默模式执行拉黑必须显式加 --yes 作为确认；本次未执行。")
        return 1

    # 只有真正需要操作浏览器时才导入 Playwright 相关模块，
    # 这样 --help/--self-test 不依赖浏览器环境。
    from blocker import execute_blocks
    from browser import (
        BrowserError,
        NotLoggedInError,
        PageChallengeError,
        collect_accounts,
        collect_following_accounts,
        collect_user_tab_accounts,
        ensure_page_ready,
        goto_douyin,
        launch_chrome,
        open_session,
        search_and_collect,
    )

    logger.info("=" * 64)
    logger.info("抖音批量拉黑工具")
    logger.info(
        "模式：%s / %s",
        "静默后台" if settings.silent else "交互",
        "DRY RUN（只预览）" if settings.dry_run else "EXECUTE（执行拉黑）",
    )
    logger.info("关键词：%s", "、".join(settings.keywords))
    logger.info("单次上限：%d 个；操作间隔：%.1f-%.1f 秒", settings.limit, *settings.action_delay)

    session = None
    stats: RunStats | None = None
    interrupted = False
    try:
        if args.launch_chrome:
            launch_chrome(settings)

        session = open_session(settings, create_tab=settings.new_tab)
        page = session.page

        collected: list[Account] = []
        if settings.search:
            logger.info("搜索模式：关键词 %s", "、".join(settings.keywords))
            collected = search_and_collect(page, settings)
        elif settings.following:
            logger.info("关注列表模式：打开自己的关注列表。")
            collected = collect_following_accounts(page, settings)
        elif settings.user_tab:
            logger.info("用户页签模式：采集当前搜索页的“用户”结果。")
            collected = collect_user_tab_accounts(page, settings)
        elif settings.urls:
            for url in settings.urls:
                logger.info("打开目标页面：%s", url)
                goto_douyin(page, url, settings)
                collected.extend(collect_accounts(page, settings, url))
        else:
            logger.info("使用当前抖音标签页：%s", page.url)
            ensure_page_ready(page)
            collected = collect_accounts(page, settings, page.url)

        if not collected:
            logger.warning(
                "没有从页面上识别到账号。请确认页面已加载出用户列表，"
                "并适当增大 --max-scrolls。"
            )
            return 0

        history = HistoryStore(settings.history_file)
        blocked_ids = history.blocked_ids() if settings.resume else set()
        match = select_targets(
            collected,
            settings.keywords,
            blocked_ids=blocked_ids,
            limit=settings.limit,
        )

        logger.info(
            "采集 %d 个 | 匹配 %d 个 | 历史已拉黑跳过 %d 个 | 重复 %d 个 | 无效 %d 个",
            len(collected),
            len(match.targets),
            len(match.already_blocked),
            match.duplicates,
            match.invalid,
        )

        if not match.targets:
            logger.info("没有需要处理的新账号。")
            return 0

        if settings.silent:
            logger.info("待处理账号（%d 个）：", len(match.targets))
            _print_accounts(match.targets, silent=True)
        else:
            print()
            print(f"待处理账号（{len(match.targets)} 个）：")
            _print_accounts(match.targets)
            print()

        if match.limit_reached:
            logger.warning(
                "已达到单次上限 %d 个；如确需处理更多，请分多次运行。",
                settings.limit,
            )

        if settings.dry_run:
            if settings.silent:
                logger.info(
                    "DRY RUN 完成：未执行任何拉黑。确认日志中的列表后，"
                    "可使用 --silent --execute --yes 执行。"
                )
            else:
                print("DRY RUN：以上仅为预览，未执行任何拉黑操作。")
                print("确认列表无误后，使用 --execute 执行（默认仍会再次确认）。")
            return 0

        if not settings.silent and not args.yes:
            if not _confirm_batch(len(match.targets)):
                logger.info("用户取消，未执行任何拉黑操作。")
                return 0

        stats = execute_blocks(
            page,
            match.targets,
            settings,
            history,
            confirm_one=(
                _make_each_confirmer()
                if settings.confirm_each and not settings.silent
                else None
            ),
            continue_on_error=settings.continue_on_error,
        )
        _print_summary(stats, silent=settings.silent)
        logger.info("历史记录已写入：%s", settings.history_file)
        return 0 if stats.failed == 0 else 2

    except KeyboardInterrupt:
        interrupted = True
        logger.warning(
            "收到中断。已成功的账号已写入 history.csv，重新运行会自动跳过；"
            "未完成的账号会重新检查状态。"
        )
        return 130
    except (BrowserError, NotLoggedInError, PageChallengeError) as exc:
        logger.error("浏览器操作失败：%s", exc)
        return 1
    except HistoryError as exc:
        logger.error("历史记录错误：%s", exc)
        return 1
    except Exception as exc:
        logger.exception("未预期错误：%s", exc)
        return 1
    finally:
        if session is not None:
            keep_page = settings.keep_tab or interrupted or bool(stats and stats.failed)
            session.close(keep_page=keep_page)
            if keep_page and session.created_page:
                logger.info("已保留自动化标签页，便于人工检查。")


if __name__ == "__main__":
    raise SystemExit(main())
