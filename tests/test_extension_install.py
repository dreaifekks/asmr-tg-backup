from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ytb_tg_backup.extension_catalog import resolve_trusted_extension
from ytb_tg_backup.extension_install import (
    ExtensionInstallError,
    InstallTarget,
    detect_install_target,
    ensure_extension_installed,
    validate_extension_compatibility,
    verify_extension_distribution,
)
from ytb_tg_backup.extension_install import _redact_subprocess_detail


class ExtensionInstallTest(unittest.TestCase):
    def test_catalog_matches_only_trusted_exact_names(self):
        self.assertEqual(
            resolve_trusted_extension("proxy").extension_id,
            "dreaife.proxy-router",
        )
        self.assertEqual(
            resolve_trusted_extension("niconico-origin").distribution,
            "asmr-tg-backup-ext-niconico-origin",
        )
        with self.assertRaisesRegex(ValueError, "unknown trusted extension"):
            resolve_trusted_extension("proxy-router; touch /tmp/nope")

    def test_catalog_compatibility_is_checked_before_install(self):
        extension = resolve_trusted_extension("proxy")
        self.assertEqual(extension.core_requires, ">=0.6.0,<0.7.0")
        with self.assertRaisesRegex(ExtensionInstallError, "process is 0.5.0"):
            validate_extension_compatibility(extension, core_version="0.5.0")
        with self.assertRaisesRegex(ExtensionInstallError, "runtime API 1"):
            validate_extension_compatibility(extension, runtime_api_level=2)
        with self.assertRaisesRegex(ExtensionInstallError, "setup API 1"):
            validate_extension_compatibility(extension, setup_api_level=2)

    def test_detects_pipx_from_current_prefix_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "asmr-tg-backup"
            prefix.mkdir()
            (prefix / "pipx_metadata.json").write_text(
                json.dumps({"main_package": {"package": "asmr-tg-backup"}}),
                encoding="utf-8",
            )
            with mock.patch(
                "ytb_tg_backup.extension_install.shutil.which",
                return_value="/usr/bin/pipx",
            ):
                target = detect_install_target(
                    prefix=prefix,
                    base_prefix=Path("/usr"),
                    python=prefix / "bin/python",
                    environ={},
                    container=False,
                )

        self.assertEqual(target.kind, "pipx")
        self.assertEqual(target.environment, "asmr-tg-backup")

    def test_detects_virtual_environment_only_with_working_pip(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "venv"
            prefix.mkdir()
            (prefix / "pyvenv.cfg").write_text("home = /usr/bin\n")
            runner = mock.Mock(
                return_value=subprocess.CompletedProcess([], 0, "pip 25", "")
            )
            target = detect_install_target(
                prefix=prefix,
                base_prefix=Path("/usr"),
                python=prefix / "bin/python",
                environ={},
                container=False,
                runner=runner,
            )

        self.assertEqual(target.kind, "venv")
        self.assertEqual(runner.call_args.args[0][-2:], ["pip", "--version"])

    def test_pipx_install_uses_inject_and_static_verification(self):
        extension = resolve_trusted_extension("proxy")
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, "installed", "")
        )
        target = InstallTarget(
            kind="pipx",
            python=Path("/venv/bin/python"),
            pipx=Path("/usr/bin/pipx"),
            environment="asmr-tg-backup",
        )
        with (
            mock.patch(
                "ytb_tg_backup.extension_install._distribution_version",
                return_value=None,
            ),
            mock.patch(
                "ytb_tg_backup.extension_install.verify_extension_distribution"
            ) as verify,
        ):
            result = ensure_extension_installed(
                extension,
                runner=runner,
                target=target,
            )

        self.assertTrue(result.installed)
        self.assertEqual(
            runner.call_args.args[0],
            [
                "/usr/bin/pipx",
                "inject",
                "asmr-tg-backup",
                "asmr-tg-backup-ext-proxy-router==0.2.0",
            ],
        )
        verify.assert_called_once_with(extension)

    def test_container_never_installs_missing_extension(self):
        extension = resolve_trusted_extension("nico")
        runner = mock.Mock()
        target = InstallTarget(
            kind="container",
            python=Path("/usr/bin/python"),
        )
        with (
            mock.patch(
                "ytb_tg_backup.extension_install._distribution_version",
                return_value=None,
            ),
            self.assertRaisesRegex(ExtensionInstallError, "derived image"),
        ):
            ensure_extension_installed(extension, runner=runner, target=target)
        runner.assert_not_called()

    def test_static_verification_never_imports_entry_points(self):
        extension = resolve_trusted_extension("proxy")
        distribution = SimpleNamespace(
            metadata={"Name": extension.distribution},
            version=extension.version,
        )
        runtime = mock.Mock(
            name=extension.extension_id,
            value=extension.runtime_entry_point,
            dist=SimpleNamespace(name=extension.distribution),
        )
        runtime.name = extension.extension_id
        runtime.value = extension.runtime_entry_point
        runtime.dist = SimpleNamespace(name=extension.distribution)
        setup_entry = mock.Mock()
        setup_entry.name = extension.extension_id
        setup_entry.value = extension.setup_entry_point
        setup_entry.dist = SimpleNamespace(name=extension.distribution)

        def entry_points(*, group):
            if group == "asmr_tg_backup.extensions":
                return [runtime]
            return [setup_entry]

        with (
            mock.patch(
                "ytb_tg_backup.extension_install.metadata.distribution",
                return_value=distribution,
            ),
            mock.patch(
                "ytb_tg_backup.extension_install.metadata.entry_points",
                side_effect=entry_points,
            ),
        ):
            verify_extension_distribution(extension)

        runtime.load.assert_not_called()
        setup_entry.load.assert_not_called()

    def test_install_error_detail_redacts_index_credentials(self):
        detail = _redact_subprocess_detail(
            "failed https://user:private-token@packages.example/simple"
        )

        self.assertNotIn("private-token", detail)
        self.assertIn("https://<redacted>@packages.example/simple", detail)


if __name__ == "__main__":
    unittest.main()
