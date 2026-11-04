"""
Registry of the tasks of installed Fractal task packages.

Each task is registered under a unique key,
``"<task name> [<package>==<version>]"``, with what is needed to show its
form and run it: its argument schemas, its executables and where its run
files go (``tasks/<package>/<version>/<task>/`` under ``paths.ROOT_DIR``).

No Qt here: the plugin's widget registers a package's tasks once it is
installed and lists the keys in its task picker.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .manifests import task_kind
from .paths import task_run_dir
from .runner import RUNNABLE_KINDS

# --------------------------------------------------------------------------
# Task manager
# --------------------------------------------------------------------------


class FractalTaskManager:
    """Registry of installed tasks: their schemas, executables and run
    files."""

    def __init__(self):
        self.tasks = dict()

    def add_task(
        self,
        name,
        package_name,
        parent_dir,
        executable_parallel,
        properties,
        defs,
        required,
        type,
        title,
        base_name=None,
        version=None,
        docs_info=None,
        docs_link=None,
        kind='parallel',
        executable_non_parallel=None,
        schema_parallel=None,
        schema_non_parallel=None,
    ):
        # `name` is the unique key (e.g. "Cellpose Segmentation
        # [fractal-tasks-core==1.4.0]"); `base_name` is the manifest name.

        task_dict = dict(
            title=title,
            base_name=base_name or name,
            version=version,
            docs_info=docs_info,
            docs_link=docs_link,
            # parallel | non_parallel | compound (see runner)
            kind=kind,
            executable_non_parallel=executable_non_parallel,
            schema_parallel=schema_parallel,
            schema_non_parallel=schema_non_parallel,
            package_name=package_name,
            parent_dir=parent_dir,
            executable_parallel=executable_parallel,
            properties=properties,
            defs=defs,
            required=required,
            type=type,
        )
        self.tasks[name] = task_dict

    def get_executable_path(
        self,
        name,
        which='parallel',
    ):
        parent_dir = self.tasks[name]['parent_dir']
        exec_fname = self.tasks[name][f'executable_{which}']
        if not exec_fname:
            return None
        return os.path.join(parent_dir, exec_fname)

    def get_package_name(
        self,
        name
    ):

        return self.tasks[name]['package_name']

    def get_run_dir(self, name):
        """tasks/<package>/<version>/<task>/: the task's run files."""
        task = self.tasks[name]
        pkg, _, pkg_version = task['package_name'].partition('==')
        version = task.get('version') or pkg_version or 'unversioned'
        return task_run_dir(pkg, version, task['base_name'])

    def get_path_to_json(self, name):
        """Arguments JSON passed to the task (--args-json)."""
        return str(self.get_run_dir(name) / f"{self.tasks[name]['title']}.json")

    def get_out_json_path(self, name):
        """Output JSON the task writes (--out-json)."""
        return str(self.get_run_dir(name) / 'out_file.json')

    def write_args(self, name, args):
        """Write the task's arguments JSON and remove the output JSON of an
        earlier run; returns both paths (--args-json, --out-json)."""
        self.get_run_dir(name).mkdir(parents=True, exist_ok=True)
        args_json = self.get_path_to_json(name)
        with open(args_json, 'w') as f:
            json.dump(args, f, indent=1)
        out_json = self.get_out_json_path(name)
        if os.path.exists(out_json):
            os.remove(out_json)
        return args_json, out_json

    def get_task(
        self,
        name
    ):
        return self.tasks[name]

    def get_properties(
        self,
        name
    ):
        return self.tasks[name]['properties']

    def get_schema(self, name):
        """The task's argument schema, as one JSON Schema object."""
        task = self.tasks[name]
        return {
            "type": "object",
            "properties": task["properties"],
            "required": task.get("required") or [],
            "$defs": task.get("defs") or {},
        }


# --------------------------------------------------------------------------
# Registering the tasks of an installed package
# --------------------------------------------------------------------------


def load_manifest(entry: dict, fallback: dict) -> dict:
    """Manifest of an installed package (catalog install ``entry``); the
    wheel's manifest ``fallback`` if it cannot be read."""
    try:
        with open(entry['manifest_path']) as f:
            return json.load(f)
    except OSError:
        return fallback


@dataclass
class RegisteredTasks:
    """What :func:`register_manifest_tasks` did with a manifest."""

    # manifest task name -> task key, for every runnable task
    keys: dict[str, str] = field(default_factory=dict)
    # keys that were not registered before
    added: list[str] = field(default_factory=list)
    # names of the tasks that do not run on existing images (converters)
    skipped: list[str] = field(default_factory=list)


def register_manifest_tasks(
    task_manager: FractalTaskManager,
    manifest: dict,
    package_key: str,
    version: str,
    src_path: str,
) -> RegisteredTasks:
    """Register the runnable tasks of an installed package's manifest.

    ``package_key`` is ``"<package>==<version>"``; ``src_path`` the folder
    holding the package's executables.
    """
    registered = RegisteredTasks()
    for task in manifest["task_list"]:
        kind = task_kind(task)
        if kind not in RUNNABLE_KINDS:
            registered.skipped.append(task["name"])
            continue
        key = f"{task['name']} [{package_key}]"
        if key not in task_manager.tasks:
            schema_p = task.get("args_schema_parallel")
            schema_np = task.get("args_schema_non_parallel")
            # The form shown first: the per-image arguments, except for
            # non-parallel tasks
            schema = schema_np if kind == "non_parallel" else schema_p
            task_manager.add_task(
                name=key,
                base_name=task["name"],
                version=version,
                package_name=package_key,
                parent_dir=src_path,
                executable_parallel=task.get("executable_parallel"),
                executable_non_parallel=task.get("executable_non_parallel"),
                properties=schema.get("properties", {}),
                defs=schema.get("$defs", None),
                required=schema.get("required", []),
                type=schema.get("type"),
                title=schema.get("title", task["name"]),
                docs_info=task.get("docs_info"),
                docs_link=task.get("docs_link"),
                kind=kind,
                schema_parallel=schema_p,
                schema_non_parallel=schema_np,
            )
            registered.added.append(key)
        registered.keys[task["name"]] = key
    return registered


# --------------------------------------------------------------------------
# Task docs
# --------------------------------------------------------------------------


def docs_summary(docs: str) -> str:
    """The first lines of a task's ``docs_info`` as one short plain-text
    paragraph (headings and Markdown emphasis removed)."""
    paragraphs = [p.strip() for p in docs.split('\n\n')]
    lines = [ln for ln in '\n'.join(paragraphs).splitlines()
             if ln.strip() and not ln.lstrip().startswith('#')]
    first = ' '.join(
        re.sub(r'^\s*[-*]\s+', '', ln) for ln in lines[:3]
    ).strip()
    first = re.sub(r'(\*\*|__|`)', '', first)
    return first[:300] + ('…' if len(first) > 300 else '')
