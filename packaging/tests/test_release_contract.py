from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ReleaseContractTests(unittest.TestCase):
    def test_pyinstaller_is_windowed_and_bundles_only_frontend_data(self) -> None:
        spec = (ROOT / "packaging" / "tideguard.spec").read_text(encoding="utf-8")
        self.assertIn('console=False', spec)
        self.assertIn('datas=[(str(FRONTEND_DIST), "frontend/dist")]', spec)
        self.assertNotRegex(spec, re.compile(r"\.env|sqlite3|credentials\.json", re.I))

    def test_installer_is_per_user_and_uses_runtime_mutex(self) -> None:
        installer = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
        launcher = (ROOT / "desktop" / "tideguard_desktop.py").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", installer)
        self.assertIn(r"DefaultDirName={localappdata}\Programs\Tideguard", installer)
        self.assertIn("MicrosoftEdgeWebview2Setup.exe", installer)
        self.assertIn("F3017226-FE2A-4295-8BDF-00C3A9A7E4C5", installer)
        mutex = re.search(r'^AppMutex=(.+)$', installer, re.MULTILINE)
        self.assertIsNotNone(mutex)
        for mutex_name in mutex.group(1).split(","):
            self.assertIn(mutex_name, launcher)
        self.assertIn('Parameters: "--credentials"', installer)
        self.assertIn('Parameters: "--daemon"', installer)
        self.assertIn('Parameters: "--stop-daemon"', installer)
        self.assertIn(r'Name: "{userstartup}\Tideguard 后台服务"', installer)
        self.assertIn('Tasks: autostart', installer)
        self.assertNotRegex(
            installer,
            re.compile(r'Name: "autostart"[^\r\n]*Flags:\s*unchecked'),
        )
        self.assertIn('Type: filesandordirs; Name: "{app}\\_internal"', installer)
        self.assertNotIn('Type: filesandordirs; Name: "{app}\\*"', installer)

    def test_release_workflow_has_narrow_permissions(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^permissions:\s*\n  contents: read$")
        self.assertRegex(workflow, r"(?m)^    permissions:\s*\n      contents: write$")
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093", workflow)
        self.assertIn("innosetup-6.7.1.exe", workflow)
        self.assertIn("4d11e8050b6185e0d49bd9e8cc661a7a59f44959a621d31d11033124c4e8a7b0", workflow)
        self.assertIn("Get-AuthenticodeSignature", workflow)
        self.assertNotIn("choco install innosetup", workflow)
        self.assertIn("EXPECTED_SHA: ${{ github.sha }}", workflow)
        self.assertIn("git/ref/tags/$tag", workflow)
        self.assertIn("gh release", workflow)

    def test_launcher_has_no_all_interface_bind(self) -> None:
        launcher = (ROOT / "desktop" / "tideguard_desktop.py").read_text(encoding="utf-8")
        self.assertNotIn('"0.0.0.0"', launcher)
        self.assertIn('def _reserve_loopback_socket(port: int = LOCAL_PORT)', launcher)
        self.assertIn('sock.bind(("127.0.0.1", port))', launcher)

    def test_release_build_rejects_vite_environment_injection(self) -> None:
        build_script = (ROOT / "packaging" / "build-release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("Assert-SafeFrontendBuildEnvironment", build_script)
        self.assertIn('Get-ChildItem Env:', build_script)
        self.assertIn('$_.Name -like "VITE_*"', build_script)
        self.assertIn('$_.Name -like ".env*"', build_script)


if __name__ == "__main__":
    unittest.main()
