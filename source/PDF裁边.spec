# -*- mode: python ; coding: utf-8 -*-
# Build with build_pdf_crop_exe.ps1, or: python -m PyInstaller --noconfirm PDF裁边.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

HERE = Path(SPECPATH)

datas = [(str(HERE / "pdf_crop.ico"), ".")]
binaries = []
hiddenimports = ["fix_pdf_edges", "PIL", "PIL.Image", "numpy"]
tmp_ret = collect_all("pymupdf")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    [str(HERE / "pdf_crop_app.py")],
    pathex=[str(HERE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "PySide6.QtWebEngine",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtNetwork",
        "PySide6.QtPdf",
        "PIL.AvifImagePlugin",
        "PIL._avif",
        "ssl",
        "_ssl",
        "_hashlib",
    ],
    noarchive=False,
    optimize=0,
)

# Files the app never loads: Qt Quick/QML (dragged in by the virtual-keyboard
# plugin), software OpenGL, Qt's PDF/network/TLS stacks, OpenSSL, AVIF, the
# MuPDF C++ headers/libs, and every Qt translation except the zh_CN one the app
# installs for Qt's own buttons.
DROP = (
    "opengl32sw.dll",
    "qt6quick",
    "qt6qml",
    "qt6virtualkeyboard",
    "qtvirtualkeyboardplugin",
    "qtuiotouchplugin",
    "qt6pdf",
    "imageformats/qpdf",
    "qt6network",
    "qtnetwork",
    "plugins/tls/",
    "plugins/networkinformation/",
    "libssl",
    "libcrypto",
    "_ssl",
    "_hashlib",
    "_avif",
    "mupdf-devel/",
)


def _keep(entry) -> bool:
    dest = "/" + entry[0].replace("\\", "/").lower()
    if any(part in dest for part in DROP):
        return False
    return "/translations/" not in dest or dest.endswith("/qtbase_zh_cn.qm")


a.binaries = [entry for entry in a.binaries if _keep(entry)]
a.datas = [entry for entry in a.datas if _keep(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDF裁边",
    icon=str(HERE / "pdf_crop.ico"),
    version=str(HERE / "pdf_crop_version.txt"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name="PDF裁边",
)
