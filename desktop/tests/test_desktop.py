from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from unittest.mock import patch


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

    def test_credential_delete_requires_exact_confirmation(self) -> None:
        api = desktop.CredentialManagerApi()
        with patch("okx_demo_lab.secrets.delete_credentials") as delete:
            result = api.remove("wrong")
        self.assertFalse(result["ok"])
        delete.assert_not_called()

    def test_self_test_never_configures_user_logging(self) -> None:
        with (
            patch.object(desktop, "_configure_logging") as configure_logging,
            patch.object(desktop, "_self_test", return_value=0) as self_test,
        ):
            self.assertEqual(desktop.run(["--self-test"]), 0)
        configure_logging.assert_not_called()
        self_test.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
