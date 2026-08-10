from contextlib import redirect_stderr, redirect_stdout
from importlib import resources
import io
from pathlib import Path
import signal
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock

from ytb_tg_backup import __version__
from ytb_tg_backup.cli import main
from ytb_tg_backup.config import load_config
from ytb_tg_backup.extension_management import ExtensionManagementError
from ytb_tg_backup.models import Origin
from ytb_tg_backup.source_catalog import (
    SourceCatalog,
    load_source_catalog,
    write_source_catalog,
)
from ytb_tg_backup.store import Store


class CliTest(unittest.TestCase):
    def _source_config(self, root: Path):
        return SimpleNamespace(
            path=root / "config.toml",
            db_path=root / "state.db",
            app=SimpleNamespace(log_level="INFO", max_attempts=7),
            sources=SimpleNamespace(path=root / "sources.toml"),
            origins=[],
            legacy_sources_declared=False,
        )

    def test_version_does_not_load_config(self):
        output = io.StringIO()
        with (
            mock.patch("ytb_tg_backup.cli.load_config") as load_config,
            redirect_stdout(output),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), f"asmr-tg-backup {__version__}\n")
        load_config.assert_not_called()

    def test_extensions_enable_is_single_command_orchestration(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            extension = SimpleNamespace(
                slug="proxy-router",
                extension_id="dreaife.proxy-router",
                distribution="asmr-tg-backup-ext-proxy-router",
                version="0.2.0",
            )
            result = SimpleNamespace(
                extension=extension,
                already_enabled=False,
                config_path=Path(tmp) / "extensions/proxy-router.toml",
                service_restarted=True,
                suggested_origins=(),
            )
            stdout = io.StringIO()
            with (
                mock.patch(
                    "ytb_tg_backup.cli.enable_extension",
                    return_value=result,
                ) as enable,
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "extensions",
                        "enable",
                        "proxy",
                        "--config",
                        str(config_path),
                    ]
                )

        self.assertEqual(exit_code, 0)
        enable.assert_called_once_with(
            "proxy",
            config_path,
            reconfigure=False,
            restart_service=True,
        )
        self.assertIn("enabled extension: proxy-router", stdout.getvalue())
        self.assertIn("restarted asmr-tg-backup.service", stdout.getvalue())

    def test_extensions_enable_runtime_failure_returns_one(self):
        stderr = io.StringIO()
        with (
            mock.patch(
                "ytb_tg_backup.cli.enable_extension",
                side_effect=ExtensionManagementError("package install failed"),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = main(
                [
                    "extensions",
                    "enable",
                    "niconico",
                    "--config",
                    "/tmp/config.toml",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("package install failed", stderr.getvalue())

    def test_init_config_creates_private_file_before_loading_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "private" / "nested" / "config.toml"
            stdout = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config") as load_config,
                redirect_stdout(stdout),
            ):
                result = main(["init-config", "--output", str(output_path)])

            expected = resources.files("ytb_tg_backup").joinpath("config.example.toml").read_bytes()
            self.assertEqual(result, 0)
            self.assertEqual(output_path.read_bytes(), expected)
            self.assertEqual(output_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(stdout.getvalue(), f"created {output_path}\n")
            load_config.assert_not_called()

    def test_init_config_refuses_to_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "config.toml"
            output_path.write_text("keep-me", encoding="utf-8")
            stderr = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config") as load_config,
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["init-config", "--output", str(output_path)])

            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "keep-me")
            self.assertIn("refusing to overwrite existing config", stderr.getvalue())
            load_config.assert_not_called()

    def test_setup_defaults_to_mtproto_with_official_build_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "config-home"
            data_home = Path(tmp) / "data-home"
            config_path = config_home / "asmr-tg-backup" / "config.toml"
            token = "123456:secret-token"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "XDG_CONFIG_HOME": str(config_home),
                        "XDG_DATA_HOME": str(data_home),
                    },
                ),
                mock.patch("builtins.input", side_effect=["", "-1001234567890", "123456789"]),
                mock.patch("ytb_tg_backup.setup.getpass.getpass", return_value=token),
                mock.patch(
                    "ytb_tg_backup.setup.resolve_mtproto_credentials",
                    return_value=(12345, "official-hash"),
                ),
                mock.patch(
                    "ytb_tg_backup.config.official_mtproto_credentials",
                    return_value=(12345, "official-hash"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = main(["setup"])

            self.assertEqual(result, 0)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn(token, stdout.getvalue())
            self.assertNotIn(token, stderr.getvalue())
            self.assertIn(
                f"next: asmr-tg-backup service install --config {config_path}",
                stdout.getvalue(),
            )

            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["telegram"]["upload_transport"], "mtproto")
            self.assertNotIn("api_id", raw["telegram"]["mtproto"])
            self.assertNotIn("api_hash", raw["telegram"]["mtproto"])
            self.assertEqual(raw["telegram"]["mtproto"]["max_upload_bytes"], 1_990_000_000)
            self.assertEqual(raw["control"]["allowed_user_ids"], ["123456789"])

            with mock.patch(
                "ytb_tg_backup.config.official_mtproto_credentials",
                return_value=(12345, "official-hash"),
            ):
                config = load_config(config_path)
            self.assertTrue(config.telegram.enabled)
            self.assertTrue(config.control.enabled)
            self.assertEqual(config.telegram.bot_token, token)
            self.assertEqual(config.telegram.mtproto.api_id, 12345)
            self.assertEqual(config.control.allowed_user_ids, ["123456789"])
            self.assertEqual(config.app.data_dir, data_home / "asmr-tg-backup")
            self.assertTrue(config.db_path.is_file())
            self.assertEqual(config.db_path.stat().st_mode & 0o777, 0o600)

    def test_service_install_uses_default_or_late_config_without_loading_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            result = mock.Mock(
                unit_path=root / "asmr-tg-backup.service",
                config_path=config_path,
                environment_path=root / "env",
                user_name="test-user",
            )
            stdout = io.StringIO()
            with (
                mock.patch(
                    "ytb_tg_backup.cli.install_application_service",
                    return_value=result,
                ) as install,
                mock.patch("ytb_tg_backup.cli.load_config") as load_runtime_config,
                redirect_stdout(stdout),
            ):
                exit_code = main(["service", "install", "--config", str(config_path)])

            self.assertEqual(exit_code, 0)
            install.assert_called_once_with(config_path)
            load_runtime_config.assert_not_called()
            self.assertIn("installed and started asmr-tg-backup.service", stdout.getvalue())
            self.assertIn("service uninstall", stdout.getvalue())

    def test_service_uninstall_does_not_require_a_config(self):
        stdout = io.StringIO()
        with (
            mock.patch(
                "ytb_tg_backup.cli.uninstall_application_service",
                return_value=Path("/tmp/asmr-tg-backup.service"),
            ) as uninstall,
            mock.patch("ytb_tg_backup.cli.load_config") as load_runtime_config,
            redirect_stdout(stdout),
        ):
            exit_code = main(["service", "uninstall"])

        self.assertEqual(exit_code, 0)
        uninstall.assert_called_once_with()
        load_runtime_config.assert_not_called()
        self.assertIn("configuration, environment, database, and downloads were kept", stdout.getvalue())

    def test_sources_path_does_not_initialize_store_or_construct_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._source_config(root)
            stdout = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config", return_value=config) as load_runtime_config,
                mock.patch("ytb_tg_backup.cli.Store") as store_type,
                mock.patch("ytb_tg_backup.cli.SourceCatalogManager") as manager_type,
                mock.patch("ytb_tg_backup.cli.BackupService") as service_type,
                redirect_stdout(stdout),
            ):
                exit_code = main(["sources", "path", "--config", str(config.path)])

            self.assertEqual(exit_code, 0)
            load_runtime_config.assert_called_once_with(str(config.path))
            store_type.assert_not_called()
            manager_type.assert_not_called()
            service_type.assert_not_called()
            self.assertEqual(stdout.getvalue(), f"{config.sources.path}\n")

    def test_read_only_sources_commands_do_not_create_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            data_dir = root / "data"
            catalog_path = root / "settings" / "sources.toml"
            config_path.write_text(
                "[app]\n"
                f'data_dir = "{data_dir}"\n\n'
                "[sources]\n"
                f'path = "{catalog_path}"\n',
                encoding="utf-8",
            )
            write_source_catalog(catalog_path, SourceCatalog(source_filter=""))
            snapshot_path = root / "snapshot.toml"

            stdout = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.BackupService") as service_type,
                redirect_stdout(stdout),
            ):
                self.assertEqual(
                    main(["sources", "path", "--config", str(config_path)]),
                    0,
                )
                self.assertEqual(
                    main(["sources", "validate", "--config", str(config_path)]),
                    0,
                )
                self.assertEqual(
                    main(["sources", "list", "--config", str(config_path)]),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "export",
                            "--output",
                            str(snapshot_path),
                            "--config",
                            str(config_path),
                        ]
                    ),
                    0,
                )

            service_type.assert_not_called()
            self.assertFalse((data_dir / "state.db").exists())
            self.assertIn(str(catalog_path), stdout.getvalue())
            self.assertIn("valid source catalog", stdout.getvalue())

    def test_sources_list_accepts_global_config_and_prints_every_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._source_config(root)
            origin = Origin(
                id="twitch-example",
                provider="twitch",
                kind="vods",
                name="Example 主播",
                external_id="example",
                enabled=False,
                bootstrap="all",
                credential_ref="twitch-main",
                options={"recording_mode": "live"},
            )
            catalog = SimpleNamespace(version=1, source_filter="ASMR|音声", origins=(origin,))
            stdout = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config", return_value=config),
                mock.patch("ytb_tg_backup.cli.Store") as store_type,
                mock.patch("ytb_tg_backup.cli.SourceCatalogManager") as manager_type,
                mock.patch(
                    "ytb_tg_backup.cli.load_source_catalog",
                    return_value=catalog,
                ),
                mock.patch("ytb_tg_backup.cli.BackupService") as service_type,
                redirect_stdout(stdout),
            ):
                exit_code = main(["--config", str(config.path), "sources", "list"])

            self.assertEqual(exit_code, 0)
            store_type.assert_not_called()
            manager_type.assert_not_called()
            service_type.assert_not_called()
            rendered = stdout.getvalue()
            for expected in (
                "version: 1",
                "source_filter: ASMR|音声",
                "sources: 1",
                "id: twitch-example",
                "provider: twitch",
                "kind: vods",
                "name: Example 主播",
                "external_id: example",
                "enabled: false",
                "bootstrap: all",
                "credential_ref: twitch-main",
                'options: {"recording_mode": "live"}',
            ):
                self.assertIn(expected, rendered)

    def test_sources_validate_custom_file_reports_errors_through_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._source_config(root)
            candidate = root / "candidate.toml"
            stderr = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config", return_value=config),
                mock.patch("ytb_tg_backup.cli.Store") as store_type,
                mock.patch("ytb_tg_backup.cli.SourceCatalogManager") as manager_type,
                mock.patch(
                    "ytb_tg_backup.cli.load_source_catalog",
                    side_effect=ValueError("duplicate source id: example"),
                ),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["sources", "validate", "--file", str(candidate), "--config", str(config.path)])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("duplicate source id: example", stderr.getvalue())
            store_type.assert_not_called()
            manager_type.assert_not_called()

    def test_sources_store_initialization_error_closes_store_and_uses_parser_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._source_config(root)
            store = mock.Mock()
            store.initialize.side_effect = OSError("database is not writable")
            stderr = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config", return_value=config),
                mock.patch("ytb_tg_backup.cli.Store", return_value=store),
                mock.patch("ytb_tg_backup.cli.SourceCatalogManager") as manager_type,
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["sources", "migrate", "--config", str(config.path)])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("database is not writable", stderr.getvalue())
            store.close.assert_called_once_with()
    def test_sources_apply_file_atomically_replaces_and_syncs_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._source_config(root)
            candidate_path = root / "candidate.toml"
            candidate = SimpleNamespace(origins=(mock.sentinel.origin,))
            applied = SimpleNamespace(origins=(mock.sentinel.origin,))
            manager = mock.Mock(path=config.sources.path)
            manager.replace.return_value = applied
            stdout = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config", return_value=config),
                mock.patch("ytb_tg_backup.cli.Store"),
                mock.patch("ytb_tg_backup.cli.SourceCatalogManager", return_value=manager),
                mock.patch("ytb_tg_backup.cli.load_source_catalog", return_value=candidate) as load_catalog,
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    ["sources", "apply", "--file", str(candidate_path), "--config", str(config.path)]
                )

            self.assertEqual(exit_code, 0)
            load_catalog.assert_called_once_with(candidate_path)
            manager.replace.assert_called_once_with(candidate)
            manager.apply.assert_not_called()
            self.assertIn("applied 1 sources", stdout.getvalue())

    def test_sources_export_and_migrate_use_canonical_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._source_config(root)
            output_path = root / "backup" / "sources.toml"
            catalog = SimpleNamespace(source_filter="", origins=())
            stdout = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config", return_value=config),
                mock.patch("ytb_tg_backup.cli.Store") as store_type,
                mock.patch("ytb_tg_backup.cli.SourceCatalogManager") as manager_type,
                mock.patch("ytb_tg_backup.cli.load_source_catalog", return_value=catalog) as load_catalog,
                mock.patch("ytb_tg_backup.cli.write_source_catalog") as write_catalog,
                redirect_stdout(stdout),
            ):
                export_code = main(
                    ["sources", "--config", str(config.path), "export", "--output", str(output_path)]
                )

            self.assertEqual(export_code, 0)
            store_type.assert_not_called()
            manager_type.assert_not_called()
            load_catalog.assert_called_once_with(config.sources.path)
            write_catalog.assert_called_once_with(output_path, catalog)
            self.assertIn(f"exported source catalog to {output_path}", stdout.getvalue())

            manager = mock.Mock(path=config.sources.path)
            manager.ensure.return_value = catalog
            stdout = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.load_config", return_value=config),
                mock.patch("ytb_tg_backup.cli.Store"),
                mock.patch("ytb_tg_backup.cli.SourceCatalogManager", return_value=manager),
                redirect_stdout(stdout),
            ):
                migrate_code = main(["sources", "migrate", "--config", str(config.path)])

            self.assertEqual(migrate_code, 0)
            manager.ensure.assert_called_once_with(
                legacy_origins=[],
                legacy_declared=False,
            )
            self.assertIn("source catalog ready", stdout.getvalue())

    def test_docker_entrypoint_routes_sources_to_application_cli(self):
        entrypoint = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"
        script = entrypoint.read_text(encoding="utf-8")

        self.assertIn("|sources)", script)
        self.assertIn("[ ! -d /settings ]", script)
        self.assertIn("chown -R app:app /settings", script)

    def test_sources_commands_round_trip_real_catalog_and_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            data_dir = root / "data"
            catalog_path = root / "settings" / "sources.toml"
            candidate_path = root / "candidate.toml"
            snapshot_path = root / "snapshots" / "sources.toml"
            config_path.write_text(
                "[app]\n"
                f'data_dir = "{data_dir}"\n\n'
                "[sources]\n"
                f'path = "{catalog_path}"\n',
                encoding="utf-8",
            )
            candidate = SourceCatalog(
                source_filter="ASMR",
                origins=(
                    Origin(
                        id="youtube-example",
                        provider="youtube",
                        kind="uploads",
                        name="Example",
                        external_id="UCexample",
                        bootstrap="all",
                        options={"playlist": "uploads"},
                    ),
                ),
            )
            write_source_catalog(candidate_path, candidate)

            stdout = io.StringIO()
            with (
                mock.patch("ytb_tg_backup.cli.BackupService") as service_type,
                redirect_stdout(stdout),
            ):
                self.assertEqual(
                    main(["--config", str(config_path), "sources", "migrate"]),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "apply",
                            "--file",
                            str(candidate_path),
                            "--config",
                            str(config_path),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "--config",
                            str(config_path),
                            "export",
                            "--output",
                            str(snapshot_path),
                        ]
                    ),
                    0,
                )

            service_type.assert_not_called()
            self.assertEqual(load_source_catalog(catalog_path), candidate)
            self.assertEqual(load_source_catalog(snapshot_path), candidate)
            self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o600)
            store = Store(data_dir / "state.db")
            try:
                self.assertEqual(store.list_origins(), [candidate.origins[0]])
            finally:
                store.close()

    def test_setup_source_build_prompts_for_complete_mtproto_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "private" / "config.toml"
            token = "123456:secret-token"
            api_hash = "0123456789abcdef0123456789abcdef"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict(
                    "os.environ",
                    {"XDG_DATA_HOME": str(Path(tmp) / "data")},
                ),
                mock.patch(
                    "builtins.input",
                    side_effect=["", "", "12345", "@archive", "123456789"],
                ),
                mock.patch(
                    "ytb_tg_backup.setup.getpass.getpass",
                    side_effect=[api_hash, token],
                ),
                mock.patch(
                    "ytb_tg_backup.setup.resolve_mtproto_credentials",
                    return_value=(None, ""),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = main(["setup", "--config", str(config_path)])

            self.assertEqual(result, 0)
            self.assertNotIn(token, stdout.getvalue())
            self.assertNotIn(api_hash, stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["telegram"]["upload_transport"], "mtproto")
            self.assertEqual(raw["telegram"]["mtproto"]["api_id"], 12345)
            self.assertEqual(raw["telegram"]["mtproto"]["api_hash"], api_hash)
            config = load_config(config_path)
            self.assertEqual(config.telegram.mtproto.api_id, 12345)
            self.assertEqual(config.telegram.mtproto.api_hash, api_hash)

    def test_setup_custom_single_uses_trusted_api_and_keeps_file_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "private" / "config.toml"
            data_home = Path(tmp) / "data-home"
            token = "987654:another-secret"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(data_home)}),
                mock.patch(
                    "builtins.input",
                    side_effect=[
                        "2",
                        "a",
                        "ftp://invalid.example",
                        "https://bot-api.example/internal/",
                        "@archive",
                        "987654321",
                    ],
                ),
                mock.patch("ytb_tg_backup.setup.getpass.getpass", return_value=token),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = main(["setup", "--config", str(config_path)])

            self.assertEqual(result, 0)
            self.assertIn("must be an http(s) URL", stderr.getvalue())
            self.assertNotIn(token, stdout.getvalue())
            self.assertNotIn(token, stderr.getvalue())
            config = load_config(config_path)
            self.assertEqual(
                config.telegram.bot_api.api_base,
                "https://bot-api.example/internal",
            )
            self.assertEqual(config.telegram.upload_transport, "bot_api")
            self.assertEqual(config.telegram.bot_api.max_upload_bytes, 1_990_000_000)
            self.assertFalse(config.telegram.bot_api.split_large_audio)
            self.assertEqual(config.telegram.bot_token, token)

    def test_setup_default_registers_local_bot_api_user_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "config-home"
            data_home = Path(tmp) / "data-home"
            config_path = config_home / "asmr-tg-backup" / "config.toml"
            stdout = io.StringIO()
            stderr = io.StringIO()
            systemctl = Path("/usr/bin/systemctl")
            bot_api = Path("/opt/telegram-bot-api")
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "XDG_CONFIG_HOME": str(config_home),
                        "XDG_DATA_HOME": str(data_home),
                    },
                ),
                mock.patch(
                    "builtins.input",
                    side_effect=["2", "b", "12345", "@archive", "123456789"],
                ),
                mock.patch(
                    "ytb_tg_backup.setup.getpass.getpass",
                    side_effect=[
                        "0123456789abcdef0123456789abcdef",
                        "123456:test-secret",
                    ],
                ),
                mock.patch(
                    "ytb_tg_backup.setup._find_systemctl",
                    return_value=systemctl,
                ),
                mock.patch(
                    "ytb_tg_backup.setup._find_local_bot_api_executable",
                    return_value=bot_api,
                ),
                mock.patch("ytb_tg_backup.setup._assert_local_port_available"),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user") as run_systemctl,
                mock.patch("ytb_tg_backup.setup._wait_local_api_ready") as wait_ready,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = main(["setup"])

            self.assertEqual(result, 0)
            self.assertNotIn("123456:test-secret", stdout.getvalue())
            self.assertNotIn(
                "0123456789abcdef0123456789abcdef",
                stdout.getvalue(),
            )
            self.assertIn("did not call Telegram cloud logOut", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            config = load_config(config_path)
            self.assertEqual(config.telegram.upload_transport, "bot_api")
            self.assertEqual(config.telegram.bot_api.api_base, "http://127.0.0.1:18081")
            self.assertEqual(config.telegram.bot_api.max_upload_bytes, 1_990_000_000)
            self.assertFalse(config.telegram.bot_api.split_large_audio)

            credentials = config_home / "asmr-tg-backup" / "telegram-bot-api.env"
            unit = (
                config_home
                / "systemd"
                / "user"
                / "asmr-tg-backup-telegram-bot-api.service"
            )
            self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
            self.assertEqual(unit.stat().st_mode & 0o777, 0o600)
            self.assertIn("TELEGRAM_API_ID=12345", credentials.read_text())
            self.assertIn(
                "TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef",
                credentials.read_text(),
            )
            unit_text = unit.read_text()
            self.assertIn("--http-ip-address=127.0.0.1", unit_text)
            self.assertIn("--http-port=18081", unit_text)
            self.assertIn("--local", unit_text)
            self.assertIn("NoNewPrivileges=true", unit_text)
            self.assertNotIn(
                "0123456789abcdef0123456789abcdef",
                unit_text,
            )
            wait_ready.assert_called_once_with()
            self.assertEqual(
                run_systemctl.call_args_list,
                [
                    mock.call(systemctl, "daemon-reload"),
                    mock.call(
                        systemctl,
                        "enable",
                        "--now",
                        "asmr-tg-backup-telegram-bot-api.service",
                    ),
                    mock.call(
                        systemctl,
                        "is-active",
                        "--quiet",
                        "asmr-tg-backup-telegram-bot-api.service",
                    ),
                ],
            )

    def test_setup_refuses_existing_config_before_prompting_for_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("keep-me", encoding="utf-8")
            stderr = io.StringIO()
            with (
                mock.patch("builtins.input") as prompt,
                mock.patch("ytb_tg_backup.setup.getpass.getpass") as getpass,
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["--config", str(config_path), "setup"])

            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(config_path.read_text(encoding="utf-8"), "keep-me")
            self.assertIn("refusing to overwrite existing application config", stderr.getvalue())
            prompt.assert_not_called()
            getpass.assert_not_called()

    def test_setup_reprompts_for_invalid_secret_chat_and_user_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(Path(tmp) / "data")}),
                mock.patch(
                    "builtins.input",
                    side_effect=["2", "c", "", "@archive", "not-a-number", "42"],
                ),
                mock.patch(
                    "ytb_tg_backup.setup.getpass.getpass",
                    side_effect=["bad token", "123:valid"],
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = main(["setup", "--config", str(config_path)])

            self.assertEqual(result, 0)
            self.assertIn("bot token must be non-empty", stderr.getvalue())
            self.assertIn("destination chat must be non-empty", stderr.getvalue())
            self.assertIn("Telegram user ID must be a positive integer", stderr.getvalue())
            config = load_config(config_path)
            self.assertEqual(config.telegram.bot_token, "123:valid")
            self.assertEqual(config.telegram.chat_id, "@archive")
            self.assertEqual(config.control.allowed_user_ids, ["42"])

    def test_run_signal_requests_service_stop_and_restores_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(f'[app]\ndata_dir = "{tmp}"')
            service = mock.Mock()
            handlers: dict[signal.Signals, object] = {}

            def install_handler(sig, handler):
                handlers[sig] = handler

            def run_forever():
                handlers[signal.SIGTERM](signal.SIGTERM, None)

            service.run_forever.side_effect = run_forever
            with (
                mock.patch("ytb_tg_backup.cli.BackupService", return_value=service),
                mock.patch("ytb_tg_backup.cli.signal.getsignal", return_value=signal.SIG_DFL),
                mock.patch("ytb_tg_backup.cli.signal.signal", side_effect=install_handler) as set_signal,
            ):
                result = main(["--config", str(config_path), "run"])

        self.assertEqual(result, 0)
        service.stop.assert_called_once_with()
        service.close.assert_called_once_with()
        self.assertEqual(set_signal.call_count, 4)


if __name__ == "__main__":
    unittest.main()
