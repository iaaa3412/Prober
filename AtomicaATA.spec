# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Atomica Tester GUI (Engineer GUI) - a
single, portable AtomicaProber.exe with nothing else that has to travel
alongside it (see EngineerGUI/workdir.py's own _PREF_PATH/_exe_dir
comments, and EngineerGUI/gds_parser_panel.py's _GDS_DIR comment, for
why that deployment shape is safe: the one file that must persist across
runs self-creates next to the exe on first use, and everything else is
read-only and bundled in here).

Build with:
    pyinstaller AtomicaATA.spec

Output: dist/AtomicaProber.exe

Entry point is EngineerGUI/app.py, NOT main.py. main.py launches app.py
via runpy.run_path() on a path computed at runtime - invisible to
PyInstaller's static import scanner, so building from main.py directly
would produce an exe missing the whole application. EngineerGUI/app.py
has its own `if __name__ == "__main__":` block and is already designed
to run standalone (`python EngineerGUI/app.py`) - see main.py's own
docstring.

Module discovery is DYNAMIC (glob EngineerGUI/*.py and instruments/*.py),
not a hand-maintained list - the project's previous spec (gui/
AtomicaTester.spec) hardcoded module names and had drifted badly stale
(missing most of the app - recipe_panel, cassette_panel, switch_topology,
workdir, export_formats, app_settings, and a dozen others - because
nobody had to remember to add each new file to it by hand). Every
EngineerGUI/*.py and instruments/*.py file is imported BARE elsewhere in
this codebase (`import switch_topology`, not
`import EngineerGUI.switch_topology`) because EngineerGUI/ and
instruments/ are both put directly on sys.path at runtime - PyInstaller's
analyzer can't discover a bare sibling import through static analysis
alone, so every one of those modules has to be told to it explicitly,
and globbing is what keeps that list correct as the project grows instead
of silently falling behind again.
"""
import glob
import os

from PyInstaller.utils.hooks import copy_metadata

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(SPEC)))
GUI_DIR = os.path.join(ROOT, "EngineerGUI")
INSTRUMENTS_DIR = os.path.join(ROOT, "instruments")


def _module_names(directory: str) -> list:
    names = []
    for path in glob.glob(os.path.join(directory, "*.py")):
        name = os.path.splitext(os.path.basename(path))[0]
        if name not in ("__init__", "app"):  # app.py is the entry script itself
            names.append(name)
    return sorted(names)


hidden_gui = _module_names(GUI_DIR)
hidden_instruments = [f"instruments.{n}" for n in _module_names(INSTRUMENTS_DIR)]

# Reached only through the raw-copied gds/ data dir below (imported at
# runtime via sys.path, not a real `import` PyInstaller's analyzer can
# follow) - gdstk especially matters here since it's a compiled C
# extension, not something copying loose .py files alone would bundle.
hidden_extra = [
    "ata_gds_core",
    "ata_gds2_parser",
    "ata_gds_gui",
    "gdstk",
    "yaml",
    "pyvisa",
    "pyvisa.backends.ivi",
    "pyvisa.backends.ni",
    "pyvisa_py",
    "gpib_ctypes",
]

a = Analysis(
    [os.path.join(GUI_DIR, "app.py")],
    pathex=[ROOT, GUI_DIR, INSTRUMENTS_DIR],
    binaries=[],
    datas=[
        (os.path.join(GUI_DIR, "logo2.jpg"), "."),
        # app.py's splash screen and header bar both also load
        # logo_otto.jpg (the Otto wordmark) at runtime - missing here
        # meant a built exe's os.path.exists() check on it silently came
        # back False and the logo just never rendered anywhere, no error.
        (os.path.join(GUI_DIR, "logo_otto.jpg"), "."),
        # app_icon.png: bundled so app.py can call self.iconphoto() with
        # it at startup (see AtomicaDashboard.__init__) - the EXE's OWN
        # embedded icon (app_icon.ico, used below) only ever covers the
        # Explorer/file icon; the taskbar/title-bar icon is a live Tk
        # window property that has to be set in code or it falls back to
        # Tk's own default feather icon, regardless of what's embedded
        # in the exe.
        (os.path.join(GUI_DIR, "app_icon.png"), "."),
        (os.path.join(ROOT, "gds", "*.py"), "gds"),
    ] + copy_metadata("pyvisa-py"),
    hiddenimports=hidden_gui + hidden_instruments + hidden_extra,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Onefile - deliberately NOT the exclude_binaries=True + separate
# COLLECT() combo (that produces a whole dist/AtomicaATA/ folder to ship
# instead of one file). See this file's own module docstring.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AtomicaProber",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows Explorer file icon for the exe itself - separate from the
    # logo2.jpg/logo_otto.jpg/app_icon.ico data files above (those are
    # the in-window logos and the runtime taskbar-icon source - see
    # AtomicaDashboard.__init__'s iconbitmap call). PyInstaller embeds
    # this .ico directly into the binary; without it, the exe falls back
    # to PyInstaller's own default icon. Same image as app_icon.ico
    # (ottologo2's diamond badge) so the file icon and the running
    # window's icon match.
    icon=os.path.join(GUI_DIR, "app_icon.ico"),
)
