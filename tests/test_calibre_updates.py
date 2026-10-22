"""GitHub updater regressions; no live network or Calibre installation required."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import unittest
import urllib.error
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "calibre"))
from bl_catalog_calibre import updates
from bl_catalog_calibre.version import UPDATE_REPOSITORY


def make_package(tag="v1.2.0", *, extra=None, plugin_name="BL-Catalog", version_tag=None):
    files = {name: "" for name in (
        "action.py", "updates.py", "update_ui.py", "apply.py", "client.py", "config.py", "dialog.py",
        "matcher.py", "source.py", "style.py", "plugin-import-name-bl_catalog_calibre.txt",
    )}
    files["__init__.py"] = f"class BLCatalogPlugin:\n    name = {plugin_name!r}\n"
    files["version.py"] = (
        f"PLUGIN_VERSION = {updates.version_key(tag)[:3]!r}\nRELEASE_TAG = {version_tag or tag!r}\n"
    )
    files.update(extra or {})
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return target.getvalue()


def release_fixture(tag="v1.2.0", data=None):
    data = data if data is not None else make_package(tag)
    prefix = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/{tag}/"
    digest = hashlib.sha256(data).hexdigest()
    manifest = {
        "schema": 1, "plugin": "BL-Catalog", "tag": tag, "asset": updates.ASSET_NAME,
        "sha256": digest, "size": len(data), "minimum_calibre_version": [5, 0, 0],
    }
    raw = {
        "tag_name": tag, "draft": False, "prerelease": "-beta." in tag, "body": "Fixture release notes",
        "assets": [
            {"name": updates.ASSET_NAME, "browser_download_url": prefix + updates.ASSET_NAME,
             "size": len(data), "digest": "sha256:" + digest},
            {"name": updates.MANIFEST_NAME, "browser_download_url": prefix + updates.MANIFEST_NAME},
        ],
    }
    release = updates.PluginRelease(tag, raw["body"], prefix + updates.ASSET_NAME, digest, len(data), (5, 0, 0))
    return raw, manifest, release, data


class CalibreUpdateTest(unittest.TestCase):
    def setUp(self):
        self.raw, self.manifest, self.release, self.data = release_fixture()
        self.releases = [self.raw]
        self.fetch = Mock(side_effect=self.fetch_fixture)

    def fetch_fixture(self, url, limit):
        if url == updates.API_URL:
            return json.dumps(self.releases).encode()
        if url.endswith(updates.MANIFEST_NAME):
            return json.dumps(self.manifest).encode()
        if url == self.release.url:
            return self.data
        raise AssertionError(url)

    def check(self, channel="stable", **kwargs):
        return updates.check_for_update(channel, installed_tag="v1.1.0", fetch=self.fetch, **kwargs)

    def test_stable_discovers_and_verifies_release(self):
        self.assertEqual(self.check(calibre_version=(8, 0, 0)), self.release)
        self.assertEqual(updates.download_update(self.release, fetch=self.fetch), self.data)

    def test_current_release_does_not_download_manifest(self):
        self.assertIsNone(updates.check_for_update(installed_tag="v1.2.0", fetch=self.fetch))
        self.assertEqual(self.fetch.call_count, 1)

    def test_stable_ignores_beta_drafts_and_non_plugin_tags(self):
        self.releases = [release_fixture("v1.3.0-beta.1")[0], dict(self.raw, draft=True), dict(self.raw, tag_name="unrelated")]
        self.assertIsNone(self.check())

    def test_beta_selects_newest_and_stable_wins_same_version(self):
        beta, manifest, release, _ = release_fixture("v1.3.0-beta.2")
        self.releases = [self.raw, beta]
        self.manifest = manifest
        self.assertEqual(self.check("beta"), release)
        self.releases.append(release_fixture("v1.3.0")[0])
        self.manifest = release_fixture("v1.3.0")[1]
        self.assertEqual(self.check("beta").tag, "v1.3.0")

    def test_numeric_beta_order_and_invalid_versions(self):
        self.assertGreater(updates.version_key("v1.3.0-beta.10"), updates.version_key("v1.3.0-beta.2"))
        for value in ("v1.2", "v01.2.0", "v1.2.0-beta.0", "v1.2.0-rc.1", "../../file"):
            with self.subTest(value=value), self.assertRaises(updates.UpdateError):
                updates.version_key(value)

    def test_invalid_channel_and_release_list(self):
        with self.assertRaises(updates.UpdateError):
            self.check("nightly")
        self.fetch.side_effect = None
        for payload in (b"not JSON", b"{}"):
            self.fetch.return_value = payload
            with self.assertRaises(updates.UpdateError):
                self.check()

    def test_foreign_repository_asset_and_duplicates_rejected(self):
        self.raw["assets"][0]["browser_download_url"] = "https://github.com/other/repo/releases/download/v1.2.0/plugin.zip"
        with self.assertRaises(updates.UpdateError):
            self.check()
        self.raw["assets"][0]["browser_download_url"] = self.release.url
        self.raw["assets"].append(dict(self.raw["assets"][0]))
        with self.assertRaises(updates.UpdateError):
            self.check()

    def test_invalid_manifests_rejected(self):
        for field, value in (
            ("schema", 2), ("plugin", "Another Plugin"), ("tag", "v9.0.0"), ("asset", "other.zip"),
            ("sha256", "bad"), ("size", True), ("size", updates.MAX_ZIP_BYTES + 1),
            ("minimum_calibre_version", [True, 0, 0]),
        ):
            original = self.manifest[field]
            self.manifest[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(updates.UpdateError):
                self.check()
            self.manifest[field] = original

    def test_calibre_requirement_checked_before_download(self):
        with self.assertRaisesRegex(updates.UpdateError, "Update Calibre"):
            self.check(calibre_version=(4, 23, 0))

    def test_api_checksum_must_agree(self):
        self.raw["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(updates.UpdateError):
            self.check()

    def test_corrupt_download_never_installs(self):
        installer = Mock()
        with self.assertRaises(updates.UpdateError):
            updates.install_downloaded_update(self.release, self.data[:-1] + b"!", installer=installer)
        installer.assert_not_called()

    def test_zip_identity_paths_version_and_limits(self):
        for data in (
            b"not a zip",
            make_package(plugin_name="Another Plugin"),
            make_package(version_tag="v9.9.9"),
            make_package(extra={"../escape.py": "bad"}),
            make_package(extra={"C:/escape.py": "bad"}),
        ):
            release = release_fixture(data=data)[2]
            with self.subTest(size=len(data)), self.assertRaises(updates.UpdateError):
                updates.validate_package(release, data)
        with patch.object(updates, "MAX_EXPANDED_BYTES", 1), self.assertRaises(updates.UpdateError):
            updates.validate_package(self.release, self.data)

    def test_install_calls_calibre_installer_and_removes_temporary_zip(self):
        paths = []

        def installer(path):
            self.assertEqual(Path(path).read_bytes(), self.data)
            paths.append(path)

        updates.install_downloaded_update(self.release, self.data, installer=installer)
        self.assertFalse(Path(paths[0]).exists())

    def test_download_rejects_foreign_url_before_fetch(self):
        with self.assertRaises(updates.UpdateError):
            updates.download_update(replace(self.release, url="http://localhost/plugin.zip"), fetch=self.fetch)
        self.fetch.assert_not_called()

    def test_https_and_redirect_allowlist(self):
        for url in ("http://github.com/file", "https://evil.example/file", "https://github.com:bad/file", "https://user:pass@github.com/file"):
            with self.subTest(url=url), self.assertRaises(updates.UpdateError):
                updates._validate_url(url)
        request = urllib.request.Request("https://github.com/file")
        with self.assertRaises(updates.UpdateError):
            updates._GitHubRedirects().redirect_request(request, None, 302, "Found", {}, "http://evil.example/file")

    def test_fetch_has_no_authorization_and_enforces_size(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {}
        response.geturl.return_value = updates.API_URL
        response.read.return_value = b"[]"
        opener = Mock()
        opener.open.return_value = response
        with patch.object(updates.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(updates.fetch_bytes(updates.API_URL, 10), b"[]")
            request = opener.open.call_args.args[0]
            self.assertIsNone(request.get_header("Authorization"))
            response.read.return_value = b"x" * 11
            with self.assertRaises(updates.UpdateError):
                updates.fetch_bytes(updates.API_URL, 10)
            opener.open.side_effect = urllib.error.HTTPError(updates.API_URL, 403, "Limited", {}, None)
            with self.assertRaisesRegex(updates.UpdateError, "limited"):
                updates.fetch_bytes(updates.API_URL, 10)


if __name__ == "__main__":
    unittest.main()
