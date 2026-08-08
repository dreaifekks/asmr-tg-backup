from dataclasses import replace
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import ytb_tg_backup.source_catalog as source_catalog_module
from ytb_tg_backup.config import load_config
from ytb_tg_backup.models import Origin
from ytb_tg_backup.source_catalog import (
    SourceCatalog,
    SourceCatalogError,
    SourceCatalogManager,
    load_source_catalog,
    render_source_catalog,
    validate_source_catalog,
    write_source_catalog,
)
from ytb_tg_backup.source_filter import DEFAULT_SOURCE_FILTER_PATTERN
from ytb_tg_backup.store import Store


def _youtube(
    origin_id: str = "youtube-main",
    *,
    external_id: str = "UC123",
    name: str = "YouTube Main",
    **changes: object,
) -> Origin:
    values: dict[str, object] = {
        "id": origin_id,
        "provider": "youtube",
        "kind": "uploads",
        "name": name,
        "external_id": external_id,
        "options": {"routes": ["channel", "live"], "future_option": "kept"},
    }
    values.update(changes)
    return Origin(**values)  # type: ignore[arg-type]


def _twitch(
    origin_id: str = "twitch-main",
    *,
    external_id: str = "ExampleStreamer",
    **changes: object,
) -> Origin:
    values: dict[str, object] = {
        "id": origin_id,
        "provider": "twitch",
        "kind": "vods",
        "name": "Twitch Main",
        "external_id": external_id,
        "options": {"recording_mode": "vod", "future_option": 7},
    }
    values.update(changes)
    return Origin(**values)  # type: ignore[arg-type]


