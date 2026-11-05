"""Covers task_packages, manifests, pixi_config, snapshot and install."""

import json
import tomllib
import zipfile

import pytest

from workflow_tasks_core import install, manifests, paths, pixi_config, snapshot, task_packages


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    monkeypatch.setattr(paths, "ENVS_DIR", tmp_path / "envs")
    return tmp_path


def _make_wheel(path, manifest, extras=("fractal-tasks",)):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("my_tasks/__FRACTAL_MANIFEST__.json", json.dumps(manifest))
        meta = "Metadata-Version: 2.1\nName: my-tasks\nVersion: 1.2.0\n"
        meta += "Requires-Python: >=3.11,<3.13\n"
        meta += "".join(f"Provides-Extra: {e}\n" for e in extras)
        z.writestr("my_tasks-1.2.0.dist-info/METADATA", meta)


def _pkg(kind="pypi"):
    versions = [
        task_packages.PackageVersion("1.10.0", "https://x/my_tasks-1.10.0-py3-none-any.whl", "my_tasks-1.10.0-py3-none-any.whl"),
        task_packages.PackageVersion("2.0.0a1", "https://x/a.whl", "my_tasks-2.0.0a1-py3-none-any.whl"),
        task_packages.PackageVersion("1.2.0", "https://x/my_tasks-1.2.0-py3-none-any.whl", "my_tasks-1.2.0-py3-none-any.whl"),
    ]
    versions.sort(key=lambda v: task_packages.version_sort_key(v.version), reverse=True)
    return task_packages.TaskPackage("https://pypi.org/project/my-tasks", kind, "my-tasks", versions)


def test_versions_sorted_and_default_skips_prerelease():
    pkg = _pkg()
    assert [v.version for v in pkg.versions] == ["2.0.0a1", "1.10.0", "1.2.0"]
    assert pkg.default_version.version == "1.10.0"


def test_task_type():
    assert manifests.task_type({"executable_parallel": "a.py"}) == "parallel"
    assert manifests.task_type({"executable_non_parallel": "a.py"}) == "non_parallel"
    assert (
        manifests.task_type({"executable_parallel": "a", "executable_non_parallel": "b"})
        == "compound"
    )


def test_read_wheel_info(tmp_path):
    manifest = {"task_list": [{"name": "T", "executable_parallel": "t.py"}]}
    wheel = tmp_path / "my_tasks-1.2.0-py3-none-any.whl"
    _make_wheel(wheel, manifest)
    info = manifests.read_wheel_info(str(wheel))
    assert info.module_name == "my_tasks"
    assert info.task_list[0]["name"] == "T"
    assert info.extras == ["fractal-tasks"]
    assert pixi_config.choose_python(info.requires_python) == "3.12"


def test_choose_python():
    assert pixi_config.choose_python("<3.12,>=3.11") == "3.11"
    assert pixi_config.choose_python(None) == "3.12"


@pytest.mark.parametrize("kind", ["pypi", "github"])
def test_pixi_manifest_pins_version(isolated_home, kind):
    pkg = _pkg(kind)
    path = pixi_config.write_pixi_manifest(pkg, "1.2.0", ["fractal-tasks"], ">=3.11,<3.12")
    data = tomllib.loads(path.read_text())
    dep = data["pypi-dependencies"]["my-tasks"]
    if kind == "pypi":
        assert dep["version"] == "==1.2.0"
    else:
        assert dep["url"].endswith("my_tasks-1.2.0-py3-none-any.whl")
    assert dep["extras"] == ["fractal-tasks"]
    assert data["dependencies"]["python"] == "3.11.*"
    assert path.parent == isolated_home / "envs" / "my-tasks" / "1.2.0"


def test_pixi_manifest_with_pins(isolated_home):
    pkg = _pkg()
    path = pixi_config.write_pixi_manifest(
        pkg, "1.2.0", [], None, pins=["cellpose<4.2", "numpy==1.26.4"]
    )
    deps = tomllib.loads(path.read_text())["pypi-dependencies"]
    assert deps["cellpose"] == "<4.2"
    assert deps["numpy"] == "==1.26.4"
    assert deps["my-tasks"]["version"] == "==1.2.0"


def test_invalid_pin_raises():
    with pytest.raises(ValueError):
        pixi_config.parse_pins(["cellpose<<4"])


