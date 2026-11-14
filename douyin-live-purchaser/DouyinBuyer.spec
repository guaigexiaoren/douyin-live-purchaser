# -*- mode: python ; coding: utf-8 -*-
# 抖音直播自动购买 —— 文件夹版（onedir）打包配置
import os
from PyInstaller.utils.hooks import collect_all

# 本机 Playwright 实际使用的 Chromium 版本（缓存里另有旧的 1223，必须用 1234）
CHROMIUM_REV = 'chromium-1234'
SRC_CHROMIUM = r'C:\Users\YYMY\AppData\Local\ms-playwright\chromium-1234'

datas = [(SRC_CHROMIUM, os.path.join('playwright-browsers', CHROMIUM_REV))]
binaries = []
hiddenimports = ['pandas', 'playwright', 'taobao_buy']

_tmp = collect_all('playwright')
datas += _tmp[0]
binaries += _tmp[1]
hiddenimports += _tmp[2]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 这些是别的 GUI/绘图库，本程序在 Windows 上走 WebView2，完全用不到，剔掉可减小体积
    excludes=[
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'shiboken2', 'shiboken6', 'qtpy',
        'matplotlib', 'PIL', 'tkinter', 'IPython', 'pytest',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DouyinBuyer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DouyinBuyer',
)