class SourceCatalogTest(unittest.TestCase):
    def test_config_resolves_sources_path_next_to_config_with_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "nested" / "config.toml"
            config_path.parent.mkdir()
            config_path.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                config = load_config(config_path)
                self.assertEqual(config.sources.path, config_path.parent / "sources.toml")
                self.assertFalse(config.legacy_sources_declared)

            config_path.write_text('[sources]\npath = "catalog/custom.toml"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                config = load_config(config_path)
                self.assertEqual(
                    config.sources.path,
                    config_path.parent / "catalog/custom.toml",
                )
                self.assertFalse(config.legacy_sources_declared)

            with mock.patch.dict(
                os.environ,
                {"ASMR_TG_BACKUP_SOURCES_PATH": "from-env.toml"},
                clear=True,
            ):
                self.assertEqual(
                    load_config(config_path).sources.path,
                    config_path.parent / "from-env.toml",
                )

            config_path.write_text("channels = []\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertTrue(load_config(config_path).legacy_sources_declared)

    def test_round_trip_preserves_flat_unknown_options_and_writes_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.toml"
            catalog = SourceCatalog(
                source_filter="ASMR|sleep",
                origins=(_youtube(), _twitch()),
            )

            write_source_catalog(path, catalog)
            loaded = load_source_catalog(path)

            self.assertEqual(loaded, validate_source_catalog(catalog))
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode),
                0o600,
            )
            rendered = render_source_catalog(loaded)
            self.assertIn('future_option = "kept"', rendered)
            self.assertIn("future_option = 7", rendered)

    def test_validation_rejects_bad_regex_nested_options_and_duplicate_identity(self):
        with self.assertRaisesRegex(SourceCatalogError, "invalid source regex"):
            validate_source_catalog(SourceCatalog(source_filter="["))
        with self.assertRaisesRegex(SourceCatalogError, "flat TOML value"):
            validate_source_catalog(
                SourceCatalog(origins=(_youtube(options={"nested": {"key": "value"}}),))
            )
        with self.assertRaisesRegex(SourceCatalogError, "same source identity"):
            validate_source_catalog(
                SourceCatalog(
                    origins=(
                        _twitch("one", external_id="ExampleStreamer"),
                        _twitch("two", external_id="examplestreamer"),
                    )
                )
            )

    def test_validation_rejects_unsupported_source_adapters(self):
        with self.assertRaisesRegex(SourceCatalogError, "unsupported provider 'other'"):
            validate_source_catalog(
                SourceCatalog(
                    origins=(
                        Origin("other", "other", "feed", "Other", "source"),
                    )
                )
            )
        with self.assertRaisesRegex(
            SourceCatalogError,
            "unsupported kind 'clips' for provider 'twitch'",
        ):
            validate_source_catalog(
                SourceCatalog(
                    origins=(
                        Origin("clips", "twitch", "clips", "Clips", "streamer"),
                    )
                )
            )

    def test_validation_checks_rss_option_types(self):
        base = Origin("rss", "rss", "feed", "Feed", "https://example.test/feed")
        with self.assertRaisesRegex(
            SourceCatalogError,
            "allowed_media_hosts must be an array of strings",
        ):
            validate_source_catalog(
                SourceCatalog(
                    origins=(replace(base, options={"allowed_media_hosts": "example.test"}),)
                )
            )
        with self.assertRaisesRegex(
            SourceCatalogError,
            "allow_private_media must be true or false",
        ):
            validate_source_catalog(
                SourceCatalog(
                    origins=(replace(base, options={"allow_private_media": "yes"}),)
                )
            )

        validated = validate_source_catalog(
            SourceCatalog(
                origins=(
                    replace(
                        base,
                        options={
                            "allowed_media_hosts": ["media.example.test"],
                            "allow_private_media": False,
                        },
                    ),
                )
            )
        )
        self.assertEqual(
            validated.origins[0].options["allowed_media_hosts"],
            ["media.example.test"],
        )

    def test_load_rejects_non_string_origin_schema_fields(self):
        cases = {
            "id": 'id = 123\nprovider = "youtube"\nexternal_id = "UC123"',
            "provider": 'id = "main"\nprovider = 123\nexternal_id = "UC123"',
            "kind": 'id = "main"\nprovider = "youtube"\nkind = false\nexternal_id = "UC123"',
            "name": 'id = "main"\nprovider = "youtube"\nname = false\nexternal_id = "UC123"',
            "external_id": 'id = "main"\nprovider = "youtube"\nexternal_id = 456',
            "bootstrap": 'id = "main"\nprovider = "youtube"\nexternal_id = "UC123"\nbootstrap = 1',
            "credential_ref": 'id = "main"\nprovider = "youtube"\nexternal_id = "UC123"\ncredential_ref = 99',
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.toml"
            for field, origin in cases.items():
                with self.subTest(field=field):
                    path.write_text(
                        f'version = 1\nsource_filter = ""\n\n[[origins]]\n{origin}\n',
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        SourceCatalogError,
                        rf"origins\[0\]\.{field} must be a string",
                    ):
                        load_source_catalog(path)

    def test_twitch_vods_live_and_vod_modes_are_distinct_source_identities(self):
        catalog = validate_source_catalog(
            SourceCatalog(
                origins=(
                    _twitch(
                        "live",
                        external_id="ExampleStreamer",
                        options={"recording_mode": "live"},
                    ),
                    _twitch(
                        "vod",
                        external_id="examplestreamer",
                        options={"recording_mode": "vod"},
                    ),
                )
            )
        )

        self.assertEqual([origin.id for origin in catalog.origins], ["live", "vod"])

    def test_explicit_legacy_snapshot_replaces_config_rows_and_preserves_durable_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            store.upsert_origin(_youtube("stale-config"), managed_by="config")
            store.upsert_origin(
                _youtube("catalog-main", external_id="UC777"),
                managed_by="catalog",
            )
            store.upsert_origin(_twitch(), managed_by="control")
            store.set_bot_state("source_filter_pattern", "sleep")
            manager = SourceCatalogManager(root / "sources.toml", store, 4)

            catalog = manager.ensure(
                legacy_origins=(_youtube("current-config", external_id="UC999"),),
                legacy_declared=True,
            )

            self.assertEqual(
                [item.id for item in catalog.origins],
                ["catalog-main", "current-config", "twitch-main"],
            )
            self.assertEqual(catalog.source_filter, "sleep")
            rows = store.conn.execute(
                "SELECT id, managed_by FROM origins ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [(str(row["id"]), str(row["managed_by"])) for row in rows],
                [
                    ("catalog-main", "catalog"),
                    ("current-config", "catalog"),
                    ("twitch-main", "catalog"),
                ],
            )
            store.close()

    def test_missing_catalog_without_legacy_declaration_recovers_all_database_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            store.upsert_origin(_youtube(), managed_by="config")
            store.upsert_origin(_twitch(), managed_by="control")
            manager = SourceCatalogManager(root / "sources.toml", store)

            catalog = manager.ensure()

            self.assertEqual(
                [origin.id for origin in catalog.origins],
                ["twitch-main", "youtube-main"],
            )
            store.close()

    def test_explicit_legacy_merge_deduplicates_exact_durable_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            store.upsert_origin(_twitch(), managed_by="control")
            manager = SourceCatalogManager(root / "sources.toml", store)

            catalog = manager.ensure(
                legacy_origins=(_twitch(),),
                legacy_declared=True,
            )

            self.assertEqual(catalog.origins, (_twitch(),))
            store.close()

    def test_explicit_legacy_merge_rejects_durable_source_conflicts(self):
        cases = {
            "same id": _twitch(name="Changed"),
            "same source identity": _twitch("renamed"),
        }
        for label, legacy_origin in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Store(root / "state.db")
                store.initialize()
                store.upsert_origin(_twitch(), managed_by="control")
                manager = SourceCatalogManager(root / "sources.toml", store)

                with self.assertRaisesRegex(SourceCatalogError, "conflicts with"):
                    manager.ensure(
                        legacy_origins=(legacy_origin,),
                        legacy_declared=True,
                    )

                self.assertFalse(manager.path.exists())
                self.assertEqual(store.list_origins(managed_by="control"), [_twitch()])
                store.close()

    def test_ensure_falls_back_to_legacy_config_and_default_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            manager = SourceCatalogManager(root / "sources.toml", store)

            catalog = manager.ensure(
                legacy_origins=(_youtube(),),
                legacy_declared=True,
            )

            self.assertEqual(catalog.origins, (_youtube(),))
            self.assertEqual(catalog.source_filter, DEFAULT_SOURCE_FILTER_PATTERN)
            self.assertEqual(
                store.get_bot_state("source_filter_pattern"),
                DEFAULT_SOURCE_FILTER_PATTERN,
            )
            store.close()

    def test_existing_catalog_ignores_later_legacy_declarations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            manager = SourceCatalogManager(root / "sources.toml", store)
            manager.ensure(
                legacy_origins=(_youtube(),),
                legacy_declared=True,
            )

            catalog = manager.ensure(
                legacy_origins=(_youtube("replacement", external_id="UC999"),),
                legacy_declared=True,
            )

            self.assertEqual(catalog.origins, (_youtube(),))
            store.close()

    def test_ensure_and_apply_harden_existing_catalog_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            manager = SourceCatalogManager(root / "sources.toml", store)
            write_source_catalog(manager.path, SourceCatalog(origins=(_youtube(),)))

            manager.path.chmod(0o644)
            manager.ensure()
            self.assertEqual(stat.S_IMODE(manager.path.stat().st_mode), 0o600)

            manager.path.chmod(0o644)
            manager.apply()
            self.assertEqual(stat.S_IMODE(manager.path.stat().st_mode), 0o600)
            store.close()

    def test_apply_reports_catalog_permission_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            manager = SourceCatalogManager(root / "sources.toml", store)
            write_source_catalog(manager.path, SourceCatalog(origins=(_youtube(),)))

            with (
                mock.patch.object(Path, "chmod", side_effect=PermissionError("denied")),
                self.assertRaisesRegex(
                    SourceCatalogError,
                    "cannot set source catalog permissions to 0600",
                ),
            ):
                manager.apply()
            store.close()

    def test_mutations_preserve_unknown_options_and_reconcile_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            manager = SourceCatalogManager(root / "sources.toml", store)
            manager.ensure(legacy_origins=(_twitch(),), legacy_declared=True)

            manager.set_recording_mode("twitch-main", "live")
            manager.set_enabled("twitch-main", False)
            manager.rename("twitch-main", "Renamed")
            manager.request_backfill("twitch-main")
            manager.set_filter("")
            origin = manager.list()[0]

            self.assertEqual(origin.options["recording_mode"], "live")
            self.assertEqual(origin.options["future_option"], 7)
            self.assertFalse(origin.enabled)
            self.assertEqual(origin.name, "Renamed")
            self.assertEqual(origin.bootstrap, "all")
            row = store.conn.execute(
                "SELECT name, enabled, bootstrap, managed_by FROM origins WHERE id='twitch-main'"
            ).fetchone()
            self.assertEqual(
                (row["name"], row["enabled"], row["bootstrap"], row["managed_by"]),
                ("Renamed", 0, "all", "catalog"),
            )

            manager.add(_youtube())
            self.assertEqual([item.id for item in manager.list()], ["twitch-main", "youtube-main"])
            manager.delete("twitch-main")
            self.assertEqual([item.id for item in manager.list()], ["youtube-main"])
            self.assertIsNone(
                store.conn.execute(
                    "SELECT 1 FROM origins WHERE id='twitch-main'"
                ).fetchone()
            )
            store.close()

    def test_apply_rejects_an_in_place_identity_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            manager = SourceCatalogManager(root / "sources.toml", store)
            manager.ensure(legacy_origins=(_youtube(),), legacy_declared=True)
            write_source_catalog(
                manager.path,
                SourceCatalog(origins=(_youtube(external_id="UC-DIFFERENT"),)),
            )

            with self.assertRaisesRegex(SourceCatalogError, "identity is immutable"):
                manager.apply()

            row = store.conn.execute(
                "SELECT external_id FROM origins WHERE id='youtube-main'"
            ).fetchone()
            self.assertEqual(row["external_id"], "UC123")
            store.close()

    def test_failed_mutation_restores_previous_catalog_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            manager = SourceCatalogManager(root / "sources.toml", store)
            manager.ensure(legacy_origins=(_youtube(),), legacy_declared=True)
            before = manager.path.read_bytes()

            with (
                mock.patch.object(
                    store,
                    "reconcile_source_catalog",
                    side_effect=ValueError("database rejected catalog"),
                ),
                self.assertRaisesRegex(SourceCatalogError, "database rejected catalog"),
            ):
                manager.rename("youtube-main", "Not persisted")

            self.assertEqual(manager.path.read_bytes(), before)
            store.close()

    def test_failed_atomic_replace_after_install_restores_previous_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state.db")
            store.initialize()
            manager = SourceCatalogManager(root / "sources.toml", store)
            manager.ensure(legacy_origins=(_youtube(),), legacy_declared=True)
            before = manager.path.read_bytes()
            real_atomic_replace = source_catalog_module._atomic_replace
            calls = 0

            def fail_first_replace_after_install(path: Path, content: bytes) -> None:
                nonlocal calls
                calls += 1
                real_atomic_replace(path, content)
                if calls == 1:
                    raise OSError("directory fsync failed")

            with (
                mock.patch.object(
                    source_catalog_module,
                    "_atomic_replace",
                    side_effect=fail_first_replace_after_install,
                ),
                self.assertRaisesRegex(OSError, "directory fsync failed"),
            ):
                manager.rename("youtube-main", "Not persisted")

            self.assertEqual(calls, 2)
            self.assertEqual(manager.path.read_bytes(), before)
            row = store.conn.execute(
                "SELECT name FROM origins WHERE id='youtube-main'"
            ).fetchone()
            self.assertEqual(row["name"], "YouTube Main")
            store.close()

    def test_store_reconcile_is_one_transaction_and_preserves_legacy_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            store.initialize()
            store.upsert_origin(_youtube("first"), managed_by="config")
            store.upsert_origin(_twitch("second"), managed_by="control")
            store.upsert_origin(
                Origin("legacy", "youtube", "legacy_feed", "Legacy", "old-feed"),
                managed_by="legacy",
            )

            with self.assertRaisesRegex(ValueError, "identity is immutable"):
                store.reconcile_source_catalog(
                    (
                        _youtube("first", name="Changed"),
                        _twitch("second", external_id="different"),
                    ),
                    "sleep",
                )

            first = store.conn.execute(
                "SELECT name, managed_by FROM origins WHERE id='first'"
            ).fetchone()
            self.assertEqual((first["name"], first["managed_by"]), ("YouTube Main", "config"))
            self.assertIsNone(store.get_bot_state("source_filter_pattern"))

            store.reconcile_source_catalog((_youtube("first"),), "")
            rows = store.conn.execute(
                "SELECT id, managed_by FROM origins ORDER BY id"
            ).fetchall()
            self.assertEqual(
                [(row["id"], row["managed_by"]) for row in rows],
                [("first", "catalog"), ("legacy", "legacy")],
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
