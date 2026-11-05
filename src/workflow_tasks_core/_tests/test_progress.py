"""Install progress: the step an install log line starts, elapsed time."""

import pytest

from workflow_tasks_core.progress import format_elapsed, install_step


@pytest.mark.parametrize("line, step", [
    ("Looking for pixi.lock in https://x/y.tar.gz", "Downloading"),
    ("Using the cached source archive", "Downloading"),
    ("Installing my-tasks==1.1 from its pixi.lock (exact versions)",
     "Installing dependencies"),
    ("Solving my-tasks==1.1 from its pixi config", "Installing dependencies"),
    ("Caching the environment's native libraries", "Caching"),
    ("Checking that the tasks import", "Checking"),
    ("Import check OK (3 task modules)", "Checking"),
])
def test_install_step(line, step):
    assert install_step(line).startswith(step)


def test_install_step_ignores_other_lines():
    assert install_step("  ✔ The default environment has been installed.") is None
    assert install_step("") is None


def test_format_elapsed():
    assert format_elapsed(7.9) == "7s"
    assert format_elapsed(125) == "2m 05s"
