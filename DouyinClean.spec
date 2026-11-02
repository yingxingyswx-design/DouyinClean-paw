# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件、控制台窗口自动最小化）。

本地构建：
    python -m pip install -r requirements-dev.txt
    python -m PyInstaller --noconfirm --clean DouyinClean.spec
输出：dist/DouyinClean.exe

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

a = Analysis(
    [os.path.join(project_dir, "gui.py")],
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

pyz = PYZ(a.pure)

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
