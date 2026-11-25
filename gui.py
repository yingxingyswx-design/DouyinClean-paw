"""DouyinClean GUI：独立运行、自动循环、失败退避，不依赖外部操控。"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import quote

from blocker import execute_blocks
from browser import (
    BrowserError,
    NotLoggedInError,
    PageChallengeError,
    collect_accounts,
    goto_douyin,
    kill_debug_chrome,
    open_login_chrome,
    open_tool_browser,
)
from config import (
    APP_VERSION,
    HISTORY_FILE,
    KEYWORDS_FILE,
    LOG_FILE,
    PROJECT_DIR,
    Settings,
    default_chrome_path,
    load_keywords,
)
from logger import HistoryStore, setup_logger
from matcher import select_targets

DEFAULT_QUERIES = [
    "短剧",
    "剧场",
    "短剧推荐",
    "短剧全集",
    "微短剧",
    "热门短剧",
    "短剧解说",
    "剧场推荐",
    "短剧分享",
    "短剧追剧",
    "短剧控",
    "剧场君",
    "短剧迷",
    "剧场号",
    "短剧社",
    "短剧部落",
    "短剧世界",
    "剧场迷",
    "短剧推荐官",
    "短剧汇",
    "短剧君",
    "短剧工厂",
    "短剧大本营",
    "短剧精选",
    "剧场社",
    "短剧圈",
]

SEARCH_URL = "https://www.douyin.com/jingxuan/search/{term}?type=user"


def _find_profile_dir() -> Path | None:
    pointer = PROJECT_DIR / "profile_path.txt"
    if pointer.is_file():
        try:
            pointed = Path(
                pointer.read_text(encoding="utf-8-sig").strip().strip('"')
            ).expanduser()
            if pointed.is_dir():
                return pointed
        except OSError:
            pass

    candidates: list[Path] = []
    for base in [PROJECT_DIR, *PROJECT_DIR.parents[:3]]:
        candidate = base / ".chrome-profile"
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


class GuiLogHandler(logging.Handler):
    """把日志送进 Tk 主线程队列，避免跨线程操作控件。"""

    def __init__(self, events: queue.Queue) -> None:
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(("log", self.format(record)))
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.login_worker: threading.Thread | None = None
        self.profile_dir = _find_profile_dir()
        self.cdp_var = tk.StringVar(value="http://127.0.0.1:9229")

        self.logger = setup_logger(LOG_FILE, console=False)
        handler = GuiLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
        self.logger.addHandler(handler)

        self._build_ui()
        self._load_initial_stats()
        self.root.after(150, self._poll_events)
        self.root.after(1200, self._maybe_autostart)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        self.root.title(f"DouyinClean v{APP_VERSION} · 抖音批量拉黑")
        self.root.geometry("1000x760")
        self.root.minsize(860, 640)

        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        self.login_btn = ttk.Button(
            top, text="打开工具Chrome窗口完成登入", command=self._open_login_chrome
        )
        self.login_btn.grid(row=0, column=0, padx=6)
        self.kill_chrome_btn = ttk.Button(
            top, text="关闭工具 Chrome", command=self._kill_chrome
        )
        self.kill_chrome_btn.grid(row=0, column=1, padx=6)
        ttk.Button(top, text="打开日志", command=self._open_log).grid(
            row=0, column=2, padx=6
        )

        profile_text = str(self.profile_dir) if self.profile_dir else "未找到 .chrome-profile"
        self.profile_label = ttk.Label(top, text=f"配置：{profile_text}", foreground="#666")
        self.profile_label.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        mid = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        mid.pack(fill="both", expand=False)

        query_box = ttk.LabelFrame(mid, text="搜索词（每行一个，自动循环）", padding=6)
        query_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.query_text = tk.Text(query_box, height=10, width=34, wrap="none")
        self.query_text.pack(fill="both", expand=True)
        self.query_text.insert("1.0", "\n".join(DEFAULT_QUERIES))

        param_box = ttk.LabelFrame(mid, text="参数", padding=6)
        param_box.grid(row=0, column=1, sticky="nsew")

        ttk.Label(param_box, text="最小间隔(秒)").grid(row=0, column=0, sticky="w")
        self.min_delay = tk.StringVar(value="3.0")
        ttk.Entry(param_box, textvariable=self.min_delay, width=8).grid(row=0, column=1, padx=4)

        ttk.Label(param_box, text="最大间隔(秒)").grid(row=1, column=0, sticky="w")
        self.max_delay = tk.StringVar(value="4.5")
        ttk.Entry(param_box, textvariable=self.max_delay, width=8).grid(row=1, column=1, padx=4)

        ttk.Label(param_box, text="单轮上限").grid(row=2, column=0, sticky="w")
        self.limit_var = tk.StringVar(value="30")
        ttk.Entry(param_box, textvariable=self.limit_var, width=8).grid(row=2, column=1, padx=4)

        ttk.Label(param_box, text="最多滚动").grid(row=3, column=0, sticky="w")
        self.scroll_var = tk.StringVar(value="5")
        ttk.Entry(param_box, textvariable=self.scroll_var, width=8).grid(row=3, column=1, padx=4)

        self.loop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(param_box, text="跑完一轮后自动循环", variable=self.loop_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        self.autostart_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            param_box, text="启动后自动开始", variable=self.autostart_var
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.minimize_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            param_box, text="开始后最小化窗口", variable=self.minimize_var
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.keyword_label = ttk.Label(
            param_box, text=f"匹配关键词：{'、'.join(load_keywords(KEYWORDS_FILE))}"
        )
        self.keyword_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

        actions = ttk.Frame(self.root, padding=10)
        actions.pack(fill="x")
        self.start_btn = ttk.Button(actions, text="开始自动拉黑", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(
            actions, text="停止", command=self._stop, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="最小化", command=self.root.iconify).pack(
            side="left", padx=4
        )

        self.status_var = tk.StringVar(value="空闲")
        ttk.Label(actions, textvariable=self.status_var, foreground="#0a7").pack(
            side="left", padx=16
        )
        self.stats_var = tk.StringVar(value="成功 0 | 跳过 0 | 失败 0")
        ttk.Label(actions, textvariable=self.stats_var).pack(side="right")

        log_box = ttk.LabelFrame(self.root, text="运行日志", padding=6)
        log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_widget = scrolledtext.ScrolledText(
            log_box, height=18, state="disabled", wrap="word"
        )
        self.log_widget.pack(fill="both", expand=True)

        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

    def _load_initial_stats(self) -> None:
        try:
            counts = HistoryStore(HISTORY_FILE).counts()
            self._set_stats(counts)
        except Exception:
            pass

    # ---------- 事件 ----------
    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(str(payload))
            elif kind == "stats":
                self._set_stats(payload)
            elif kind == "status":
                self.status_var.set(str(payload))
            elif kind == "done":
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
            elif kind == "chrome_done":
                self.kill_chrome_btn.configure(state="normal")
            elif kind == "login_done":
                self.login_btn.configure(state="normal")
        self.root.after(150, self._poll_events)

    def _append_log(self, text: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", text + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _set_stats(self, counts: dict[str, int]) -> None:
        self.stats_var.set(
            f"成功 {counts.get('blocked', 0)} | "
            f"跳过 {counts.get('skipped', 0)} | "
            f"失败 {counts.get('failed', 0)}"
        )

    # ---------- 配置 ----------
    def _build_settings(self) -> Settings:
        if self.profile_dir is None:
            raise ValueError("未找到 .chrome-profile，请把它放在 exe 同目录。")

        min_delay = float(self.min_delay.get())
        max_delay = float(self.max_delay.get())
        settings = Settings(
            keywords=load_keywords(KEYWORDS_FILE),
            cdp_url=self.cdp_var.get().strip(),
            chrome_path=default_chrome_path(),
            profile_dir=self.profile_dir,
            profile_directory="Default",
            dry_run=False,
            max_scrolls=int(self.scroll_var.get()),
            limit=int(self.limit_var.get()),
            action_delay=(min_delay, max_delay),
            continue_on_error=True,
            resume=True,
        )
        settings.validate()
        return settings

    def _queries(self) -> list[str]:
        terms: list[str] = []
        for line in self.query_text.get("1.0", "end").splitlines():
            term = line.strip()
            if not term or term.startswith("#"):
                continue
            if term not in terms:
                terms.append(term)
        if not terms:
            raise ValueError("搜索词不能为空。")
        return terms

    # ---------- 动作 ----------
    def _open_login_chrome(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning(
                "任务运行中", "任务已经打开了工具 Chrome，请直接在窗口里登录。"
            )
            return
        if self.login_worker is not None and self.login_worker.is_alive():
            return
        self.login_btn.configure(state="disabled")
        self.status_var.set("正在打开登录 Chrome…")
        self.login_worker = threading.Thread(
            target=self._open_login_worker, daemon=True
        )
        self.login_worker.start()

    def _open_login_worker(self) -> None:
        try:
            settings = self._build_settings()
            open_login_chrome(settings)
            self.logger.info("登录 Chrome 已打开，请完成抖音登录后点“开始自动拉黑”。")
            self.events.put(("status", "请登录后点开始"))
        except Exception as exc:
            self.logger.error("打开登录 Chrome 失败：%s", exc)
            self.events.put(("status", "打开登录 Chrome 失败"))
        finally:
            self.events.put(("login_done", None))

    def _kill_chrome(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning("任务运行中", "请先停止拉黑任务，再关闭 Chrome。")
            return
        if not messagebox.askyesno(
            "关闭工具 Chrome",
            "将强制结束本工具使用的 Chrome（带 9229 调试端口/chrome-profile）。\n"
            "该窗口中未保存的页面操作会丢失，确定继续？",
        ):
            return

        self.kill_chrome_btn.configure(state="disabled")
        self.status_var.set("正在关闭 Chrome…")
        threading.Thread(target=self._kill_chrome_worker, daemon=True).start()

    def _kill_chrome_worker(self) -> None:
        try:
            count = kill_debug_chrome(9229)
            if count:
                self.logger.info("已结束 %d 个工具 Chrome 进程。", count)
                self.events.put(("status", "Chrome 已关闭"))
            else:
                self.logger.info("没有找到工具使用的 Chrome 进程。")
                self.events.put(("status", "未找到工具 Chrome"))
        except Exception as exc:
            self.logger.error("关闭 Chrome 失败：%s", exc)
            self.events.put(("status", "关闭 Chrome 失败"))
        finally:
            self.events.put(("chrome_done", None))

    def _open_log(self) -> None:
        try:
            import os

            os.startfile(LOG_FILE)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("打开日志失败", str(exc))

    def _maybe_autostart(self) -> None:
        if self.autostart_var.get() and self.profile_dir is not None:
            self._start(silent=True)
            if self.minimize_var.get():
                self.root.iconify()

    def _start(self, silent: bool = False) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            settings = self._build_settings()
            queries = self._queries()
        except Exception as exc:
            if silent:
                self._append_log(f"自动启动失败：{exc}")
            else:
                messagebox.showerror("参数错误", str(exc))
            return

        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("运行中")
        self.worker = threading.Thread(
            target=self._run, args=(settings, queries), daemon=True
        )
        self.worker.start()
        if self.minimize_var.get():
            self.root.iconify()

    def _stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("正在停止…")

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("退出", "任务仍在运行，确定停止并退出？"):
                return
            self.stop_event.set()
        self.root.destroy()

    # ---------- 工作线程 ----------
    def _wait(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_event.is_set():
                return False
            time.sleep(0.2)
        return True

    def _run(self, settings: Settings, queries: list[str]) -> None:
        session = None
        try:
            while not self.stop_event.is_set() and session is None:
                try:
                    self.logger.info("打开工具专用 Chrome 窗口…")
                    self.events.put(("status", "打开工具 Chrome"))
                    session = open_tool_browser(settings)
                except Exception as exc:
                    self.logger.error(
                        "打开 Chrome 失败：%s；30 秒后自动重试。", exc
                    )
                    self.events.put(("status", "打开 Chrome 失败，重试中"))
                    if not self._wait(30):
                        return
            if session is None:
                return

            page = session.page
            history = HistoryStore(settings.history_file)
            self.events.put(("stats", history.counts()))

            while not self.stop_event.is_set():
                cycle_new = 0
                for term in queries:
                    if self.stop_event.is_set():
                        break
                    url = SEARCH_URL.format(term=quote(term))
                    self.logger.info("搜索：%s", term)
                    self.events.put(("status", f"搜索：{term}"))

                    while not self.stop_event.is_set():
                        try:
                            goto_douyin(page, url, settings)
                            accounts = collect_accounts(page, settings, url)
                            match = select_targets(
                                accounts,
                                settings.keywords,
                                blocked_ids=history.blocked_ids(),
                                limit=settings.limit,
                            )
                            self.logger.info(
                                "采集 %d | 匹配 %d | 历史跳过 %d | 重复 %d",
                                len(accounts),
                                len(match.targets),
                                len(match.already_blocked),
                                match.duplicates,
                            )
                            if not match.targets:
                                break
                            stats = execute_blocks(
                                page,
                                match.targets,
                                settings,
                                history,
                                continue_on_error=True,
                                should_stop=self.stop_event.is_set,
                            )
                            cycle_new += stats.blocked
                            self.events.put(("stats", history.counts()))
                            break
                        except (PageChallengeError, NotLoggedInError) as exc:
                            self.logger.error(
                                "需要人工处理：%s；30 秒后自动重试。", exc
                            )
                            self.events.put(("status", "等待登录/验证，自动重试"))
                            if not self._wait(30):
                                return
                            continue
                        except BrowserError as exc:
                            message = str(exc)
                            if "502" in message:
                                self.logger.warning("502，180 秒后自动重试。")
                                self.events.put(("status", "502 退避中"))
                                if not self._wait(180):
                                    return
                                continue
                            self.logger.error("查询失败：%s", exc)
                            break

                    if self.stop_event.is_set():
                        break

                if self.stop_event.is_set():
                    break

                if cycle_new == 0:
                    self.logger.info("本轮没有新增，等待 2 分钟后继续循环。")
                    if not self._wait(120):
                        break
                else:
                    self.logger.info("本轮新增 %d 个，30 秒后进入下一轮。", cycle_new)
                    if not self._wait(30):
                        break

        except Exception as exc:
            self.logger.exception("运行失败：%s", exc)
            self.events.put(("status", f"已停止：{type(exc).__name__}"))
        finally:
            if session is not None:
                session.close(keep_page=True)
            if not self.stop_event.is_set():
                self.events.put(("status", "已停止"))
            self.events.put(("done", None))


def _selftest() -> int:
    """不打开窗口，验证打包后的 Tk/Playwright 驱动是否可用。"""
    import traceback

    log_path = PROJECT_DIR / "selftest.log"

    def write(text: str) -> None:
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            pass

    try:
        log_path.write_text("selftest start\n", encoding="utf-8")
        import tkinter  # noqa: F401

        write("tkinter ok")
        from playwright.sync_api import sync_playwright

        write("playwright import ok")
        playwright = sync_playwright().start()
        try:
            write("playwright driver ok")
        finally:
            playwright.stop()
        write("selftest ok")
        return 0
    except BaseException:
        write(traceback.format_exc())
        return 1


def _minimize_console() -> None:
    """双击 console 版 exe 时，立刻把黑色控制台最小化。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    except Exception:
        pass


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    _minimize_console()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
