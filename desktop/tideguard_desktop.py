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


APP_NAME = "Tideguard"  # Stable internal identity used by health checks and data paths.
PUBLIC_APP_NAME = "墨衡 MOHENG"
APP_VERSION = "0.4.0"
UI_MUTEX = r"Local\Tideguard.Desktop.2d2663b4-03de-4f3d-bc77-12556deba51f"
APP_MUTEX = UI_MUTEX
BACKEND_MUTEX = r"Local\Tideguard.Backend.2d2663b4-03de-4f3d-bc77-12556deba51f"
CREDENTIALS_MUTEX = r"Local\Tideguard.Credentials.2d2663b4-03de-4f3d-bc77-12556deba51f"
BACKEND_STOP_EVENT = r"Local\Tideguard.BackendStop.2d2663b4-03de-4f3d-bc77-12556deba51f"
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
STARTUP_TIMEOUT_SECONDS = 20.0
LOCAL_PORT = 8791
WINDOW_SIZE = (1440, 900)
WINDOW_MIN_SIZE = (1080, 680)
BACKEND_URL = f"http://127.0.0.1:{LOCAL_PORT}"


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
            raise AlreadyRunningError("墨衡已经在运行")
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


class BackendStopSignal:
    """A per-user named event used only to stop the background daemon."""

    EVENT_MODIFY_STATE = 0x0002
    SYNCHRONIZE = 0x00100000

    def __init__(self, name: str = BACKEND_STOP_EVENT) -> None:
        self._name = name
        self._handle: int | None = None
        self._kernel32: Any | None = None

    def create(self) -> None:
        if os.name != "nt":  # pragma: no cover
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateEventW(None, True, False, self._name)
        if not handle:
            raise DesktopStartupError("无法创建后台停止事件")
        self._kernel32 = kernel32
        self._handle = int(handle)

    def wait(self, timeout_ms: int) -> bool:
        if self._handle is None or self._kernel32 is None:
            time.sleep(max(0, timeout_ms) / 1_000)
            return False
        return (
            self._kernel32.WaitForSingleObject(
                ctypes.c_void_p(self._handle), max(0, int(timeout_ms))
            )
            == WAIT_OBJECT_0
        )

    @classmethod
    def signal(cls, name: str = BACKEND_STOP_EVENT) -> bool:
        if os.name != "nt":  # pragma: no cover
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenEventW.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.OpenEventW.restype = ctypes.c_void_p
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.SetEvent.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenEventW(cls.EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)

    def close(self) -> None:
        if self._handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        self._handle = None
        self._kernel32 = None


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
        raise DesktopStartupError("桌面前端资源缺失，请重新安装墨衡 MOHENG")
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


