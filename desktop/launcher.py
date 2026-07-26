"""Windows desktop host for the local-only DS_DCF Streamlit application."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import http.client
import importlib.util
import json
import logging
import logging.handlers
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from desktop.console_output import write_console_message
from desktop.updater import (
    UpdateError,
    check_for_update,
    download_update_package,
    install_update_package,
    load_update_manifest_url,
)
from desktop.version import __version__


PRODUCT_NAME = "DS_DCF"
SERVER_START_TIMEOUT_SECONDS = 90
_HEALTH_REQUIRED_RESOURCE_FILES = (
    "app.py",
    "data/financial_balance_sheet_evidence.json",
    "data/financial_zero_capex_evidence.json",
    "data/financial_zero_revenue_evidence.json",
    "data/industry_f10.json",
    "data/industry_em_map.json",
    "data/industry_capco_2025h2.json",
    "data/industry_exchange_new_listings_2026.json",
    "tools/china_a_share_trading_calendar.json",
)
_HEALTH_REQUIRED_MODULES = ("data", "engine", "ui")


def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root).resolve() if frozen_root else Path(__file__).resolve().parents[1]


def _application_data_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()).expanduser().resolve()
    return base / PRODUCT_NAME


def _configure_runtime_environment() -> Path:
    root = _application_data_root()
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["DS_DCF_CACHE_DIR"] = str(cache)
    os.environ["DS_DCF_DESKTOP"] = "1"
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    return root


def _configure_logging(data_root: Path) -> logging.Logger:
    logs = data_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ds_dcf.desktop")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            logs / "desktop.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _health_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/_stcore/health"


def _app_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def _streamlit_child_command(port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--streamlit-child", "--port", str(port)]
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(_resource_root() / "app.py"),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--server.fileWatcherType",
        "none",
        "--global.developmentMode",
        "false",
        "--browser.gatherUsageStats",
        "false",
    ]


def _run_streamlit_child(port: int) -> int:
    app_path = _resource_root() / "app.py"
    if not app_path.is_file():
        raise RuntimeError(f"bundled app.py is missing: {app_path}")
    from streamlit.web import cli as streamlit_cli

    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--server.fileWatcherType",
        "none",
        "--global.developmentMode",
        "false",
        "--browser.gatherUsageStats",
        "false",
    ]
    return int(streamlit_cli.main() or 0)


def _wait_until_healthy(process: subprocess.Popen, port: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not started"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"local server exited with code {process.returncode}")
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)
        try:
            connection.request("GET", "/_stcore/health")
            response = connection.getresponse()
            body = response.read(64).decode("utf-8", errors="replace").strip().lower()
            if response.status == 200 and body == "ok":
                return
            last_error = f"health status={response.status} body={body!r}"
        except (OSError, http.client.HTTPException) as exc:
            last_error = type(exc).__name__
        finally:
            connection.close()
        time.sleep(0.25)
    raise RuntimeError(f"local server did not become healthy within {timeout:.0f}s ({last_error})")


def _terminate_server(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _message_box(title: str, message: str, *, error: bool = False) -> None:
    try:
        import ctypes

        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    except Exception:
        write_console_message(f"{title}: {message}", error=True)


def _public_update_error_message(error: object, *, operation: str) -> str:
    """Translate updater diagnostics while keeping exact internals in the log."""
    raw = str(error or "").casefold()
    if any(
        marker in raw
        for marker in (
            "http",
            "request",
            "redirect",
            "timeout",
            "timed out",
            "connection",
            "network",
            "dns",
        )
    ):
        return "暂时无法连接更新服务器，请稍后重试；当前版本仍可正常使用。"
    if any(
        marker in raw
        for marker in (
            "signature",
            "sha-256",
            "integrity",
            "checksum",
            "certificate",
            "replay",
            "downgrade",
            "unsafe",
            "does not match",
            "differs",
        )
    ):
        return "更新文件未通过安全校验，程序已停止更新并保留当前版本。"
    if operation == "config" or any(
        marker in raw for marker in ("update_config", "manifest_url", "semantic version", "invalid shape")
    ):
        return "当前安装包的更新配置无效，请重新安装官方版本。"
    if operation == "install" or any(
        marker in raw for marker in ("package", "archive", "zip", "extract", "install", "shortcut")
    ):
        return "更新包下载、校验或安装未完成；旧版本未被覆盖，请稍后重试。"
    return "更新检查未完成，请稍后重试；当前版本仍可正常使用。"


class DesktopController:
    def __init__(self, process: subprocess.Popen, port: int, data_root: Path, logger: logging.Logger):
        import tkinter as tk

        self.process = process
        self.port = port
        self.data_root = data_root
        self.logger = logger
        self.root = tk.Tk()
        self.root.title(f"DS_DCF {__version__}")
        self.root.geometry("440x235")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.status = tk.StringVar(value="本地服务已启动，仅监听 127.0.0.1")

        tk.Label(self.root, text="DS_DCF", font=("Segoe UI", 20, "bold")).pack(pady=(18, 2))
        tk.Label(self.root, text=f"版本 {__version__} · 沪深 A 股估值与七类型诊断").pack()
        tk.Label(self.root, textvariable=self.status, wraplength=400).pack(pady=12)
        buttons = tk.Frame(self.root)
        buttons.pack(pady=5)
        tk.Button(buttons, text="打开分析界面", width=14, command=self.open_browser).grid(row=0, column=0, padx=5)
        tk.Button(buttons, text="检查更新", width=14, command=self.check_update).grid(row=0, column=1, padx=5)
        tk.Button(buttons, text="退出程序", width=14, command=self.close).grid(row=0, column=2, padx=5)
        tk.Label(self.root, text="关闭此窗口会同时停止本地服务。", fg="#666666").pack(pady=10)

    def open_browser(self) -> None:
        webbrowser.open(_app_url(self.port), new=2)

    def _show_info(self, title: str, message: str) -> None:
        from tkinter import messagebox

        messagebox.showinfo(title, message, parent=self.root)

    def _show_error(self, title: str, message: str) -> None:
        from tkinter import messagebox

        messagebox.showerror(title, message, parent=self.root)

    def check_update(self) -> None:
        try:
            manifest_url = load_update_manifest_url(_resource_root())
        except UpdateError as exc:
            self.logger.warning("update config invalid: %s", exc)
            self._show_error("更新配置无效", _public_update_error_message(exc, operation="config"))
            return
        if manifest_url is None:
            self._show_info("尚未配置更新源", "当前安装包没有配置 HTTPS 更新清单地址。")
            return
        self.status.set("正在检查更新…")

        def worker() -> None:
            try:
                result = check_for_update(
                    manifest_url,
                    watermark_path=self.data_root / "update-manifest-watermark.json",
                )
            except UpdateError as exc:
                self.logger.warning("update check failed: %s", exc)
                message = _public_update_error_message(exc, operation="check")
                self.root.after(0, lambda: self._update_check_failed(message))
                return
            except Exception as exc:
                self.logger.exception("unexpected update check failure")
                message = _public_update_error_message(exc, operation="check")
                self.root.after(0, lambda: self._update_check_failed(message))
                return
            self.root.after(0, lambda: self._update_check_complete(result, manifest_url))

        threading.Thread(target=worker, name="ds-dcf-update-check", daemon=True).start()

    def _update_check_failed(self, message: str) -> None:
        self.status.set("更新检查失败；当前版本仍可正常使用")
        self._show_error("更新检查失败", message)

    def _update_check_complete(self, result, manifest_url: str) -> None:
        del manifest_url
        if not result.update_available:
            self.status.set(f"当前已是最新版本 {__version__}")
            self._show_info("无需更新", f"当前版本 {__version__} 已是更新清单中的最新版本。")
            return
        from tkinter import messagebox

        confirmed = messagebox.askyesno(
            "发现新版本",
            f"发现 DS_DCF {result.manifest.version}。\n\n"
            "更新包会先校验 HTTPS、字节数和 SHA-256，再安装到独立版本目录。\n"
            "是否现在下载并安装？",
            parent=self.root,
        )
        if not confirmed:
            self.status.set(f"已暂缓更新到 {result.manifest.version}")
            return
        self.status.set(f"正在下载并校验 {result.manifest.version}…")

        def worker() -> None:
            try:
                package = self.data_root / "updates" / result.manifest.version / "package.zip"
                package.parent.mkdir(parents=True, exist_ok=True)
                download_update_package(
                    result.manifest,
                    package,
                    watermark_path=self.data_root / "update-manifest-watermark.json",
                )
                installed = install_update_package(package, result.manifest)
            except UpdateError as exc:
                self.logger.warning("update install failed: %s", exc)
                message = _public_update_error_message(exc, operation="install")
                self.root.after(0, lambda: self._update_install_failed(message))
                return
            except Exception as exc:
                self.logger.exception("unexpected update install failure")
                message = _public_update_error_message(exc, operation="install")
                self.root.after(0, lambda: self._update_install_failed(message))
                return
            self.root.after(0, lambda: self._update_install_complete(installed))

        threading.Thread(target=worker, name="ds-dcf-update-install", daemon=True).start()

    def _update_install_failed(self, message: str) -> None:
        self.status.set("更新失败；旧版本未被覆盖，可继续使用")
        self._show_error("更新失败", message)

    def _update_install_complete(self, installed) -> None:
        from tkinter import messagebox

        self.status.set(f"版本 {installed.version} 已安装并通过校验")
        restart = messagebox.askyesno(
            "更新完成",
            f"DS_DCF {installed.version} 已安全安装。是否立即启动新版本？",
            parent=self.root,
        )
        if restart:
            try:
                subprocess.Popen([str(installed.executable)], cwd=installed.install_dir)
            except OSError as exc:
                self.logger.exception("new version launch failed")
                self._update_install_failed(f"新版本启动失败：{type(exc).__name__}")
            else:
                self.close()

    def close(self) -> None:
        self.status.set("正在停止本地服务…")
        self.root.update_idletasks()
        _terminate_server(self.process)
        self.root.destroy()

    def run(self) -> int:
        self.root.after(1000, self._poll_server)
        self.root.mainloop()
        return 0

    def _poll_server(self) -> None:
        if self.process.poll() is not None:
            self._show_error("本地服务已停止", f"服务退出代码：{self.process.returncode}")
            self.root.destroy()
            return
        self.root.after(1000, self._poll_server)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DS_DCF Windows desktop launcher")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--server-smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--streamlit-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = _parser().parse_args(argv)
    if args.version:
        write_console_message(__version__)
        return 0
    data_root = _configure_runtime_environment()
    if args.health_check:
        resources = _resource_root()
        missing_files = [
            str(resources / relative)
            for relative in _HEALTH_REQUIRED_RESOURCE_FILES
            if not (resources / relative).is_file()
        ]
        missing_modules = [module for module in _HEALTH_REQUIRED_MODULES if importlib.util.find_spec(module) is None]
        if missing_files or missing_modules:
            write_console_message(
                json.dumps(
                    {
                        "ok": False,
                        "missing_files": missing_files,
                        "missing_modules": missing_modules,
                    },
                    ensure_ascii=True,
                )
            )
            return 1
        write_console_message(
            json.dumps(
                {"ok": True, "version": __version__, "cache_dir": os.environ["DS_DCF_CACHE_DIR"]},
                ensure_ascii=True,
            )
        )
        return 0
    logger = _configure_logging(data_root)
    if args.streamlit_child:
        if not 1 <= args.port <= 65535:
            raise SystemExit("--port must be between 1 and 65535")
        return _run_streamlit_child(args.port)

    port = _find_free_port()
    log_path = data_root / "logs" / "streamlit.log"
    log_handle = open(log_path, "a", encoding="utf-8")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        _streamlit_child_command(port),
        cwd=_resource_root(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
        env=dict(os.environ),
    )
    try:
        _wait_until_healthy(process, port, timeout=SERVER_START_TIMEOUT_SECONDS)
        logger.info("server healthy on 127.0.0.1:%s", port)
        if args.server_smoke_test:
            write_console_message(
                json.dumps({"ok": True, "version": __version__, "server_health": "ok"}, ensure_ascii=True)
            )
            return 0
        if not args.no_browser:
            webbrowser.open(_app_url(port), new=2)
        try:
            controller = DesktopController(process, port, data_root, logger)
        except Exception as exc:
            logger.exception("desktop control window failed")
            _message_box("DS_DCF", f"控制窗口启动失败：{type(exc).__name__}: {exc}", error=True)
            return 1
        return controller.run()
    except Exception as exc:
        logger.exception("desktop startup failed")
        _message_box("DS_DCF 启动失败", f"{type(exc).__name__}: {exc}\n\n日志：{log_path}", error=True)
        return 1
    finally:
        _terminate_server(process)
        log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
