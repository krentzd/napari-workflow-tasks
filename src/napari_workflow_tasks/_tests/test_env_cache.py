"""Tests for the install caches (_env_cache) and their use in
catalog.install_version: native-library store, install recipes, import-check
cache, failed-install marker, no-build retry on the pixi-config path."""
import errno
import json
import os
import subprocess

import pytest

from napari_workflow_tasks import _env_cache as ec
from napari_workflow_tasks import catalog


# --------------------------------------------------------------------------
# Native-library store
# --------------------------------------------------------------------------


def _write(path, data: bytes, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def _env(root, libs: dict):
    for rel, data in libs.items():
        _write(root / rel, data)
    return root


def test_native_lib_pattern():
    for name in ("libz.dylib", "libz.1.3.dylib", "_core.cpython-312-darwin.so",
                 "libfoo.so", "libfoo.so.1", "libfoo.so.1.2.3"):
        assert ec.NATIVE_LIB_RE.search(name), name
    for name in ("foo.py", "libfoo.a", "foo.so.txt", "libso", "x.dylib.json"):
        assert not ec.NATIVE_LIB_RE.search(name), name


def test_dedup_store_and_relink(tmp_path):
    store = tmp_path / "store"
    a = _env(tmp_path / "a", {
        "lib/libshared.dylib": b"shared" * 100,
        "lib/libonly_a.so.1": b"only a",
        "lib/python3.12/site-packages/m/_x.cpython-312-darwin.so": b"ext",
        "lib/readme.txt": b"not a lib",
    })
    os.symlink("libshared.dylib", a / "lib" / "libshared.1.dylib")
    st = ec.dedup_tree(a, store)
    assert (st.scanned, st.added, st.relinked) == (3, 3, 0)
    assert st.disabled is None
    assert len(list(store.iterdir())) == 3
    assert os.stat(a / "lib/libshared.dylib").st_nlink == 2
    assert os.path.islink(a / "lib/libshared.1.dylib")  # symlinks untouched
    assert os.stat(a / "lib/readme.txt").st_nlink == 1

    b = _env(tmp_path / "b", {
        "lib/libshared.dylib": b"shared" * 100,
        "lib/libonly_b.so": b"only b",
    })
    before = (b / "lib/libshared.dylib").read_bytes()
    st = ec.dedup_tree(b, store)
    assert (st.relinked, st.added) == (1, 1)
    assert (b / "lib/libshared.dylib").read_bytes() == before
    assert os.path.samefile(a / "lib/libshared.dylib", b / "lib/libshared.dylib")
    assert os.stat(store / ec.file_sha256(b / "lib/libshared.dylib")).st_nlink == 3
    assert not list(b.rglob("*" + ec._TMP_SUFFIX))

    # A second pass does no work (already linked: no hashing)
    st = ec.dedup_tree(b, store)
    assert (st.already_linked, st.relinked, st.added) == (2, 0, 0)


def test_dedup_mode_mismatch_not_relinked(tmp_path):
    store = tmp_path / "store"
    a = _env(tmp_path / "a", {"libx.so": b"x"})
    ec.dedup_tree(a, store)
    b = tmp_path / "b"
    _write(b / "libx.so", b"x", mode=0o644)
    st = ec.dedup_tree(b, store)
    assert (st.relinked, st.skipped) == (0, 1)
    assert os.stat(b / "libx.so").st_nlink == 1


def test_dedup_disabled_by_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(ec.DEDUP_ENV_VAR, "0")
    a = _env(tmp_path / "a", {"libx.so": b"x"})
    st = ec.dedup_tree(a, tmp_path / "store")
    assert st.scanned == 0 and "=0" in st.disabled
    assert not (tmp_path / "store").exists()


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EPERM])
def test_dedup_link_failure_falls_back(tmp_path, monkeypatch, code):
    a = _env(tmp_path / "a", {"liba.so": b"a", "libb.so": b"b"})
    msgs = []

    def no_link(src, dst, **kw):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(ec.os, "link", no_link)
    st = ec.dedup_tree(a, tmp_path / "store", log=msgs.append)
    assert st.disabled and errno.errorcode[code] in st.disabled
    assert len(msgs) == 1  # logged once, then stopped
    assert (a / "liba.so").read_bytes() == b"a"
    assert (a / "libb.so").read_bytes() == b"b"


