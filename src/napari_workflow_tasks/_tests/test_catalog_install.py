"""Tests for the wheel-path install fixes: system requirements, wheel
selection / sdist fallback and the Python retry."""
import json
import subprocess

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

import pytest

from napari_workflow_tasks import catalog


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    monkeypatch.setattr(catalog, "ENVS_DIR", tmp_path / "envs")
    # install caches isolated; the packages here have no source_url, so
    # only the resolved install (c) is used
    monkeypatch.setattr(catalog, "CACHE_DIR", tmp_path / "cache")
    return tmp_path


def _fake_pixi_info(monkeypatch, virtual_packages, platform="linux-64",
                    version="pixi 0.83.0"):
    payload = json.dumps(
        {"platform": platform, "virtual_packages": virtual_packages}
    )

    def fake_run(cmd, **kw):
        if cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=version, stderr="")
        assert cmd[1:] == ["info", "--json"]
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    monkeypatch.setattr(catalog, "find_pixi", lambda: "pixi")
    monkeypatch.setattr(catalog.subprocess, "run", fake_run)


@pytest.mark.parametrize(
    "virtual, platform, expected",
    [
        (
            ["__unix=0=0", "__linux=5.15=0", "__glibc=2.28=0",
             "__cuda=12.4=0", "__archspec=1=x86_64_v3"],
            "linux-64",
            {"glibc": "2.28", "cuda": "12.4"},
        ),
        (["__win=0=0", "__archspec=1=x86_64"], "win-64", {}),
        (["__unix=0=0", "__osx=13.6=0", "__archspec=1=x86_64"], "osx-64",
         {"macos": "13.6"}),
        (["__unix=0=0", "__osx=26.7.1=0", "__archspec=1=m4"], "osx-arm64",
         {"macos": "26.7.1"}),
    ],
)
def test_host_system_requirements(monkeypatch, isolated_home, virtual,
                                  platform, expected):
    _fake_pixi_info(monkeypatch, virtual, platform)
    reqs = catalog.host_system_requirements()
    assert reqs == expected

    # rich platform entry (pixi >= 0.71): named, never the bare platform
    entry = catalog.host_platform_entry(platform, reqs)
    if not expected:
        assert entry == platform
    else:
        assert entry == {"name": platform + "-host", "platform": platform,
                         **expected}
    # [system-requirements] table (older pixi)
    toml = catalog.system_requirements_toml(reqs)
    if not expected:
        assert toml == ""
        return
    data = tomllib.loads(toml)
    assert data["system-requirements"] == catalog.sysreqs_to_table(expected)
    if "glibc" in expected:
        assert data["system-requirements"]["libc"] == {
            "family": "glibc", "version": "2.28"}


@pytest.mark.parametrize("version, rich", [
    ("pixi 0.83.0", True), ("pixi 0.71.0", True), ("pixi 1.2.3", True),
    ("pixi 0.70.2", False), ("pixi 0.44.0", False), ("", False),
])
def test_system_requirements_in_manifest(monkeypatch, isolated_home,
                                         version, rich):
    """pixi >= 0.71: requirements on the (named) platform entry; older (or
    unknown) pixi: the [system-requirements] table, bare platform."""
    _fake_pixi_info(
        monkeypatch, ["__unix=0=0", "__glibc=2.28=0", "__cuda=12.4=0"],
        version=version,
    )
    monkeypatch.setattr(catalog, "current_pixi_platform", lambda: "linux-64")
    assert catalog.use_rich_platforms(version) is rich
    pkg = catalog.TaskPackage(
        "https://pypi.org/project/my-tasks", "pypi", "my-tasks",
        [catalog.PackageVersion("1.0", "https://x/my_tasks-1.0-py3-none-any.whl",
                                "my_tasks-1.0-py3-none-any.whl")],
    )
    data = tomllib.loads(catalog.write_pixi_manifest(pkg, "1.0", [], None)
                         .read_text())
    if rich:
        assert "system-requirements" not in data
        assert data["workspace"]["platforms"] == [{
            "name": "linux-64-host", "platform": "linux-64",
            "glibc": "2.28", "cuda": "12.4"}]
    else:
        assert data["system-requirements"] == {
            "libc": {"family": "glibc", "version": "2.28"}, "cuda": "12.4"
        }
        assert data["workspace"]["platforms"] == ["linux-64"]


