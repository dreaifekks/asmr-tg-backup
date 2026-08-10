from __future__ import annotations

import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup.config import ExtensionSettings, ExtensionsConfig
from ytb_tg_backup.extension_api import (
    EXTENSION_API_LEVEL,
    ExtensionError,
    ExtensionManifest,
    ExtensionSetupContext,
    ExtensionSetupManifest,
    ExtensionSetupResult,
    SourceProviderDefinition,
)
from ytb_tg_backup.extensions import (
    ExtensionHost,
    RuntimeBuilder,
    prepare_extension_setup,
)
from ytb_tg_backup.models import DiscoveryResult


class _Source:
    provider = "example"

    def discover(self, origin, checkpoint=None):
        return DiscoveryResult(items=[])


def _definition(provider: str = "example") -> SourceProviderDefinition:
    return SourceProviderDefinition(
        provider=provider,
        kinds=frozenset({"feed"}),
        default_kind="feed",
        adapter_factory=lambda _context: _Source(),
        validate_origin=lambda origin: origin,
        identity=lambda origin: (origin.provider, origin.kind, origin.external_id, ""),
        is_live_origin=lambda _origin: False,
        seed_content_kind=lambda _origin: None,
        poll_variant=lambda _origin: None,
    )


class _Extension:
    def __init__(self, extension_id: str, events: list[str]):
        self.manifest = ExtensionManifest(
            id=extension_id,
            version="1.0.0",
            api_level=EXTENSION_API_LEVEL,
            capabilities=frozenset({"source:example"}),
        )
        self.events = events

    def register(self, registrar, context) -> None:
        self.events.append(f"register:{context.extension_id}")
        registrar.add_source_provider(_definition(context.extension_id.rsplit(".", 1)[-1]))

    def start(self) -> None:
        self.events.append(f"start:{self.manifest.id}")

    def stop(self) -> None:
        self.events.append(f"stop:{self.manifest.id}")


class ExtensionHostTest(unittest.TestCase):
    def test_only_enabled_entry_points_are_imported_and_lifecycle_is_reversed(self):
        events: list[str] = []
        first = _Extension("example.first", events)
        second = _Extension("example.second", events)
        disabled = mock.Mock()
        entry_points = {
            "example.first": mock.Mock(load=mock.Mock(return_value=lambda: first)),
            "example.second": mock.Mock(load=mock.Mock(return_value=lambda: second)),
            "example.disabled": disabled,
        }
        config = ExtensionsConfig(
            enabled=("example.first", "example.second"),
            settings={
                "example.first": ExtensionSettings("example.first"),
                "example.second": ExtensionSettings("example.second"),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            host = ExtensionHost(
                config,
                data_dir=Path(tmp),
                logger=logging.getLogger("test.extensions"),
            )
            with mock.patch.object(host, "_entry_points", return_value=entry_points):
                builder = RuntimeBuilder()
                host.load_into(builder)
                host.start()
                host.stop()

        disabled.load.assert_not_called()
        self.assertEqual(
            events,
            [
                "register:example.first",
                "register:example.second",
                "start:example.first",
                "start:example.second",
                "stop:example.second",
                "stop:example.first",
            ],
        )

    def test_required_missing_extension_fails_before_runtime_start(self):
        config = ExtensionsConfig(
            enabled=("example.missing",),
            settings={
                "example.missing": ExtensionSettings(
                    "example.missing",
                    required=True,
                )
            },
        )
        host = ExtensionHost(
            config,
            data_dir=Path("/tmp/extensions-test"),
            logger=logging.getLogger("test.extensions"),
        )
        with (
            mock.patch.object(host, "_entry_points", return_value={}),
            self.assertRaisesRegex(ExtensionError, "required extension"),
        ):
            host.load_into(RuntimeBuilder())

    def test_config_file_is_private_input_and_inline_options_override_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extension.toml"
            path.write_text("route = 'file'\n[scopes]\nmedia_download = 'proxy'\n")
            settings = ExtensionSettings(
                id="example.extension",
                config_file=path,
                options={"route": "inline"},
            )
            merged = ExtensionHost._settings_config(settings)

        self.assertEqual(merged["route"], "inline")
        self.assertEqual(merged["scopes"]["media_download"], "proxy")

    def test_static_setup_manifest_is_loaded_without_runtime_registration(self):
        manifest = ExtensionSetupManifest(extension_id="example.extension")
        entry_point = mock.Mock()
        entry_point.name = "example.extension"
        entry_point.load.return_value = lambda: manifest
        prompts = mock.Mock()
        context = ExtensionSetupContext(
            interactive=True,
            reconfigure=False,
            existing_config={},
            prompts=prompts,
        )
        with mock.patch(
            "ytb_tg_backup.extensions.metadata.entry_points",
            return_value=[entry_point],
        ):
            prepared = prepare_extension_setup("example.extension", context)

        self.assertIs(prepared.manifest, manifest)
        self.assertIsNone(prepared.result.config)
        prompts.assert_not_called()

    def test_configurator_result_and_manifest_id_are_validated(self):
        class Configurator:
            manifest = ExtensionSetupManifest(extension_id="other.extension")

            def configure(self, _context):
                return ExtensionSetupResult(config={})

        entry_point = mock.Mock()
        entry_point.name = "example.extension"
        entry_point.load.return_value = lambda: Configurator()
        context = ExtensionSetupContext(
            interactive=True,
            reconfigure=False,
            existing_config={},
            prompts=mock.Mock(),
        )
        with (
            mock.patch(
                "ytb_tg_backup.extensions.metadata.entry_points",
                return_value=[entry_point],
            ),
            self.assertRaisesRegex(ExtensionError, "manifest for 'other.extension'"),
        ):
            prepare_extension_setup("example.extension", context)


class RuntimeBuilderTest(unittest.TestCase):
    def test_capability_ownership_rejects_duplicate_provider_and_policy(self):
        builder = RuntimeBuilder()
        builder.scoped("one").add_source_provider(_definition())
        with self.assertRaisesRegex(ExtensionError, "already registered"):
            builder.scoped("two").add_source_provider(_definition())

        builder.scoped("one").set_connection_policy(mock.Mock())
        with self.assertRaisesRegex(ExtensionError, "already registered"):
            builder.scoped("two").set_connection_policy(mock.Mock())


if __name__ == "__main__":
    unittest.main()
