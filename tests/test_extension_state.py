from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ytb_tg_backup.config import load_config
from ytb_tg_backup.extension_state import (
    ExtensionStateError,
    ManagedExtensionSettings,
    ManagedExtensionState,
    extension_private_config_path,
    extension_sidecar_path,
    render_extension_config,
    render_managed_extension_state,
)


class ExtensionStateTest(unittest.TestCase):
    def test_main_config_values_override_managed_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "worker.toml"
            config_path.write_text(
                "[extensions]\n"
                'enabled = ["manual.extension", "DREAIFE.PROXY-ROUTER"]\n\n'
                '[extensions."dreaife.proxy-router"]\n'
                'config_file = "manual-proxy.toml"\n'
                "required = false\n",
                encoding="utf-8",
            )
            state = ManagedExtensionState(
                enabled=("dreaife.proxy-router",),
                settings={
                    "dreaife.proxy-router": ManagedExtensionSettings(
                        required=True,
                        config_file="extensions/proxy-router.toml",
                    )
                },
            )
            sidecar = extension_sidecar_path(config_path)
            sidecar.write_bytes(render_managed_extension_state(state))

            config = load_config(config_path)

        self.assertEqual(
            config.extensions.enabled,
            ("manual.extension", "dreaife.proxy-router"),
        )
        settings = config.extensions.settings["dreaife.proxy-router"]
        self.assertFalse(settings.required)
        self.assertEqual(settings.config_file, root / "manual-proxy.toml")

    def test_sidecar_name_is_scoped_to_each_main_config(self):
        self.assertEqual(
            extension_sidecar_path(Path("/tmp/config.toml")),
            Path("/tmp/config.extensions.toml"),
        )
        self.assertEqual(
            extension_sidecar_path(Path("/tmp/prod.toml")),
            Path("/tmp/prod.extensions.toml"),
        )

    def test_private_config_is_scoped_to_each_main_config(self):
        self.assertEqual(
            extension_private_config_path(
                Path("/tmp/prod.toml"),
                "dreaife.proxy-router",
            ),
            Path("/tmp/extensions/prod/dreaife.proxy-router.toml"),
        )
        self.assertNotEqual(
            extension_private_config_path(
                Path("/tmp/prod.toml"),
                "dreaife.proxy-router",
            ),
            extension_private_config_path(
                Path("/tmp/test.toml"),
                "dreaife.proxy-router",
            ),
        )

    def test_nested_extension_config_quotes_dotted_scope_keys(self):
        rendered = render_extension_config(
            {
                "fail_closed": True,
                "scopes": {
                    "media.download": "proxy",
                    "telegram.delivery.bot_api": "direct",
                },
            }
        ).decode("utf-8")

        self.assertIn('[scopes]\n"media.download" = "proxy"', rendered)
        self.assertIn('"telegram.delivery.bot_api" = "direct"', rendered)

    def test_symlink_sidecar_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            target = root / "target.toml"
            target.write_text(
                "schema = 1\n[extensions]\nenabled = []\n",
                encoding="utf-8",
            )
            extension_sidecar_path(config_path).symlink_to(target)

            with self.assertRaisesRegex(ExtensionStateError, "refusing symlink"):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
