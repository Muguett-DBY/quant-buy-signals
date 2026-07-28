import hashlib
import json
from pathlib import Path
import re
import tomllib
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from tools import android_release
from tools.android_release import build_android_update_manifest, write_android_update_manifest


TEST_GIT_SHA = "1234567890abcdef1234567890abcdef12345678"


def test_android_manifest_uses_a_packaged_launcher_icon():
    project_root = Path(__file__).resolve().parents[1]
    manifest_path = project_root / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    application = ET.parse(manifest_path).getroot().find("application")
    icon = application.attrib["{http://schemas.android.com/apk/res/android}icon"]

    assert icon.startswith("@drawable/")
    icon_name = icon.removeprefix("@drawable/")
    assert (manifest_path.parent / "res" / "drawable" / f"{icon_name}.xml").is_file()


def test_android_user_facing_copy_uses_plain_chinese_and_hides_parser_errors():
    project_root = Path(__file__).resolve().parents[1]
    strings_path = project_root / "android" / "app" / "src" / "main" / "res" / "values" / "strings.xml"
    main_activity = (
        project_root / "android" / "app" / "src" / "main" / "java" / "com" / "muguett" / "dsdcf" / "MainActivity.java"
    ).read_text(encoding="utf-8")
    string_nodes = list(ET.parse(strings_path).getroot().iter("string"))
    visible_text = " ".join(node.text or "" for node in string_nodes)
    strings = {node.attrib["name"]: node.text or "" for node in string_nodes}

    for internal_term in ("APK", "HTTPS", "SHA-256", "JSON"):
        assert internal_term not in visible_text
    assert "部分公司仍可能显示资料不足" in strings["status_refreshed"]
    assert "数据完整性和质量检查均已通过" not in strings["status_refreshed"]
    assert "friendlyMessage(error)" in main_activity
    assert "error.getClass().getSimpleName()" not in main_activity


def test_android_company_results_are_visible_and_explain_empty_filters():
    project_root = Path(__file__).resolve().parents[1]
    strings_path = project_root / "android" / "app" / "src" / "main" / "res" / "values" / "strings.xml"
    main_activity = (
        project_root / "android" / "app" / "src" / "main" / "java" / "com" / "muguett" / "dsdcf" / "MainActivity.java"
    ).read_text(encoding="utf-8")
    strings = {node.attrib["name"]: node.text or "" for node in ET.parse(strings_path).getroot().iter("string")}

    assert "当前列出 %3$d 家公司" in strings["results_count"]
    assert "点公司名称查看详情" in strings["results_count"]
    assert "请切换上方的名单类型" in strings["results_empty"]
    assert "请清空搜索词" in strings["results_empty_search"]
    assert "点这里查看七类数量明细" in strings["market_summary"]
    assert (
        "marketData.typeCoverageSummary()"
        not in main_activity[
            main_activity.index("private void renderSummary()") : main_activity.index(
                "private void showCoverageSummary()"
            )
        ]
    )
    assert "companyList.addHeaderView(header, null, false)" in main_activity
    assert "emptyState.setVisibility(visibleEntries.isEmpty() ? View.VISIBLE : View.GONE)" in main_activity
    assert "position - companyList.getHeaderViewsCount()" in main_activity
    assert "visibleEntries.size()" in main_activity


def test_android_update_check_uses_a_dedicated_stable_manifest_release():
    project_root = Path(__file__).resolve().parents[1]
    repository = (
        project_root
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "muguett"
        / "dsdcf"
        / "MarketRepository.java"
    ).read_text(encoding="utf-8")

    assert "releases/download/android-app/android-update-manifest.json" in repository
    assert "releases/download/android-app/android-update-manifest.sig" in repository
    assert "releases/latest/download/android-update-manifest.json" not in repository


def test_android_update_manifest_has_an_independent_pinned_signing_key():
    project_root = Path(__file__).resolve().parents[1]
    repository = (
        project_root
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "muguett"
        / "dsdcf"
        / "MarketRepository.java"
    ).read_text(encoding="utf-8")
    signer = (project_root / "tools" / "sign_android_update_manifest.ps1").read_text(encoding="utf-8")
    pinned_key = (
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjic3+c4snSCoVhcipasA9t3ppCwvRO5u88dg/"
        "M1oul+Y3Wp0BwR/Z9bq9ywZK3NgDn7SH3pluAU3MOdQqcVoIA=="
    )

    assert pinned_key in repository
    assert pinned_key in signer
    assert set(re.findall(r"\$env:([A-Z][A-Z0-9_]+)", signer)) == {"DS_DCF_ANDROID_UPDATE_SIGNING_PRIVATE_KEY_BASE64"}
    assert "MOBILE_DATA_SIGNING_PRIVATE_KEY_BASE64" not in signer
    assert "DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64" not in signer
    assert "Rfc3279DerSequence" in signer


