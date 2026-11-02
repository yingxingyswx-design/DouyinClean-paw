# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件、控制台窗口自动最小化）。

本地构建：
    python -m pip install -r requirements-dev.txt
    python -m PyInstaller --noconfirm --clean DouyinClean.spec
输出：dist/DouyinClean.exe、dist/DouyinCleanConsole.exe

两个 exe 是同一份 GUI 代码，区别只在输出流：
- DouyinClean.exe：入口 gui.py，标准输出被丢弃，双击后只剩 GUI 窗口。
- DouyinCleanConsole.exe：入口 gui_console.py，保留控制台与 stdout/stderr，
  适合在终端里运行、看 Python 报错或在脚本里调用。

CI 使用同一个 spec（.github/workflows/release.yml），保证本地与发布产物一致。
"""

import os

from PyInstaller.utils.hooks import collect_all

project_dir = os.path.abspath(SPECPATH)

datas = []
binaries = []
hiddenimports = []
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries += pw_binaries
hiddenimports += pw_hidden


def _analysis(entry_script):
    return Analysis(
        [os.path.join(project_dir, entry_script)],
        pathex=[project_dir],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
        optimize=0,
    )


a = _analysis("gui.py")
a_console = _analysis("gui_console.py")

pyz = PYZ(a.pure)
pyz_console = PYZ(a_console.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DouyinClean",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=".pyi-tmp",
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_console = EXE(
    pyz_console,
    a_console.scripts,
    a_console.binaries,
    a_console.datas,
    [],
    name="DouyinCleanConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=".pyi-tmp",
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
