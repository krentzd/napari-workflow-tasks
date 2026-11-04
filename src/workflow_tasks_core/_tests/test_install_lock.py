import tomllib

import pytest

from workflow_tasks_core import install, paths, pixi_config, task_packages

LOCK = """version: 6
environments:
  default:
    channels:
    - url: https://conda.anaconda.org/conda-forge/
    packages:
      linux-64:
      - conda: https://x/a.conda
      osx-arm64:
      - pypi: .
  dev:
    channels:
    - url: https://conda.anaconda.org/conda-forge/
    packages:
      win-64:
      - conda: https://x/b.conda
packages:
- conda: https://x/a.conda
"""


def test_lock_platforms():
    assert pixi_config.lock_platforms(LOCK) == ["linux-64", "osx-arm64"]
    assert pixi_config.lock_platforms(LOCK, "dev") == ["win-64"]
    assert pixi_config.lock_platforms("version: 6\npackages: []\n") == []


def test_merge_system_requirements():
    merged, problems = pixi_config.merge_system_requirements({}, {"macos": "26.7.1"})
    assert merged == {"macos": "26.7.1"} and problems == []
    merged, problems = pixi_config.merge_system_requirements(
        {"macos": "14.0"}, {"macos": "26.7.1"})
    assert merged == {"macos": "14.0"} and problems == []
    _, problems = pixi_config.merge_system_requirements({"macos": "15"}, {"macos": "13.0"})
    assert problems
    merged, _ = pixi_config.merge_system_requirements(
        {"libc": {"family": "glibc", "version": "2.28"}}, {"libc": "2.35"})
    assert merged["libc"]["version"] == "2.28"


def test_write_system_requirements(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('[project]\nname = "x"\n\n[tool.pixi.system-requirements]\n'
                 'macos = "11"\n\n[tool.pixi.tasks]\na = "b"\n')
    pixi_config.write_system_requirements(p, {"macos": "26.7.1", "cuda": "12"})
    d = tomllib.loads(p.read_text())
    assert d["tool"]["pixi"]["system-requirements"] == {"macos": "26.7.1", "cuda": "12"}
    assert d["tool"]["pixi"]["tasks"] == {"a": "b"}


def test_unmet_pins():
    v = {"cellpose": "4.1.1", "numpy": "2.0.0"}
    assert pixi_config.unmet_pins(["cellpose<4.2"], v) == []
    assert pixi_config.unmet_pins(["numpy<2"], v) == ["numpy<2 (env has 2.0.0)"]
    assert pixi_config.unmet_pins(["torch<3"], v) == []


def test_is_installed_source_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ENVS_DIR", tmp_path)
    p = tmp_path / "pkg" / "1.0" / "source" / "pkg-1.0"
    (p / ".pixi" / "envs").mkdir(parents=True)
    (p / "pyproject.toml").write_text("")
    assert install.is_installed("pkg", "1.0")
    # a failed / interrupted install is not reported as installed
    (tmp_path / "pkg" / "1.0" / install.INCOMPLETE_MARKER).write_text("")
    assert not install.is_installed("pkg", "1.0")


def test_write_system_requirements_pixi_toml(tmp_path):
    p = tmp_path / "pixi.toml"
    p.write_text('[workspace]\nname = "x"\n\n[system-requirements]\nmacos = "11"\n'
                 '\n[tasks]\na = "b"\n')
    pixi_config.write_system_requirements(p, {"macos": "26.7.1"})
    assert pixi_config.write_pypi_no_build(p, ["pyqt6"]) == ["pyqt6"]
    d = tomllib.loads(p.read_text())
    assert d["system-requirements"] == {"macos": "26.7.1"}
    assert d["pypi-options"] == {"no-build": ["pyqt6"]}
    assert d["tasks"] == {"a": "b"} and d["workspace"]["name"] == "x"


def test_write_skip_to_pixi_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pixi_config.sys, "platform", "darwin")
    p = tmp_path / "pixi.toml"
    p.write_text('[workspace]\nname = "x"\n\n[pypi-options]\n'
                 'no-build = ["pyqt6"]\n'
                 'dependency-overrides = { numpy = "<2" }\n')
    pixi_config.write_skip_to_pixi_config(p, ["cupy-cuda12x"])
    options = tomllib.loads(p.read_text())["pypi-options"]
    assert options["no-build"] == ["pyqt6"]
    assert options["dependency-overrides"] == {
        "numpy": "<2",
        "cupy-cuda12x": {"version": "*",
                         "env-markers": "sys_platform != 'darwin'"},
    }