def test_android_update_watermark_binds_same_version_to_the_verified_manifest_hash():
    project_root = Path(__file__).resolve().parents[1]
    repository = (
        project_root
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "muguett"
        / "dsdcf"
        / "MarketRepository.java"
    ).read_text(encoding="utf-8")
    check_method = repository[repository.index("public UpdateInfo checkForUpdate()") :]

    assert check_method.index("parseSignedUpdateManifest(") < check_method.index(
        "String manifestSha256 = sha256(manifestBytes);"
    )
    assert check_method.index("String manifestSha256 = sha256(manifestBytes);") < check_method.index(
        "acceptUpdateManifestWatermark("
    )
    assert "candidateVersionCode == stored.versionCode" in repository
    assert "!candidateManifestSha256.equals(stored.manifestSha256)" in repository
    assert "candidateVersionName.equals(stored.versionName)" in repository
    assert "readUpdateManifestWatermark" in repository
    assert "acceptUpdateVersionCode" not in repository


def test_android_source_version_is_bound_to_python_and_desktop_versions():
    project_root = Path(__file__).resolve().parents[1]
    metadata = android_release.read_source_release_metadata()
    pyproject_version = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    desktop_version = re.search(
        r'^__version__\s*=\s*"([^"]+)"\s*$',
        (project_root / "desktop" / "version.py").read_text(encoding="utf-8"),
        re.MULTILINE,
    ).group(1)

    assert metadata.version_code > 0
    assert metadata.version_name == pyproject_version == desktop_version


