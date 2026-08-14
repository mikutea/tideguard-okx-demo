from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import ModuleType
from typing import Any


APP_NAME = "Tideguard"
APP_MUTEX = r"Local\Tideguard.Desktop.2d2663b4-03de-4f3d-bc77-12556deba51f"
CREDENTIALS_MUTEX = r"Local\Tideguard.Credentials.2d2663b4-03de-4f3d-bc77-12556deba51f"
ERROR_ALREADY_EXISTS = 183
STARTUP_TIMEOUT_SECONDS = 20.0
LOCAL_PORT = 8791
WINDOW_SIZE = (1440, 900)
WINDOW_MIN_SIZE = (1080, 680)


class DesktopStartupError(RuntimeError):
    """Raised when the private local desktop host cannot start safely."""


class AlreadyRunningError(DesktopStartupError):
    """Raised when another Tideguard desktop process owns the app mutex."""


def _local_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        root = str(Path.home() / "AppData" / "Local")
    path = Path(root) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _configure_logging() -> tuple[logging.Logger, Path]:
    log_dir = _local_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "desktop.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    return logging.getLogger("tideguard.desktop"), log_path


def _show_error(title: str, message: str) -> None:
    if os.name == "nt":
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, message, title, 0x00000010 | 0x00001000)
    else:  # pragma: no cover - the distributable is Windows-only
        sys.stderr.write(f"{title}: {message}\n")


class SingleInstance:
    """Own a per-user Windows mutex for the lifetime of the desktop process."""

    def __init__(self, name: str = APP_MUTEX) -> None:
        self.name = name
        self._handle: int | None = None
        self._kernel32: Any | None = None

    def acquire(self) -> None:
        if os.name != "nt":  # pragma: no cover - useful for source linting on non-Windows
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise DesktopStartupError("无法创建单实例锁")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise AlreadyRunningError("Tideguard 已经在运行")
        self._kernel32 = kernel32
        self._handle = int(handle)

    def close(self) -> None:
        if self._handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        self._handle = None
        self._kernel32 = None

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _resource_root() -> Path:
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if not bundle_root:
            raise DesktopStartupError("冻结资源目录不可用")
        return Path(bundle_root).resolve()
    return Path(__file__).resolve().parents[1]


def _frontend_dist() -> Path:
    dist = (_resource_root() / "frontend" / "dist").resolve()
    if not dist.is_dir() or not (dist / "index.html").is_file():
        raise DesktopStartupError("桌面前端资源缺失，请重新安装 Tideguard")
    return dist


def _reserve_loopback_socket(port: int = LOCAL_PORT) -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # The source launcher also owns 127.0.0.1:8791. Keeping the same fixed
        # endpoint makes the OS socket reservation a cross-entrypoint process
        # lock, so source and packaged trading backends cannot share one state DB.
        sock.bind(("127.0.0.1", port))
        return sock, int(sock.getsockname()[1])
    except BaseException:
        sock.close()
        raise


def _configure_application(frontend_dist: Path, port: int) -> Any:
    from fastapi.staticfiles import StaticFiles
    from okx_demo_lab import main as app_module

    local_authority = f"127.0.0.1:{port}"
    app_module.ALLOWED_HOSTS.add(local_authority)
    app_module.ALLOWED_ORIGINS.add(f"http://{local_authority}")

    # The business app computes its source-tree path at import time. Replace only
    # its final catch-all route so frozen builds serve the explicitly bundled UI.
    app_module.app.router.routes[:] = [
        route
        for route in app_module.app.router.routes
        if getattr(route, "name", None) not in {"frontend", "development_root"}
    ]
    app_module.app.mount(
        "/",
        StaticFiles(directory=str(frontend_dist), html=True),
        name="frontend",
    )
    return app_module.app


class LocalServer:
    def __init__(self, app: Any, sock: socket.socket, port: int) -> None:
        import uvicorn

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_config=None,
            log_level="warning",
            lifespan="on",
            workers=1,
        )
        self.server = uvicorn.Server(config)
        self.sock = sock
        self.port = port
        self.failure_type: str | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="tideguard-loopback",
            daemon=True,
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _run(self) -> None:
        try:
            asyncio.run(self.server.serve(sockets=[self.sock]))
        except BaseException as exc:  # do not persist exception messages or request data
            self.failure_type = type(exc).__name__

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            if self.failure_type or (not self.thread.is_alive() and not self.server.started):
                raise DesktopStartupError(
                    f"本地服务启动失败（{self.failure_type or 'ServerStopped'}）"
                )
            if self.server.started:
                try:
                    with opener.open(f"{self.url}/healthz", timeout=0.5) as response:
                        if response.status == 200:
                            return
                except OSError:
                    pass
            time.sleep(0.05)
        raise DesktopStartupError("本地服务启动超时")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=10)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(timeout=2)
        try:
            self.sock.close()
        except OSError:
            pass


