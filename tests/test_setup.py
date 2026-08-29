from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ytb_tg_backup import setup
from ytb_tg_backup.config import load_config


class SetupTest(unittest.TestCase):
    def test_generated_config_uses_process_retention_and_keeps_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "config" / "config.toml"
            answers = setup.SetupAnswers(
                profile="source-mtproto",
                upload_transport="mtproto",
                bot_token="123456:secret",
                chat_id="@archive",
                allowed_user_id="12345",
                mtproto_api_id=12345,
                mtproto_api_hash="0123456789abcdef0123456789abcdef",
            )
            with mock.patch.dict(
                "os.environ",
                {"XDG_DATA_HOME": str(root / "data")},
            ):
                setup._write_setup_config(output, answers)
                config = load_config(output)

            self.assertIn(
                "[storage]\nprocess_retention_hours = 24\n"
                "backup_retention_hours = 0\n"
                'archive_dir = ""\n'
                "archive_after_delivery_hours = 24\n"
                "archive_require_mount = true",
                output.read_text(encoding="utf-8"),
            )
            self.assertEqual(config.storage.process_retention_hours, 24)
            self.assertEqual(config.storage.backup_retention_hours, 0)
            self.assertIsNone(config.storage.archive_dir)
            self.assertEqual(config.storage.archive_after_delivery_hours, 24)
            self.assertTrue(config.storage.archive_require_mount)
            self.assertFalse(config.control.allow_disk_delete)
            self.assertFalse(config.control.reaction_favorites_enabled)

    def test_managed_service_inspection_matches_exact_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            unit_path = root / setup.APPLICATION_UNIT
            unit_path.write_text(
                setup._render_application_unit(
                    executable=Path(sys.executable),
                    config_path=config_path,
                    environment_path=root / "env",
                ),
                encoding="utf-8",
            )
            active = subprocess.CompletedProcess([], 0, "", "")
            with (
                mock.patch(
                    "ytb_tg_backup.setup.application_service_path",
                    return_value=unit_path,
                ),
                mock.patch(
                    "ytb_tg_backup.setup._find_systemctl",
                    return_value=Path("/usr/bin/systemctl"),
                ),
                mock.patch(
                    "ytb_tg_backup.setup._require_managed_application_fragment"
                ) as require_fragment,
                mock.patch(
                    "ytb_tg_backup.setup._run_systemctl_user",
                    return_value=active,
                ) as systemctl,
            ):
                status = setup.inspect_managed_application_service(config_path)

            self.assertTrue(status.matched)
            self.assertTrue(status.active)
            require_fragment.assert_called_once()
            systemctl.assert_called_once_with(
                Path("/usr/bin/systemctl"),
                "is-active",
                "--quiet",
                setup.APPLICATION_UNIT,
                check=False,
            )

    def test_managed_service_inspection_ignores_another_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_path = root / setup.APPLICATION_UNIT
            unit_path.write_text(
                setup._render_application_unit(
                    executable=Path("/bin/true"),
                    config_path=root / "other.toml",
                    environment_path=root / "env",
                ),
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "ytb_tg_backup.setup.application_service_path",
                    return_value=unit_path,
                ),
                mock.patch("ytb_tg_backup.setup._find_systemctl") as find_systemctl,
            ):
                status = setup.inspect_managed_application_service(
                    root / "config.toml"
                )

            self.assertFalse(status.matched)
            self.assertFalse(status.active)
            find_systemctl.assert_not_called()

    def test_managed_service_inspection_ignores_another_python_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            unit_path = root / setup.APPLICATION_UNIT
            unit_path.write_text(
                setup._render_application_unit(
                    executable=Path("/another/venv/bin/python"),
                    config_path=config_path,
                    environment_path=root / "env",
                ),
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "ytb_tg_backup.setup.application_service_path",
                    return_value=unit_path,
                ),
                mock.patch("ytb_tg_backup.setup._find_systemctl") as find_systemctl,
            ):
                status = setup.inspect_managed_application_service(config_path)

            self.assertFalse(status.matched)
            self.assertFalse(status.active)
            find_systemctl.assert_not_called()

    def test_restart_managed_service_skips_inactive_unit_by_default(self):
        status = setup.ManagedApplicationServiceStatus(matched=True, active=False)
        with (
            mock.patch(
                "ytb_tg_backup.setup.inspect_managed_application_service",
                return_value=status,
            ),
            mock.patch("ytb_tg_backup.setup._find_systemctl") as find_systemctl,
        ):
            restarted = setup.restart_managed_application_service(
                Path("/tmp/config.toml")
            )

        self.assertFalse(restarted)
        find_systemctl.assert_not_called()

    def test_api_hash_prompt_rejects_wrong_length(self):
        stderr = io.StringIO()
        valid_hash = "0123456789abcdef0123456789abcdef"
        with (
            mock.patch(
                "ytb_tg_backup.setup.getpass.getpass",
                side_effect=["abcd", valid_hash],
            ),
            redirect_stderr(stderr),
        ):
            result = setup._prompt_api_hash()

        self.assertEqual(result, valid_hash)
        self.assertIn("32-character hexadecimal", stderr.getvalue())

    def test_mtproto_defaults_skip_application_credential_prompts(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "ytb_tg_backup.setup.resolve_mtproto_credentials",
                return_value=(12345, "0123456789abcdef0123456789abcdef"),
            ),
            mock.patch("builtins.input", side_effect=["@archive", "42"]) as prompt,
            mock.patch(
                "ytb_tg_backup.setup.getpass.getpass",
                return_value="123:bot-token",
            ) as secret_prompt,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            answers = setup._prompt_mtproto_setup()

        self.assertEqual(answers.profile, "mtproto-defaults")
        self.assertEqual(answers.upload_transport, "mtproto")
        self.assertIsNone(answers.mtproto_api_id)
        self.assertEqual(answers.mtproto_api_hash, "")
        self.assertEqual(prompt.call_count, 2)
        secret_prompt.assert_called_once_with("Telegram bot token (hidden): ")
        self.assertEqual(stderr.getvalue(), "")

    def test_source_mtproto_prompt_can_switch_to_cloud_bot_api_splitting(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "ytb_tg_backup.setup.resolve_mtproto_credentials",
                return_value=(None, ""),
            ),
            mock.patch("builtins.input", side_effect=["b", "c", "@archive", "42"]),
            mock.patch(
                "ytb_tg_backup.setup.getpass.getpass",
                return_value="123:bot-token",
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            answers = setup._prompt_mtproto_setup()

        self.assertEqual(answers.profile, "official-api-split")
        self.assertEqual(answers.upload_transport, "bot_api")
        self.assertEqual(answers.api_base, "https://api.telegram.org")
        self.assertEqual(answers.bot_api_max_upload_bytes, 49_000_000)
        self.assertTrue(answers.bot_api_split_large_audio)
        self.assertEqual(stderr.getvalue(), "")

    def test_non_loopback_http_api_requires_explicit_confirmation(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "builtins.input",
                side_effect=[
                    "http://192.0.2.10:8081",
                    "no",
                    "http://192.0.2.10:8081",
                    "yes",
                ],
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = setup._prompt_api_base()

        self.assertEqual(result, "http://192.0.2.10:8081")
        self.assertEqual(stderr.getvalue().count("without transport encryption"), 2)

    def test_loopback_http_api_does_not_require_confirmation(self):
        stdout = io.StringIO()
        with (
            mock.patch("builtins.input", return_value="http://127.0.0.1:18081") as prompt,
            redirect_stdout(stdout),
        ):
            result = setup._prompt_api_base()

        self.assertEqual(result, "http://127.0.0.1:18081")
        prompt.assert_called_once_with("Existing Bot API base URL: ")

    def test_private_file_does_not_chmod_existing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "shared"
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            target = parent / "config.toml"

            setup._write_private_file(target, b"private")

            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_private_file_reports_partial_write_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "private.env"
            with (
                mock.patch.object(Path, "chmod", side_effect=OSError("chmod boom")),
                mock.patch.object(Path, "unlink", side_effect=OSError("unlink boom")),
                self.assertRaises(setup.SetupError) as raised,
            ):
                setup._write_private_file(target, b"super-secret")

            message = str(raised.exception)
            self.assertIn("chmod boom", message)
            self.assertIn("rollback incomplete; manual cleanup required", message)
            self.assertIn("unlink boom", message)
            self.assertEqual(target.read_bytes(), b"super-secret")

    def test_owned_private_directory_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual"
            actual.mkdir()
            link = root / "private"
            link.symlink_to(actual, target_is_directory=True)

            with self.assertRaisesRegex(setup.SetupError, "refusing symlink"):
                setup._ensure_owned_private_directory(link)

    def test_unused_target_check_rejects_broken_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            target.symlink_to(Path(tmp) / "missing.toml")

            with self.assertRaisesRegex(setup.SetupTargetExistsError, "overwrite"):
                setup._require_unused_target(target, "application config")

    def test_user_unit_uses_verified_local_only_flags_and_private_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = setup.LocalApiSetup(
                executable=Path("/opt/Telegram Bot API/telegram-bot-api"),
                systemctl=Path("/usr/bin/systemctl"),
                api_id="12345",
                api_hash="0123456789abcdef",
                paths=setup.LocalApiPaths(
                    credentials=root / "private credentials.env",
                    unit=root / "unit.service",
                    data_dir=root / "bot data",
                    temp_dir=root / "bot data" / "tmp",
                ),
            )

            unit = setup._render_local_api_unit(local)

            self.assertIn("Type=exec", unit)
            self.assertIn('ExecStart="/opt/Telegram Bot API/telegram-bot-api"', unit)
            self.assertIn("--local", unit)
            self.assertIn("--http-ip-address=127.0.0.1", unit)
            self.assertIn("--http-port=18081", unit)
            self.assertIn('"--dir=', unit)
            self.assertIn('"--temp-dir=', unit)
            self.assertIn("NoNewPrivileges=true", unit)
            self.assertIn("UMask=0077", unit)
            escaped_credentials = str(local.paths.credentials).replace(" ", "\\x20")
            self.assertIn(
                f"EnvironmentFile={escaped_credentials}",
                unit,
            )
            self.assertNotIn('EnvironmentFile="', unit)
            self.assertNotIn(local.api_id, unit)
            self.assertNotIn(local.api_hash, unit)

    def test_environment_file_path_rejects_relative_and_glob_paths(self):
        with self.assertRaisesRegex(setup.SetupError, "must be absolute"):
            setup._unit_environment_file_path(Path("relative.env"))
        with self.assertRaisesRegex(setup.SetupError, "unsupported character"):
            setup._unit_environment_file_path(Path("/tmp/credentials*.env"))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is unavailable")
    def test_rendered_user_unit_passes_systemd_parser_with_spaced_env_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = setup.LocalApiSetup(
                executable=Path("/bin/true"),
                systemctl=Path("/usr/bin/systemctl"),
                api_id="12345",
                api_hash="0123456789abcdef",
                paths=setup.LocalApiPaths(
                    credentials=root / "private credentials.env",
                    unit=root / setup.LOCAL_API_UNIT,
                    data_dir=root / "bot data",
                    temp_dir=root / "bot data" / "tmp",
                ),
            )
            local.paths.credentials.write_text(
                "TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=0123456789abcdef\n",
                encoding="utf-8",
            )
            local.paths.unit.write_text(
                setup._render_local_api_unit(local),
                encoding="utf-8",
            )

            result = subprocess.run(
                ["systemd-analyze", "--user", "verify", str(local.paths.unit)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")

    def test_binary_inspection_requires_official_cli_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "telegram-bot-api"
            executable.write_text("placeholder", encoding="utf-8")
            executable.chmod(0o700)
            help_text = " ".join(setup.LOCAL_API_REQUIRED_FLAGS)
            completed = subprocess.CompletedProcess(
                [str(executable), "--help"],
                0,
                help_text,
                "",
            )
            with mock.patch("ytb_tg_backup.setup.subprocess.run", return_value=completed) as run:
                result = setup._validate_local_bot_api_executable(executable)

            self.assertEqual(result, executable.resolve())
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], [str(executable.resolve()), "--help"])

            incomplete = subprocess.CompletedProcess(
                [str(executable), "--help"],
                0,
                "--api-id --api-hash --local",
                "",
            )
            with (
                mock.patch("ytb_tg_backup.setup.subprocess.run", return_value=incomplete),
                self.assertRaises(setup.SetupError),
            ):
                setup._validate_local_bot_api_executable(executable)

    def test_port_conflict_is_rejected(self):
        listener = mock.MagicMock()
        listener.__enter__.return_value.bind.side_effect = OSError("busy")
        with (
            mock.patch("ytb_tg_backup.setup.socket.socket", return_value=listener),
            self.assertRaisesRegex(setup.SetupError, "127.0.0.1:18081 is already in use"),
        ):
            setup._assert_local_port_available()

    def test_wait_local_api_ready_retries_until_tcp_connects(self):
        connection = mock.MagicMock()
        with (
            mock.patch(
                "ytb_tg_backup.setup.socket.create_connection",
                side_effect=[OSError("connection refused"), connection],
            ) as connect,
            mock.patch(
                "ytb_tg_backup.setup.time.monotonic",
                side_effect=[100.0, 100.1],
            ),
            mock.patch("ytb_tg_backup.setup.time.sleep") as sleep,
        ):
            setup._wait_local_api_ready(timeout_seconds=1.0, poll_interval_seconds=0.2)

        self.assertEqual(connect.call_count, 2)
        connect.assert_called_with(
            (setup.LOCAL_API_HOST, setup.LOCAL_API_PORT),
            timeout=setup.LOCAL_API_CONNECT_TIMEOUT_SECONDS,
        )
        sleep.assert_called_once_with(0.2)

    def test_wait_local_api_ready_times_out_without_using_a_real_port(self):
        with (
            mock.patch(
                "ytb_tg_backup.setup.socket.create_connection",
                side_effect=OSError("connection refused"),
            ) as connect,
            mock.patch(
                "ytb_tg_backup.setup.time.monotonic",
                side_effect=[100.0, 101.0],
            ),
            mock.patch("ytb_tg_backup.setup.time.sleep") as sleep,
            self.assertRaisesRegex(setup.SetupError, "did not become ready.*within 1 seconds"),
        ):
            setup._wait_local_api_ready(timeout_seconds=1.0, poll_interval_seconds=0.2)

        connect.assert_called_once_with(
            (setup.LOCAL_API_HOST, setup.LOCAL_API_PORT),
            timeout=setup.LOCAL_API_CONNECT_TIMEOUT_SECONDS,
        )
        sleep.assert_not_called()

    def test_execstart_quote_rejects_line_breaks_and_nul(self):
        for character in ("\x00", "\r", "\n"):
            with self.subTest(character=repr(character)):
                with self.assertRaisesRegex(setup.SetupError, "ExecStart argument"):
                    setup._unit_exec_quote(f"/opt/telegram{character}-bot-api")

    def test_failed_systemctl_registration_removes_unit_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = setup.LocalApiSetup(
                executable=Path("/opt/telegram-bot-api"),
                systemctl=Path("/usr/bin/systemctl"),
                api_id="12345",
                api_hash="0123456789abcdef",
                paths=setup.LocalApiPaths(
                    credentials=root / "config" / "telegram-bot-api.env",
                    unit=root / "systemd" / "user" / setup.LOCAL_API_UNIT,
                    data_dir=root / "data" / "telegram-bot-api",
                    temp_dir=root / "data" / "telegram-bot-api" / "tmp",
                ),
            )
            calls: list[tuple[tuple[str, ...], bool]] = []

            def systemctl(_path, *arguments, check=True):
                calls.append((arguments, check))
                if arguments[:2] == ("enable", "--now"):
                    raise setup.SetupError("simulated start failure")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch("ytb_tg_backup.setup._assert_local_port_available"),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user", side_effect=systemctl),
                self.assertRaisesRegex(setup.SetupError, "simulated start failure"),
            ):
                setup._install_local_api(local)

            self.assertFalse(local.paths.credentials.exists())
            self.assertFalse(local.paths.unit.exists())
            self.assertIn(
                (("disable", "--now", setup.LOCAL_API_UNIT), False),
                calls,
            )
            self.assertEqual(local.paths.data_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(local.paths.temp_dir.stat().st_mode & 0o777, 0o700)

    def test_credentials_write_failure_does_not_touch_systemd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = setup.LocalApiSetup(
                executable=Path("/opt/telegram-bot-api"),
                systemctl=Path("/usr/bin/systemctl"),
                api_id="12345",
                api_hash="0123456789abcdef",
                paths=setup.LocalApiPaths(
                    credentials=root / "config" / "telegram-bot-api.env",
                    unit=root / "systemd" / "user" / setup.LOCAL_API_UNIT,
                    data_dir=root / "data" / "telegram-bot-api",
                    temp_dir=root / "data" / "telegram-bot-api" / "tmp",
                ),
            )

            with (
                mock.patch("ytb_tg_backup.setup._assert_local_port_available"),
                mock.patch(
                    "ytb_tg_backup.setup._write_private_file",
                    side_effect=OSError("credential write failed"),
                ),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user") as systemctl,
                self.assertRaisesRegex(setup.SetupError, "credential write failed"),
            ):
                setup._install_local_api(local)

            systemctl.assert_not_called()

    def test_readiness_timeout_removes_unit_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = setup.LocalApiSetup(
                executable=Path("/opt/telegram-bot-api"),
                systemctl=Path("/usr/bin/systemctl"),
                api_id="12345",
                api_hash="0123456789abcdef",
                paths=setup.LocalApiPaths(
                    credentials=root / "config" / "telegram-bot-api.env",
                    unit=root / "systemd" / "user" / setup.LOCAL_API_UNIT,
                    data_dir=root / "data" / "telegram-bot-api",
                    temp_dir=root / "data" / "telegram-bot-api" / "tmp",
                ),
            )
            calls: list[tuple[tuple[str, ...], bool]] = []

            def systemctl(_path, *arguments, check=True):
                calls.append((arguments, check))
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch("ytb_tg_backup.setup._assert_local_port_available"),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user", side_effect=systemctl),
                mock.patch(
                    "ytb_tg_backup.setup._wait_local_api_ready",
                    side_effect=setup.SetupError("local Bot API did not become ready"),
                ) as wait_ready,
                self.assertRaisesRegex(setup.SetupError, "did not become ready"),
            ):
                setup._install_local_api(local)

            wait_ready.assert_called_once_with()
            self.assertFalse(local.paths.credentials.exists())
            self.assertFalse(local.paths.unit.exists())
            self.assertIn(
                (("disable", "--now", setup.LOCAL_API_UNIT), False),
                calls,
            )

    def test_database_initialization_failure_removes_application_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "config" / "config.toml"
            answers = setup.SetupAnswers(
                profile="official-api-split",
                upload_transport="bot_api",
                bot_token="123456:secret",
                chat_id="@archive",
                allowed_user_id="12345",
                api_base="https://api.telegram.org",
                bot_api_max_upload_bytes=49_000_000,
                bot_api_split_large_audio=True,
            )
            service = mock.Mock()
            service.initialize.side_effect = OSError("database unavailable")
            with (
                mock.patch("ytb_tg_backup.setup.prompt_setup", return_value=answers),
                mock.patch("ytb_tg_backup.setup.BackupService", return_value=service),
                self.assertRaisesRegex(setup.SetupError, "database unavailable"),
            ):
                setup.run_interactive_setup(output)

            self.assertFalse(output.exists())
            service.store.close.assert_called_once_with()

    def test_database_initialization_error_survives_store_close_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "config" / "config.toml"
            answers = setup.SetupAnswers(
                profile="official-api-split",
                upload_transport="bot_api",
                bot_token="123456:secret",
                chat_id="@archive",
                allowed_user_id="12345",
                api_base="https://api.telegram.org",
                bot_api_max_upload_bytes=49_000_000,
                bot_api_split_large_audio=True,
            )
            service = mock.Mock()
            service.initialize.side_effect = OSError("initialize boom")
            service.store.close.side_effect = OSError("close boom")
            with (
                mock.patch("ytb_tg_backup.setup.prompt_setup", return_value=answers),
                mock.patch("ytb_tg_backup.setup.BackupService", return_value=service),
                self.assertRaises(setup.SetupError) as raised,
            ):
                setup.run_interactive_setup(output)

            message = str(raised.exception)
            self.assertIn("database initialization failed: initialize boom", message)
            self.assertIn("additionally could not close the store: close boom", message)
            self.assertFalse(output.exists())

    def test_database_failure_reports_incomplete_config_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "config" / "config.toml"
            answers = setup.SetupAnswers(
                profile="official-api-split",
                upload_transport="bot_api",
                bot_token="123456:secret",
                chat_id="@archive",
                allowed_user_id="12345",
                api_base="https://api.telegram.org",
                bot_api_max_upload_bytes=49_000_000,
                bot_api_split_large_audio=True,
            )
            service = mock.Mock()
            service.initialize.side_effect = OSError("database unavailable")
            with (
                mock.patch("ytb_tg_backup.setup.prompt_setup", return_value=answers),
                mock.patch("ytb_tg_backup.setup.BackupService", return_value=service),
                mock.patch(
                    "ytb_tg_backup.setup._remove_created_file",
                    return_value=["could not remove application config"],
                ),
                self.assertRaisesRegex(
                    setup.SetupError,
                    "rollback incomplete; manual cleanup required",
                ),
            ):
                setup.run_interactive_setup(output)

    def test_systemctl_cleanup_failure_is_reported(self):
        failed = subprocess.CompletedProcess([], 1, "", "permission denied")
        self.assertEqual(
            setup._systemctl_cleanup_issue("disable --now", failed),
            ["systemctl --user disable --now failed with code 1: permission denied"],
        )

    def test_existing_local_credentials_are_rejected_before_secret_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = Path(tmp) / "config"
            credentials = config_home / "asmr-tg-backup" / "telegram-bot-api.env"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("keep", encoding="utf-8")
            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup.getpass.getpass") as secret_prompt,
                self.assertRaisesRegex(setup.SetupTargetExistsError, "credentials"),
            ):
                setup._prompt_local_api_setup()

            secret_prompt.assert_not_called()
            self.assertEqual(credentials.read_text(encoding="utf-8"), "keep")

    def test_application_service_install_enables_linger_and_starts_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config home"
            config_path = config_home / "asmr-tg-backup" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                f'[app]\ndata_dir = "{root / "data"}"\n',
                encoding="utf-8",
            )
            events: list[tuple[str, tuple[str, ...]]] = []

            def run_loginctl(_path, *arguments):
                events.append(("loginctl", arguments))
                return subprocess.CompletedProcess([], 0, "", "")

            def run_systemctl(_path, *arguments, check=True):
                events.append(("systemctl", arguments))
                if arguments[0] == "list-unit-files":
                    return subprocess.CompletedProcess([], 1, "", "")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup._find_systemctl", return_value=Path("/usr/bin/systemctl")),
                mock.patch("ytb_tg_backup.setup._find_loginctl", return_value=Path("/usr/bin/loginctl")),
                mock.patch("ytb_tg_backup.setup._current_user_name", return_value="test-user"),
                mock.patch("ytb_tg_backup.setup._run_loginctl", side_effect=run_loginctl),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user", side_effect=run_systemctl),
            ):
                result = setup.install_application_service(
                    config_path,
                    python_executable=Path("/bin/true"),
                )

            unit = config_home / "systemd" / "user" / setup.APPLICATION_UNIT
            self.assertEqual(result.unit_path, unit)
            self.assertEqual(result.environment_path, config_path.parent / "env")
            self.assertEqual(unit.stat().st_mode & 0o777, 0o600)
            unit_text = unit.read_text(encoding="utf-8")
            self.assertTrue(unit_text.startswith(setup.APPLICATION_UNIT_MARKER))
            self.assertIn('ExecStart="/bin/true" -m ytb_tg_backup run --config ', unit_text)
            self.assertIn("EnvironmentFile=-", unit_text)
            self.assertIn("Restart=always", unit_text)
            self.assertEqual(
                events,
                [
                    (
                        "systemctl",
                        (
                            "list-units",
                            "--all",
                            "--full",
                            "--plain",
                            "--no-legend",
                            "--no-pager",
                            setup.APPLICATION_UNIT,
                        ),
                    ),
                    (
                        "systemctl",
                        (
                            "list-unit-files",
                            "--full",
                            "--no-legend",
                            "--no-pager",
                            setup.APPLICATION_UNIT,
                        ),
                    ),
                    ("loginctl", ("enable-linger", "test-user")),
                    ("systemctl", ("daemon-reload",)),
                    ("systemctl", ("enable", setup.APPLICATION_UNIT)),
                    ("systemctl", ("restart", setup.APPLICATION_UNIT)),
                    ("systemctl", ("is-active", "--quiet", setup.APPLICATION_UNIT)),
                ],
            )

    def test_application_service_login_failure_does_not_write_or_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config"
            config_path = root / "config.toml"
            config_path.write_text(
                f'[app]\ndata_dir = "{root / "data"}"\n',
                encoding="utf-8",
            )
            unit = config_home / "systemd" / "user" / setup.APPLICATION_UNIT
            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup._find_systemctl", return_value=Path("/usr/bin/systemctl")),
                mock.patch("ytb_tg_backup.setup._find_loginctl", return_value=Path("/usr/bin/loginctl")),
                mock.patch("ytb_tg_backup.setup._current_user_name", return_value="test-user"),
                mock.patch(
                    "ytb_tg_backup.setup._run_loginctl",
                    side_effect=setup.SetupError("linger unavailable"),
                ),
                mock.patch(
                    "ytb_tg_backup.setup._run_systemctl_user",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as systemctl,
                self.assertRaisesRegex(setup.SetupError, "linger unavailable"),
            ):
                setup.install_application_service(
                    config_path,
                    python_executable=Path("/bin/true"),
                )

            self.assertFalse(unit.exists())
            self.assertEqual(systemctl.call_count, 2)
            self.assertEqual(systemctl.call_args_list[0].args[1], "list-units")
            self.assertEqual(systemctl.call_args_list[1].args[1], "list-unit-files")

    def test_application_service_install_updates_its_managed_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config"
            config_path = root / "config.toml"
            config_path.write_text(
                f'[app]\ndata_dir = "{root / "data"}"\n',
                encoding="utf-8",
            )
            unit = config_home / "systemd" / "user" / setup.APPLICATION_UNIT
            unit.parent.mkdir(parents=True)
            unit.write_text(
                f"{setup.APPLICATION_UNIT_MARKER}\n[Service]\nExecStart=/old/path\n",
                encoding="utf-8",
            )
            events: list[tuple[str, ...]] = []

            def run_systemctl(_path, *arguments, check=True):
                events.append(arguments)
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup._find_systemctl", return_value=Path("/usr/bin/systemctl")),
                mock.patch("ytb_tg_backup.setup._find_loginctl", return_value=Path("/usr/bin/loginctl")),
                mock.patch("ytb_tg_backup.setup._current_user_name", return_value="test-user"),
                mock.patch("ytb_tg_backup.setup._run_loginctl"),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user", side_effect=run_systemctl),
            ):
                setup.install_application_service(
                    config_path,
                    python_executable=Path("/bin/true"),
                )

            unit_text = unit.read_text(encoding="utf-8")
            self.assertNotIn("/old/path", unit_text)
            self.assertIn('ExecStart="/bin/true"', unit_text)
            self.assertEqual(unit.stat().st_mode & 0o777, 0o600)
            self.assertIn(("restart", setup.APPLICATION_UNIT), events)

    def test_application_service_restart_failure_rolls_back_new_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config"
            config_path = root / "config.toml"
            config_path.write_text(
                f'[app]\ndata_dir = "{root / "data"}"\n',
                encoding="utf-8",
            )
            calls: list[tuple[str, ...]] = []

            def run_systemctl(_path, *arguments, check=True):
                calls.append(arguments)
                if arguments[:1] == ("restart",):
                    raise setup.SetupError("simulated worker start failure")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup._find_systemctl", return_value=Path("/usr/bin/systemctl")),
                mock.patch("ytb_tg_backup.setup._find_loginctl", return_value=Path("/usr/bin/loginctl")),
                mock.patch("ytb_tg_backup.setup._current_user_name", return_value="test-user"),
                mock.patch("ytb_tg_backup.setup._run_loginctl"),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user", side_effect=run_systemctl),
                self.assertRaisesRegex(setup.SetupError, "simulated worker start failure"),
            ):
                setup.install_application_service(
                    config_path,
                    python_executable=Path("/bin/true"),
                )

            unit = config_home / "systemd" / "user" / setup.APPLICATION_UNIT
            self.assertFalse(unit.exists())
            self.assertIn(("disable", "--now", setup.APPLICATION_UNIT), calls)
            self.assertEqual(calls[-1], ("daemon-reload",))

    def test_application_service_update_failure_restores_previous_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config"
            config_path = root / "config.toml"
            config_path.write_text(
                f'[app]\ndata_dir = "{root / "data"}"\n',
                encoding="utf-8",
            )
            unit = config_home / "systemd" / "user" / setup.APPLICATION_UNIT
            unit.parent.mkdir(parents=True)
            previous = (
                f"{setup.APPLICATION_UNIT_MARKER}\n"
                "[Service]\nExecStart=/previous/path\n"
            )
            unit.write_text(previous, encoding="utf-8")
            calls: list[tuple[tuple[str, ...], bool]] = []

            def run_systemctl(_path, *arguments, check=True):
                calls.append((arguments, check))
                if arguments[:1] == ("restart",) and check:
                    raise setup.SetupError("simulated update failure")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup._find_systemctl", return_value=Path("/usr/bin/systemctl")),
                mock.patch("ytb_tg_backup.setup._find_loginctl", return_value=Path("/usr/bin/loginctl")),
                mock.patch("ytb_tg_backup.setup._current_user_name", return_value="test-user"),
                mock.patch("ytb_tg_backup.setup._run_loginctl"),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user", side_effect=run_systemctl),
                self.assertRaisesRegex(setup.SetupError, "simulated update failure"),
            ):
                setup.install_application_service(
                    config_path,
                    python_executable=Path("/bin/true"),
                )

            self.assertEqual(unit.read_text(encoding="utf-8"), previous)
            self.assertEqual(
                calls[-2:],
                [
                    (("daemon-reload",), False),
                    (("restart", setup.APPLICATION_UNIT), False),
                ],
            )

    def test_application_service_uninstall_keeps_runtime_files_and_linger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config"
            unit = config_home / "systemd" / "user" / setup.APPLICATION_UNIT
            config = config_home / "asmr-tg-backup" / "config.toml"
            environment = config.parent / "env"
            database = root / "data" / "state.db"
            unit.parent.mkdir(parents=True)
            config.parent.mkdir(parents=True)
            database.parent.mkdir(parents=True)
            config.write_text("keep config", encoding="utf-8")
            environment.write_text("keep env", encoding="utf-8")
            database.write_text("keep db", encoding="utf-8")
            unit.write_text(
                setup._render_application_unit(
                    executable=Path("/bin/true"),
                    config_path=config,
                    environment_path=environment,
                ),
                encoding="utf-8",
            )
            calls: list[tuple[str, ...]] = []

            def run_systemctl(_path, *arguments, check=True):
                calls.append(arguments)
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup._find_systemctl", return_value=Path("/usr/bin/systemctl")),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user", side_effect=run_systemctl),
                mock.patch("ytb_tg_backup.setup._run_loginctl") as loginctl,
            ):
                removed = setup.uninstall_application_service()

            self.assertEqual(removed, unit)
            self.assertFalse(unit.exists())
            self.assertTrue(config.exists())
            self.assertTrue(environment.exists())
            self.assertTrue(database.exists())
            loginctl.assert_not_called()
            self.assertEqual(
                calls,
                [
                    (
                        "list-units",
                        "--all",
                        "--full",
                        "--plain",
                        "--no-legend",
                        "--no-pager",
                        setup.APPLICATION_UNIT,
                    ),
                    (
                        "list-unit-files",
                        "--full",
                        "--no-legend",
                        "--no-pager",
                        setup.APPLICATION_UNIT,
                    ),
                    ("disable", "--now", setup.APPLICATION_UNIT),
                    ("daemon-reload",),
                ],
            )

    def test_application_service_refuses_foreign_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config"
            config_path = root / "config.toml"
            config_path.write_text(
                f'[app]\ndata_dir = "{root / "data"}"\n',
                encoding="utf-8",
            )
            unit = config_home / "systemd" / "user" / setup.APPLICATION_UNIT
            unit.parent.mkdir(parents=True)
            unit.write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")
            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup._run_loginctl") as loginctl,
                self.assertRaisesRegex(setup.SetupTargetExistsError, "not created by this command"),
            ):
                setup.install_application_service(
                    config_path,
                    python_executable=Path("/bin/true"),
                )

            loginctl.assert_not_called()
            self.assertIn("/bin/false", unit.read_text(encoding="utf-8"))

    def test_application_service_refuses_unit_loaded_from_another_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_home = root / "config"
            config_path = root / "config.toml"
            config_path.write_text(
                f'[app]\ndata_dir = "{root / "data"}"\n',
                encoding="utf-8",
            )
            foreign_unit = Path("/etc/systemd/user") / setup.APPLICATION_UNIT

            def run_systemctl(_path, *arguments, check=True):
                if arguments[0] == "list-units":
                    return subprocess.CompletedProcess(
                        [],
                        0,
                        f"{setup.APPLICATION_UNIT} loaded inactive dead test\n",
                        "",
                    )
                if arguments[0] == "show":
                    return subprocess.CompletedProcess([], 0, f"{foreign_unit}\n", "")
                return subprocess.CompletedProcess([], 0, "", "")

            with (
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(config_home)}),
                mock.patch("ytb_tg_backup.setup._find_systemctl", return_value=Path("/usr/bin/systemctl")),
                mock.patch("ytb_tg_backup.setup._find_loginctl", return_value=Path("/usr/bin/loginctl")),
                mock.patch("ytb_tg_backup.setup._current_user_name", return_value="test-user"),
                mock.patch("ytb_tg_backup.setup._run_systemctl_user", side_effect=run_systemctl),
                mock.patch("ytb_tg_backup.setup._run_loginctl") as loginctl,
                self.assertRaisesRegex(setup.SetupTargetExistsError, "another path"),
            ):
                setup.install_application_service(
                    config_path,
                    python_executable=Path("/bin/true"),
                )

            loginctl.assert_not_called()
            self.assertFalse(
                (config_home / "systemd" / "user" / setup.APPLICATION_UNIT).exists()
            )

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is unavailable")
    def test_rendered_application_unit_passes_systemd_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "path with spaces"
            root.mkdir(parents=True)
            unit = root / setup.APPLICATION_UNIT
            unit.write_text(
                setup._render_application_unit(
                    executable=Path("/bin/true"),
                    config_path=root / "config $value%.toml",
                    environment_path=root / "env $value%",
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                ["systemd-analyze", "--user", "verify", str(unit)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