# --------------------------------------------------------------------------
# GitHub source archive: release sdist asset, tag archive only as fallback
# --------------------------------------------------------------------------


def _release(tag, *names):
    return {"tag_name": tag, "assets": [
        {"name": n, "size": 1234,
         "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{n}"}
        for n in names]}


def test_github_versions_prefer_sdist_asset(monkeypatch):
    monkeypatch.setattr(task_packages, "_github_get_json", lambda url: [
        _release("v1.1", "my_tasks-1.1-py3-none-any.whl", "my_tasks-1.1.tar.gz"),
        _release("v1.0", "my_tasks-1.0-py3-none-any.whl"),
    ])
    v11, v10 = task_packages._list_github_versions("o", "r")
    assert v11.source_url == "https://github.com/o/r/releases/download/v1.1/my_tasks-1.1.tar.gz"
    assert v11.source_size == 1234
    assert v10.source_url == "https://github.com/o/r/archive/refs/tags/v1.0.tar.gz"
    assert v10.source_size is None
    # has_pixi_lock reads the raw file by tag for both URL forms
    assert task_packages._github_tag_from_source_url(v11.source_url) == ("o", "r", "v1.1")
    assert task_packages._github_tag_from_source_url(v10.source_url) == ("o", "r", "v1.0")


def test_has_pixi_lock_release_url(monkeypatch):
    seen = []

    class _Res:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append((req.get_method(), req.full_url))
        return _Res()

    monkeypatch.setattr(task_packages.urllib.request, "urlopen", fake_urlopen)
    pv = task_packages.PackageVersion(
        "1.1", "https://github.com/o/r/releases/download/v1.1/my_tasks-1.1-py3-none-any.whl",
        "my_tasks-1.1-py3-none-any.whl",
        source_url="https://github.com/o/r/releases/download/v1.1/my_tasks-1.1.tar.gz")
    assert task_packages.has_pixi_lock(pv) is True
    assert seen == [("HEAD", "https://raw.githubusercontent.com/o/r/v1.1/pixi.lock")]


def test_source_build_env_for_tag_archive():
    pkg = task_packages.TaskPackage("https://github.com/o/r", "github", "my-tasks", [])
    env = install._source_build_env(pkg, "1.0")
    assert env["SETUPTOOLS_SCM_PRETEND_VERSION"] == "1.0"
    assert env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MY_TASKS"] == "1.0"
    assert task_packages.is_tag_archive("https://github.com/o/r/archive/refs/tags/v1.tar.gz")
    assert not task_packages.is_tag_archive(
        "https://github.com/o/r/releases/download/v1/my_tasks-1.tar.gz")


