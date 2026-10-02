# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Operator GUI - a single, portable
OperatorV<VERSION>.exe, built the same way as the Engineer GUI's
AtomicaATA.spec (see that file for why it's onefile and why the module list
is globbed rather than hand-maintained).

Build from the project root:
    pyinstaller OperatorGUI/OperatorV1.spec --distpath OperatorGUI/dist --workpath OperatorGUI/build

Output: OperatorGUI/dist/OperatorV<VERSION>.exe

Entry point is OperatorGUI/operator_app.py. Unlike AtomicaATA.spec,
EngineerGUI/app.py is an ordinary module here (operator_app.py subclasses its
AtomicaDashboard), so it is bundled like every other EngineerGUI module.
"""
import glob
import os

from PyInstaller.utils.hooks import copy_metadata

VERSION = 3

OPERATOR_DIR = os.path.abspath(os.path.dirname(os.path.abspath(SPEC)))
ROOT = os.path.dirname(OPERATOR_DIR)
GUI_DIR = os.path.join(ROOT, "EngineerGUI")
INSTRUMENTS_DIR = os.path.join(ROOT, "instruments")


def _module_names(directory: str, skip=("__init__",)) -> list:
    names = []
    for path in glob.glob(os.path.join(directory, "*.py")):
        name = os.path.splitext(os.path.basename(path))[0]
        if name not in skip:
            names.append(name)
    return sorted(names)


# EngineerGUI/, OperatorGUI/ and instruments/ modules are all imported bare
# (their folders go on sys.path at runtime), which the analyzer can't follow
# on its own.
hidden_gui = _module_names(GUI_DIR)
hidden_operator = _module_names(OPERATOR_DIR, skip=("__init__", "operator_app"))
hidden_instruments = [f"instruments.{n}" for n in _module_names(INSTRUMENTS_DIR)]

# Same extras as AtomicaATA.spec: the GDS modules are reached through the
# raw-copied gds/ data dir, and the VISA backends are loaded dynamically.
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
    [os.path.join(OPERATOR_DIR, "operator_app.py")],
    pathex=[ROOT, GUI_DIR, INSTRUMENTS_DIR, OPERATOR_DIR],
    binaries=[],
    datas=[
        # Header logos and the window icon. EngineerGUI/app.py (splash
        # screen) and operator_app.py (header, icon) both read them from the
        # bundle's root when frozen.
        (os.path.join(GUI_DIR, "logo2.jpg"), "."),
        (os.path.join(GUI_DIR, "logo_otto.jpg"), "."),
        (os.path.join(GUI_DIR, "app_icon.png"), "."),
        (os.path.join(ROOT, "gds", "*.py"), "gds"),
    ] + copy_metadata("pyvisa-py"),
    hiddenimports=hidden_gui + hidden_operator + hidden_instruments + hidden_extra,
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
    name=f"OperatorV{VERSION}",
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
    icon=os.path.join(GUI_DIR, "app_icon.ico"),
)
