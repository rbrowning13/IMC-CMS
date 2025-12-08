# impact_cms.spec
# PyInstaller spec file for Impact Medical CMS (MacOS .app build)

import os
from PyInstaller.utils.hooks import collect_submodules

# --- PATHS ---
project_root = os.path.abspath(".")
app_folder = os.path.join(project_root, "")
templates_folder = os.path.join(project_root, "app", "templates")
static_folder = os.path.join(project_root, "app", "static")

# --- COLLECT ALL MODULES ---
hidden_imports = collect_submodules("impact_cms_initial")

# --- DATA FILES TO INCLUDE ---
datas = [
    (templates_folder, "templates"),
    (static_folder, "static"),
]

# Ensure documents folder is created inside .app bundle
documents_path = os.path.join(project_root, "documents")
os.makedirs(documents_path, exist_ok=True)
datas.append((documents_path, "documents"))

# --- EXECUTABLE CONFIG ---
block_cipher = None

a = Analysis(
    ["impact_launcher.py"],   # Launch entrypoint
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --- BUILD .APP BUNDLE ---
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ImpactCMS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,     # No terminal window
)

app = BUNDLE(
    exe,
    name="ImpactCMS.app",
    icon=None,         # You can add your logo later
    bundle_identifier="com.impactmed.cms",
)