def test_tag_archive_upgraded_to_release_asset(tmp_path, monkeypatch):
    """Old catalogs (tag archive as source_url): the release's sdist asset is
    used when it exists (one HEAD, no API), with no pretend-version env."""
    import io
    import tarfile

    monkeypatch.setattr(paths, "ENVS_DIR", tmp_path / "envs")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in {"pyproject.toml": "[tool.pixi.workspace]\n",
                           "pixi.lock": "version: 6\n"}.items():
            info = tarfile.TarInfo(f"my_tasks-1.0/{name}")
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text.encode()))
    got = []
    monkeypatch.setattr(task_packages, "http_get", lambda url, headers=None: (got.append(url), buf.getvalue())[1])
    asset = "https://github.com/o/r/releases/download/v1.0/my_tasks-1.0.tar.gz"
    monkeypatch.setattr(task_packages, "release_sdist_guess", lambda pv: (asset, 10))
    pv = task_packages.PackageVersion(
        "1.0", "https://github.com/o/r/releases/download/v1.0/my_tasks-1.0-py3-none-any.whl",
        "my_tasks-1.0-py3-none-any.whl", pixi_lock=True,
        source_url="https://github.com/o/r/archive/refs/tags/v1.0.tar.gz")
    pkg = task_packages.TaskPackage("https://github.com/o/r", "github", "my-tasks", [pv])
    src = install._prepare_source(pkg, "1.0", lambda m: None)
    assert got == [asset] and src.url == asset and src.env is None
    # no asset: the tag archive, built with the pretend version
    monkeypatch.setattr(task_packages, "release_sdist_guess", lambda pv: None)
    src = install._prepare_source(pkg, "1.0", lambda m: None)
    assert src.url == pv.source_url
    assert src.env["SETUPTOOLS_SCM_PRETEND_VERSION"] == "1.0"


# --------------------------------------------------------------------------
# Rich platforms (pixi >= 0.71): lock keys, manifest rewrite for (b)
# --------------------------------------------------------------------------

# pixi 0.83 lock of platforms = [{ name = "osx-arm64-host", platform =
# "osx-arm64", macos = "26.7.1" }, "linux-64"] (+ a [system-requirements]
# style auto name)
LOCK_V7_NAMED = """version: 7
platforms:
- name: linux-64
  virtual-packages:
  - __unix=0=0
  - __glibc=2.28
- name: osx-arm64-host
  subdir: osx-arm64
  virtual-packages:
  - __osx=26.7.1
- name: 'osx-64-macos-14-0'
  subdir: osx-64
  virtual-packages:
  - __osx=14.0
environments:
  default:
    channels:
    - url: https://conda.anaconda.org/conda-forge/
    packages:
      linux-64:
      - conda: https://x/a.conda
      osx-arm64-host:
      - conda: https://x/b.conda
      osx-64-macos-14-0:
      - conda: https://x/c.conda
  dev:
    packages:
      osx-arm64-host:
      - conda: https://x/b.conda
packages:
- conda: https://x/a.conda
"""


def test_lock_platforms_named():
    assert pixi_config.lock_platform_names(LOCK_V7_NAMED) == {
        "linux-64": "linux-64", "osx-arm64-host": "osx-arm64",
        "osx-64-macos-14-0": "osx-64"}
    assert pixi_config.lock_platforms(LOCK_V7_NAMED) == ["linux-64", "osx-arm64", "osx-64"]
    assert pixi_config.lock_platforms(LOCK_V7_NAMED, "dev") == ["osx-arm64"]
    # v6 (keyed by subdir) unchanged
    assert pixi_config.lock_platform_names(LOCK) == {"linux-64": "linux-64",
                                                     "osx-arm64": "osx-arm64"}


def test_check_pins_against_lock(tmp_path, monkeypatch):
    seen = []

    def fake_list(pixi, manifest, name, environment="default"):
        seen.append(name)
        return {"osx-arm64-host": {"numpy": ("2.4.3", "pypi"),
                                   "python": ("3.14.3", "conda"),
                                   "cellpose": ("4.1.1", "pypi")},
                "linux-64": {"libgcc": ("15", "conda")}}.get(name)

    monkeypatch.setattr(pixi_config, "lock_package_versions", fake_list)
    m = tmp_path / "pixi.toml"
    r = pixi_config.check_pins_against_lock(
        "pixi", m, LOCK_V7_NAMED, "osx-arm64", ["cellpose<4.2", "numpy<2"])
    assert r.checked and r.unmet == ["numpy<2 (lock has 2.4.3)"]
    assert r.versions == {"cellpose": "4.1.1", "numpy": "2.4.3"}
    assert r.kinds["python"] == "conda" and r.kinds["numpy"] == "pypi"
    assert seen[0] == "osx-arm64-host"  # the lock's name, not the subdir
    r = pixi_config.check_pins_against_lock(
        "pixi", m, LOCK_V7_NAMED, "osx-arm64", ["cellpose<4.2"])
    assert r.checked and r.unmet == []
    # lock does not cover the platform: not checked, kinds from another one
    r = pixi_config.check_pins_against_lock(
        "pixi", m, LOCK_V7_NAMED, "win-64", ["numpy<2"])
    assert not r.checked and r.kinds


