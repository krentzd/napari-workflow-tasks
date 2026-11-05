"""pixi commands must not inherit the variables that make a Python process
act as the plugin's own Python (napari's macOS relaunch sets
PYTHONEXECUTABLE, which broke installs from a package's pixi.lock with
"No module named 'hatchling'")."""

import sys

from workflow_tasks_core import install, pixi_config, runner

PRINT_IT = "import os; print(os.environ.get('PYTHONEXECUTABLE'))"


def test_pixi_env_drops_python_redirects(monkeypatch):
    for name in pixi_config.PYTHON_REDIRECT_VARS:
        monkeypatch.setenv(name, "/plugin/python")
    monkeypatch.setenv("KEEP_ME", "1")
    env = pixi_config.pixi_env({"EXTRA": "2"})
    assert not set(pixi_config.PYTHON_REDIRECT_VARS) & set(env)
    assert env["KEEP_ME"] == "1"
    assert env["EXTRA"] == "2"


def test_install_commands_do_not_inherit_it(monkeypatch):
    monkeypatch.setenv("PYTHONEXECUTABLE", "/plugin/python")
    out = install._run_streaming([sys.executable, "-c", PRINT_IT], lambda _l: None)
    assert out.strip() == "None"


def test_task_runs_do_not_inherit_it(monkeypatch):
    monkeypatch.setenv("PYTHONEXECUTABLE", "/plugin/python")
    lines = list(runner.stream_process([sys.executable, "-c", PRINT_IT]))
    assert lines == ["None"]


def test_pixi_env_drops_host_module_mpl_backend(monkeypatch):
    # Jupyter sets this; task environments have no matplotlib_inline
    monkeypatch.setenv("MPLBACKEND", "module://matplotlib_inline.backend_inline")
    assert "MPLBACKEND" not in pixi_config.pixi_env()


def test_pixi_env_keeps_builtin_mpl_backend(monkeypatch):
    monkeypatch.setenv("MPLBACKEND", "agg")
    assert pixi_config.pixi_env()["MPLBACKEND"] == "agg"
