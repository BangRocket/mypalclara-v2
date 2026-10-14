import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from frontend import server_client


class ServerLauncherTests(unittest.TestCase):
    def test_ready_server_is_reused(self) -> None:
        with (
            patch.object(server_client, "server_is_ready", return_value=True),
            patch("frontend.server_client.subprocess.Popen") as popen,
        ):
            server_client.ensure_server()

        popen.assert_not_called()

    def test_missing_server_is_started_detached(self) -> None:
        process = Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            with (
                patch.object(
                    server_client,
                    "SERVER_LOCK_PATH",
                    temporary_path / "server.lock",
                ),
                patch.object(
                    server_client,
                    "SERVER_LOG_PATH",
                    temporary_path / "server.log",
                ),
                patch.object(
                    server_client,
                    "server_is_ready",
                    side_effect=[False, False, True],
                ),
                patch(
                    "frontend.server_client.subprocess.Popen", return_value=process
                ) as popen,
                patch("frontend.server_client.time.sleep"),
            ):
                server_client.ensure_server(timeout=1)

        if sys.platform == "win32":
            self.assertIn("creationflags", popen.call_args.kwargs)
        else:
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(
            popen.call_args.args[0],
            [server_client.sys.executable, "-m", "backend.server"],
        )


class ServerUrlTests(unittest.TestCase):
    def test_http_server_url_becomes_websocket_url(self) -> None:
        with patch.object(server_client, "SERVER_URL", "http://127.0.0.1:8765"):
            self.assertEqual(
                server_client._events_url(), "ws://127.0.0.1:8765/v1/events"
            )
