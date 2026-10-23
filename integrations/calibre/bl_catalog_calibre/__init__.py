"""BL-Catalog Calibre Plugin: Reading Lamp GUI Action + Metadata/Cover Source (Ctrl+D).

Can be loaded by Calibre from a plugin ZIP (built via ``integrations/calibre/build_plugin.py``
or ``calibre-customize -b integrations/calibre/bl_catalog_calibre``) and imported directly
as a standard Python package in tests and CLI scripts.
"""

from __future__ import annotations

from typing import Any, ClassVar

from .apply import (
    ApplyRefused,
    CalibreDbWriter,
    StaleBookError,
    append_log,
    apply_list,
    apply_matches,
    file_sha256,
    read_id_list,
)
from .client import (
    HttpCatalogClient,
    Lookups,
    SnapshotCatalogClient,
    configured_url,
    open_readonly_sqlite,
    read_calibre_books_readonly,
)
from .config import (
    DEFAULTS,
    ApplyOptions,
    current_apply_options,
    get_prefs,
    update_prefs,
)
from .dialog import BLCatalogConfigWidget, DialogController
from .matcher import (
    APPLICABLE_BUCKETS,
    BUCKETS,
    CERTAIN,
    CSV_FIELDS,
    WORK_ID_RE,
    CatalogueError,
    apply_manual_pick,
    author,
    author_aliases,
    author_relation,
    authors_of,
    build_proposals,
    calibre_title_norm,
    choose_isbn,
    classify,
    cover_of,
    demote_shared,
    isbns_of,
    norm,
    offered_credits,
    picked_contents_match,
    plain_title,
    run_scan,
    scalar_value,
    series_of,
    tags_of,
    title_of,
    to_isbn13,
    valid_isbn,
    write_csv,
)
from .snapshot import migrate_snapshot_path
from .source import BLCatalogMetadataSource, work_to_calibre_metadata
from .style import BUCKET_STYLE, TOKENS, build_stylesheet, render_work_detail_html
from .version import MINIMUM_CALIBRE_VERSION, PLUGIN_VERSION

try:
    from calibre.customize import InterfaceActionBase as _CalibrePluginBase  # type: ignore[import-not-found]
except ImportError:

    class _CalibrePluginBase:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.plugin_path = args[0] if args else None


class BLCatalogPlugin(_CalibrePluginBase):
    """Primary Calibre plugin entry point: Toolbar GUI Action + Metadata Source registration."""

    name = "BL-Catalog"
    description = (
        "Match Calibre books, identifiers, metadata, and Lexicanum covers against BL-Catalog "
        "(Reading Lamp GUI matcher + Ctrl+D Metadata Source)"
    )
    supported_platforms: ClassVar[list[str]] = ["windows", "osx", "linux"]
    author = "BL-Catalog"
    version = PLUGIN_VERSION
    minimum_calibre_version = MINIMUM_CALIBRE_VERSION
    actual_plugin = "calibre_plugins.bl_catalog_calibre.action:BLCatalogAction"

    def initialize(self) -> None:
        """Register the companion Metadata Source plugin so Ctrl+D discovers BL-Catalog too."""
        super_init = getattr(super(), "initialize", None)
        if callable(super_init):
            super_init()
        previous = get_prefs().get("snapshot_path")
        try:
            snapshot = migrate_snapshot_path(previous)
            if snapshot != previous:
                update_prefs({"snapshot_path": snapshot})
        except OSError:
            # Loading the plugin must remain possible when its snapshot cannot be copied.
            pass
        self._register_metadata_source()

    def _register_metadata_source(self) -> BLCatalogMetadataSource | None:
        try:
            from calibre.customize import ui  # type: ignore[import-not-found]

            for existing in getattr(ui, "_initialized_plugins", ()):
                if isinstance(existing, BLCatalogMetadataSource) or (
                    getattr(existing, "name", None) == BLCatalogMetadataSource.name
                    and existing is not self
                ):
                    return existing
            source_plugin = BLCatalogMetadataSource(getattr(self, "plugin_path", None))
            if hasattr(ui, "_initialized_plugins") and isinstance(ui._initialized_plugins, list):
                ui._initialized_plugins.append(source_plugin)
            return source_plugin
        except ImportError:
            return None

    def is_customizable(self) -> bool:
        return True

    def config_widget(self):
        return BLCatalogConfigWidget()

    def save_settings(self, config_widget: Any) -> None:
        config_widget.save_settings()


__all__ = [
    "APPLICABLE_BUCKETS",
    "BUCKETS",
    "BUCKET_STYLE",
    "CERTAIN",
    "CSV_FIELDS",
    "DEFAULTS",
    "TOKENS",
    "WORK_ID_RE",
    "ApplyOptions",
    "ApplyRefused",
    "BLCatalogConfigWidget",
    "BLCatalogMetadataSource",
    "BLCatalogPlugin",
    "CalibreDbWriter",
    "CatalogueError",
    "DialogController",
    "HttpCatalogClient",
    "Lookups",
    "SnapshotCatalogClient",
    "StaleBookError",
    "append_log",
    "apply_list",
    "apply_manual_pick",
    "apply_matches",
    "author",
    "author_aliases",
    "author_relation",
    "authors_of",
    "build_proposals",
    "build_stylesheet",
    "calibre_title_norm",
    "choose_isbn",
    "classify",
    "configured_url",
    "cover_of",
    "current_apply_options",
    "demote_shared",
    "file_sha256",
    "get_prefs",
    "isbns_of",
    "norm",
    "offered_credits",
    "open_readonly_sqlite",
    "picked_contents_match",
    "plain_title",
    "read_calibre_books_readonly",
    "read_id_list",
    "render_work_detail_html",
    "run_scan",
    "scalar_value",
    "series_of",
    "tags_of",
    "title_of",
    "to_isbn13",
    "update_prefs",
    "valid_isbn",
    "work_to_calibre_metadata",
    "write_csv",
]
