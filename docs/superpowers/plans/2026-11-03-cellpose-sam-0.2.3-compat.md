# Cellpose SAM 0.2.3 Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the plugin run `fractal_cellpose_sam_task-0.2.3` end to end with Cellpose 4.2, without breaking 0.1.0, which works today.

**Architecture:** The task form currently decides how to show and read a field only from its JSON Schema `type` key, and anything else goes through `str()`. Replace that with one schema-driven rule in a new pure module, `_schema_values.py`, used both when building widgets and when reading their values. Extract argument collection out of `_execute_task` so it can be tested without running pixi. Fix the result-reload step to resolve the new `{channel_identifier}` placeholder.

**Tech Stack:** Python 3.11, napari 0.6.6, qtpy, pytest + pytest-qt, jsonschema (tests only).

**Spec:** The user's request ("make the plugin compatible with 0.2.3"), plus the schema diff between `/Users/dakren/Documents/Projects/fractal_cellpose_sam_task-0.1.0.tar.gz` and `/Users/dakren/Documents/Projects/fractal_cellpose_sam_task-0.2.3.tar.gz`. These are the 0.2.3 failures observed in a real run:

| Field | 0.2.3 schema | Plugin sent | Task wants |
|---|---|---|---|
| `level_path`, `custom_model` | `anyOf[string, null]`, default `null` | `"None"` | omit, or a string |
| `iterator_configuration` | `anyOf[$ref IteratorConfig, null]`, default `null` | `"None"` | omit, or an object |
| `create_masking_roi_table` | `oneOf[2 $refs]`, default `{"mode": "Skip Creating Masking ROI Table"}` | `"{'mode': ...}"` (Python repr) | object |
| `pre_post_process.pre_process` / `.post_process` | array of `oneOf` objects, default `[]` | `["[]"]` | list |
| `channels.identifiers` | array of strings, **required** in 0.2.3 | `null` when left empty | 1 to 3 strings |
| `advanced_parameters.*` numbers | `type: number` | `"0.4"` (string; the code only handles `"float"`) | float |
| `label_name` (reload) | default `{channel_identifier}_segmented` | reload looks for the literal name, so no layer appears | resolved name, e.g. `DAPI_segmented` |

## Global Constraints

- Work on branch `dev`. Uncommitted user edits in `pixi.lock` and `pyproject.toml` must **never** be staged. Always `git add` explicit paths.
- `_widget.py` contains an uncommitted 2-line `PyQt5` → `qtpy` import fix (lines 11–12). Commit it on its own before Task 2, with message `fix: import Qt names via qtpy instead of PyQt5`.
- 0.1.0 must keep producing args that its task accepts (regression fixture in Task 2).
- No new runtime dependencies. `jsonschema` is used only in tests, through `pytest.importorskip`. Don't edit `pyproject.toml`.
- Test command (`$TEST`), from the repo root:
  `uv run -q --no-project --python 3.11 --with-editable . --with "napari==0.6.6" --with pyqt5 --with napari-ome-zarr --with pytest --with pytest-qt --with jsonschema pytest -q -p no:warnings`
  Run test files by name. `_tests/test_widget.py` already fails to collect (leftover template) and is out of scope.
- Errors shown to the user go through `napari.utils.notifications.show_error`, not `print`.
- Cellpose pin: `cellpose>=4.2,<4.3`, applied as a **PyPI** dependency. conda-forge has no 4.2 release. Today, 0.2.3's shipped `pixi.lock` gives 4.1.1, and 0.1.0 resolves to 4.2.1.1.
- Out of scope: the pixi "No module named 'hatchling'" build failure seen when loading 0.2.3. It's a separate issue (see the note at the end).

## Review Focus

1. A JSON field typed with Python-style single quotes (`{'mode': 'x'}`) should give an error that names the field, not a crash or a string sent to the task. Pinned in Task 1 and Task 2.
2. Clearing a field that has a non-null default (e.g. `batch_size`) should leave it out, so the task's default applies. It must not send `null`. Pinned in Task 1.
3. `DAPI,` (trailing comma) or `DAPI, nanog` in an array field should become `["DAPI"]` / `["DAPI", "nanog"]`. Pinned in Task 1.
4. An integer field typed as `4.0` or `eight` should show an error naming the field, and the task must not run. Pinned in Task 2.
5. Loading 0.1.0 after this change should still produce the args that worked today. Pinned in Task 2 (0.1.0 fixture).
6. Loading a package that ships a `pixi.lock` with another Cellpose version (0.2.3 ships 4.1.1) should still end up with 4.2.x installed. Pinned in Task 4, with a manual check.

