from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from desktop import tideguard_desktop as desktop  # noqa: E402


class DesktopContractTests(unittest.TestCase):
    def test_source_resource_root_is_project_root(self) -> None:
        self.assertEqual(desktop._resource_root(), PROJECT_ROOT)

    def test_frozen_resource_root_uses_meipass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(desktop.sys, "frozen", True, create=True),
                patch.object(desktop.sys, "_MEIPASS", temp_dir, create=True),
            ):
                self.assertEqual(desktop._resource_root(), Path(temp_dir).resolve())

    def test_loopback_socket_never_binds_all_interfaces(self) -> None:
        self.assertEqual(
            signature(desktop._reserve_loopback_socket).parameters["port"].default,
            desktop.LOCAL_PORT,
        )
        sock, port = desktop._reserve_loopback_socket(0)
        try:
            self.assertEqual(sock.getsockname(), ("127.0.0.1", port))
            self.assertGreater(port, 0)
        finally:
            sock.close()

    def test_loopback_socket_excludes_a_second_backend(self) -> None:
        first, port = desktop._reserve_loopback_socket(0)
        try:
            with self.assertRaises(OSError):
                desktop._reserve_loopback_socket(port)
        finally:
            first.close()

    @unittest.skipUnless(os.name == "nt", "Windows named mutex")
    def test_named_mutex_rejects_second_instance(self) -> None:
        name = f"Local\\Tideguard.Test.{os.getpid()}"
        first = desktop.SingleInstance(name)
        second = desktop.SingleInstance(name)
        try:
            first.acquire()
            with self.assertRaises(desktop.AlreadyRunningError):
                second.acquire()
        finally:
            second.close()
            first.close()

    @unittest.skipUnless(os.name == "nt", "Windows named event")
    def test_backend_stop_signal_round_trip(self) -> None:
        name = f"Local\\Tideguard.StopTest.{os.getpid()}"
        signal = desktop.BackendStopSignal(name)
        try:
            signal.create()
            self.assertTrue(desktop.BackendStopSignal.signal(name))
            self.assertTrue(signal.wait(100))
        finally:
            signal.close()

    def test_frontend_bundle_requires_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(desktop, "_resource_root", return_value=Path(temp_dir)):
                with self.assertRaises(desktop.DesktopStartupError):
                    desktop._frontend_dist()

    def test_credential_api_never_returns_secret_values(self) -> None:
        api = desktop.CredentialManagerApi()
        with patch("okx_demo_lab.secrets.set_credentials") as save:
            result = api.save("api-key-value", "secret-value", "passphrase-value")
        self.assertTrue(result["ok"])
        self.assertNotIn("api-key-value", repr(result))
        self.assertNotIn("secret-value", repr(result))
        self.assertNotIn("passphrase-value", repr(result))
        save.assert_called_once()
        self.assertEqual(save.call_args.args[1], "demo")

    def test_live_credentials_are_written_to_the_separate_service(self) -> None:
        api = desktop.CredentialManagerApi()
        with patch("okx_demo_lab.secrets.set_credentials") as save:
            result = api.save("live-key", "live-secret", "live-passphrase", "live")
        self.assertTrue(result["ok"])
        self.assertEqual(result["environment"], "live")
        self.assertEqual(save.call_args.args[1], "live")

    def test_credential_delete_requires_exact_confirmation(self) -> None:
        api = desktop.CredentialManagerApi()
        with patch("okx_demo_lab.secrets.delete_credentials") as delete:
            result = api.remove("wrong")
        self.assertFalse(result["ok"])
        delete.assert_not_called()

    def test_live_credential_delete_uses_a_distinct_confirmation(self) -> None:
        api = desktop.CredentialManagerApi()
        with patch("okx_demo_lab.secrets.delete_credentials") as delete:
            wrong = api.remove("DELETE-DEMO-CREDENTIALS", "live")
            accepted = api.remove("DELETE-LIVE-CREDENTIALS", "live")
        self.assertFalse(wrong["ok"])
        self.assertTrue(accepted["ok"])
        delete.assert_called_once_with("live")

    def test_self_test_never_configures_user_logging(self) -> None:
        with (
            patch.object(desktop, "_configure_logging") as configure_logging,
            patch.object(desktop, "_self_test", return_value=0) as self_test,
        ):
            self.assertEqual(desktop.run(["--self-test"]), 0)
        configure_logging.assert_not_called()
        self_test.assert_called_once_with()

    def test_daemon_mode_never_opens_a_webview(self) -> None:
        logger = Mock()
        with (
            patch.object(desktop, "_configure_logging", return_value=(logger, Path("log"))),
            patch.object(desktop, "_run_daemon", return_value=0) as daemon,
            patch.dict(sys.modules, {"webview": Mock()}),
        ):
            self.assertEqual(desktop.run(["--daemon"]), 0)
        daemon.assert_called_once_with(logger)

    def test_stop_daemon_is_idempotent_when_backend_is_absent(self) -> None:
        logger = Mock()
        with (
            patch.object(desktop, "_configure_logging", return_value=(logger, Path("log"))),
            patch.object(desktop.BackendStopSignal, "signal", return_value=False),
            patch.object(desktop, "_probe_backend", return_value=False),
        ):
            self.assertEqual(desktop.run(["--stop-daemon"]), 0)

    def test_stop_daemon_waits_until_health_endpoint_disappears(self) -> None:
        with (
            patch.object(desktop.BackendStopSignal, "signal", return_value=True),
            patch.object(desktop, "_probe_backend", side_effect=[True, True, False]),
            patch.object(desktop.time, "sleep") as sleep,
        ):
            self.assertTrue(desktop._stop_daemon_and_wait(timeout=1.0))
        self.assertGreaterEqual(sleep.call_count, 1)

    def test_backend_probe_requires_exact_tideguard_identity(self) -> None:
        class Response:
            status = 200

            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return self.payload

        opener = Mock()
        opener.open.return_value = Response(
            b'{"status":"ok","app":"Tideguard","environment":"demo","version":"0.4.0"}'
        )
        with patch.object(desktop.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(desktop._probe_backend())

        opener.open.return_value = Response(
            b'{"status":"ok","app":"other","environment":"demo","version":"0.4.0"}'
        )
        with patch.object(desktop.urllib.request, "build_opener", return_value=opener):
            self.assertFalse(desktop._probe_backend())

        opener.open.return_value = Response(
            b'{"status":"ok","app":"Tideguard","environment":"live","version":"0.4.0"}'
        )
        with patch.object(desktop.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(desktop._probe_backend())


if __name__ == "__main__":
    unittest.main()
