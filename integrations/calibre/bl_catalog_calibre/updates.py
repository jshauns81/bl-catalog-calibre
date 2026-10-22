"""GitHub release discovery and bounded, verified plugin downloads."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .version import PLUGIN_VERSION, RELEASE_TAG, UPDATE_REPOSITORY

ASSET_NAME = "bl-catalog-calibre.zip"
MANIFEST_NAME = "bl-catalog-calibre.json"
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
API_URL = f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases?per_page=100"
_TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.([1-9]\d*))?")
_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class UpdateError(Exception):
    """An update could not be checked, verified or installed."""


def version_key(tag: str) -> tuple[int, int, int, int, int]:
    match = _TAG.fullmatch(tag)
    if match is None:
        raise UpdateError("Unsupported plugin release version.")
    major, minor, patch, beta = match.groups()
    return int(major), int(minor), int(patch), int(beta is None), int(beta or 0)


def _validate_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("The update URL is invalid.") from exc
    if (
        parsed.scheme != "https" or parsed.hostname not in _HOSTS
        or parsed.username or parsed.password or port not in (None, 443)
    ):
        raise UpdateError("The update URL is not an approved GitHub HTTPS address.")


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bytes(url: str, limit: int) -> bytes:
    """Use a separate, unauthenticated client; catalogue credentials are never involved."""
    _validate_url(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": f"BL-Catalog-Calibre/{'.'.join(map(str, PLUGIN_VERSION))}",
        "Accept": "application/vnd.github+json" if url.startswith("https://api.github.com/") else "*/*",
    })
    opener = urllib.request.build_opener(_GitHubRedirects())
    try:
        with opener.open(request, timeout=20) as response:
            _validate_url(response.geturl())
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > limit:
                raise UpdateError("The update download exceeds the size limit.")
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateError("GitHub temporarily limited update checks. Please try again later.") from exc
        raise UpdateError(f"GitHub update request failed (HTTP {exc.code}).") from exc
    except (OSError, ValueError) as exc:
        raise UpdateError("Could not reach GitHub. Check your connection and try again.") from exc
    if len(data) > limit:
        raise UpdateError("The update download exceeds the size limit.")
    return data


def _json(data: bytes) -> Any:
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise UpdateError("GitHub returned invalid update information.") from exc


def _asset(release: dict[str, Any], name: str) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("The plugin release has no downloadable files.")
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
    if len(matches) != 1:
        raise UpdateError(f"The plugin release must contain exactly one {name} file.")
    tag = release["tag_name"]
    expected = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/{tag}/{name}"
    if matches[0].get("browser_download_url") != expected:
        raise UpdateError("The release download does not belong to the plugin repository.")
    return matches[0]


@dataclass(frozen=True)
class PluginRelease:
    tag: str
    notes: str
    url: str
    sha256: str
    size: int
    minimum_calibre: tuple[int, int, int]


def check_for_update(
    channel: str = "stable", *, installed_tag: str = RELEASE_TAG,
    calibre_version: tuple[int, ...] | None = None, fetch=fetch_bytes,
) -> PluginRelease | None:
    if channel not in ("stable", "beta"):
        raise UpdateError("Choose the Stable or Beta update channel.")
    installed = version_key(installed_tag)
    releases = _json(fetch(API_URL, 2 * 1024 * 1024))
    if not isinstance(releases, list):
        raise UpdateError("GitHub returned an invalid release list.")
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is not False:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        try:
            key = version_key(tag)
        except UpdateError:
            continue
        beta = key[3] == 0
        if release.get("prerelease") is not beta or (channel == "stable" and beta):
            continue
        if key > installed:
            candidates.append((key, release))
    if not candidates:
        return None
    release = max(candidates, key=lambda item: item[0])[1]
    package = _asset(release, ASSET_NAME)
    manifest_asset = _asset(release, MANIFEST_NAME)
    manifest = _json(fetch(manifest_asset["browser_download_url"], 16 * 1024))
    if not isinstance(manifest, dict) or (
        manifest.get("schema") != 1 or manifest.get("plugin") != "BL-Catalog"
        or manifest.get("tag") != release["tag_name"] or manifest.get("asset") != ASSET_NAME
    ):
        raise UpdateError("The release manifest does not match this plugin.")
    digest = manifest.get("sha256")
    size = manifest.get("size")
    minimum = manifest.get("minimum_calibre_version")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("The release has no valid SHA-256 checksum.")
    if type(size) is not int or not 0 < size <= MAX_ZIP_BYTES or package.get("size") != size:
        raise UpdateError("The release package size is invalid.")
    api_digest = package.get("digest")
    if api_digest is not None and api_digest != "sha256:" + digest:
        raise UpdateError("GitHub's checksum disagrees with the release manifest.")
    if not isinstance(minimum, list) or len(minimum) != 3 or any(type(v) is not int or v < 0 for v in minimum):
        raise UpdateError("The release's Calibre requirement is invalid.")
    if calibre_version is not None and tuple(calibre_version[:3]) < tuple(minimum):
        raise UpdateError("Update Calibre before installing this plugin release.")
    return PluginRelease(
        tag=release["tag_name"], notes=str(release.get("body") or "No release notes provided.")[:16000],
        url=package["browser_download_url"], sha256=digest, size=size,
        minimum_calibre=tuple(minimum),
    )


def validate_package(release: PluginRelease, data: bytes) -> None:
    if len(data) != release.size or len(data) > MAX_ZIP_BYTES:
        raise UpdateError("The downloaded plugin size does not match the release.")
    if hashlib.sha256(data).hexdigest() != release.sha256:
        raise UpdateError("The downloaded plugin failed its SHA-256 check. Nothing was installed.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = archive.infolist()
            names = [f.filename for f in files]
            if len(names) != len(set(names)) or len(names) > 100:
                raise UpdateError("The plugin ZIP has duplicate or excessive entries.")
            if sum(f.file_size for f in files) > MAX_EXPANDED_BYTES:
                raise UpdateError("The expanded plugin exceeds the size limit.")
            for item in files:
                path = Path(item.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in item.filename or ":" in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                    raise UpdateError("The plugin ZIP contains an unsafe file path.")
            required = {
                "__init__.py", "version.py", "action.py", "updates.py", "update_ui.py",
                "apply.py", "client.py", "config.py", "dialog.py", "matcher.py", "source.py", "style.py",
                "plugin-import-name-bl_catalog_calibre.txt",
            }
            if not required.issubset(names):
                raise UpdateError("The ZIP is not a complete BL-Catalog plugin.")
            constants = {}
            for node in ast.parse(archive.read("version.py").decode("utf-8")).body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in ("PLUGIN_VERSION", "RELEASE_TAG"):
                            constants[target.id] = ast.literal_eval(node.value)
            if constants.get("RELEASE_TAG") != release.tag or constants.get("PLUGIN_VERSION") != version_key(release.tag)[:3]:
                raise UpdateError("The plugin ZIP version does not match the release.")
            entry = ast.parse(archive.read("__init__.py").decode("utf-8"))
            plugin_names = [
                ast.literal_eval(node.value)
                for cls in entry.body if isinstance(cls, ast.ClassDef) and cls.name == "BLCatalogPlugin"
                for node in cls.body if isinstance(node, ast.Assign)
                if any(isinstance(t, ast.Name) and t.id == "name" for t in node.targets)
            ]
            if plugin_names != ["BL-Catalog"] or archive.testzip() is not None:
                raise UpdateError("The plugin ZIP identity or integrity check failed.")
    except (zipfile.BadZipFile, UnicodeError, SyntaxError, ValueError, KeyError, RuntimeError) as exc:
        raise UpdateError("The downloaded plugin ZIP is invalid.") from exc


def download_update(release: PluginRelease, *, fetch=fetch_bytes) -> bytes:
    expected = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/{release.tag}/{ASSET_NAME}"
    if release.url != expected:
        raise UpdateError("The plugin package is not from the release repository.")
    data = fetch(release.url, MAX_ZIP_BYTES)
    validate_package(release, data)
    return data


def install_downloaded_update(release: PluginRelease, data: bytes, *, installer=None) -> None:
    validate_package(release, data)
    if installer is None:
        from calibre.customize.ui import add_plugin  # type: ignore[import-not-found]

        installer = add_plugin
    with tempfile.TemporaryDirectory(prefix="blc-plugin-update-") as temporary:
        target = Path(temporary) / ASSET_NAME
        target.write_bytes(data)
        installer(str(target))