def test_manifest_requirements_only_for_their_os(monkeypatch, isolated_home):
    """e.g. CONDA_OVERRIDE_CUDA on macOS: no cuda on an osx entry."""
    assert catalog.host_platform_entry(
        "osx-arm64", {"macos": "26.7.1", "cuda": "12"}) == {
        "name": "osx-arm64-host", "platform": "osx-arm64", "macos": "26.7.1"}
    assert catalog.host_platform_entry("win-64", {"cuda": "12"})["cuda"] == "12"
    assert catalog.host_platform_entry("osx-64", {}) == "osx-64"


def test_host_system_requirements_pixi_fails(monkeypatch, isolated_home):
    def boom():
        raise FileNotFoundError("no pixi")

    monkeypatch.setattr(catalog, "find_pixi", boom)
    msgs = []
    assert catalog.host_system_requirements(msgs.append) == {}
    assert "Warning" in msgs[0]


def test_candidate_pythons():
    assert catalog.candidate_pythons(">=3.11") == ["3.12", "3.11", "3.13"]
    assert catalog.candidate_pythons("==3.11.*") == ["3.11"]
    assert catalog.candidate_pythons(None) == catalog.PREFERRED_PYTHONS
    assert catalog.choose_python("<3.12,>=3.11") == "3.11"


def test_pick_wheel_filters_platform_and_python():
    linux = "a-1.0-cp311-cp311-linux_x86_64.whl"
    host = [
        w for w in ("a-1.0-cp311-cp311-macosx_11_0_arm64.whl",
                    "a-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
                    "a-1.0-cp311-cp311-manylinux_2_17_aarch64.whl",
                    "a-1.0-cp311-cp311-win_amd64.whl")
        if catalog.wheel_is_compatible(w, "3.11")
    ]
    assert len(host) == 1  # exactly one matches the machine running tests
    assert catalog._pick_wheel([linux], python="3.11") is None or \
        catalog.current_pixi_platform() == "linux-64"
    assert catalog._pick_wheel([linux, host[0]], python="3.11") == host[0]
    assert catalog._pick_wheel([linux, host[0]], python="3.12") is None
    # listing: pure first, then host-compatible, then anything
    pure = "a-1.0-py3-none-any.whl"
    assert catalog._pick_wheel([host[0], pure]) == pure
    assert catalog._pick_wheel([linux, host[0]]) == host[0]
    assert catalog._pick_wheel(["a-1.0.tar.gz"]) is None


def _gh_pkg(wheel):
    url = f"https://github.com/o/r/releases/download/v1/{wheel}"
    return catalog.TaskPackage(
        "https://github.com/o/r", "github", "abc-tasks",
        [catalog.PackageVersion("1.0", url, wheel)],
    )


def test_resolve_install_source_sdist_fallback(monkeypatch, isolated_home):
    if catalog.current_pixi_platform() == "linux-64":
        pytest.skip("the linux wheel would be compatible here")
    pkg = _gh_pkg("abc_tasks-1.0-cp311-cp311-linux_x86_64.whl")
    monkeypatch.setattr(catalog, "_github_get_json", lambda url: {"assets": [
        {"name": "abc_tasks-1.0-cp311-cp311-linux_x86_64.whl",
         "browser_download_url": "https://dl/abc_tasks-1.0-cp311-cp311-linux_x86_64.whl"},
        {"name": "abc_tasks-1.0.tar.gz",
         "browser_download_url": "https://dl/abc_tasks-1.0.tar.gz"},
    ]})
    src = catalog.resolve_install_source(pkg, "1.0", ">=3.11,<3.14")
    assert src.dep_fields == ['url = "https://dl/abc_tasks-1.0.tar.gz"']
    assert src.pythons == ["3.12", "3.11", "3.13"]