def _self_test() -> int:
    # Exercise the frozen FastAPI/Uvicorn/resource path without touching the
    # user's database or Credential Manager.
    with tempfile.TemporaryDirectory(prefix="TideguardSelfTest-") as temp_dir:
        os.environ["TIDEGUARD_DATA_DIR"] = temp_dir
        os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        dist = _frontend_dist()
        sock, port = _reserve_loopback_socket(0)
        server: LocalServer | None = None
        try:
            app = _configure_application(dist, port)
            server = LocalServer(app, sock, port)
            server.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"{server.url}/api/v1/ml/status", timeout=2) as response:
                if response.status != 200:
                    raise DesktopStartupError("冻结模型状态端点自检失败")
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or "engine" not in payload:
                    raise DesktopStartupError("冻结模型状态响应结构异常")
        finally:
            if server is not None:
                server.stop()
            else:
                sock.close()
    return 0


class CredentialManagerApi:
    """Minimal bridge used only by the isolated credential-management window."""

    @staticmethod
    def _failure(exc: BaseException) -> dict[str, object]:
        return {
            "ok": False,
            "configured": False,
            "message": f"Windows Credential Manager 操作失败（{type(exc).__name__}）",
        }

    def status(self) -> dict[str, object]:
        try:
            from okx_demo_lab.secrets import get_credentials

            configured = get_credentials() is not None
            return {
                "ok": True,
                "configured": configured,
                "message": "已配置" if configured else "尚未配置",
            }
        except BaseException as exc:
            return self._failure(exc)

    def save(
        self, api_key: object, api_secret: object, passphrase: object
    ) -> dict[str, object]:
        try:
            from okx_demo_lab.secrets import Credentials, set_credentials

            if not all(
                isinstance(value, str)
                for value in (api_key, api_secret, passphrase)
            ):
                raise ValueError("invalid credential field type")
            set_credentials(Credentials(api_key, api_secret, passphrase))
            return {
                "ok": True,
                "configured": True,
                "message": "凭证已安全保存",
            }
        except BaseException as exc:
            return self._failure(exc)

    def remove(self, confirmation: object) -> dict[str, object]:
        if confirmation != "DELETE-DEMO-CREDENTIALS":
            return {
                "ok": False,
                "configured": True,
                "message": "确认短语不匹配，未删除",
            }
        try:
            from okx_demo_lab.secrets import delete_credentials

            delete_credentials()
            return {
                "ok": True,
                "configured": False,
                "message": "凭证已删除",
            }
        except BaseException as exc:
            return self._failure(exc)