---

### Task 1: Schema-driven value conversion

**Files:**
- Create: `src/napari_workflow_tasks/_schema_values.py`
- Test: `src/napari_workflow_tasks/_tests/test_schema_values.py`

**Interfaces:**
- Produces:
  - `MISSING: object`, a sentinel meaning "leave this argument out".
  - `field_kind(schema: dict) -> str` returns one of `"integer" | "number" | "string" | "boolean" | "array" | "json"`. An `anyOf` with exactly one non-null branch is that branch's kind. `"array"` means a list of scalars. Everything else (`$ref`, `oneOf`, lists of objects, multi-branch `anyOf`) is `"json"`.
  - `nullable_ref(schema: dict) -> str | None` returns the def name (e.g. `"IteratorConfig"`) when the schema is `{"$ref": ...}` or `anyOf[{"$ref": ...}, {"type": "null"}]`, otherwise `None`.
  - `format_value(value, schema: dict) -> str` returns `""` for `None`, `str(value)` for kind `"string"`, and `json.dumps(value)` otherwise.
  - `parse_text(text: str, schema: dict)` returns `MISSING` for empty or whitespace text. It returns `int`/`float`/`str` for scalars. For `"array"`, it splits on commas and drops empty items, unless the text starts with `[`, in which case it's parsed as JSON. Otherwise it parses JSON. It raises `ValueError("expected JSON (use double quotes), got ...")` when JSON parsing fails.

- [ ] **Step 1: Write the failing tests**

```python
NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
NULLABLE_REF = {"anyOf": [{"$ref": "#/$defs/IteratorConfig"}, {"type": "null"}], "default": None}
ONE_OF = {"oneOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}],
          "default": {"mode": "Skip Creating Masking ROI Table"}}
STR_ARRAY = {"type": "array", "items": {"type": "string"}}
OBJ_ARRAY = {"type": "array", "items": {"oneOf": [{"$ref": "#/$defs/A"}]}, "default": []}

# parametrized test_field_kind:
#   {"type":"integer"}->"integer", {"type":"number"}->"number", {"type":"boolean"}->"boolean",
#   NULLABLE_STR->"string", anyOf[number,null]->"number", NULLABLE_REF->"json",
#   ONE_OF->"json", STR_ARRAY->"array", OBJ_ARRAY->"json"
def test_nullable_ref_names_the_def():
    assert nullable_ref(NULLABLE_REF) == "IteratorConfig"
    assert nullable_ref({"$ref": "#/$defs/CellposeChannels"}) == "CellposeChannels"
    assert nullable_ref(NULLABLE_STR) is None
def test_null_default_is_shown_as_empty_not_none():
    assert format_value(None, NULLABLE_STR) == ""
def test_object_default_round_trips_as_json():
    text = format_value(ONE_OF["default"], ONE_OF)
    assert text == '{"mode": "Skip Creating Masking ROI Table"}'
    assert parse_text(text, ONE_OF) == ONE_OF["default"]
def test_empty_list_default_round_trips():
    assert parse_text(format_value([], OBJ_ARRAY), OBJ_ARRAY) == []
def test_empty_text_is_missing():
    assert parse_text("", NULLABLE_REF) is MISSING
    assert parse_text("   ", {"type": "integer", "default": 8}) is MISSING
def test_number_is_float():
    assert parse_text("0.4", {"type": "number"}) == 0.4
def test_nullable_string_keeps_digits_as_string():
    assert parse_text("1", NULLABLE_STR) == "1"
def test_string_array_from_commas():
    assert parse_text("DAPI, nanog", STR_ARRAY) == ["DAPI", "nanog"]
    assert parse_text("DAPI,", STR_ARRAY) == ["DAPI"]
def test_string_array_from_json():
    assert parse_text('["DAPI"]', STR_ARRAY) == ["DAPI"]
def test_python_style_json_raises():
    with pytest.raises(ValueError, match="JSON"):
        parse_text("{'mode': 'x'}", ONE_OF)
def test_bad_integer_raises():
    for text in ("eight", "4.0"):
        with pytest.raises(ValueError):
            parse_text(text, {"type": "integer"})
```

