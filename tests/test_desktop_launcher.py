from __future__ import annotations

import json
import os
import socket
from types import SimpleNamespace

from desktop import launcher


def test_desktop_runtime_uses_writable_local_app_data_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("DS_DCF_CACHE_DIR", raising=False)

    root = launcher._configure_runtime_environment()

    assert root == tmp_path / "DS_DCF"
    assert os.environ["DS_DCF_CACHE_DIR"] == str(root / "cache")
    assert (root / "cache").is_dir()
    assert os.environ["DS_DCF_DESKTOP"] == "1"


def test_desktop_launcher_selects_a_real_loopback_port():
    port = launcher._find_free_port()
    assert 1 <= port <= 65535
    assert launcher._health_url(port) == f"http://127.0.0.1:{port}/_stcore/health"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))


def test_source_streamlit_command_is_loopback_headless_and_telemetry_free():
    command = launcher._streamlit_child_command(54321)
    joined = " ".join(command)
    assert "streamlit run" in joined
    assert "--server.address 127.0.0.1" in joined
    assert "--server.port 54321" in joined
    assert "--server.headless true" in joined
    assert "--global.developmentMode false" in joined
    assert "--browser.gatherUsageStats false" in joined


def test_desktop_health_check_reports_version_and_resources(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    resources = tmp_path / "frozen-layout"
    for relative in launcher._HEALTH_REQUIRED_RESOURCE_FILES:
        path = resources / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    # PyInstaller stores Python packages in its embedded PYZ archive, not as
    # physical ``engine``/``ui`` directories beside bundled data files.
    monkeypatch.setattr(launcher, "_resource_root", lambda: resources)

    assert launcher.main(["--health-check"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["version"] == launcher.__version__
    assert payload["cache_dir"].endswith("DS_DCF\\cache") or payload["cache_dir"].endswith("DS_DCF/cache")


def test_server_smoke_test_starts_waits_and_always_stops_child(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "DS_DCF" / "logs").mkdir(parents=True)
    calls: list[object] = []

    class FakeProcess:
        returncode = None

        @staticmethod
        def poll():
            return None

    process = FakeProcess()
    monkeypatch.setattr(launcher, "_configure_logging", lambda _root: SimpleNamespace(info=lambda *args: None))
    monkeypatch.setattr(launcher, "_find_free_port", lambda: 54321)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        launcher,
        "_wait_until_healthy",
        lambda actual, port, *, timeout: calls.append((actual, port, timeout)),
    )
    monkeypatch.setattr(launcher, "_terminate_server", lambda actual: calls.append(("stopped", actual)))

    assert launcher.main(["--server-smoke-test"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "version": launcher.__version__, "server_health": "ok"}
    assert calls == [
        (process, 54321, launcher.SERVER_START_TIMEOUT_SECONDS),
        ("stopped", process),
    ]