def test_android_release_git_gate_requires_the_repository_root_clean_head_commit(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    responses = {
        ("rev-parse", "--show-toplevel"): str(root),
        ("rev-parse", "--verify", "HEAD"): TEST_GIT_SHA,
        ("status", "--porcelain=v1", "--untracked-files=all"): "",
        ("cat-file", "-t", TEST_GIT_SHA): "commit",
    }

    def fake_git_output(arguments, *, root: Path):
        assert root == tmp_path / "repository"
        return responses[tuple(arguments)]

    monkeypatch.setattr(android_release, "_git_output", fake_git_output)
    assert android_release._require_clean_committed_git_sha(root=root) == TEST_GIT_SHA

    responses[("rev-parse", "--show-toplevel")] = str(root / "nested")
    with pytest.raises(RuntimeError, match="repository root"):
        android_release._require_clean_committed_git_sha(root=root)
    responses[("rev-parse", "--show-toplevel")] = str(root)

    responses[("rev-parse", "--verify", "HEAD")] = "not-a-commit"
    with pytest.raises(RuntimeError, match="exact commit SHA"):
        android_release._require_clean_committed_git_sha(root=root)
    responses[("rev-parse", "--verify", "HEAD")] = TEST_GIT_SHA

    responses[("status", "--porcelain=v1", "--untracked-files=all")] = "?? untracked.apk"
    with pytest.raises(RuntimeError, match="clean committed"):
        android_release._require_clean_committed_git_sha(root=root)
    responses[("status", "--porcelain=v1", "--untracked-files=all")] = ""

    responses[("cat-file", "-t", TEST_GIT_SHA)] = "blob"
    with pytest.raises(RuntimeError, match="commit object"):
        android_release._require_clean_committed_git_sha(root=root)


@pytest.mark.parametrize(
    ("android_code", "android_name", "python_name", "desktop_name", "message"),
    [
        (0, "11.2.0", "11.2.0", "11.2.0", "positive integer"),
        (1, "11.2.1", "11.2.0", "11.2.1", "must match"),
        (1, "11.2.1", "11.2.1", "11.2.0", "must match"),
    ],
)
def test_android_source_version_binding_fails_closed(
    tmp_path,
    android_code,
    android_name,
    python_name,
    desktop_name,
    message,
):
    (tmp_path / "android" / "app").mkdir(parents=True)
    (tmp_path / "desktop").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "test"\nversion = "{python_name}"\n',
        encoding="utf-8",
    )
    (tmp_path / "android" / "app" / "build.gradle.kts").write_text(
        f'defaultConfig {{\n    versionCode = {android_code}\n    versionName = "{android_name}"\n}}\n',
        encoding="utf-8",
    )
    (tmp_path / "desktop" / "version.py").write_text(
        f'__version__ = "{desktop_name}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        android_release.read_source_release_metadata(tmp_path)


def test_android_stable_manifest_downloads_bypass_intermediary_caches():
    project_root = Path(__file__).resolve().parents[1]
    repository = (
        project_root
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "muguett"
        / "dsdcf"
        / "MarketRepository.java"
    ).read_text(encoding="utf-8")

    assert "connection.setUseCaches(false);" in repository
    assert 'connection.setRequestProperty("Cache-Control", "no-cache, no-store, max-age=0");' in repository
    assert 'connection.setRequestProperty("Pragma", "no-cache");' in repository


def test_android_update_manifest_is_hash_bound_and_uses_the_pinned_release_path(tmp_path, monkeypatch):
    source = android_release.read_source_release_metadata()
    apk = tmp_path / f"DS_DCF-v{source.version_name}-android-release.apk"
    apk.write_bytes(b"signed-apk-placeholder")
    monkeypatch.setattr(
        android_release,
        "_inspect_apk",
        lambda _path: SimpleNamespace(
            package_id="com.muguett.dsdcf",
            version_code=source.version_code,
            version_name=source.version_name,
            signer_sha256=android_release.RELEASE_CERT_SHA256,
        ),
        raising=False,
    )

    manifest = build_android_update_manifest(
        apk,
        version_code=source.version_code,
        version_name=source.version_name,
        release_tag=f"v{source.version_name}",
    )
    target = tmp_path / "android-update-manifest.json"
    write_android_update_manifest(target, manifest)

    assert manifest["apk_url"].endswith(f"/v{source.version_name}/DS_DCF-v{source.version_name}-android-release.apk")
    assert manifest["apk_size"] == apk.stat().st_size
    assert manifest["apk_sha256"] == hashlib.sha256(apk.read_bytes()).hexdigest()
    assert manifest["signer_sha256"] == android_release.RELEASE_CERT_SHA256
    assert set(manifest) == {
        "apk_sha256",
        "apk_size",
        "apk_url",
        "package_id",
        "schema_version",
        "signer_sha256",
        "version_code",
        "version_name",
    }
    assert "git_sha" not in manifest
    assert json.loads(target.read_text(encoding="utf-8")) == manifest
    assert target.read_bytes().endswith(b"}\n")
    assert b"\r\n" not in target.read_bytes()


def test_android_release_cli_binds_local_provenance_to_clean_head_without_changing_public_schema(
    tmp_path,
    monkeypatch,
    capsys,
):
    source = android_release.read_source_release_metadata()
    apk = tmp_path / f"DS_DCF-v{source.version_name}-android-release.apk"
    apk.write_bytes(b"signed-apk-placeholder")
    monkeypatch.setattr(android_release, "_require_clean_committed_git_sha", lambda: TEST_GIT_SHA)
    monkeypatch.setattr(
        android_release,
        "_inspect_apk",
        lambda _path: SimpleNamespace(
            package_id=android_release.ANDROID_PACKAGE_ID,
            version_code=source.version_code,
            version_name=source.version_name,
            signer_sha256=android_release.RELEASE_CERT_SHA256,
        ),
    )
    manifest_path = tmp_path / "android-update-manifest.json"
    provenance_path = tmp_path / "android-release-provenance.json"

    assert (
        android_release.main(
            [
                "--apk",
                str(apk),
                "--version-code",
                str(source.version_code),
                "--version-name",
                source.version_name,
                "--release-tag",
                f"v{source.version_name}",
                "--output",
                str(manifest_path),
                "--provenance-output",
                str(provenance_path),
            ]
        )
        == 0
    )

    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    provenance_bytes = provenance_path.read_bytes()
    provenance = json.loads(provenance_bytes)
    assert manifest_bytes.endswith(b"}\n") and b"\r\n" not in manifest_bytes
    assert provenance_bytes.endswith(b"}\n") and b"\r\n" not in provenance_bytes
    assert "git_sha" not in manifest
    assert provenance == {
        "apk_sha256": manifest["apk_sha256"],
        "git_sha": TEST_GIT_SHA,
        "product": "DS_DCF-android",
        "schema_version": 1,
        "update_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "version_code": source.version_code,
        "version_name": source.version_name,
    }
    captured = capsys.readouterr()
    assert TEST_GIT_SHA in captured.err
    assert str(provenance_path.resolve()) in captured.err


def test_android_release_cli_rejects_a_dirty_tree_before_writing_outputs(tmp_path, monkeypatch):
    source = android_release.read_source_release_metadata()
    apk = tmp_path / f"DS_DCF-v{source.version_name}-android-release.apk"
    apk.write_bytes(b"not-inspected")
    manifest_path = tmp_path / "android-update-manifest.json"
    provenance_path = android_release.default_android_release_provenance_path(manifest_path)

    def reject_dirty_tree():
        raise RuntimeError("Android release manifest requires a clean committed Git work tree")

    monkeypatch.setattr(android_release, "_require_clean_committed_git_sha", reject_dirty_tree)
    with pytest.raises(RuntimeError, match="clean committed"):
        android_release.main(
            [
                "--apk",
                str(apk),
                "--version-code",
                str(source.version_code),
                "--version-name",
                source.version_name,
                "--release-tag",
                f"v{source.version_name}",
                "--output",
                str(manifest_path),
            ]
        )
    assert not manifest_path.exists()
    assert not provenance_path.exists()


def test_android_update_manifest_rejects_arbitrary_bytes_as_an_apk(tmp_path):
    apk = tmp_path / "not-an-apk.apk"
    apk.write_bytes(b"ordinary bytes")
    source = android_release.read_source_release_metadata()

    with pytest.raises(ValueError, match="valid signed APK"):
        build_android_update_manifest(
            apk,
            version_code=source.version_code,
            version_name=source.version_name,
            release_tag=f"v{source.version_name}",
        )


def test_windows_android_batch_wrapper_never_routes_apk_path_through_cmd(tmp_path):
    tool = tmp_path / "build-tools" / "35.0.0" / "apksigner.bat"
    jar = tool.parent / "lib" / "apksigner.jar"
    jar.parent.mkdir(parents=True)
    tool.write_text("@echo off\n", encoding="utf-8")
    jar.write_bytes(b"jar")
    hostile_apk = str(tmp_path / "release&whoami%PATH%.apk")

    command = android_release._android_tool_command(
        tool,
        ("verify", "--verbose", hostile_apk),
        windows=True,
        java_executable="C:\\Java\\bin\\java.exe",
    )

    assert command[:2] == ["C:\\Java\\bin\\java.exe", "-jar"]
    assert command[-1] == hostile_apk
    assert all(part.lower() not in {"cmd", "cmd.exe", "/c"} for part in command)


def test_android_batch_wrapper_falls_back_to_java_home(tmp_path, monkeypatch):
    tool = tmp_path / "build-tools" / "35.0.0" / "apksigner.bat"
    jar = tool.parent / "lib" / "apksigner.jar"
    java = tmp_path / "java-home" / "bin" / ("java.exe" if android_release.os.name == "nt" else "java")
    jar.parent.mkdir(parents=True)
    java.parent.mkdir(parents=True)
    tool.write_text("@echo off\n", encoding="utf-8")
    jar.write_bytes(b"jar")
    java.write_bytes(b"java")
    monkeypatch.setattr(android_release.shutil, "which", lambda _name: None)
    monkeypatch.setenv("JAVA_HOME", str(java.parent.parent))

    command = android_release._android_tool_command(tool, ("verify", "application.apk"), windows=True)

    assert Path(command[0]) == java.resolve()
    assert command[1:4] == ["-jar", str(jar.resolve()), "verify"]


def test_android_update_manifest_rejects_apk_larger_than_the_client_limit(tmp_path, monkeypatch):
    apk = tmp_path / "too-large.apk"
    apk.write_bytes(b"1234")
    monkeypatch.setattr(android_release, "MAX_APK_BYTES", 3)

    with pytest.raises(ValueError, match="download limit"):
        build_android_update_manifest(
            apk,
            version_code=1,
            version_name="11.2.0",
            release_tag="v11.2.0",
        )


def test_android_update_manifest_rejects_a_version_code_not_declared_by_gradle(tmp_path):
    apk = tmp_path / "application.apk"
    apk.write_bytes(b"apk")
    source = android_release.read_source_release_metadata()

    with pytest.raises(ValueError, match="Android source versionCode"):
        build_android_update_manifest(
            apk,
            version_code=source.version_code + 1,
            version_name=source.version_name,
            release_tag=f"v{source.version_name}",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"version_code": 0, "version_name": "11.2.0", "release_tag": "v11.2.0"}, "version_code"),
        ({"version_code": 1, "version_name": "11.2", "release_tag": "v11.2.0"}, "version_name"),
        ({"version_code": 1, "version_name": "11.2.0", "release_tag": "mobile-market-data"}, "release_tag"),
        ({"version_code": 1, "version_name": "11.2.0", "release_tag": "v11.2.1"}, "release_tag"),
        (
            {"version_code": 1, "version_name": "11.2.0", "release_tag": "v11.2.0", "asset_name": "../bad.apk"},
            "asset_name",
        ),
        (
            {"version_code": 1, "version_name": "11.2.0", "release_tag": "v11.2.0", "asset_name": "bad%3F.apk"},
            "asset_name",
        ),
    ],
)
def test_android_update_manifest_rejects_unsafe_release_metadata(tmp_path, kwargs, message):
    apk = tmp_path / "application.apk"
    apk.write_bytes(b"apk")
    with pytest.raises(ValueError, match=message):
        build_android_update_manifest(apk, **kwargs)