- [ ] **Step 2: Run** `$TEST src/napari_workflow_tasks/_tests/test_schema_values.py`. Expected: collection ERROR, `No module named 'napari_workflow_tasks._schema_values'`.
- [ ] **Step 3: Implement** the five names above in `_schema_values.py`. It's pure Python: only `json`, no Qt or napari imports.
- [ ] **Step 4: Run** `$TEST src/napari_workflow_tasks/_tests/test_schema_values.py`. Expected: all pass.
- [ ] **Step 5: Commit**

```bash
git add src/napari_workflow_tasks/_schema_values.py src/napari_workflow_tasks/_tests/test_schema_values.py
git commit -m "feat: schema-driven conversion between task args and widget text"
```

---

### Task 2: Build the form and collect args from the schema

**Files:**
- Modify: `src/napari_workflow_tasks/_widget.py`, specifically:
  - `FractalTaskManager.write_to_json` (156–173)
  - `FractalTaskManager.get_widget_value` (210–283)
  - `TasksQWidget._execute_task` (536–595)
  - `TasksQWidget._add_task_tab` (612–742)
- Create: `src/napari_workflow_tasks/_tests/data/cellpose_sam_0.1.0_args_schema.json` and `src/napari_workflow_tasks/_tests/data/cellpose_sam_0.2.3_args_schema.json`
- Test: `src/napari_workflow_tasks/_tests/test_task_form.py`

**Interfaces:**
- Consumes: Task 1 (`MISSING`, `field_kind`, `nullable_ref`, `format_value`, `parse_text`).
- Produces:
  - `class TaskArgsError(ValueError)` in `_widget.py`.
  - `TasksQWidget._collect_task_args(task_name: str) -> dict`. It returns the exact dict to be written as the task's args JSON and doesn't write files or launch anything. It raises `TaskArgsError` whose message names each bad field by its widget title, in the form `"Channels → Identifiers: required"` or `"Batch Size: invalid literal ..."`.
  - Widgets in `task_manager.get_task(name)["widget_dict"]`: a group field (`nullable_ref(...)` is not None) maps to a `dict[str, QWidget]`; other fields map to a `QLineEdit` or a `QCheckBox` (`field_kind == "boolean"`).

**Behaviour to implement:**
- **Widget choice**, for top-level properties and for properties inside a group: `boolean` gets a `QCheckBox`; a field where `nullable_ref` returns a name gets a nested group tab (one level, as today); everything else gets a `QLineEdit` with text `format_value(default, schema)`. Fields nested two levels deep (e.g. `IteratorConfig.masking`) are `json` text fields. `IGNORE_PROPERTIES` stays as it is.
- **Collection:** a `MISSING` value is left out. A group whose subfields are all `MISSING` is left out. Group values are written as plain dicts. Remove the `{"args": ..., "type": ...}` wrapper from `get_widget_value` and the matching `['args']` unwrap from `write_to_json`. `task_wrapper.py`, the only reader of `type`, isn't called anywhere.
- **Required fields:** check the top-level `required` (except `zarr_url`, which `_execute_task` sets) and each group def's `required`. Every missing one is listed in a single `TaskArgsError`.
- **`_execute_task`:** set `zarr_url` as today, call `_collect_task_args`, and on `TaskArgsError` call `show_error(f"{task_name}: {exc}")` and return before writing JSON, disabling buttons, or launching pixi. Otherwise write the dict and launch as today. Remove the dead `update_task_property(task_name, 'channel', ...)` line, because neither version has a `channel` property.

- [ ] **Step 1: Save the two schema fixtures** from the tarballs (`args_schema_parallel` of the task named `Cellpose SAM Segmentation`):

```bash
cd src/napari_workflow_tasks/_tests && mkdir -p data && for v in 0.1.0 0.2.3; do tar -xzOf /Users/dakren/Documents/Projects/fractal_cellpose_sam_task-$v.tar.gz fractal_cellpose_sam_task-$v/src/fractal_cellpose_sam_task/__FRACTAL_MANIFEST__.json | python3 -I -c "import json,sys; t=[t for t in json.load(sys.stdin)['task_list'] if t['name']=='Cellpose SAM Segmentation'][0]; json.dump(t['args_schema_parallel'], open('data/cellpose_sam_${v}_args_schema.json','w'), indent=1)"; done
```

