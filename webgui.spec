# -*- mode: python ; coding: utf-8 -*-
# Build with: pyinstaller webgui.spec
# Produces a single Windows executable that bundles webgui.py + webex-space-archive.py
# so the target machine needs neither Python nor 'pip install requests'.

a = Analysis(
    ['webgui.py'],
    pathex=[],
    binaries=[],
    datas=[('webex-space-archive.py', '.')],
    # webex-space-archive.py is only loaded at runtime via runpy (see webgui.py's
    # run_worker), so PyInstaller's static analysis never sees its "import requests"
    # and won't bundle it unless told to explicitly.
    hiddenimports=['requests'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Webex-Space-Archiver-GUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Keep the console window: it shows the server log/URL and is how you stop
    # the server (Ctrl+C or just close the window).
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