def test_prune_store(tmp_path):
    store = tmp_path / "store"
    a = _env(tmp_path / "a", {"libshared.so": b"s", "libonly_a.so": b"a"})
    b = _env(tmp_path / "b", {"libshared.so": b"s"})
    ec.dedup_tree(a, store)
    ec.dedup_tree(b, store)
    (store / ("junk" + ec._TMP_SUFFIX)).write_bytes(b"tmp")
    assert ec.prune_store(store).removed == 1  # only the temp file
    import shutil

    shutil.rmtree(a)
    st = ec.prune_store(store)
    assert (st.removed, st.kept) == (1, 1)  # libonly_a gone, shared kept
    assert (b / "libshared.so").read_bytes() == b"s"
    shutil.rmtree(b)
    assert ec.prune_store(store).removed == 1
    assert list(store.iterdir()) == []


def test_delete_env_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "ENVS_DIR", tmp_path / "envs")
    monkeypatch.setattr(catalog, "CACHE_DIR", tmp_path / "cache")
    env = catalog.env_dir("p", "1") / ".pixi" / "envs" / "default"
    _env(env, {"lib/libx.dylib": b"x"})
    ec.dedup_tree(env, catalog.native_store_dir())
    assert len(list(catalog.native_store_dir().iterdir())) == 1
    # default: recently orphaned libraries are kept for a fast re-install
    assert catalog.delete_env("p", "1")["removed"] == 0
    assert not catalog.env_dir("p", "1").exists()
    assert catalog.prune_native_store(unused_for=3600)["removed"] == 0
    assert catalog.prune_native_store()["removed"] == 1


# --------------------------------------------------------------------------
# Recipes
# --------------------------------------------------------------------------


def test_recipe_key_stable():
    f = {"pkg": "a", "version": "1", "pins": {"x": "<2", "y": ">1"},
         "extras": [], "system_requirements": {"macos": "26.7.1"}}
    k = ec.recipe_key(f)
    assert k == ec.recipe_key(dict(reversed(list(f.items()))))
    assert k == ec.recipe_key(json.loads(json.dumps(f)))
    assert k != ec.recipe_key({**f, "pins": {"x": "<3", "y": ">1"}})
    assert k != ec.recipe_key({**f, "system_requirements": {"macos": "13.0"}})
    assert len(k) == 40


def test_install_recipe_key_inputs(monkeypatch):
    monkeypatch.setattr(catalog, "pixi_version", lambda pixi: "pixi 0.83.0")
    pkg = _pkg()
    key = lambda **kw: catalog.install_recipe_key(  # noqa: E731
        pkg, "1.0", kw.get("extras", []), kw.get("pins"),
        kw.get("sysreqs", {"macos": "26.7.1"}), "pixi")
    base = key()
    assert base == key()
    # pin normalisation: spacing / order of specifiers do not matter
    assert key(pins=["numpy <2, >=1.20"]) == key(pins=["numpy>=1.20,<2"])
    assert key(pins=["numpy<2"]) != base
    assert key(extras=["gpu"]) != base
    assert key(sysreqs={"macos": "13.0"}) != base
    monkeypatch.setattr(catalog, "pixi_version", lambda pixi: "pixi 0.84.0")
    assert key() != base
    monkeypatch.setattr(catalog, "current_pixi_platform", lambda: "linux-64")
    assert key() != base


def test_recipe_roundtrip_and_damage(tmp_path):
    d = tmp_path / "recipes"
    r = {"env_kind": "resolved", "manifest_relpath": "pixi.toml",
         "manifest_text": "[workspace]\n", "lock_text": "version: 6\n"}
    ec.save_recipe(d, "k1", r)
    got = ec.load_recipe(d, "k1")
    assert got["lock_sha256"] == ec.text_sha256("version: 6\n")
    assert got["env_kind"] == "resolved"

    # Corrupt the stored lock text: the recipe is rejected and deleted
    data = json.loads((d / "k1.json").read_text())
    data["lock_text"] = "garbage"
    (d / "k1.json").write_text(json.dumps(data))
    msgs = []
    assert ec.load_recipe(d, "k1", msgs.append) is None
    assert not (d / "k1.json").exists() and "damaged" in msgs[0]

    (d / "k2.json").write_text("{not json")
    assert ec.load_recipe(d, "k2", lambda m: None) is None
    assert not (d / "k2.json").exists()


