from __future__ import annotations

import tempfile
import tomllib
from pathlib import Path
import unittest
from unittest import mock

from ytb_tg_backup.config import load_config
from ytb_tg_backup.extension_api import (
    ExtensionSetupManifest,
    ExtensionSetupResult,
    OriginSuggestion,
)
from ytb_tg_backup.extension_install import ExtensionInstallResult
from ytb_tg_backup.extension_management import (
    ExtensionManagementError,
    enable_extension,
)
from ytb_tg_backup.extension_state import extension_sidecar_path
from ytb_tg_backup.extension_state import replace_private_file as real_replace_private_file
from ytb_tg_backup.extensions import PreparedExtensionSetup


class ExtensionManagementTest(unittest.TestCase):
    def test_enable_writes_private_sidecar_without_touching_main_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            original = config_path.read_bytes()
            calls: list[str] = []
            prepared = PreparedExtensionSetup(
                manifest=ExtensionSetupManifest(
                    extension_id="dreaife.proxy-router",
                    config_filename="proxy-router.toml",
                ),
                result=ExtensionSetupResult(
                    config={
                        "fail_closed": True,
                        "routes": ["socks5h://127.0.0.1:7891"],
                        "scopes": {"media.download": "proxy"},
                    }
                ),
            )

            def installer(extension):
                calls.append("install")
                return ExtensionInstallResult(
                    installed=True,
                    distribution=extension.distribution,
                    version=extension.version,
                )

            def doctor(config):
                calls.append("doctor")
                self.assertIn(
                    "dreaife.proxy-router",
                    config.extensions.enabled,
                )
                self.assertTrue(
                    config.extensions.settings[
                        "dreaife.proxy-router"
                    ].config_file.is_file()
                )

            with mock.patch(
                "ytb_tg_backup.extension_management.prepare_extension_setup",
                return_value=prepared,
            ):
                result = enable_extension(
                    "proxy",
                    config_path,
                    installer=installer,
                    doctor=doctor,
                    restarter=lambda _path: calls.append("restart") or True,
                )

            self.assertEqual(calls, ["install", "doctor", "restart"])
            self.assertTrue(result.installed)
            self.assertTrue(result.configured)
            self.assertTrue(result.enabled)
            self.assertTrue(result.service_restarted)
            self.assertEqual(config_path.read_bytes(), original)
            sidecar = extension_sidecar_path(config_path)
            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result.config_path.stat().st_mode & 0o777, 0o600)
            proxy_raw = tomllib.loads(result.config_path.read_text(encoding="utf-8"))
            self.assertEqual(proxy_raw["scopes"]["media.download"], "proxy")
            loaded = load_config(config_path)
            self.assertEqual(
                loaded.extensions.enabled,
                ("dreaife.proxy-router",),
            )

    def test_doctor_failure_removes_new_state_and_private_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            prepared = PreparedExtensionSetup(
                manifest=ExtensionSetupManifest(
                    extension_id="dreaife.proxy-router",
                    config_filename="proxy-router.toml",
                ),
                result=ExtensionSetupResult(config={"routes": []}),
            )
            install_result = ExtensionInstallResult(
                installed=True,
                distribution="asmr-tg-backup-ext-proxy-router",
                version="0.2.0",
            )
            with (
                mock.patch(
                    "ytb_tg_backup.extension_management.prepare_extension_setup",
                    return_value=prepared,
                ),
                self.assertRaisesRegex(
                    ExtensionManagementError,
                    "runtime rejected setup",
                ),
            ):
                enable_extension(
                    "proxy-router",
                    config_path,
                    installer=lambda _extension: install_result,
                    doctor=lambda _config: (_ for _ in ()).throw(
                        ValueError("runtime rejected setup")
                    ),
                    restarter=lambda _path: self.fail("must not restart"),
                )

            self.assertFalse(extension_sidecar_path(config_path).exists())
            self.assertFalse(
                (root / "extensions" / "config" / "proxy-router.toml").exists()
            )
            self.assertEqual(load_config(config_path).extensions.enabled, ())

    def test_partial_private_write_failure_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            prepared = PreparedExtensionSetup(
                manifest=ExtensionSetupManifest(
                    extension_id="dreaife.proxy-router",
                    config_filename="proxy-router.toml",
                ),
                result=ExtensionSetupResult(
                    config={"routes": ["socks5h://127.0.0.1:7891"]}
                ),
            )

            def write_then_fail(path, content):
                real_replace_private_file(path, content)
                raise OSError("fsync failed after replace")

            with (
                mock.patch(
                    "ytb_tg_backup.extension_management.prepare_extension_setup",
                    return_value=prepared,
                ),
                mock.patch(
                    "ytb_tg_backup.extension_management.replace_private_file",
                    side_effect=write_then_fail,
                ),
                self.assertRaisesRegex(ExtensionManagementError, "fsync failed"),
            ):
                enable_extension(
                    "proxy",
                    config_path,
                    installer=self._installed_result,
                    doctor=lambda _config: self.fail("doctor must not run"),
                    restarter=lambda _path: self.fail("must not restart"),
                )

            self.assertFalse(
                (root / "extensions" / "config" / "proxy-router.toml").exists()
            )
            self.assertFalse(extension_sidecar_path(config_path).exists())

    def test_restart_failure_restores_previous_sidecar_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            sidecar = extension_sidecar_path(config_path)
            sidecar.write_text(
                "# Managed by `asmr-tg-backup extensions`.\n"
                "schema = 1\n\n"
                "[extensions]\n"
                "enabled = []\n",
                encoding="utf-8",
            )
            old_sidecar = sidecar.read_bytes()
            private = root / "extensions" / "config" / "proxy-router.toml"
            private.parent.mkdir(parents=True)
            private.write_text('routes = ["http://old:8080"]\n', encoding="utf-8")
            old_private = private.read_bytes()
            prepared = PreparedExtensionSetup(
                manifest=ExtensionSetupManifest(
                    extension_id="dreaife.proxy-router",
                    config_filename="proxy-router.toml",
                ),
                result=ExtensionSetupResult(
                    config={"routes": ["http://new:8080"]}
                ),
            )
            restored: list[Path] = []
            with (
                mock.patch(
                    "ytb_tg_backup.extension_management.prepare_extension_setup",
                    return_value=prepared,
                ),
                self.assertRaisesRegex(ExtensionManagementError, "restart failed"),
            ):
                enable_extension(
                    "proxy-router",
                    config_path,
                    reconfigure=True,
                    installer=self._installed_result,
                    doctor=lambda _config: None,
                    restarter=lambda _path: (_ for _ in ()).throw(
                        RuntimeError("restart failed")
                    ),
                    service_restorer=lambda path: restored.append(path) or True,
                )

            self.assertEqual(sidecar.read_bytes(), old_sidecar)
            self.assertEqual(private.read_bytes(), old_private)
            self.assertEqual(restored, [config_path])

    def test_failed_service_restore_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            prepared = PreparedExtensionSetup(
                manifest=ExtensionSetupManifest(
                    extension_id="dreaife.niconico-origin",
                ),
                result=ExtensionSetupResult(),
            )
            with (
                mock.patch(
                    "ytb_tg_backup.extension_management.prepare_extension_setup",
                    return_value=prepared,
                ),
                self.assertRaisesRegex(
                    ExtensionManagementError,
                    "rollback incomplete: the previous managed service was not restored",
                ),
            ):
                enable_extension(
                    "nico",
                    config_path,
                    installer=lambda extension: ExtensionInstallResult(
                        installed=True,
                        distribution=extension.distribution,
                        version=extension.version,
                    ),
                    doctor=lambda _config: None,
                    restarter=lambda _path: (_ for _ in ()).throw(
                        RuntimeError("restart failed")
                    ),
                    service_restorer=lambda _path: False,
                )

    def test_second_enable_is_healthy_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = self._write_config(root)
            suggestion = OriginSuggestion(
                provider="niconico",
                kind="live_search",
                name="Niconico ASMR",
                external_id="ASMR",
            )
            prepared = PreparedExtensionSetup(
                manifest=ExtensionSetupManifest(
                    extension_id="dreaife.niconico-origin",
                    suggested_origins=(suggestion,),
                ),
                result=ExtensionSetupResult(),
            )
            with mock.patch(
                "ytb_tg_backup.extension_management.prepare_extension_setup",
                return_value=prepared,
            ):
                first = enable_extension(
                    "nico",
                    config_path,
                    installer=self._installed_result,
                    doctor=lambda _config: None,
                    restarter=lambda _path: False,
                )
            sidecar = extension_sidecar_path(config_path)
            before = sidecar.read_bytes()
            with mock.patch(
                "ytb_tg_backup.extension_management.prepare_extension_setup"
            ) as setup:
                second = enable_extension(
                    "niconico",
                    config_path,
                    installer=self._installed_result,
                    doctor=lambda _config: None,
                    restarter=lambda _path: self.fail("must not restart"),
                )

            self.assertEqual(first.suggested_origins, (suggestion,))
            self.assertTrue(second.already_enabled)
            self.assertEqual(sidecar.read_bytes(), before)
            setup.assert_not_called()

    @staticmethod
    def _installed_result(extension):
        return ExtensionInstallResult(
            installed=False,
            distribution=extension.distribution,
            version=extension.version,
        )

    @staticmethod
    def _write_config(root: Path) -> Path:
        config_path = root / "config.toml"
        sources_path = root / "sources.toml"
        data_dir = root / "data"
        config_path.write_text(
            "[app]\n"
            f'data_dir = "{data_dir}"\n\n'
            "[sources]\n"
            f'path = "{sources_path}"\n',
            encoding="utf-8",
        )
        sources_path.write_text(
            'version = 1\nsource_filter = ""\n',
            encoding="utf-8",
        )
        return config_path


if __name__ == "__main__":
    unittest.main()
