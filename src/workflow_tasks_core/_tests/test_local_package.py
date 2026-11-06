"""Task packages from a source archive (.tar.gz) on this computer."""

import io
import json
import tarfile
import tomllib

import pytest

from workflow_tasks_core import Package, install, manifests, paths, pixi_config
from workflow_tasks_core.task_packages import LOCAL, local_package, sdist_metadata

MANIFEST = {"manifest_version": "2", "task_list": [
    {"name": "Segment", "executable_parallel": "segment.py"}]}
PKG_INFO = """Metadata-Version: 2.1
Name: My_Tasks
Version: 1.0
Requires-Python: >=3.11
Provides-Extra: dev
"""
PYPROJECT = """[project]
name = "my-tasks"
version = "0.5"
requires-python = ">=3.10"
[project.optional-dependencies]
gpu = ["cupy"]
"""


def _sdist(path, files):
    """A .tar.gz with ``files`` ({path in the project: text}) in one top
    folder, like an sdist."""
    with tarfile.open(path, "w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"my_tasks-1.0/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def sdist(tmp_path):
    return _sdist(tmp_path / "my_tasks-1.0.tar.gz", {
        "PKG-INFO": PKG_INFO,
        "pyproject.toml": PYPROJECT,
        "src/my_tasks/__FRACTAL_MANIFEST__.json": json.dumps(MANIFEST),
        "pixi.lock": "",
    })


def test_metadata_from_pkg_info(sdist):
    assert sdist_metadata(sdist) == {
        "name": "My_Tasks", "version": "1.0", "requires_python": ">=3.11",
        "extras": ["dev"], "has_pixi_lock": True}


def test_metadata_from_pyproject(tmp_path):
    path = _sdist(tmp_path / "a.tar.gz", {"pyproject.toml": PYPROJECT})
    meta = sdist_metadata(path)
    assert (meta["name"], meta["version"], meta["extras"]) == ("my-tasks", "0.5", ["gpu"])
    assert meta["has_pixi_lock"] is False


def test_metadata_needs_name_and_version(tmp_path):
    path = _sdist(tmp_path / "a.tar.gz", {"pyproject.toml": "[project]\nname = 'x'\n"})
    with pytest.raises(ValueError, match="python -m build --sdist"):
        sdist_metadata(path)


def test_local_package(sdist):
    pkg = local_package(sdist)
    assert (pkg.kind, pkg.pkg_name, pkg.source) == (LOCAL, "my-tasks", str(sdist))
    (pv,) = pkg.versions
    assert pv.version == "1.0"
    assert pv.source_url == pv.wheel_url == sdist.as_uri()
    assert pv.pixi_lock is True


def test_manifest_of_a_local_archive(sdist, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    info = manifests.get_version_info(local_package(sdist), "1.0")
    assert info.manifest == MANIFEST
    assert info.module_name == "my_tasks"  # src/ layout
    assert info.manifest_relpath == "my_tasks/__FRACTAL_MANIFEST__.json"
    assert info.extras == ["dev"]
    assert not (tmp_path / "catalog").exists()  # read again every time


def test_archive_without_manifest(tmp_path):
    path = _sdist(tmp_path / "a.tar.gz", {"PKG-INFO": PKG_INFO})
    with pytest.raises(ValueError, match="no __FRACTAL_MANIFEST__.json"):
        manifests.read_sdist_info(path)


def test_package_from_file(sdist):
    pkg = Package.from_file(sdist)
    assert (pkg.key, pkg.tasks) == ("my-tasks==1.0", ["Segment"])
    assert not pkg.is_installed


def test_installed_from_its_path(sdist):
    source = pixi_config.resolve_install_source(local_package(sdist), "1.0")
    # a TOML string: backslashes of a Windows path are escaped
    [field] = source.dep_fields
    assert tomllib.loads(field) == {"path": str(sdist.resolve())}


def test_rebuilt_archive_is_installed_again(sdist):
    """Same name and version, new content: not the cached install recipe."""
    def key():
        return install.install_recipe_key(
            local_package(sdist), "1.0", [], None, {}, "pixi", pixi_ver="0.40")

    before = key()
    assert key() == before
    _sdist(sdist, {"PKG-INFO": PKG_INFO, "src/my_tasks/__init__.py": "x = 1"})
    assert key() != before


def test_local_source_is_read_not_cached(sdist, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path / "cache")
    assert install._fetch_source(sdist.as_uri(), print) == sdist.read_bytes()
    assert not (tmp_path / "cache").exists()