def test_recipes_disabled(tmp_path, monkeypatch):
    d = tmp_path / "recipes"
    ec.save_recipe(d, "k", {"manifest_text": "", "lock_text": ""})
    monkeypatch.setenv(ec.RECIPES_ENV_VAR, "0")
    assert ec.load_recipe(d, "k") is None


# --------------------------------------------------------------------------
# install_version with a fake pixi
# --------------------------------------------------------------------------


def _pkg(source_url=None):
    return catalog.TaskPackage(
        "https://pypi.org/project/my-tasks", "pypi", "my-tasks",
        [catalog.PackageVersion("1.0", "https://x/my_tasks-1.0-py3-none-any.whl",
                                "my_tasks-1.0-py3-none-any.whl", ">=3.11",
                                source_url=source_url)],
    )


SRC_URL = "https://x/my_tasks-1.0.tar.gz"


class FakePixi:
    """Stands in for `pixi install` / `pixi run` (check_env)."""

    def __init__(self, tmp_path):
        self.calls = []
        self.fail_install = None  # callable(cmd) -> error text or None
        self.fail_check = False
        self.pkg_dir = tmp_path / "site" / "my_tasks"
        self.pkg_dir.mkdir(parents=True)

    def __call__(self, cmd, log):
        self.calls.append(cmd)
        manifest = cmd[cmd.index("--manifest-path") + 1]
        root = os.path.dirname(manifest)
        if cmd[1] == "install":
            if self.fail_install:
                msg = self.fail_install(cmd)
                if msg:
                    err = RuntimeError("Command failed: " + msg[:20])
                    err.output = msg
                    raise err
            lock = os.path.join(root, "pixi.lock")
            if os.path.exists(lock) and "broken" in open(lock).read():
                err = RuntimeError("Command failed: bad lock")
                err.output = "failed to load the lock file"
                raise err
            if "--frozen" not in cmd:
                with open(lock, "w") as f:
                    f.write("version: 6\n# solved from " + open(manifest).read())
            prefix = os.path.join(root, ".pixi", "envs", "default")
            os.makedirs(os.path.join(prefix, "conda-meta"), exist_ok=True)
            open(os.path.join(prefix, "conda-meta", "python-3.12.json"), "w").close()
            lib = os.path.join(prefix, "lib", "libfake.dylib")
            os.makedirs(os.path.dirname(lib), exist_ok=True)
            if not os.path.exists(lib):
                with open(lib, "wb") as f:
                    f.write(b"\xcf\xfa\xed\xfe fake native lib")
            return ""
        if cmd[1] == "run":
            if self.fail_check:
                err = RuntimeError("Command failed")
                err.output = "CHECKFAIL=my_tasks.t: ImportError: boom"
                raise err
            return f"PKGDIR={self.pkg_dir}\nIMPORTOK=my_tasks.t"
        raise AssertionError(cmd)

    @property
    def installs(self):
        return [c for c in self.calls if c[1] == "install"]

    @property
    def checks(self):
        return [c for c in self.calls if c[1] == "run"]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "CATALOG_CACHE_DIR", tmp_path / "catalog")
    monkeypatch.setattr(catalog, "ENVS_DIR", tmp_path / "envs")
    monkeypatch.setattr(catalog, "CACHE_DIR", tmp_path / "cache")
    info = catalog.VersionInfo(
        {"task_list": [{"name": "T", "executable_parallel": "t.py"}]},
        "my_tasks", "my_tasks/__FRACTAL_MANIFEST__.json", [], ">=3.11")
    monkeypatch.setattr(catalog, "get_version_info", lambda p, v: info)
    monkeypatch.setattr(catalog, "find_pixi", lambda: "pixi")
    monkeypatch.setattr(catalog, "pixi_version", lambda pixi: "pixi 0.83.0")
    monkeypatch.setattr(catalog, "host_system_requirements",
                        lambda log=None: {"macos": "26.7.1"})
    # `pixi list` (pins): nothing locked / installed unless a test says so
    monkeypatch.setattr(catalog, "_locked_versions", lambda pixi, m: {})
    monkeypatch.setattr(catalog, "lock_package_versions",
                        lambda pixi, m, name, environment="default": None)
    f = FakePixi(tmp_path)
    monkeypatch.setattr(catalog, "_run_streaming", f)
    return f


