import json
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
import zipfile

import pytest

from napari_workflow_tasks import catalog


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    monkeypatch.setattr(catalog, "ENVS_DIR", tmp_path / "envs")
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
        catalog.PackageVersion("1.10.0", "https://x/my_tasks-1.10.0-py3-none-any.whl", "my_tasks-1.10.0-py3-none-any.whl"),
        catalog.PackageVersion("2.0.0a1", "https://x/a.whl", "my_tasks-2.0.0a1-py3-none-any.whl"),
        catalog.PackageVersion("1.2.0", "https://x/my_tasks-1.2.0-py3-none-any.whl", "my_tasks-1.2.0-py3-none-any.whl"),
    ]
    versions.sort(key=lambda v: catalog.version_sort_key(v.version), reverse=True)
    return catalog.TaskPackage("https://pypi.org/project/my-tasks", kind, "my-tasks", versions)


def test_versions_sorted_and_default_skips_prerelease():
    pkg = _pkg()
    assert [v.version for v in pkg.versions] == ["2.0.0a1", "1.10.0", "1.2.0"]
    assert pkg.default_version.version == "1.10.0"


def test_task_type():
    assert catalog.task_type({"executable_parallel": "a.py"}) == "parallel"
    assert catalog.task_type({"executable_non_parallel": "a.py"}) == "non_parallel"
    assert (
        catalog.task_type({"executable_parallel": "a", "executable_non_parallel": "b"})
        == "compound"
    )


def test_read_wheel_info(tmp_path):
    manifest = {"task_list": [{"name": "T", "executable_parallel": "t.py"}]}
    wheel = tmp_path / "my_tasks-1.2.0-py3-none-any.whl"
    _make_wheel(wheel, manifest)
    info = catalog.read_wheel_info(str(wheel))
    assert info.module_name == "my_tasks"
    assert info.task_list[0]["name"] == "T"
    assert info.extras == ["fractal-tasks"]
    assert catalog.choose_python(info.requires_python) == "3.12"


def test_choose_python():
    assert catalog.choose_python("<3.12,>=3.11") == "3.11"
    assert catalog.choose_python(None) == "3.12"


@pytest.mark.parametrize("kind", ["pypi", "github"])
def test_pixi_manifest_pins_version(isolated_home, kind):
    pkg = _pkg(kind)
    path = catalog.write_pixi_manifest(pkg, "1.2.0", ["fractal-tasks"], ">=3.11,<3.12")
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
    path = catalog.write_pixi_manifest(
        pkg, "1.2.0", [], None, pins=["cellpose<4.2", "numpy==1.26.4"]
    )
    deps = tomllib.loads(path.read_text())["pypi-dependencies"]
    assert deps["cellpose"] == "<4.2"
    assert deps["numpy"] == "==1.26.4"
    assert deps["my-tasks"]["version"] == "==1.2.0"


def test_invalid_pin_raises():
    with pytest.raises(ValueError):
        catalog.parse_pins(["cellpose<<4"])


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

    monkeypatch.setattr(catalog.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(catalog, "_github_token", lambda: None)
    url = "https://api.github.com/repos/o/r/releases"

    assert catalog._github_get_json(url) == [{"tag_name": "v1"}]  # fresh
    assert catalog._github_get_json(url) == [{"tag_name": "v1"}]  # 304 -> cache
    assert calls[1].get("If-none-match") == '"abc"'
    assert catalog._github_get_json(url) == [{"tag_name": "v1"}]  # limited -> cache

    with pytest.raises(catalog.GitHubRateLimitError):  # limited, no cache
        catalog._github_get_json(url + "?other")


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
        return catalog.VersionInfo(manifest, "my_tasks",
                                   "my_tasks/__FRACTAL_MANIFEST__.json",
                                   ["fractal-tasks"], ">=3.11")

    for mod in (builder.catalog,):
        monkeypatch.setattr(mod, "load_package", lambda source: pkg)
        monkeypatch.setattr(mod, "read_remote_wheel_info", fake_remote)

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
    (pkg_snap,) = catalog.packages_from_index(data, base_url="http://x/")
    assert pkg_snap.default_version.version == "1.10.0"
    info = catalog.get_display_info(pkg_snap, "1.2.0")  # no network needed
    assert info.task_list[0]["name"] == "T"
    assert catalog.task_type(info.task_list[0]) == "parallel"
    assert info.extras == ["fractal-tasks"]
    assert "args_schema_parallel" not in info.task_list[0]  # summary only