def _probe_backend(url: str = BACKEND_URL, *, timeout: float = 0.75) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}/healthz", timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("app") == APP_NAME
        and payload.get("environment") in {"demo", "live"}
        and payload.get("version") == APP_VERSION
    )


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
        while time.monotonic() < deadline:
            if self.failure_type or (not self.thread.is_alive() and not self.server.started):
                raise DesktopStartupError(
                    f"本地服务启动失败（{self.failure_type or 'ServerStopped'}）"
                )
            if self.server.started and _probe_backend(self.url, timeout=0.5):
                return
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

    @staticmethod
    def _environment(value: object) -> str:
        if value not in {"demo", "live"}:
            raise ValueError("credential environment must be demo or live")
        return str(value)

    def status(self, environment: object = "demo") -> dict[str, object]:
        try:
            from okx_demo_lab.secrets import get_credentials

            selected = self._environment(environment)
            configured = get_credentials(selected) is not None
            return {
                "ok": True,
                "configured": configured,
                "environment": selected,
                "message": "已配置" if configured else "尚未配置",
            }
        except BaseException as exc:
            return self._failure(exc)

    def save(
        self,
        api_key: object,
        api_secret: object,
        passphrase: object,
        environment: object = "demo",
    ) -> dict[str, object]:
        try:
            from okx_demo_lab.secrets import Credentials, set_credentials

            if not all(
                isinstance(value, str)
                for value in (api_key, api_secret, passphrase)
            ):
                raise ValueError("invalid credential field type")
            selected = self._environment(environment)
            set_credentials(Credentials(api_key, api_secret, passphrase), selected)
            return {
                "ok": True,
                "configured": True,
                "environment": selected,
                "message": "凭证已安全保存",
            }
        except BaseException as exc:
            return self._failure(exc)

    def remove(
        self, confirmation: object, environment: object = "demo"
    ) -> dict[str, object]:
        try:
            selected = self._environment(environment)
        except BaseException as exc:
            return self._failure(exc)
        required = (
            "DELETE-DEMO-CREDENTIALS"
            if selected == "demo"
            else "DELETE-LIVE-CREDENTIALS"
        )
        if confirmation != required:
            return {
                "ok": False,
                "configured": True,
                "environment": selected,
                "message": "确认短语不匹配，未删除",
            }
        try:
            from okx_demo_lab.secrets import delete_credentials

            delete_credentials(selected)
            return {
                "ok": True,
                "configured": False,
                "environment": selected,
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
  <title>墨衡凭证管理</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, "Segoe UI", "Microsoft YaHei UI", sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: radial-gradient(circle at 50% -20%, #17324a 0, #08121b 46%, #050a10 100%); color: #e8efec; }
    main { max-width: 700px; margin: 0 auto; padding: 28px; }
    header { display: flex; align-items: center; gap: 14px; margin-bottom: 20px; }
    header img { width: 58px; height: 58px; border-radius: 14px; }
    .eyebrow { color: #7ed6c4; font-size: 11px; font-weight: 750; letter-spacing: .18em; }
    h1 { margin: 4px 0 0; font-size: 26px; font-weight: 680; letter-spacing: .04em; }
    .tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; }
    .tab { min-height: 46px; border: 1px solid #274152; background: #0b1822; color: #a9bbc2; }
    .tab.active { border-color: #7ed6c4; color: #e8f8f3; background: #143129; }
    .tab.live.active { border-color: #d75b51; color: #ffe2dd; background: #3a1717; }
    .note { color: #9eb0b5; line-height: 1.65; margin: 0 0 18px; font-size: 13px; }
    .warning { display: none; margin-bottom: 14px; padding: 13px 14px; border: 1px solid #873d38; border-radius: 10px; background: #2e1414; color: #ffd5cf; line-height: 1.55; font-size: 13px; }
    .warning.visible { display: block; }
    .card { background: #0b161fdd; border: 1px solid #223947; border-radius: 14px; padding: 22px; box-shadow: 0 20px 60px #0007; }
    label { display: block; margin: 14px 0 6px; color: #bdc9c8; font-size: 13px; }
    input { width: 100%; min-height: 44px; border: 1px solid #294554; border-radius: 9px; padding: 11px 12px; background: #061019; color: #fff; outline: none; }
    input:focus { border-color: #7ed6c4; box-shadow: 0 0 0 3px #7ed6c422; }
    .actions { display: flex; gap: 10px; margin-top: 18px; }
    button { min-height: 44px; border: 0; border-radius: 9px; padding: 11px 17px; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .55; cursor: wait; }
    .primary { background: #7ed6c4; color: #061511; }
    .primary.live { background: #d75b51; color: #fff; }
    .danger { background: transparent; border: 1px solid #6f3331; color: #ffaaa2; }
    .status { margin-top: 18px; border-radius: 9px; padding: 12px; border: 1px solid #203945; background: #07131c; color: #b2c1c1; min-height: 44px; }
    .delete { border-top: 1px solid #203945; margin-top: 24px; padding-top: 18px; }
    code { color: #e8b56d; }
  </style>
</head>
<body>
<main>
  <header><img src="__MOHENG_ICON__" alt=""><div><div class="eyebrow">MOHENG · WINDOWS CREDENTIAL MANAGER</div><h1>墨衡凭证管理</h1></div></header>
  <div class="tabs" role="tablist"><button class="tab active" data-env="demo" type="button">OKX 模拟盘</button><button class="tab live" data-env="live" type="button">OKX 实盘</button></div>
  <p class="note">Demo 与 Live 凭证完全隔离，只写入当前 Windows 用户的凭证管理器；不会经过墨衡本地 HTTP 服务，也不会进入安装包、日志或项目文件。</p>
  <div id="live-warning" class="warning"><strong>真实资金凭证</strong><br>仅创建 Read + Trade、禁 Withdraw、绑定固定公网 IP 的独立 API Key。OKX 的 Trade 权限还可能包含转账和配置写操作，因此请使用独立子账户和严格资金上限。</div>
  <section class="card">
    <form id="save-form" autocomplete="off">
      <label id="api-key-label" for="api-key">OKX Demo API Key</label>
      <input id="api-key" type="password" autocomplete="new-password" spellcheck="false" required>
      <label id="secret-label" for="api-secret">OKX Demo Secret</label>
      <input id="api-secret" type="password" autocomplete="new-password" spellcheck="false" required>
      <label id="passphrase-label" for="passphrase">OKX Demo Passphrase</label>
      <input id="passphrase" type="password" autocomplete="new-password" spellcheck="false" required>
      <div class="actions"><button id="save" class="primary" type="submit">安全保存</button></div>
    </form>
    <div class="delete">
      <label for="confirmation">删除时输入 <code id="delete-phrase">DELETE-DEMO-CREDENTIALS</code></label>
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
  let environment = 'demo';
  function show(result) { statusNode.textContent = result.message || '操作已完成'; }
  async function refresh() { show(await window.pywebview.api.status(environment)); }
  function selectEnvironment(next) {
    environment = next;
    document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.env === next));
    const live = next === 'live';
    document.getElementById('live-warning').classList.toggle('visible', live);
    saveButton.classList.toggle('live', live);
    document.getElementById('api-key-label').textContent = `OKX ${live ? 'Live' : 'Demo'} API Key`;
    document.getElementById('secret-label').textContent = `OKX ${live ? 'Live' : 'Demo'} Secret`;
    document.getElementById('passphrase-label').textContent = `OKX ${live ? 'Live' : 'Demo'} Passphrase`;
    document.getElementById('delete-phrase').textContent = live ? 'DELETE-LIVE-CREDENTIALS' : 'DELETE-DEMO-CREDENTIALS';
    document.getElementById('save-form').reset();
    document.getElementById('confirmation').value = '';
    refresh();
  }
  window.addEventListener('pywebviewready', refresh);
  document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => selectEnvironment(tab.dataset.env)));
  document.getElementById('save-form').addEventListener('submit', async (event) => {
    event.preventDefault(); saveButton.disabled = true;
    try {
      const result = await window.pywebview.api.save(
        document.getElementById('api-key').value,
        document.getElementById('api-secret').value,
        document.getElementById('passphrase').value,
        environment
      );
      show(result);
      if (result.ok) event.target.reset();
    } catch (_) { statusNode.textContent = '凭证保存失败'; }
    finally { saveButton.disabled = false; }
  });
  removeButton.addEventListener('click', async () => {
    removeButton.disabled = true;
    try {
      const result = await window.pywebview.api.remove(document.getElementById('confirmation').value, environment);
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

    icon_path = _resource_root() / "assets" / "brand" / "moheng-app-icon.png"
    if not icon_path.is_file():
        raise DesktopStartupError("墨衡品牌资源缺失，请重新安装")
    icon_data = base64.b64encode(icon_path.read_bytes()).decode("ascii")
    credential_html = _CREDENTIALS_HTML.replace(
        "__MOHENG_ICON__", f"data:image/png;base64,{icon_data}"
    )

    webview.create_window(
        "墨衡凭证管理",
        html=credential_html,
        js_api=CredentialManagerApi(),
        width=720,
        height=800,
        min_size=(640, 720),
        resizable=True,
        background_color="#071018",
    )
    webview.start(gui="edgechromium", debug=False)
    return 0


def _start_owned_backend() -> tuple[SingleInstance, LocalServer]:
    backend_instance = SingleInstance(BACKEND_MUTEX)
    reserved_socket: socket.socket | None = None
    try:
        backend_instance.acquire()
        reserved_socket, port = _reserve_loopback_socket()
        app = _configure_application(_frontend_dist(), port)
        server = LocalServer(app, reserved_socket, port)
        server.start()
        reserved_socket = None
        return backend_instance, server
    except BaseException:
        if reserved_socket is not None:
            reserved_socket.close()
        backend_instance.close()
        raise


def _connect_or_start_backend() -> tuple[str, SingleInstance | None, LocalServer | None]:
    if _probe_backend():
        return BACKEND_URL, None, None
    try:
        backend_instance, server = _start_owned_backend()
        return server.url, backend_instance, server
    except AlreadyRunningError:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _probe_backend():
                return BACKEND_URL, None, None
            time.sleep(0.05)
        raise DesktopStartupError("后台服务持有互斥锁，但未通过本机身份检查")


def _run_daemon(logger: logging.Logger) -> int:
    backend_instance: SingleInstance | None = None
    server: LocalServer | None = None
    stop_signal = BackendStopSignal()
    try:
        backend_instance, server = _start_owned_backend()
        stop_signal.create()
        logger.info("Background daemon started on loopback port %d", server.port)
        while server.thread.is_alive() and not server.failure_type:
            if stop_signal.wait(1_000):
                logger.info("Background daemon received a local stop signal")
                break
        if server.failure_type:
            raise DesktopStartupError(
                f"后台服务异常终止（{server.failure_type}）"
            )
        return 0
    except AlreadyRunningError:
        if _probe_backend():
            return 0
        raise DesktopStartupError("后台互斥锁已占用，但本机服务身份校验失败")
    finally:
        if server is not None:
            server.stop()
        stop_signal.close()
        if backend_instance is not None:
            backend_instance.close()


def _stop_daemon_and_wait(timeout: float = 90.0) -> bool:
    signaled = BackendStopSignal.signal()
    if not signaled and not _probe_backend():
        return True
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if not _probe_backend():
            return True
        time.sleep(0.1)
    return not _probe_backend()


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
    if argv == ["--stop-daemon"]:
        return 0 if _stop_daemon_and_wait() else 1
    credential_mode = argv == ["--credentials"]
    daemon_mode = argv == ["--daemon"]
    if daemon_mode:
        try:
            return _run_daemon(logger)
        except BaseException as exc:
            logger.error("Background daemon failed: %s", type(exc).__name__)
            return 1
    instance = SingleInstance(CREDENTIALS_MUTEX if credential_mode else UI_MUTEX)
    backend_instance: SingleInstance | None = None
    local_server: LocalServer | None = None
    try:
        instance.acquire()
        if credential_mode:
            return _run_credentials_window()
        if argv:
            raise DesktopStartupError("不支持的启动参数")

        local_url, backend_instance, local_server = _connect_or_start_backend()
        if local_server is None:
            logger.info("Desktop connected to existing background daemon")
        else:
            logger.info("Desktop host started on loopback port %d", local_server.port)

        import webview

        webview.create_window(
            PUBLIC_APP_NAME,
            local_url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=WINDOW_MIN_SIZE,
            background_color="#07111f",
        )
        webview.start(gui="edgechromium", debug=False)
        return 0
    except AlreadyRunningError:
        _show_error(PUBLIC_APP_NAME, "墨衡已经在运行。请切换到现有窗口。")
        return 2
    except BaseException as exc:
        error_type = type(exc).__name__
        logger.error("Desktop startup failed: %s", error_type)
        _show_error(
            f"{PUBLIC_APP_NAME} 启动失败",
            "无法启动墨衡桌面窗口。请确认 Microsoft Edge WebView2 Runtime "
            f"可用后重试。错误类型：{error_type}\n日志：{log_path}",
        )
        return 1
    finally:
        if local_server is not None:
            local_server.stop()
        if backend_instance is not None:
            backend_instance.close()
        instance.close()


if __name__ == "__main__":
    raise SystemExit(run())