def test_recipe_reused_and_check_cached(fake):
    pkg, msgs = _pkg(), []
    e1 = catalog.install_version(pkg, "1.0", log=msgs.append)
    assert e1["env_kind"] == "resolved" and "from_recipe" not in e1
    assert e1["pixi_frozen"] is False
    assert len(fake.installs) == 1 and len(fake.checks) == 1
    recipe = json.loads(
        (catalog.recipes_dir() / f"{e1['recipe_key']}.json").read_text())
    assert recipe["env_kind"] == "resolved" and recipe["python"] == "3.12"
    assert recipe["check"]["ok"] and recipe["check"]["modules"] == ["my_tasks", "t.py"]
    assert catalog.is_installed("my-tasks", "1.0")
    # the native lib went into the store
    assert len(list(catalog.native_store_dir().iterdir())) == 1

    # S2: env present -> recipe, --frozen, no import check
    e2 = catalog.install_version(pkg, "1.0", log=msgs.append)
    assert e2["from_recipe"] and e2["src_path"] == e1["src_path"]
    assert "--frozen" in fake.installs[-1]
    assert len(fake.checks) == 1
    assert any("Import check skipped" in m for m in msgs)

    # S3: env deleted -> recipe (frozen), store relinks, check runs again
    catalog.delete_env("my-tasks", "1.0")
    assert not catalog.is_installed("my-tasks", "1.0")
    msgs.clear()
    e3 = catalog.install_version(pkg, "1.0", log=msgs.append)
    assert e3["from_recipe"] and "--frozen" in fake.installs[-1]
    assert len(fake.checks) == 2
    assert any("1 linked to the cache" in m for m in msgs)
    lock = catalog.env_dir("my-tasks", "1.0") / "pixi.lock"
    assert lock.read_text() == recipe["lock_text"]


def test_recheck_when_env_changes(fake):
    pkg = _pkg()
    catalog.install_version(pkg, "1.0", log=lambda m: None)
    prefix = catalog.env_dir("my-tasks", "1.0") / ".pixi" / "envs" / "default"
    (prefix / "conda-meta" / "numpy-2.0.json").write_text("{}")  # env changed
    catalog.install_version(pkg, "1.0", log=lambda m: None)
    assert len(fake.checks) == 2


def test_changed_pins_new_recipe(fake):
    pkg = _pkg()
    e1 = catalog.install_version(pkg, "1.0", log=lambda m: None)
    e2 = catalog.install_version(pkg, "1.0", log=lambda m: None, pins=["numpy<2"])
    assert e2["recipe_key"] != e1["recipe_key"] and "from_recipe" not in e2
    assert "--frozen" not in fake.installs[-1]  # full flow (solve)
    assert len(list(catalog.recipes_dir().glob("*.json"))) == 2
    assert e1["pins_applied"] is None
    assert e2["pins_applied"]["pins"] == ["numpy<2"]
    assert "generated pixi.toml" in e2["pins_applied"]["how"]
    # the recipe keeps how the pins were applied
    e3 = catalog.install_version(pkg, "1.0", log=lambda m: None, pins=["numpy<2"])
    assert e3["from_recipe"] and e3["pins_applied"]["how"] == e2["pins_applied"]["how"]


def test_failed_frozen_install_drops_recipe(fake):
    pkg, msgs = _pkg(), []
    e1 = catalog.install_version(pkg, "1.0", log=msgs.append)
    fake.fail_install = lambda cmd: "lock broken" if "--frozen" in cmd else None
    e2 = catalog.install_version(pkg, "1.0", log=msgs.append)
    assert "from_recipe" not in e2
    assert any("Install from the cached recipe failed" in m for m in msgs)
    # full flow succeeded and saved a fresh recipe under the same key
    assert e2["recipe_key"] == e1["recipe_key"]
    assert (catalog.recipes_dir() / f"{e1['recipe_key']}.json").exists()


