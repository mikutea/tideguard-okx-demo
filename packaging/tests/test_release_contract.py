from __future__ import annotations

import re
import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ReleaseContractTests(unittest.TestCase):
    def test_pyinstaller_is_windowed_and_bundles_only_frontend_data(self) -> None:
        spec = (ROOT / "packaging" / "tideguard.spec").read_text(encoding="utf-8")
        self.assertIn('console=False', spec)
        self.assertIn('icon=str(APP_ICON)', spec)
        self.assertTrue((ROOT / "assets" / "brand" / "moheng.ico").is_file())
        self.assertIn('(str(FRONTEND_DIST), "frontend/dist")', spec)
        self.assertIn('(str(BRAND_ASSETS), "assets/brand")', spec)
        self.assertIn('(str(PROJECT_LICENSE), ".")', spec)
        self.assertIn('(str(THIRD_PARTY_NOTICES), ".")', spec)
        self.assertIn('version=str(VERSION_INFO)', spec)
        self.assertTrue((ROOT / "LICENSE").is_file())
        self.assertTrue((ROOT / "THIRD-PARTY-NOTICES.md").is_file())
        self.assertTrue((ROOT / "packaging" / "windows-version-info.txt").is_file())
        self.assertNotRegex(spec, re.compile(r"\.env|sqlite3|credentials\.json", re.I))

    def test_installer_is_per_user_and_uses_runtime_mutex(self) -> None:
        installer = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
        launcher = (ROOT / "desktop" / "tideguard_desktop.py").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", installer)
        self.assertIn(r"DefaultDirName={localappdata}\Programs\Tideguard", installer)
        self.assertIn("AppName={#MyAppName}", installer)
        self.assertIn(r"LicenseFile=..\LICENSE", installer)
        self.assertIn("UsePreviousGroup=no", installer)
        self.assertIn("UsePreviousTasks=no", installer)
        self.assertIn(r"SetupIconFile=..\assets\brand\moheng.ico", installer)
        self.assertIn("MicrosoftEdgeWebview2Setup.exe", installer)
        self.assertIn("F3017226-FE2A-4295-8BDF-00C3A9A7E4C5", installer)
        mutex = re.search(r'^AppMutex=(.+)$', installer, re.MULTILINE)
        self.assertIsNotNone(mutex)
        for mutex_name in mutex.group(1).split(","):
            self.assertIn(mutex_name, launcher)
        self.assertIn('Parameters: "--credentials"', installer)
        self.assertIn('Parameters: "--daemon"', installer)
        self.assertIn('Parameters: "--stop-daemon"', installer)
        self.assertIn('Name: "{group}\\启动墨衡后台服务"', installer)
        self.assertIn('ValueName: "Tideguard.BackgroundService"', installer)
        self.assertIn('ValueData: "{code:GetAutostartCommand}"', installer)
        self.assertIn("uninsdeletevalue", installer)
        self.assertIn('Tasks: autostart', installer)
        self.assertRegex(
            installer,
            re.compile(r'Name: "autostart"[^\r\n]*Flags:\s*unchecked'),
        )
        self.assertIn('Type: filesandordirs; Name: "{app}\\_internal"', installer)
        self.assertIn('Type: filesandordirs; Name: "{userprograms}\\Tideguard"', installer)
        self.assertIn('Type: files; Name: "{autodesktop}\\Tideguard.lnk"', installer)
        self.assertIn('Type: files; Name: "{userstartup}\\Tideguard 后台服务.lnk"', installer)
        self.assertIn('Type: files; Name: "{userstartup}\\墨衡后台服务.lnk"', installer)
        self.assertRegex(installer, r"else if ResultCode <> 0 then\s+Result := False")
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
        self.assertIn("git merge-base --is-ancestor", workflow)
        self.assertIn("refs/remotes/origin/main", workflow)
        self.assertIn("--prerelease", workflow)
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
        self.assertIn('Invoke-Native "corepack" "pnpm" "test"', build_script)
        self.assertIn("WebView2BootstrapperPath", build_script)
        self.assertIn("Get-AuthenticodeSignature", build_script)
        self.assertIn("[IO.File]::WriteAllLines", build_script)
        self.assertIn("$StagedSourceRoot", build_script)
        self.assertIn('Join-Path $ProjectRoot "LICENSE"', build_script)
        self.assertIn("sourceRevision", build_script)
        self.assertIn("sourceTreeDirty", build_script)
        self.assertIn("$stagedBuildInfo", build_script)
        self.assertIn("BUILD_REVISION", build_script)
        self.assertIn("mapped/UNC share", build_script)
        self.assertIn(
            '(Join-Path $StagedSourceRoot "packaging\\tideguard.spec")',
            build_script,
        )
        self.assertIn('$_.Name -like "VITE_*"', build_script)
        self.assertIn('$_.Name -like ".env*"', build_script)

    def test_all_public_versions_match(self) -> None:
        backend = tomllib.loads(
            (ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        frontend = json.loads(
            (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
        )["version"]
        desktop_source = (ROOT / "desktop" / "tideguard_desktop.py").read_text(
            encoding="utf-8"
        )
        installer_source = (ROOT / "packaging" / "installer.iss").read_text(
            encoding="utf-8"
        )
        config_source = (ROOT / "backend" / "src" / "okx_demo_lab" / "config.py").read_text(
            encoding="utf-8"
        )
        package_source = (ROOT / "backend" / "src" / "okx_demo_lab" / "__init__.py").read_text(
            encoding="utf-8"
        )
        version_info_source = (ROOT / "packaging" / "windows-version-info.txt").read_text(
            encoding="utf-8"
        )
        desktop = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"$', desktop_source, re.M)
        config = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"$', config_source, re.M)
        package = re.search(r'^__version__\s*=\s*"([^"]+)"$', package_source, re.M)
        installer = re.search(
            r'^\s*#define MyAppVersion "([^"]+)"$', installer_source, re.M
        )
        windows = re.search(
            r"StringStruct\('ProductVersion',\s*'([^']+)'\)",
            version_info_source,
        )
        self.assertIsNotNone(desktop)
        self.assertIsNotNone(config)
        self.assertIsNotNone(package)
        self.assertIsNotNone(installer)
        self.assertIsNotNone(windows)
        self.assertEqual(
            {
                backend,
                frontend,
                desktop.group(1),
                config.group(1),
                package.group(1),
                installer.group(1),
                windows.group(1),
            },
            {"0.4.0"},
        )


if __name__ == "__main__":
    unittest.main()
