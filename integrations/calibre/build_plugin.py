#!/usr/bin/env python3
"""Build the installable BL-Catalog Calibre plugin ZIP archive.

Syncs the canonical warm brass BLC icon assets from ``src/bl_catalog/api/`` into
``integrations/calibre/bl_catalog_calibre/images/`` and packs all plugin modules
into a deterministic ZIP that can be installed via:
  - Calibre -> Preferences -> Plugins -> Load plugin from file
  - ``calibre-customize -a dist/bl-catalog-calibre.zip``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PLUGIN_DIR = HERE / "bl_catalog_calibre"
DEFAULT_OUT = HERE / "dist" / "bl-catalog-calibre.zip"

CANONICAL_PNG = REPO_ROOT / "src" / "bl_catalog" / "api" / "web" / "icons" / "icon-v3-192.png"
CANONICAL_SVG = REPO_ROOT / "src" / "bl_catalog" / "api" / "assets" / "bl-catalog-warm-brass-icon.svg"

PLUGIN_FILES = (
    "__init__.py",
    "__main__.py",
    "action.py",
    "apply.py",
    "client.py",
    "config.py",
    "dialog.py",
    "matcher.py",
    "scan_cache.py",
    "source.py",
    "style.py",
    "version.py",
    "updates.py",
    "update_ui.py",
    "plugin-import-name-bl_catalog_calibre.txt",
    "images/icon.png",
    "images/icon.svg",
)


def sync_icons(plugin_dir: Path = PLUGIN_DIR) -> None:
    """Copy the canonical BLC v3 warm brass PNG and SVG icons into ``images/``."""
    img_dir = plugin_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    if CANONICAL_PNG.is_file():
        shutil.copyfile(CANONICAL_PNG, img_dir / "icon.png")
    if CANONICAL_SVG.is_file():
        shutil.copyfile(CANONICAL_SVG, img_dir / "icon.svg")


def build_plugin_zip(output_path: str | Path = DEFAULT_OUT, plugin_dir: Path = PLUGIN_DIR) -> Path:
    """Pack ``bl_catalog_calibre`` into a Calibre plugin ZIP at ``output_path``."""
    sync_icons(plugin_dir)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in PLUGIN_FILES:
            src = plugin_dir / rel
            if not src.is_file():
                raise FileNotFoundError(f"Missing required plugin file: {src}")
            info = zipfile.ZipInfo(rel, date_time=(2026, 10, 21, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, src.read_bytes())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_OUT),
        help="Path for the output Calibre plugin .zip file",
    )
    args = parser.parse_args(argv)
    out = build_plugin_zip(args.output)
    version = runpy.run_path(str(PLUGIN_DIR / "version.py"))
    manifest = {
        "schema": 1,
        "plugin": "BL-Catalog",
        "tag": version["RELEASE_TAG"],
        "asset": out.name,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "size": out.stat().st_size,
        "minimum_calibre_version": list(version["MINIMUM_CALIBRE_VERSION"]),
    }
    out.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (out.parent / "SHA256SUMS").write_text(
        f"{manifest['sha256']}  {out.name}\n", encoding="utf-8",
    )
    print(f"Built Calibre plugin ZIP: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