def test_unreadable_recipe_lock_recovers(fake):
    """A recipe whose lock pixi cannot read (sha still consistent): the
    frozen install fails, the recipe is dropped and the full flow must not
    start from the restored bad lock."""
    pkg, msgs = _pkg(), []
    e1 = catalog.install_version(pkg, "1.0", log=msgs.append)
    path = catalog.recipes_dir() / f"{e1['recipe_key']}.json"
    r = json.loads(path.read_text())
    r["lock_text"] = "version: 6\nenvironments: [broken\n"
    r["lock_sha256"] = ec.text_sha256(r["lock_text"])
    path.write_text(json.dumps(r))
    e2 = catalog.install_version(pkg, "1.0", log=msgs.append)
    assert "from_recipe" not in e2
    assert any("cached recipe failed" in m for m in msgs)
    lock = catalog.env_dir("my-tasks", "1.0") / "pixi.lock"
    assert "broken" not in lock.read_text()
    assert "broken" not in json.loads(path.read_text())["lock_text"]


def test_failed_check_not_installed(fake):
    fake.fail_check = True
    with pytest.raises(RuntimeError, match="can't be imported"):
        catalog.install_version(_pkg(), "1.0", log=lambda m: None)
    d = catalog.env_dir("my-tasks", "1.0")
    assert (d / ".pixi" / "envs").exists()  # kept for debugging
    assert (d / catalog.INCOMPLETE_MARKER).exists()
    assert not catalog.is_installed("my-tasks", "1.0")
    assert not list(catalog.recipes_dir().glob("*.json"))
    fake.fail_check = False
    catalog.install_version(_pkg(), "1.0", log=lambda m: None)
    assert catalog.is_installed("my-tasks", "1.0")


def test_dedup_off_still_installs(fake, monkeypatch):
    monkeypatch.setenv(ec.DEDUP_ENV_VAR, "0")
    catalog.install_version(_pkg(), "1.0", log=lambda m: None)
    assert not catalog.native_store_dir().exists()
    assert catalog.is_installed("my-tasks", "1.0")


def test_exdev_install_still_succeeds(fake, monkeypatch):
    def no_link(src, dst, **kw):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(ec.os, "link", no_link)
    msgs = []
    entry = catalog.install_version(_pkg(), "1.0", log=msgs.append)
    assert entry["src_path"]
    assert any("dedup skipped" in m and "EXDEV" in m for m in msgs)


# --------------------------------------------------------------------------
# (b) pixi-config path: no-build retry from the error, recipe restore
# --------------------------------------------------------------------------

PYPROJECT = """[project]
name = "my-tasks"
version = "1.0"

[tool.pixi.workspace]
channels = ["conda-forge"]
platforms = ["{plat}"]

[tool.pixi.pypi-options]
index-url = "https://pypi.org/simple"

[tool.pixi.pypi-dependencies]
my-tasks = {{ path = ".", editable = true }}
"""


@pytest.fixture
def project_pkg(fake, tmp_path, monkeypatch):
    import tarfile
    import tomllib

    src = tmp_path / "sdist" / "my_tasks-1.0"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text(
        PYPROJECT.format(plat=catalog.current_pixi_platform()))
    # the lock does not cover this platform: (a) is skipped, (b) re-solves
    (src / "pixi.lock").write_text(
        "version: 6\nenvironments:\n  default:\n    packages:\n      some-os:\n"
        "      - pypi: .\npackages: []\n")
    archive = tmp_path / "my_tasks-1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src, arcname="my_tasks-1.0")
    downloads = []

    def fake_http_get(url, headers=None):
        downloads.append(url)
        return archive.read_bytes()

    monkeypatch.setattr(catalog, "_http_get", fake_http_get)

    def fail(cmd):
        manifest = cmd[cmd.index("--manifest-path") + 1]
        if "--frozen" in cmd or not manifest.endswith("pyproject.toml"):
            return None
        cfg = tomllib.loads(open(manifest).read())["tool"]["pixi"]
        nb = cfg.get("pypi-options", {}).get("no-build", [])
        if "pyqt6" not in nb:
            return "  ├─▶ Failed to build `pyqt6==6.12.0`\n  ╰─▶ build backend"
        if "pyqt6-qt6" not in nb:
            return "Failed to build `\x1b[36mPyQt6-Qt6==6.12.0\x1b[39m`"
        return None

    fake.fail_install = fail
    return downloads