def test_pixi_manifest_with_skipped_packages(isolated_home, monkeypatch):
    monkeypatch.setattr(pixi_config.sys, "platform", "darwin")
    pkg = _pkg()
    path = pixi_config.write_pixi_manifest(
        pkg, "1.2.0", [], None, skip=["cupy-cuda12x"], no_build=["pyqt6"]
    )
    options = tomllib.loads(path.read_text())["pypi-options"]
    assert options["no-build"] == ["pyqt6"]
    # an override whose marker is false on this computer drops the package
    assert options["dependency-overrides"] == {
        "cupy-cuda12x": {"version": "*",
                         "env-markers": "sys_platform != 'darwin'"},
    }


def test_parse_skip():
    assert pixi_config.parse_skip(["Cupy_CUDA12x", "cupy-cuda12x"]) == [
        "cupy-cuda12x"
    ]
    assert pixi_config.parse_skip(None) == []
    with pytest.raises(ValueError):
        pixi_config.parse_skip(["cupy-cuda12x<14"])  # names only


def test_install_with_skipped_packages(fake_pixi, monkeypatch):
    monkeypatch.setattr(pixi_config.sys, "platform", "darwin")
    entry = install.install_version(
        _pkg_with_source(b""), "1.2.0", log=lambda _l: None, use_lock=False,
        skip=["cupy-cuda12x"],
    )
    assert entry["skipped"] == ["cupy-cuda12x"]
    manifest = tomllib.loads(paths.env_dir("my-tasks", "1.2.0")
                             .joinpath("pixi.toml").read_text())
    assert "cupy-cuda12x" in manifest["pypi-options"]["dependency-overrides"]


def test_suggested_skip(monkeypatch):
    monkeypatch.setattr(install.sys, "platform", "darwin")
    assert install.suggested_skip("abbott") == ["cupy-cuda12x"]
    assert install.suggested_skip("my-tasks") == []
    monkeypatch.setattr(install.sys, "platform", "linux")
    assert install.suggested_skip("abbott") == []


class _FakeResponse:
    def __init__(self, body, etag):
        self._body, self.headers = body, {"ETag": etag}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, headers):
    import email.message
    import urllib.error

    msg = email.message.Message()
    for k, v in headers.items():
        msg[k] = v
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", msg, None)


