from pathlib import Path
import signal
import tempfile
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


if __name__ == "__main__":
    unittest.main()