def test_pixi_config_no_build_retry(fake, project_pkg):
    import tomllib

    msgs = []
    entry = catalog.install_version(_pkg(SRC_URL), "1.0", log=msgs.append)
    assert entry["env_kind"] == "pixi config" and entry["pixi_frozen"] is False
    manifest = entry["pixi_manifest_path"]
    cfg = tomllib.loads(open(manifest).read())["tool"]["pixi"]
    # names come from the errors; other pypi-options are kept
    assert cfg["pypi-options"]["no-build"] == ["pyqt6", "pyqt6-qt6"]
    assert cfg["pypi-options"]["index-url"] == "https://pypi.org/simple"
    # host requirements on the platform entry (no deprecated table)
    plat = catalog.current_pixi_platform()
    assert cfg["workspace"]["platforms"] == [
        catalog.host_platform_entry(plat, {"macos": "26.7.1"})]
    assert "system-requirements" not in cfg
    assert len(fake.installs) == 3
    recipe = json.loads(
        (catalog.recipes_dir() / f"{entry['recipe_key']}.json").read_text())
    assert recipe["no_build"] == ["pyqt6", "pyqt6-qt6"]
    assert recipe["manifest_relpath"] == "source/my_tasks-1.0/pyproject.toml"
    assert recipe["source_url"] == SRC_URL

    # Env and project deleted: restored from the recipe + cached sdist,
    # one frozen install, no download
    catalog.delete_env("my-tasks", "1.0")
    e2 = catalog.install_version(_pkg(SRC_URL), "1.0", log=msgs.append)
    assert e2["from_recipe"] and e2["env_kind"] == "pixi config"
    assert "--frozen" in fake.installs[-1] and len(fake.installs) == 4
    assert len(project_pkg) == 1  # source archive downloaded once (cached)
    assert open(e2["pixi_manifest_path"]).read() == recipe["manifest_text"]


def test_no_build_retry_gives_up_without_names(fake):
    fake.fail_install = lambda cmd: "No module named 'pkg_resources'"
    with pytest.raises(RuntimeError, match="every allowed Python"):
        catalog.install_version(_pkg(), "1.0", log=lambda m: None)
    # one attempt per Python, no no-build retries
    assert len(fake.installs) == len(catalog.candidate_pythons(">=3.11"))


# --------------------------------------------------------------------------
# _widget._prefill_channel: identifier lists with minItems
# --------------------------------------------------------------------------


def test_prefill_channel_min_items(qapp=None):
    pytest.importorskip("napari")
    from qtpy.QtWidgets import QApplication

    from napari_workflow_tasks._schema_form import SchemaForm
    from napari_workflow_tasks._widget import _prefill_channel

    app = QApplication.instance() or QApplication([])  # noqa: F841
    chan = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "default": "label"},
            "identifiers": {"type": "array", "items": {"type": "string"},
                            "default": [], "minItems": 1},
        },
    }
    schema = {
        "type": "object",
        "properties": {
            "channel": {"$ref": "#/$defs/Chan"},
            "extra": {"anyOf": [{"$ref": "#/$defs/Chan"}, {"type": "null"}]},
            "other": {"type": "array", "items": {"type": "string"},
                      "default": []},
        },
        "required": ["channel"],
        "$defs": {"Chan": chan},
    }
    form = SchemaForm(schema)
    _prefill_channel(form.fields, "DAPI", ["DAPI", "nanog"])
    args = form.get_args()
    assert args["channel"]["identifiers"] == ["DAPI"]
    assert "extra" not in args or args["extra"] in (None, {})
    assert args.get("other", []) == []
    # not a channel of the image: nothing prefilled -> minItems error
    form2 = SchemaForm(schema)
    _prefill_channel(form2.fields, "nuclei", ["DAPI"])
    with pytest.raises(Exception, match="at least 1"):
        form2.get_args()


