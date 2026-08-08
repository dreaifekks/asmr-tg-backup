from contextlib import redirect_stderr, redirect_stdout
from importlib import resources
import io
from pathlib import Path
import signal
import tempfile
import tomllib
import unittest
from unittest import mock

from ytb_tg_backup import __version__
from ytb_tg_backup.cli import main
from ytb_tg_backup.config import load_config


class CliTest(unittest.TestCase):
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
                f"next: asmr-tg-backup run --config {config_path}",
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
