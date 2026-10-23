"""Configuration storage for the BL-Catalog Calibre plugin.

Uses Calibre's ``JSONConfig('plugins/bl_catalog')`` when running inside Calibre,
and falls back to a standard dictionary / optional JSON file when imported outside
Calibre (for unit tests and CLI dry runs).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://192.168.10.2:12404"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_APPLY = 50
try:
    from calibre.constants import config_dir as _calibre_config_dir
except ImportError:
    _calibre_config_dir = str(Path.home() / ".config" / "calibre")
DEFAULT_SNAPSHOT_PATH = str(Path(_calibre_config_dir) / "plugins" / "bl_catalog" / "catalogue.sqlite")
DEFAULT_LOG_PATH = str(Path(tempfile.gettempdir()) / "blc-calibre-apply.jsonl")

DEFAULTS: dict[str, Any] = {
    "url": os.environ.get("BL_CATALOG_URL", DEFAULT_URL),
    "token": "",
    "mode": "snapshot",  # "snapshot" or "http"
    "snapshot_path": DEFAULT_SNAPSHOT_PATH,
    "timeout": DEFAULT_TIMEOUT,
    "max_apply": DEFAULT_MAX_APPLY,
    "log_path": DEFAULT_LOG_PATH,
    "apply_identifiers": True,
    "apply_title": False,
    "apply_authors": False,
    "apply_series": False,
    "apply_synopsis": False,
    "apply_tags": False,
    "apply_cover": False,
    "update_channel": "stable",
    "hide_stamped": False,
}


@dataclass(frozen=True)
class ApplyOptions:
    """User-selected fields to write when applying BLC matches to Calibre books."""

    identifiers: bool = True
    title: bool = False
    authors: bool = False
    series: bool = False
    synopsis: bool = False
    tags: bool = False
    cover: bool = False

    def any_enabled(self) -> bool:
        return any((
            self.identifiers,
            self.title,
            self.authors,
            self.series,
            self.synopsis,
            self.tags,
            self.cover,
        ))


def _load_prefs_backend():
    try:
        from calibre.utils.config import JSONConfig  # type: ignore[import-not-found]

        prefs = JSONConfig("plugins/bl_catalog")
        prefs.defaults.update(DEFAULTS)
        return prefs
    except ImportError:
        return dict(DEFAULTS)


PREFS = _load_prefs_backend()


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


def get_prefs() -> dict[str, Any]:
    """Return a merged dictionary of current plugin preferences."""
    return {key: PREFS.get(key, default) for key, default in DEFAULTS.items()}


def update_prefs(values: dict[str, Any]) -> None:
    """Persist updated plugin preferences."""
    for key, value in values.items():
        if key in DEFAULTS:
            PREFS[key] = value


def current_apply_options(prefs: dict[str, Any] | None = None) -> ApplyOptions:
    """Build ``ApplyOptions`` from stored preferences."""
    source = prefs if prefs is not None else get_prefs()
    return ApplyOptions(
        identifiers=bool(source.get("apply_identifiers", True)),
        title=bool(source.get("apply_title", False)),
        authors=bool(source.get("apply_authors", False)),
        series=bool(source.get("apply_series", False)),
        synopsis=bool(source.get("apply_synopsis", False)),
        tags=bool(source.get("apply_tags", False)),
        cover=bool(source.get("apply_cover", False)),
    )


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Load preferences from an explicit JSON file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("Configuration file must contain a JSON object")
    merged = dict(DEFAULTS)
    for k, v in raw.items():
        if k in DEFAULTS:
            merged[k] = v
    return merged