def test_pixi_version_real():
    try:
        pixi = catalog.find_pixi()
    except FileNotFoundError:
        pytest.skip("no pixi")
    assert "pixi" in catalog.pixi_version(pixi)
    assert subprocess.run([pixi, "--version"], capture_output=True).returncode == 0


# --------------------------------------------------------------------------
# Fall-through between (a), (b) and (c); pins and extras
# --------------------------------------------------------------------------


@pytest.fixture
def locked_pkg(fake, tmp_path, monkeypatch):
    """Source archive whose pixi.lock covers this platform."""
    import tarfile

    src = tmp_path / "sdist_a" / "my_tasks-1.0"
    src.mkdir(parents=True)
    plat = catalog.current_pixi_platform()
    (src / "pyproject.toml").write_text(PYPROJECT.format(plat=plat))
    (src / "pixi.lock").write_text(
        f"version: 6\nenvironments:\n  default:\n    packages:\n      {plat}:\n"
        "      - pypi: .\npackages: []\n")
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src, arcname="my_tasks-1.0")
    monkeypatch.setattr(catalog, "_http_get",
                        lambda url, headers=None: archive.read_bytes())
    return fake


def _lock_has(monkeypatch, versions):
    """The release's pixi.lock (as `pixi list --frozen` reads it) has
    ``versions`` {name: (version, kind)}."""
    monkeypatch.setattr(catalog, "lock_package_versions",
                        lambda pixi, m, name, environment="default": versions)


def _src_lock_sha(tmp_path):
    return ec.file_sha256(tmp_path / "sdist_a" / "my_tasks-1.0" / "pixi.lock")


def test_locked_install_pins_satisfied(locked_pkg, monkeypatch, tmp_path):
    """Pins the lock satisfies: (a) as is (extras still not used), pins
    verified on the installed env and reported."""
    _lock_has(monkeypatch, {"numpy": ("1.26.4", "pypi")})
    monkeypatch.setattr(catalog, "_locked_versions",
                        lambda pixi, m: {"numpy": "1.26.4"})
    msgs = []
    e = catalog.install_version(_pkg(SRC_URL), "1.0", log=msgs.append,
                                pins=["numpy<2"], extras=["x"])
    assert e["env_kind"] == "pixi.lock" and e["pixi_frozen"] is True
    assert "--frozen" in locked_pkg.installs[0] and "--frozen" in locked_pkg.checks[0]
    assert any("Extras are not used for the pixi.lock install" in m for m in msgs)
    assert any("satisfied by the pixi.lock: numpy<2 (lock has 1.26.4)" in m
               for m in msgs)
    assert e["pins_applied"]["how"] == "satisfied by the package's pixi.lock"
    assert e["pins_applied"]["versions"] == {"numpy": "1.26.4"}
    # the package's own lock is used untouched
    lock = catalog.env_dir("my-tasks", "1.0") / "source" / "my_tasks-1.0" / "pixi.lock"
    assert ec.file_sha256(lock) == _src_lock_sha(tmp_path)


@pytest.mark.parametrize("kind, table", [("pypi", "pypi-dependencies"),
                                         ("conda", "dependencies")])
def test_lock_violates_pins_resolves_config_with_pins(
        locked_pkg, monkeypatch, tmp_path, kind, table):
    """A pin the lock violates: (a) is skipped before installing, (b)
    re-solves the package's pixi config with the pin added (conda or pypi,
    as the package comes in the lock)."""
    _lock_has(monkeypatch, {"numpy": ("2.4.3", kind)})
    monkeypatch.setattr(catalog, "_locked_versions",
                        lambda pixi, m: {"numpy": "1.26.4"})
    msgs = []
    e = catalog.install_version(_pkg(SRC_URL), "1.0", log=msgs.append,
                                pins=["numpy<2"])
    assert e["env_kind"] == "pixi config" and e["pixi_frozen"] is False
    assert not any("--frozen" in c for c in locked_pkg.installs)  # no wasted install
    assert any("does not satisfy the pinned packages: numpy<2 (lock has 2.4.3)"
               in m for m in msgs)
    cfg = tomllib_loads(open(e["pixi_manifest_path"]).read())["tool"]["pixi"]
    assert cfg[table]["numpy"] == "<2"
    other = {"pypi-dependencies", "dependencies"} - {table}
    assert "numpy" not in cfg.get(other.pop(), {})
    assert cfg["pypi-dependencies"]["my-tasks"] == {"path": ".", "editable": True}
    assert f"[tool.pixi.{table}]" in e["pins_applied"]["how"]
    assert e["pins_applied"]["versions"] == {"numpy": "1.26.4"}
    # the package's own lock is kept unmodified next to the re-solved one
    orig = catalog.env_dir("my-tasks", "1.0") / "source" / "my_tasks-1.0" / "pixi.lock.orig"
    assert ec.file_sha256(orig) == _src_lock_sha(tmp_path)