- [ ] **Step 2: Write the failing tests** in `test_task_form.py`. Use a fixture `form(make_napari_viewer, tmp_path, monkeypatch, request)`: set `HOME` to `tmp_path`, add an image layer named `img` under `napari.layers._source.layer_source(path=str(tmp_path / "img.zarr"))`, build `TasksQWidget`, register the schema with `task_manager.add_task(name=..., package_name="pkg", parent_dir=str(tmp_path), executable_parallel="t.py", properties=s["properties"], defs=s["$defs"], required=s["required"], type=s["type"], title=s["title"])`, and call `_add_task_tab`. Set `zarr_url` with `task_manager.update_task_property(name, "zarr_url", "/data/img.zarr")` before calling `_collect_task_args`. Each test that collects valid args also asserts `jsonschema.Draft202012Validator(schema).iter_errors(args)` is empty (`jsonschema = pytest.importorskip("jsonschema")`).

```python
def test_v023_defaults_with_channel_are_accepted(form_023):
    w = widgets(form_023); w["channels"]["identifiers"].setText("DAPI")
    args = form_023._collect_task_args(TASK)
    assert args["channels"] == {"mode": "label", "identifiers": ["DAPI"], "skip_if_missing": False}
    assert "level_path" not in args and "custom_model" not in args
    assert args["pre_post_process"] == {"pre_process": [], "post_process": []}
    assert args["create_masking_roi_table"] == {"mode": "Skip Creating Masking ROI Table"}
    assert args["advanced_parameters"]["flow_threshold"] == 0.4
    assert args["advanced_parameters"]["normalization"] == {"mode": "default"}
    assert "diameter" not in args["advanced_parameters"]
    assert args["label_name"] == "{channel_identifier}_segmented"
    # + jsonschema validation passes

def test_v023_roi_table_is_sent_in_iterator_configuration(form_023):
    w = widgets(form_023); w["channels"]["identifiers"].setText("DAPI")
    w["iterator_configuration"]["roi_table"].setText("ROI selection")
    args = form_023._collect_task_args(TASK)
    assert args["iterator_configuration"]["roi_table"] == "ROI selection"
    # + jsonschema validation passes

def test_v023_missing_identifiers_is_reported(form_023):
    with pytest.raises(TaskArgsError, match="Channels → Identifiers: required"):
        form_023._collect_task_args(TASK)

def test_python_style_json_names_the_field(form_023):
    w = widgets(form_023); w["channels"]["identifiers"].setText("DAPI")
    w["create_masking_roi_table"].setText("{'mode': 'Skip Creating Masking ROI Table'}")
    with pytest.raises(TaskArgsError, match="Create Masking Roi Table"):
        form_023._collect_task_args(TASK)

def test_bad_integer_names_the_field(form_023):
    w = widgets(form_023); w["channels"]["identifiers"].setText("DAPI")
    w["advanced_parameters"]["batch_size"].setText("eight")
    with pytest.raises(TaskArgsError, match="Batch Size"):
        form_023._collect_task_args(TASK)

def test_v010_args_match_what_worked(form_010):
    w = widgets(form_010)
    w["channels"]["identifiers"].setText("DAPI")
    w["iterator_configuration"]["roi_table"].setText("ROI selection")
    w["label_name"].setText("nuclei"); w["level_path"].setText("1")
    args = form_010._collect_task_args(TASK)
    assert args["channels"] == {"mode": "label", "identifiers": ["DAPI"]}
    assert args["iterator_configuration"] == {"roi_table": "ROI selection"}
    assert args["label_name"] == "nuclei" and args["level_path"] == "1"
    assert "custom_model" not in args
    # + jsonschema validation passes

def test_execute_task_with_invalid_args_shows_error_and_does_not_launch(form_023, monkeypatch):
    errors = []; monkeypatch.setattr(_widget, "show_error", errors.append)
    launched = []; monkeypatch.setattr(_widget.subprocess, "Popen", lambda *a, **k: launched.append(a))
    form_023._execute_task(TASK)                      # identifiers empty
    assert launched == [] and "Identifiers" in errors[0]
```

