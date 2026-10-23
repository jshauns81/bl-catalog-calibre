"""Persistent snapshot location and migration independent of cached plugin modules."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

try:
    from calibre.constants import config_dir as _calibre_config_dir
except ImportError:
    _calibre_config_dir = str(Path.home() / ".config" / "calibre")

DEFAULT_SNAPSHOT_PATH = str(Path(_calibre_config_dir) / "plugins" / "bl_catalog" / "catalogue.sqlite")


def migrate_snapshot_path(value: str | Path | None) -> str:
    """Keep generated snapshots out of Calibre's per-process temporary directory."""
    old = Path(value or DEFAULT_SNAPSHOT_PATH)
    target = Path(DEFAULT_SNAPSHOT_PATH)
    temporary = Path(tempfile.gettempdir())
    legacy = old.name == "bl-catalog-latest.sqlite" and (
        old.parent == temporary
        or old.parent == Path("/tmp")
        or (old.parent.name.startswith("calibre-") and old.parent.parent in (temporary, temporary.parent, Path("/tmp")))
    )
    if not legacy or old == target:
        return str(old)
    if old.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temporary_copy = Path(handle.name)
        try:
            shutil.copyfile(old, temporary_copy)
            os.replace(temporary_copy, target)
        finally:
            temporary_copy.unlink(missing_ok=True)
    return str(target)
