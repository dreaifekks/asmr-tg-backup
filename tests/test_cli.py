from contextlib import redirect_stdout
import io
from pathlib import Path
import signal
import sys
import tempfile
import types
import unittest
from unittest import mock

from ytb_tg_backup.cli import main


class CliTest(unittest.TestCase):
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
        self.assertEqual(set_signal.call_count, 4)

    def test_dev_youtube_membership_once_uses_isolated_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[[origins]]
id = "youtube-member"
provider = "youtube"
kind = "uploads"
name = "Member channel"
external_id = "UC1234567890123456789012"
enabled = false

[dev.youtube_membership]
enabled = true
origin_ids = ["youtube-member"]
""".strip()
            )
            runner = mock.Mock()
            runner.run_once.return_value = {"items": 2, "notifications": 0}
            runner_type = mock.Mock(return_value=runner)
            module = types.ModuleType("ytb_tg_backup.dev.youtube_membership")
            module.YoutubeMembershipDevRunner = runner_type
            output = io.StringIO()

            with (
                mock.patch.dict(
                    sys.modules,
                    {"ytb_tg_backup.dev.youtube_membership": module},
                ),
                mock.patch("ytb_tg_backup.cli.BackupService") as backup_service,
                redirect_stdout(output),
            ):
                result = main(
                    [
                        "dev",
                        "youtube-membership",
                        "once",
                        "--config",
                        str(config_path),
                    ]
                )

        self.assertEqual(result, 0)
        self.assertIn('"items": 2', output.getvalue())
        backup_service.assert_not_called()
        runner_type.assert_called_once()
        runner.run_once.assert_called_once_with()
        runner.close.assert_called_once_with()
        self.assertIn("stop_event", runner_type.call_args.kwargs)

    def test_dev_youtube_membership_status_passes_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"
""".strip()
            )
            runner = mock.Mock()
            runner.status.return_value = {"enabled": False}
            runner_type = mock.Mock(return_value=runner)
            module = types.ModuleType("ytb_tg_backup.dev.youtube_membership")
            module.YoutubeMembershipDevRunner = runner_type

            with (
                mock.patch.dict(
                    sys.modules,
                    {"ytb_tg_backup.dev.youtube_membership": module},
                ),
                redirect_stdout(io.StringIO()),
            ):
                result = main(
                    [
                        "--config",
                        str(config_path),
                        "dev",
                        "youtube-membership",
                        "status",
                        "--limit",
                        "7",
                    ]
                )

        self.assertEqual(result, 0)
        runner.status.assert_called_once_with(limit=7)
        runner.close.assert_called_once_with()

    def test_dev_youtube_membership_run_uses_shared_signal_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f"""
[app]
data_dir = "{tmp}"

[[origins]]
id = "youtube-member"
provider = "youtube"
external_id = "UC1234567890123456789012"
enabled = false

[dev.youtube_membership]
enabled = true
origin_ids = ["youtube-member"]
""".strip()
            )
            handlers: dict[signal.Signals, object] = {}
            captured: dict[str, object] = {}
            runner = mock.Mock()

            def create_runner(_config, *, stop_event):
                captured["stop_event"] = stop_event
                return runner

            def install_handler(sig, handler):
                handlers[sig] = handler

            def run_forever():
                handlers[signal.SIGTERM](signal.SIGTERM, None)

            runner.run_forever.side_effect = run_forever
            module = types.ModuleType("ytb_tg_backup.dev.youtube_membership")
            module.YoutubeMembershipDevRunner = mock.Mock(side_effect=create_runner)

            with (
                mock.patch.dict(
                    sys.modules,
                    {"ytb_tg_backup.dev.youtube_membership": module},
                ),
                mock.patch(
                    "ytb_tg_backup.cli.signal.getsignal",
                    return_value=signal.SIG_DFL,
                ),
                mock.patch(
                    "ytb_tg_backup.cli.signal.signal",
                    side_effect=install_handler,
                ) as set_signal,
            ):
                result = main(
                    [
                        "--config",
                        str(config_path),
                        "dev",
                        "youtube-membership",
                        "run",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertTrue(captured["stop_event"].is_set())
        runner.run_forever.assert_called_once_with()
        runner.close.assert_called_once_with()
        self.assertEqual(set_signal.call_count, 4)


if __name__ == "__main__":
    unittest.main()