- [ ] **Step 3: Run** `$TEST src/napari_workflow_tasks/_tests/test_task_form.py`. Expected: FAIL. `TaskArgsError` / `_collect_task_args` don't exist yet.
- [ ] **Step 4: Implement** the behaviour above in `_widget.py`. Make `_add_task_tab` and `get_widget_value` share a single widget factory and a single reader, both driven by `field_kind` / `nullable_ref`, instead of the current duplicated top-level and `$ref` branches.
- [ ] **Step 5: Run** `$TEST src/napari_workflow_tasks/_tests/test_schema_values.py src/napari_workflow_tasks/_tests/test_task_form.py`. Expected: all pass.
- [ ] **Step 6: Commit**

```bash
git add src/napari_workflow_tasks/_widget.py src/napari_workflow_tasks/_tests/test_task_form.py src/napari_workflow_tasks/_tests/data/
git commit -m "fix: build task form from schema so 0.2.3 args validate"
```

---

### Task 3: Show the 0.2.3 result layer after a run

**Files:**
- Modify: `src/napari_workflow_tasks/_widget.py`, `TasksQWidget._fetch_subprocess_output` (500–531)
- Test: `src/napari_workflow_tasks/_tests/test_task_form.py` (add tests)

**Interfaces:**
- Consumes: Task 2 (`_collect_task_args` output, the same dict written to JSON).
- Produces: `expected_label_name(task_name: str, args: dict) -> str | None`, a module-level function in `_widget.py`. For `Thresholding Label Task` and `Cellpose SAM Segmentation`, it returns `args["label_name"]` with `{channel_identifier}` replaced by `args["channels"]["identifiers"][0]`. For `Cellpose Segmentation`, it returns `args["output_label_name"]`. For other tasks, it returns `None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_label_placeholder_is_resolved():
    args = {"label_name": "{channel_identifier}_segmented", "channels": {"identifiers": ["DAPI", "nanog"]}}
    assert expected_label_name("Cellpose SAM Segmentation", args) == "DAPI_segmented"
def test_plain_label_name_is_unchanged():
    args = {"label_name": "nuclei", "channels": {"identifiers": ["DAPI"]}}
    assert expected_label_name("Cellpose SAM Segmentation", args) == "nuclei"
def test_unknown_task_has_no_label():
    assert expected_label_name("Some Other Task", {}) is None
```

- [ ] **Step 2: Run** `$TEST src/napari_workflow_tasks/_tests/test_task_form.py -k label`. Expected: FAIL, `ImportError: expected_label_name`.
- [ ] **Step 3: Implement** `expected_label_name`. In `_fetch_subprocess_output`, read the args from the JSON at `task_manager.get_path_to_json(task_name)` instead of `props[...]['value']`, and use `expected_label_name`. If it returns `None`, skip the reload, which matches today's behaviour for unknown tasks.
- [ ] **Step 4: Run** `$TEST src/napari_workflow_tasks/_tests/test_schema_values.py src/napari_workflow_tasks/_tests/test_task_form.py`. Expected: all pass.
- [ ] **Step 5: Manual check in napari**, done by the user:
  1. Load the 0.2.3 tarball and the `20200812-...Cycle1_mip.zarr` B/03/0 image.
  2. Set Identifiers to `DAPI` and run. Expected: no validation error, and a `DAPI_segmented` labels layer appears.
  3. Repeat with the 0.1.0 tarball and label name `nuclei`. Expected: a `nuclei` layer appears.
- [ ] **Step 6: Commit**

```bash
git add src/napari_workflow_tasks/_widget.py src/napari_workflow_tasks/_tests/test_task_form.py
git commit -m "fix: resolve {channel_identifier} when reloading task results"
```

---

### Task 4: Pin Cellpose to 4.2 in task environments

This task is independent of Tasks 1–3 and can be done first.

**Files:**
- Modify: `src/napari_workflow_tasks/io_utils.py`, specifically `PackageImporter.open_package` (35–58)
- Test: `src/napari_workflow_tasks/_tests/test_package_pins.py`

**Interfaces:**
- Produces:
  - `PINNED_PYPI_DEPENDENCIES: dict[str, str] = {"cellpose": "cellpose>=4.2,<4.3"}`, a module constant in `io_utils.py`.
  - `PackageImporter._apply_pins(toml_data: dict, manifest_path: str) -> None`. For each pinned name that appears among `toml_data["project"]["dependencies"]` (match the distribution name before any version specifier, case-insensitively), it runs `subprocess.run(["pixi", "add", "--pypi", spec, "--manifest-path", manifest_path], check=True)`.