def test_pins_unsolvable_fails_naming_the_pin(locked_pkg, monkeypatch):
    """A pin nothing can satisfy: (a) skipped, (b) and (c) fail; the error
    names the pin and the env is not reported as installed."""
    _lock_has(monkeypatch, {"my-dep": ("1.0", "pypi")})

    def fail(cmd):
        if "--frozen" in cmd:
            return None
        return "  × No solution found: my-dep==99.0 is not available"

    locked_pkg.fail_install = fail
    msgs = []
    with pytest.raises(RuntimeError, match=r"pinned packages my-dep==99\.0"):
        catalog.install_version(_pkg(SRC_URL), "1.0", log=msgs.append,
                                pins=["my-dep==99.0"])
    assert not catalog.is_installed("my-tasks", "1.0")
    assert any("Install from the package's pixi config failed" in m for m in msgs)


def test_failed_check_on_lock_falls_through(locked_pkg, monkeypatch):
    """(a) installs but the import check fails -> (b) re-solve -> pins
    violated there -> (c) resolved install with the pins applied."""
    calls = {"n": 0}
    real = locked_pkg.__call__

    def run(cmd, log):
        if cmd[1] == "run" and "--frozen" in cmd:
            err = RuntimeError("Command failed")
            err.output = "CHECKFAIL=my_tasks.t: ImportError: boom"
            raise err
        calls["n"] += 1
        return real(cmd, log)

    monkeypatch.setattr(catalog, "_run_streaming", run)
    # the lock satisfies the pin (so (a) is tried); the re-solve (b) does
    # not; the resolved install (c) does
    _lock_has(monkeypatch, {"numpy": ("1.26.4", "pypi")})
    monkeypatch.setattr(
        catalog, "_locked_versions",
        lambda pixi, m: {"numpy": "2.0.0" if os.path.exists(
            os.path.join(os.path.dirname(m), "pixi.lock.orig")) else "1.26.4"})
    msgs = []
    e = catalog.install_version(_pkg(SRC_URL), "1.0", log=msgs.append,
                                pins=["numpy<2"])
    assert e["env_kind"] == "resolved" and e["pixi_frozen"] is False
    assert any("falling back" in m for m in msgs)
    assert any("pins not satisfied: numpy<2" in m for m in msgs)
    assert not (catalog.env_dir("my-tasks", "1.0") / "source").exists()
    manifest = tomllib_loads(open(e["pixi_manifest_path"]).read())
    assert manifest["pypi-dependencies"]["numpy"] == "<2"
    assert "generated pixi.toml" in e["pins_applied"]["how"]


def test_extras_skip_resolve_of_pixi_config(fake, project_pkg):
    msgs = []
    e = catalog.install_version(_pkg(SRC_URL), "1.0", log=msgs.append,
                                extras=["gpu"])
    assert e["env_kind"] == "resolved"
    assert any("not re-solving" in m for m in msgs)


def test_use_lock_false_goes_to_resolved(locked_pkg):
    e = catalog.install_version(_pkg(SRC_URL), "1.0", log=lambda m: None,
                                use_lock=False)
    assert e["env_kind"] == "resolved"
    assert not (catalog.env_dir("my-tasks", "1.0") / "source").exists()


def tomllib_loads(text):
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        import tomli as tomllib
    return tomllib.loads(text)