_CREDENTIALS_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Tideguard 凭证管理</title>
  <style>
    :root { color-scheme: dark; font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #07111f; color: #e9f1ff; }
    main { max-width: 620px; margin: 0 auto; padding: 30px; }
    .eyebrow { color: #52d3b8; font-size: 12px; font-weight: 700; letter-spacing: .16em; }
    h1 { margin: 8px 0; font-size: 28px; }
    .note { color: #9eb0ca; line-height: 1.65; margin: 0 0 22px; }
    .card { background: #0d1b2d; border: 1px solid #20344f; border-radius: 18px; padding: 22px; box-shadow: 0 18px 45px #0006; }
    label { display: block; margin: 14px 0 6px; color: #b8c7dd; font-size: 13px; }
    input { width: 100%; border: 1px solid #29415f; border-radius: 10px; padding: 12px; background: #081524; color: #fff; outline: none; }
    input:focus { border-color: #52d3b8; box-shadow: 0 0 0 3px #52d3b822; }
    .actions { display: flex; gap: 10px; margin-top: 18px; }
    button { border: 0; border-radius: 10px; padding: 11px 17px; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .55; cursor: wait; }
    .primary { background: #52d3b8; color: #06151a; }
    .danger { background: #3a1b27; color: #ffb7c5; }
    .status { margin-top: 18px; border-radius: 10px; padding: 12px; background: #091725; color: #aebed4; min-height: 44px; }
    .delete { border-top: 1px solid #20344f; margin-top: 24px; padding-top: 18px; }
    code { color: #ffb7c5; }
  </style>
</head>
<body>
<main>
  <div class="eyebrow">WINDOWS CREDENTIAL MANAGER</div>
  <h1>OKX Demo 凭证</h1>
  <p class="note">秘密只写入当前 Windows 用户的凭证管理器，不经过 Tideguard 本地 HTTP 服务，也不会进入安装包、日志或项目文件。</p>
  <section class="card">
    <form id="save-form" autocomplete="off">
      <label for="api-key">OKX Demo API Key</label>
      <input id="api-key" type="password" autocomplete="new-password" spellcheck="false" required>
      <label for="api-secret">OKX Demo Secret</label>
      <input id="api-secret" type="password" autocomplete="new-password" spellcheck="false" required>
      <label for="passphrase">OKX Demo Passphrase</label>
      <input id="passphrase" type="password" autocomplete="new-password" spellcheck="false" required>
      <div class="actions"><button id="save" class="primary" type="submit">安全保存</button></div>
    </form>
    <div class="delete">
      <label for="confirmation">删除时输入 <code>DELETE-DEMO-CREDENTIALS</code></label>
      <input id="confirmation" type="text" autocomplete="off" spellcheck="false">
      <div class="actions"><button id="remove" class="danger" type="button">删除本机凭证</button></div>
    </div>
    <div id="status" class="status" role="status">正在读取状态…</div>
  </section>
</main>
<script>
  const statusNode = document.getElementById('status');
  const saveButton = document.getElementById('save');
  const removeButton = document.getElementById('remove');
  function show(result) { statusNode.textContent = result.message || '操作已完成'; }
  async function refresh() { show(await window.pywebview.api.status()); }
  window.addEventListener('pywebviewready', refresh);
  document.getElementById('save-form').addEventListener('submit', async (event) => {
    event.preventDefault(); saveButton.disabled = true;
    try {
      const result = await window.pywebview.api.save(
        document.getElementById('api-key').value,
        document.getElementById('api-secret').value,
        document.getElementById('passphrase').value
      );
      show(result);
      if (result.ok) event.target.reset();
    } catch (_) { statusNode.textContent = '凭证保存失败'; }
    finally { saveButton.disabled = false; }
  });
  removeButton.addEventListener('click', async () => {
    removeButton.disabled = true;
    try {
      const result = await window.pywebview.api.remove(document.getElementById('confirmation').value);
      show(result);
      if (result.ok) document.getElementById('confirmation').value = '';
    } catch (_) { statusNode.textContent = '凭证删除失败'; }
    finally { removeButton.disabled = false; }
  });
</script>
</body>
</html>
"""


def _run_credentials_window() -> int:
    import webview

    webview.create_window(
        "Tideguard 凭证管理",
        html=_CREDENTIALS_HTML,
        js_api=CredentialManagerApi(),
        width=640,
        height=690,
        resizable=False,
        background_color="#07111f",
    )
    webview.start(gui="edgechromium", debug=False)
    return 0


def run(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--self-test"]:
        # Build verification must not create logs or state under the real
        # user's LOCALAPPDATA. It reports success/failure only by exit code.
        self_test_instance = SingleInstance()
        try:
            self_test_instance.acquire()
            return _self_test()
        except BaseException:
            return 1
        finally:
            self_test_instance.close()

    logger, log_path = _configure_logging()
    credential_mode = argv == ["--credentials"]
    instance = SingleInstance(CREDENTIALS_MUTEX if credential_mode else APP_MUTEX)
    local_server: LocalServer | None = None
    reserved_socket: socket.socket | None = None
    try:
        instance.acquire()
        if credential_mode:
            return _run_credentials_window()
        if argv:
            raise DesktopStartupError("不支持的启动参数")

        dist = _frontend_dist()
        reserved_socket, port = _reserve_loopback_socket()
        app = _configure_application(dist, port)
        local_server = LocalServer(app, reserved_socket, port)
        local_server.start()
        reserved_socket = None
        logger.info("Desktop host started on loopback port %d", port)

        import webview

        webview.create_window(
            APP_NAME,
            local_server.url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=WINDOW_MIN_SIZE,
            background_color="#07111f",
        )
        webview.start(gui="edgechromium", debug=False)
        return 0
    except AlreadyRunningError:
        _show_error(APP_NAME, "Tideguard 已经在运行。请切换到现有窗口。")
        return 2
    except BaseException as exc:
        error_type = type(exc).__name__
        logger.error("Desktop startup failed: %s", error_type)
        _show_error(
            f"{APP_NAME} 启动失败",
            "无法启动 Tideguard 桌面窗口。请确认 Microsoft Edge WebView2 Runtime "
            f"可用后重试。错误类型：{error_type}\n日志：{log_path}",
        )
        return 1
    finally:
        if local_server is not None:
            local_server.stop()
        elif reserved_socket is not None:
            reserved_socket.close()
        instance.close()


if __name__ == "__main__":
    raise SystemExit(run())