def test_resolve_install_source_wheel_limits_python(isolated_home):
    pkg = _gh_pkg("abc_tasks-1.0-py3-none-any.whl")
    src = catalog.resolve_install_source(pkg, "1.0", ">=3.11")
    assert src.pythons == ["3.12", "3.11", "3.13"]
    assert "py3-none-any" in src.dep_fields[0]


def test_install_retries_next_python(monkeypatch, isolated_home):
    pkg = catalog.TaskPackage(
        "https://pypi.org/project/my-tasks", "pypi", "my-tasks",
        [catalog.PackageVersion("1.0", "https://x/my_tasks-1.0-py3-none-any.whl",
                                "my_tasks-1.0-py3-none-any.whl", ">=3.11")],
    )
    info = catalog.VersionInfo({"task_list": []}, "my_tasks",
                               "my_tasks/__FRACTAL_MANIFEST__.json", [], ">=3.11")
    monkeypatch.setattr(catalog, "get_version_info", lambda p, v: info)
    monkeypatch.setattr(catalog, "find_pixi", lambda: "pixi")
    monkeypatch.setattr(catalog, "host_system_requirements", lambda log=None: {})
    pythons = []

    def fake_run(cmd, log):
        if cmd[1] == "install":
            data = tomllib.loads(open(cmd[-1]).read())
            pythons.append(data["dependencies"]["python"])
            if len(pythons) == 1:
                raise RuntimeError("Failed to build pandas==1.5.3")
            return ""
        return "PKGDIR=/env/my_tasks"

    monkeypatch.setattr(catalog, "_run_streaming", fake_run)
    msgs = []
    entry = catalog.install_version(pkg, "1.0", log=msgs.append)
    assert pythons == ["3.12.*", "3.11.*"]
    assert entry["src_path"] == "/env/my_tasks"
    assert any("retrying with Python 3.11" in m for m in msgs)


def test_failed_builds_parsing():
    out = ("  ├─▶ Failed to build `pyqt6==6.12.0`\n"
           "  ├─▶ Failed to build `\x1b[36mPyShTools==4.14.1\x1b[39m`\n"
           "Failed to build `foo-bar @ https://x/foo.tar.gz`\n")
    assert catalog.failed_builds(out) == ["pyqt6", "pyshtools", "foo-bar"]
    assert catalog.failed_builds("No module named 'pkg_resources'") == []


def test_install_retries_without_source_build(monkeypatch, isolated_home):
    pkg = catalog.TaskPackage(
        "https://pypi.org/project/my-tasks", "pypi", "my-tasks",
        [catalog.PackageVersion("1.0", "https://x/my_tasks-1.0-py3-none-any.whl",
                                "my_tasks-1.0-py3-none-any.whl", ">=3.11")],
    )
    info = catalog.VersionInfo({"task_list": []}, "my_tasks",
                               "my_tasks/__FRACTAL_MANIFEST__.json", [], ">=3.11")
    monkeypatch.setattr(catalog, "get_version_info", lambda p, v: info)
    monkeypatch.setattr(catalog, "find_pixi", lambda: "pixi")
    monkeypatch.setattr(catalog, "host_system_requirements", lambda log=None: {})
    manifests = []

    def fake_run(cmd, log):
        if cmd[1] == "install":
            data = tomllib.loads(open(cmd[-1]).read())
            manifests.append(data)
            if "pypi-options" not in data:
                err = RuntimeError("Command failed: short tail")
                err.output = "x\n  ├─▶ Failed to build `pyqt6==6.12.0`\nmore"
                raise err
            return ""
        return "PKGDIR=/env/my_tasks"

    monkeypatch.setattr(catalog, "_run_streaming", fake_run)
    catalog.install_version(pkg, "1.0", log=lambda m: None)
    assert len(manifests) == 2
    assert manifests[1]["pypi-options"]["no-build"] == ["pyqt6"]
    assert manifests[1]["dependencies"]["python"] == "3.12.*"  # same Python