PYPROJECT_B = """[project]
name = "my-tasks"

[tool.pixi.workspace]
channels = ["conda-forge"]
platforms = [
    "osx-arm64",  # Apple silicon
    "linux-64",
    { name = "gpu", platform = "linux-64", cuda = "12" },
]

[tool.pixi.dependencies]
python = ">=3.11"
numpy = ">=1.20"

[tool.pixi.pypi-dependencies]
my-tasks = { path = ".", editable = true }
requests = { version = ">=2", extras = ["socks"] }

[tool.pixi.feature.dev]
platforms = ["osx-arm64", "linux-64"]

[tool.pixi.feature.dev.dependencies]
pytest = "*"

[tool.pixi.tasks]
test = { cmd = [
    "pytest",
    "[x]",
] }
"""


@pytest.mark.parametrize("plat", ["osx-arm64", "linux-64"])
def test_write_platform_requirements_pyproject(tmp_path, plat):
    p = tmp_path / "pyproject.toml"
    p.write_text(PYPROJECT_B)
    before = tomllib.loads(PYPROJECT_B)
    reqs = {"macos": "26.7.1"} if plat == "osx-arm64" else {"glibc": "2.28"}
    assert pixi_config.write_platform_requirements(p, plat, reqs) == plat + "-host"
    d = tomllib.loads(p.read_text())
    px = d["tool"]["pixi"]
    entries = px["workspace"]["platforms"]
    host = {"name": plat + "-host", "platform": plat, **reqs}
    other = "linux-64" if plat == "osx-arm64" else "osx-arm64"
    # only the host's entry changed (the first one for its platform)
    assert host in entries and other in entries
    assert {"name": "gpu", "platform": "linux-64", "cuda": "12"} in entries
    assert len(entries) == 3
    # features follow the renamed entry
    assert px["feature"]["dev"]["platforms"] == [
        plat + "-host" if x == plat else x for x in ["osx-arm64", "linux-64"]]
    # everything else unchanged
    for k in ("dependencies", "pypi-dependencies", "tasks"):
        assert px[k] == before["tool"]["pixi"][k]
    assert px["feature"]["dev"]["dependencies"] == {"pytest": "*"}
    assert d["project"] == before["project"]


def test_write_platform_requirements_own_table(tmp_path):
    """The package's [system-requirements] (can't be combined with
    requirements on platforms) is moved to the entries it applies to."""
    p = tmp_path / "pixi.toml"
    p.write_text('[project]\nname = "x"\nchannels = ["conda-forge"]\n'
                 'platforms = ["osx-arm64", "linux-64", "win-64"]\n\n'
                 '[system-requirements]\nmacos = "14.0"\n'
                 'libc = { family = "glibc", version = "2.34" }\n\n'
                 '[feature.lin]\nplatforms = ["linux-64"]\n\n'
                 '[dependencies]\npython = "3.12.*"\n')
    cfg = pixi_config.read_pixi_config(p)
    own = pixi_config.pixi_config_sysreqs(cfg, "osx-arm64")
    assert own == {"macos": "14.0"}
    merged, problems = pixi_config.merge_system_requirements(own, {"macos": "26.7.1"})
    assert merged == {"macos": "14.0"} and not problems  # package's own value stays
    pixi_config.write_platform_requirements(p, "osx-arm64", merged)
    d = tomllib.loads(p.read_text())
    assert "system-requirements" not in d
    assert d["project"]["platforms"] == [
        {"name": "osx-arm64-host", "platform": "osx-arm64", "macos": "14.0"},
        {"name": "linux-64-sysreq", "platform": "linux-64", "glibc": "2.34"},
        "win-64",
    ]
    assert d["feature"]["lin"]["platforms"] == ["linux-64-sysreq"]
    assert d["dependencies"] == {"python": "3.12.*"}


