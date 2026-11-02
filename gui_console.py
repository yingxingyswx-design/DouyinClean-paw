"""DouyinCleanConsole 入口：保留控制台与标准输出/错误。

与 gui.py 是同一份 GUI，区别只在于本入口不会重定向 stdout/stderr，
方便在终端里观察 Python 报错、或在脚本中调用。
"""

from __future__ import annotations

import sys

import gui

if __name__ == "__main__":
    sys.exit(gui.main())