**Behaviour:**
- Call `_apply_pins` in `open_package`, only for packages that ship their own pixi config (the `_toml_has_pixi` branch), after the manifest path is known. `pixi add` rewrites the package's `pyproject.toml` and re-solves the shipped `pixi.lock`. Because `open_package` re-extracts the tarball on every load, the pin is re-applied each time.
- Leave the non-pixi branch (`_make_pixi_env`, which runs a conda-channel `pixi add cellpose`) unchanged. conda-forge has no 4.2, and neither Cellpose SAM package uses that path. If it's needed later, it would need `--pypi` and a Python dependency in the generated `pixi.toml`.
- A failing `pixi add` raises `subprocess.CalledProcessError` from `open_package`, instead of the failure being silently ignored.

- [ ] **Step 1: Write the failing tests.** Use a fake `pixi` executable put first on `PATH` with `monkeypatch.setenv` that appends its arguments to `$tmp_path/calls.txt`. Build a tarball `my_pkg-0.1.0.tar.gz` whose `pyproject.toml` has `[project] dependencies`, `[tool.hatch.build.targets.wheel] packages = ["src/my_pkg"]` and a `[tool.pixi.workspace]` table. Load it with `PackageImporter(root_dir=str(tmp_path)).open_package(...)`.

```python
def test_cellpose_is_pinned_to_4_2(tmp_path, fake_pixi):
    importer = load(tmp_path, deps=["cellpose>=4.0.6,<5.0", "ngio>=1.1.0,<1.2"])
    manifest = importer.loaded_packages["my_pkg-0.1.0"]["pixi_manifest_path"]
    assert fake_pixi.calls() == [["add", "--pypi", "cellpose>=4.2,<4.3", "--manifest-path", manifest]]

def test_package_without_cellpose_is_not_touched(tmp_path, fake_pixi):
    load(tmp_path, deps=["ngio>=1.1.0,<1.2"])
    assert fake_pixi.calls() == []

def test_name_match_ignores_case_and_specifier(tmp_path, fake_pixi):
    load(tmp_path, deps=["Cellpose[gui]==4.0.6"])
    assert fake_pixi.calls()[0][2] == "cellpose>=4.2,<4.3"

def test_failed_pin_is_raised(tmp_path, fake_pixi_failing):
    with pytest.raises(subprocess.CalledProcessError):
        load(tmp_path, deps=["cellpose>=4.0.6,<5.0"])
```

- [ ] **Step 2: Run** `$TEST src/napari_workflow_tasks/_tests/test_package_pins.py`. Expected: FAIL, because no `pixi add --pypi` call is recorded.
- [ ] **Step 3: Implement** `PINNED_PYPI_DEPENDENCIES` and `_apply_pins`, and call `_apply_pins` from `open_package`.
- [ ] **Step 4: Run** `$TEST src/napari_workflow_tasks/_tests/test_package_pins.py`. Expected: all pass.
- [ ] **Step 5: Manual check**, done by the user: load the 0.2.3 tarball in the plugin, then run
  `ls -d ~/.napari_workflow_tasks/fractal_cellpose_sam_task-0.2.3/.pixi/envs/default/lib/python3.*/site-packages/cellpose-*.dist-info`.
  Expected: `cellpose-4.2.*.dist-info`, not `4.1.1`. Run one segmentation to confirm the task still works with 4.2.
- [ ] **Step 6: Commit**

```bash
git add src/napari_workflow_tasks/io_utils.py src/napari_workflow_tasks/_tests/test_package_pins.py
git commit -m "feat: pin cellpose to 4.2 in task pixi environments"
```

---

## Not in this plan: pixi build isolation

Loading 0.2.3 once failed with `No module named 'hatchling'` while pixi built the package. Running `pixi install` by hand in a terminal worked. The likely cause is napari's own environment variables leaking into the `pixi` subprocess. An earlier fix for this, a cleaned `env` for every pixi call, was rolled back on request. If the error shows up again on a fresh load of 0.2.3, plan it separately. Task 4 makes this more likely to come up, because its `pixi add` re-solves the lock file and rebuilds the package during the load.