def test_platform_requirements_package_rich_entry(tmp_path):
    """A package that already declares its host entry in the new syntax:
    its name and values are kept (merge rule: own values stay unless they
    exceed the host's)."""
    p = tmp_path / "pixi.toml"
    p.write_text('[workspace]\nname = "x"\nchannels = ["conda-forge"]\n'
                 'platforms = [{ name = "mac", platform = "osx-arm64", '
                 'macos = "15.0" }]\n')
    cfg = pixi_config.read_pixi_config(p)
    own = pixi_config.pixi_config_sysreqs(cfg, "osx-arm64")
    assert own == {"macos": "15.0"}
    _, problems = pixi_config.merge_system_requirements(own, {"macos": "13.0"})
    assert problems == ["macos 15.0 required, host has 13.0"]
    merged, _ = pixi_config.merge_system_requirements(own, {"macos": "26.7.1"})
    assert pixi_config.write_platform_requirements(p, "osx-arm64", merged) == "mac"
    assert tomllib.loads(p.read_text())["workspace"]["platforms"] == [
        {"name": "mac", "platform": "osx-arm64", "macos": "15.0"}]


def test_platform_requirements_unverifiable_edit_leaves_file(tmp_path):
    p = tmp_path / "pixi.toml"
    text = ('workspace = { name = "x", channels = ["conda-forge"], '
            'platforms = ["osx-arm64"] }\n')
    p.write_text(text)
    with pytest.raises(RuntimeError):
        pixi_config.write_platform_requirements(p, "osx-arm64", {"macos": "26.7.1"})
    assert p.read_text() == text


def test_write_pins_to_pixi_config(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text(PYPROJECT_B)
    applied = pixi_config.write_pins_to_pixi_config(
        p, ["numpy<2", "Requests~=2.31", "scipy<1.14", "my-tasks<9", "zarr~=2.18"],
        {"numpy": "conda", "requests": "pypi", "zarr": "conda"}, "my-tasks")
    px = tomllib.loads(p.read_text())["tool"]["pixi"]
    # conda package: ANDed with its own spec, in [dependencies]
    assert px["dependencies"]["numpy"] == ">=1.20,<2"
    assert px["dependencies"]["zarr"] == ">=2.18,2.*"  # ~= in conda syntax
    # pypi: table spec keeps its extras
    assert px["pypi-dependencies"]["requests"] == {
        "version": ">=2,~=2.31", "extras": ["socks"]}
    assert px["pypi-dependencies"]["scipy"] == "<1.14"
    # the release itself is never pinned here
    assert px["pypi-dependencies"]["my-tasks"] == {"path": ".", "editable": True}
    assert len(applied) == 4
    assert px["tasks"] == tomllib.loads(PYPROJECT_B)["tool"]["pixi"]["tasks"]


def test_pin_on_path_dependency_raises(tmp_path):
    p = tmp_path / "pixi.toml"
    text = ('[workspace]\nname = "x"\nchannels = []\nplatforms = []\n\n'
            '[pypi-dependencies]\nfoo = { path = "../foo" }\n')
    p.write_text(text)
    with pytest.raises(RuntimeError, match="from a path"):
        pixi_config.write_pins_to_pixi_config(p, ["foo<2"], {}, "x")
    assert p.read_text() == text
