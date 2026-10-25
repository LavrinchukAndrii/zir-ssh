# PyInstaller spec for zir. Build with `python scripts/build.py`, which sets:
#   ZIR_BIN_NAME  output name, e.g. zir-linux-x86_64
#   ZIR_MODE      "onefile" (single executable) or "app" (macOS zir.app bundle)

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

sys.path.insert(0, os.path.abspath(os.path.join(SPECPATH, "..")))
from zir import __version__  # noqa: E402

name = os.environ.get("ZIR_BIN_NAME", "zir")
mode = os.environ.get("ZIR_MODE", "onefile")
root = os.path.abspath(os.path.join(SPECPATH, ".."))
icons = os.path.join(SPECPATH, "icons")
icon = os.path.join(icons, "zir.icns" if sys.platform == "darwin" else "zir.ico")

from rich._unicode_data._versions import VERSIONS  # noqa: E402

# Textual's data files are tree-sitter queries for TextArea, which zir does not use.
datas = [entry for entry in collect_data_files("textual") if "tree-sitter" not in entry[0]]
# Rich loads its unicode width table by dynamic import: only the newest one is used
# (entry.py drops UNICODE_VERSION, which could ask for an older one).
latest_unicode = "rich._unicode_data.unicode" + VERSIONS[-1].replace(".", "-")
hiddenimports = (
    ["rich._unicode_data._versions", latest_unicode]
    # Widgets zir never shows (the command palette is off): Markdown pulls in
    # markdown-it and pydoc, TextArea tree-sitter.
    + [m for m in collect_submodules("textual.widgets")
       if not any(part in m for part in ("markdown", "help_panel", "text_area"))]
    + collect_submodules("zir")
)
excludes = [
    "tkinter", "PyInstaller", "PIL",
    "markdown_it", "mdit_py_plugins", "linkify_it", "rich.markdown", "pydoc", "pydoc_data",
    "textual._markup_playground", "textual.widgets._markdown", "textual.widgets.markdown",
    "textual.widgets._text_area", "textual.widgets.text_area", "textual.widgets._help_panel",
    # Standard library parts nothing in zir reaches.
    "multiprocessing", "concurrent.futures.process", "xmlrpc",
    "tarfile", "lzma", "_lzma", "bz2", "_bz2", "_zstd", "compression.zstd",
    "statistics",  # fractions / decimal stay: rich uses them
    "_codecs_jp", "_codecs_cn", "_codecs_kr", "_codecs_tw", "_codecs_hk", "_codecs_iso2022",
    "_multibytecodec", "pygments.formatters",
]
# Rich highlights Python tracebacks and Textual CSS errors; the other ~250 lexers stay out.
keep_lexers = {"_mapping", "python", "css", "special", "data"}
excludes += [m for m in collect_submodules("pygments.lexers") if m.rsplit(".", 1)[-1] not in keep_lexers]
excludes += [m for m in collect_submodules("rich._unicode_data")
             if m.rsplit(".", 1)[-1].startswith("unicode") and m != latest_unicode]
if mode == "app":
    # zir.app draws its own window (pywebview + xterm.js) instead of opening Terminal.
    datas += [(os.path.join(root, "zir", "assets"), "zir/assets")]
    datas += collect_data_files("webview")
    hiddenimports += collect_submodules("webview.platforms.cocoa") + ["webview.platforms.cocoa"]
else:
    # The single binary needs only the icon (xterm.js is for the app window).
    datas += [(os.path.join(root, "zir", "assets", "zir.png"), "zir/assets")]
    # pywebview's local web server needs these; the single binary has no window.
    excludes += ["webview", "http.server", "socketserver"]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[root],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
    optimize=1,  # drop asserts; docstrings stay (Textual reads some at runtime)
)
pyz = PYZ(a.pure)

if mode == "app":
    # onedir inside the bundle: starts instantly (onefile unpacks itself on
    # every launch, and macOS re-scans the unpacked libraries each time).
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="zir",
        console=True,
        icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="zir")
    app = BUNDLE(
        coll,
        name="zir.app",
        icon=icon,
        bundle_identifier="dev.zir.app",
        version=__version__,
        info_plist={
            "CFBundleName": "Zir SSH",
            "CFBundleDisplayName": "Zir SSH",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "11.0",
            # PyInstaller marks console bundles background-only; the window needs the Dock.
            "LSBackgroundOnly": False,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "MIT License",
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name=name,
        console=True,
        icon=icon,
        # Stripping breaks the ad-hoc signature on macOS; safe on Linux.
        strip=sys.platform.startswith("linux"),
        upx=False,
    )