def test_github_cache_etag_and_rate_limit(isolated_home, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(dict(req.header_items()))
        n = len(calls)
        if n == 1:
            return _FakeResponse(b'[{"tag_name": "v1"}]', '"abc"')
        if n == 2:
            raise _http_error(304, {})
        raise _http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"})

    monkeypatch.setattr(task_packages.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(task_packages, "_github_token", lambda: None)
    url = "https://api.github.com/repos/o/r/releases"

    assert task_packages._github_get_json(url) == [{"tag_name": "v1"}]  # fresh
    assert task_packages._github_get_json(url) == [{"tag_name": "v1"}]  # 304 -> cache
    assert calls[1].get("If-none-match") == '"abc"'
    assert task_packages._github_get_json(url) == [{"tag_name": "v1"}]  # limited -> cache

    with pytest.raises(task_packages.GitHubRateLimitError):  # limited, no cache
        task_packages._github_get_json(url + "?other")


def test_snapshot_roundtrip(isolated_home, tmp_path, monkeypatch):
    """Builder output -> plugin packages, without any network."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "build_catalog.py"
    if not script.exists():
        pytest.skip("scripts/build_catalog.py not available")
    spec = importlib.util.spec_from_file_location("build_catalog", script)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    pkg = _pkg()
    manifest = {"task_list": [{"name": "T", "executable_parallel": "t.py",
                               "args_schema_parallel": {"properties": {}}}]}

    def fake_remote(url, filename=""):
        if "2.0.0a1" in filename:
            raise ValueError(f"{filename} has no __FRACTAL_MANIFEST__.json")
        return manifests.VersionInfo(manifest, "my_tasks",
                                     "my_tasks/__FRACTAL_MANIFEST__.json",
                                     ["fractal-tasks"], ">=3.11")

    monkeypatch.setattr(builder.task_packages, "load_package",
                        lambda source: pkg)
    monkeypatch.setattr(builder.manifests, "read_remote_wheel_info",
                        fake_remote)

    out = tmp_path / "out"
    index = builder.build(out, ["https://pypi.org/project/my-tasks"], workers=2)
    assert [v["version"] for v in index["packages"][0]["versions"]] == ["1.10.0", "1.2.0"]
    assert index["errors"][0]["version"] == "2.0.0a1"
    assert (out / "manifests" / "my-tasks" / "1.2.0.json").exists()

    # Second build: identical file (no commit in CI)
    first = (out / "index.json").read_bytes()
    builder.build(out, ["https://pypi.org/project/my-tasks"], workers=2)
    assert (out / "index.json").read_bytes() == first

    # Plugin side
    data = json.loads(first)
    (pkg_snap,) = snapshot.packages_from_index(data, base_url="http://x/")
    assert pkg_snap.default_version.version == "1.10.0"
    info = manifests.get_display_info(pkg_snap, "1.2.0")  # no network needed
    assert info.task_list[0]["name"] == "T"
    assert manifests.task_type(info.task_list[0]) == "parallel"
    assert info.extras == ["fractal-tasks"]
    assert "args_schema_parallel" not in info.task_list[0]  # summary only


# ---------------------------------------------------------------------------
# Installing from the package's pixi.lock
# ---------------------------------------------------------------------------

FAKE_PIXI = r"""#!/usr/bin/env python3
import os, sys, pathlib
args = sys.argv[1:]
manifest = pathlib.Path(args[args.index("--manifest-path") + 1])
with open(os.environ["FAKE_PIXI_LOG"], "a") as f:
    f.write(" ".join(args) + "\n")
if args[0] == "install":
    if "--frozen" in args and os.environ.get("FAKE_PIXI_FAIL_FROZEN"):
        print("lock file does not contain platform osx-arm64"); sys.exit(1)
    (manifest.parent / ".pixi" / "envs" / "default").mkdir(parents=True, exist_ok=True)
    lock = manifest.parent / "pixi.lock"
    if "--frozen" not in args and not lock.exists():
        lock.write_text("version: 6\n")  # like pixi: a solve writes the lock
elif args[0] == "run":
    print("PKGDIR=" + str(manifest.parent / "site-packages" / args[-1]))
"""


def _tarball(files: dict) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"my_tasks-1.2.0/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def fake_pixi(isolated_home, tmp_path, monkeypatch):
    exe = tmp_path / "pixi"
    exe.write_text(FAKE_PIXI)
    exe.chmod(0o755)
    log = tmp_path / "pixi.log"
    monkeypatch.setenv("FAKE_PIXI_LOG", str(log))
    monkeypatch.setattr(pixi_config, "find_pixi", lambda: str(exe))
    info = manifests.VersionInfo(
        {"task_list": []}, "my_tasks", "my_tasks/__FRACTAL_MANIFEST__.json", [], ">=3.11"
    )
    monkeypatch.setattr(manifests, "get_version_info", lambda pkg, v: info)
    return log


def _pkg_with_source(archive):
    pkg = _pkg()
    pv = pkg.get_version("1.2.0")
    pv.source_url = "https://files.example/my_tasks-1.2.0.tar.gz"
    return pkg


def test_install_uses_pixi_lock(fake_pixi, monkeypatch):
    archive = _tarball({
        "pyproject.toml": "[project]\nname='my-tasks'\n[tool.pixi.workspace]\n",
        "pixi.lock": "version: 6\nenvironments:\n  default:\n    packages:\n      " + pixi_config.current_pixi_platform() + ":\n      - pypi: x\n",
    })
    monkeypatch.setattr(task_packages, "http_get", lambda url, headers=None: archive)
    entry = install.install_version(_pkg_with_source(archive), "1.2.0", log=lambda _l: None)

    assert entry["pixi_frozen"] is True
    assert entry["pixi_manifest_path"].endswith("source/my_tasks-1.2.0/pyproject.toml")
    calls = fake_pixi.read_text().splitlines()
    assert calls[0].startswith("install --frozen --manifest-path")
    assert calls[1].startswith("run --frozen --manifest-path")
    assert install.is_installed("my-tasks", "1.2.0")


def test_install_without_lock_falls_back(fake_pixi, monkeypatch):
    archive = _tarball({"pyproject.toml": "[project]\nname='my-tasks'\n"})
    monkeypatch.setattr(task_packages, "http_get", lambda url, headers=None: archive)
    entry = install.install_version(_pkg_with_source(archive), "1.2.0", log=lambda _l: None)

    assert entry["pixi_frozen"] is False
    assert entry["pixi_manifest_path"].endswith("my-tasks/1.2.0/pixi.toml")
    assert not (paths.env_dir("my-tasks", "1.2.0") / "source").exists()


def test_failed_lock_install_falls_back(fake_pixi, monkeypatch):
    archive = _tarball({"pixi.toml": "[workspace]\n", "pixi.lock": "version: 6\nenvironments:\n  default:\n    packages:\n      " + pixi_config.current_pixi_platform() + ":\n"})
    monkeypatch.setattr(task_packages, "http_get", lambda url, headers=None: archive)
    monkeypatch.setenv("FAKE_PIXI_FAIL_FROZEN", "1")
    entry = install.install_version(_pkg_with_source(archive), "1.2.0", log=lambda _l: None)

    assert entry["pixi_frozen"] is False
    assert not (paths.env_dir("my-tasks", "1.2.0") / "source").exists()
    # The release ships a pixi.lock: the fallback is not cached, so the next
    # install tries the pixi.lock again, and the install log says why
    assert "recipe_key" not in entry
    assert not list(paths.recipes_dir().glob("*.json"))
    log_file = paths.env_dir("my-tasks", "1.2.0") / install.INSTALL_LOG
    assert str(log_file) in entry["lock_note"]
    assert "lock file does not contain platform" in log_file.read_text()


def test_unavailable_source_is_not_cached(fake_pixi, monkeypatch):
    # A download problem must not stick: the fallback install is not saved
    # as the recipe for "use the pixi.lock", so the next install tries again
    def broken(url, headers=None):
        raise OSError("connection reset")

    monkeypatch.setattr(task_packages, "http_get", broken)
    entry = install.install_version(_pkg_with_source(b""), "1.2.0", log=lambda _l: None)
    assert entry["pixi_frozen"] is False
    assert "could not be checked" in entry["lock_note"]
    assert "recipe_key" not in entry
    assert not list(paths.recipes_dir().glob("*.json"))


def test_release_without_lock_is_cached_with_note(fake_pixi, monkeypatch):
    archive = _tarball({"pyproject.toml": "[project]\nname='my-tasks'\n"})
    monkeypatch.setattr(task_packages, "http_get", lambda url, headers=None: archive)
    entry = install.install_version(_pkg_with_source(archive), "1.2.0", log=lambda _l: None)
    assert entry["lock_note"] == "this release ships no pixi.lock"
    assert "recipe_key" in entry  # a release without a lock is cached


def test_lock_install_has_no_note(fake_pixi, monkeypatch):
    archive = _tarball({
        "pyproject.toml": "[project]\nname='my-tasks'\n[tool.pixi.workspace]\n",
        "pixi.lock": "version: 6\nenvironments:\n  default:\n    packages:\n      " + pixi_config.current_pixi_platform() + ":\n      - pypi: x\n",
    })
    monkeypatch.setattr(task_packages, "http_get", lambda url, headers=None: archive)
    entry = install.install_version(_pkg_with_source(archive), "1.2.0", log=lambda _l: None)
    assert entry["pixi_frozen"] is True
    assert entry["lock_note"] is None


def test_use_lock_false_skips_download(fake_pixi, monkeypatch):
    def no_download(url, headers=None):
        raise AssertionError("should not download the source archive")

    monkeypatch.setattr(task_packages, "http_get", no_download)
    entry = install.install_version(
        _pkg_with_source(b""), "1.2.0", log=lambda _l: None, use_lock=False
    )
    assert entry["pixi_frozen"] is False


def test_safe_extract_rejects_path_traversal(tmp_path):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe"):
        install._safe_extract(buf.getvalue(), tmp_path / "out")


def test_lock_for_other_platform_falls_back(fake_pixi, monkeypatch):
    archive = _tarball({
        "pixi.toml": "[workspace]\n",
        "pixi.lock": "version: 6\nenvironments:\n  default:\n    packages:\n      some-other-os:\n",
    })
    monkeypatch.setattr(task_packages, "http_get", lambda url, headers=None: archive)
    entry = install.install_version(_pkg_with_source(archive), "1.2.0", log=lambda _l: None)
    assert entry["pixi_frozen"] is False
    assert "--frozen" not in fake_pixi.read_text()
