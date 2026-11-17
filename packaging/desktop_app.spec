# PyInstaller spec for the Demand Forecast Processor desktop app.
#
# Build from the repo root:
#     pyinstaller packaging/desktop_app.spec
#
# Output: dist/DemandForecastProcessor(.exe)
#
# Before building for real customers, make sure desktop_app.py's
# LicenseManager.PUBLIC_KEY_PEM (or the LICENSE_PUBLIC_KEY env var used at
# build time) holds the PUBLIC half of the key pair whose PRIVATE half you use
# with tools/generate_license.py — otherwise issued licenses won't verify.

import sys
from pathlib import Path

block_cipher = None
repo_root = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(repo_root / "desktop_app.py")],
    pathex=[str(repo_root)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "openpyxl.cell._writer",
        "sklearn.utils._typedefs",
        "sklearn.utils._heap",
        "sklearn.utils._sorting",
        "sklearn.utils._vector_sentinel",
        "sklearn.neighbors._partition_nodes",
        "statsmodels.tsa.statespace._filters",
        "statsmodels.tsa.statespace._smoothers",
        "statsmodels.tsa.statespace._kalman_filter",
        "statsmodels.tsa.statespace._representation",
        "scipy.special.cython_special",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="DemandForecastProcessor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
