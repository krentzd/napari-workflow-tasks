"""
Qt form generated from a Fractal task's JSON Schema (``args_schema_parallel``).

Every JSON-Schema construct used by fractal-task-tools / fractal-tasks-core
manifests gets a dedicated widget:

=============================  ============================================
Schema                         Widget
=============================  ============================================
``enum``                       drop-down
``integer`` / ``number``       spin box (respects minimum/maximum)
``boolean``                    check box
``string``                     line edit (with suggestions, e.g. channels)
``array`` of scalars           line edit, comma separated
``$ref`` / object              collapsible group with a nested form
``anyOf: [X, null]``           "set" check box enabling X (None if off)
``oneOf`` / ``anyOf`` (models)  type drop-down + the selected model's form
anything else                  JSON editor with validation
=============================  ============================================

``SchemaForm.get_args()`` returns the JSON arguments to pass to the task.
Arguments left at their default are omitted, so the task applies its own
defaults (avoids sending values that do not round-trip exactly).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QDoubleValidator, QIntValidator
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# Arguments injected by the launcher (Fractal "reserved" arguments)
RESERVED_ARGS = {"zarr_url", "zarr_urls", "zarr_dir", "init_args"}

_NO_VALUE = object()  # field has no value: omit the argument


class SchemaError(ValueError):
    """Invalid value in a field; ``.path`` names the field."""

    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path


# --------------------------------------------------------------------------
# Schema helpers
# --------------------------------------------------------------------------


def resolve(schema: dict, defs: dict) -> dict:
    """Follow ``$ref`` (and single-item ``allOf``), keeping local keys such
    as title/description/default from the referencing schema."""
    schema = dict(schema)
    seen = set()
    while True:
        if "$ref" in schema:
            ref = schema.pop("$ref")
            if ref in seen:
                break
            seen.add(ref)
            target = defs.get(ref.split("/")[-1], {})
            schema = {**target, **schema}
        elif "allOf" in schema and len(schema["allOf"]) == 1:
            sub = schema.pop("allOf")[0]
            schema = {**sub, **schema}
        else:
            break
    return schema


def _variants(schema: dict) -> list[dict]:
    return schema.get("oneOf") or schema.get("anyOf") or []


def _is_null(schema: dict) -> bool:
    return schema.get("type") == "null"


def _type(schema: dict) -> Optional[str]:
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        return non_null[0] if len(non_null) == 1 else None
    return t


def _short(text: str, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------
# Field widgets
# --------------------------------------------------------------------------


class Field(QWidget):
    """Base class: one value of the schema."""

    changed = Signal()

    def __init__(self, schema: dict, path: str, required: bool, parent=None):
        super().__init__(parent)
        self.schema = schema
        self.path = path
        self.required = required

    # value returned to the task, or _NO_VALUE
    def value(self) -> Any:
        raise NotImplementedError

    def set_value(self, value: Any) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        if "default" in self.schema:
            self.set_value(copy.deepcopy(self.schema["default"]))

    def validate(self) -> list[SchemaError]:
        try:
            v = self.value()
        except SchemaError as e:
            self._mark(True)
            return [e]
        missing = self.required and v is _NO_VALUE
        self._mark(missing)
        return [SchemaError(self.path, "required")] if missing else []

    def _mark(self, invalid: bool) -> None:
        self.setProperty("invalid", invalid)
        self.setStyleSheet(
            "QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox"
            " { border: 1px solid #e05252; }" if invalid else ""
        )


class _NoWheelMixin:
    """Don't change values when scrolling the form with the mouse wheel."""

    def wheelEvent(self, event):  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _SpinBox(_NoWheelMixin, QSpinBox):
    pass


class _DoubleSpinBox(_NoWheelMixin, QDoubleSpinBox):
    pass


class _ComboBox(_NoWheelMixin, QComboBox):
    pass


class EnumField(Field):
    def __init__(self, schema, path, required, parent=None):
        super().__init__(schema, path, required, parent)
        self.combo = _ComboBox()
        self.combo.setFocusPolicy(Qt.StrongFocus)
        for v in schema["enum"]:
            self.combo.addItem(str(v), v)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo)
        self.combo.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.reset()

    def value(self):
        return self.combo.currentData()

    def set_value(self, value):
        idx = self.combo.findData(value)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)


class ConstField(Field):
    """Discriminator / literal values: shown as text, always sent."""

    def __init__(self, schema, path, required, parent=None):
        super().__init__(schema, path, required, parent)
        self._value = schema["const"] if "const" in schema else schema["enum"][0]
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(str(self._value)))

    def value(self):
        return self._value

    def set_value(self, value):
        pass


class BoolField(Field):
    def __init__(self, schema, path, required, parent=None):
        super().__init__(schema, path, required, parent)
        self.check = QCheckBox()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.check)
        lay.addStretch(1)
        self.check.toggled.connect(lambda *_: self.changed.emit())
        self.reset()

    def value(self):
        return self.check.isChecked()

    def set_value(self, value):
        self.check.setChecked(bool(value))


def _decimals_for(value) -> int:
    text = repr(float(value))
    if "e" in text:
        return 6
    frac = text.split(".")[1].rstrip("0")
    return min(6, max(2, len(frac)))


class NumberField(Field):
    """Spin box when a default exists, else a validated line edit (empty =
    not set)."""

    def __init__(self, schema, path, required, parent=None):
        super().__init__(schema, path, required, parent)
        self.is_int = _type(schema) == "integer"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        lo = schema.get("minimum", schema.get("exclusiveMinimum"))
        hi = schema.get("maximum", schema.get("exclusiveMaximum"))
        if self.is_int:
            if "exclusiveMinimum" in schema and lo is not None:
                lo = lo + 1
            if "exclusiveMaximum" in schema and hi is not None:
                hi = hi - 1
        default = schema.get("default")
        self.use_spin = isinstance(default, (int, float)) and not isinstance(
            default, bool
        )

        if self.use_spin:
            if self.is_int:
                self.spin = _SpinBox()
                self.spin.setRange(
                    int(lo) if lo is not None else -(2**31 - 1),
                    int(hi) if hi is not None else 2**31 - 1,
                )
            else:
                self.spin = _DoubleSpinBox()
                self.spin.setDecimals(_decimals_for(default))
                self.spin.setRange(
                    float(lo) if lo is not None else -1e12,
                    float(hi) if hi is not None else 1e12,
                )
                self.spin.setSingleStep(
                    10 ** -min(self.spin.decimals(), 2) if abs(default) < 10 else 1
                )
            self.spin.setFocusPolicy(Qt.StrongFocus)
            self.spin.valueChanged.connect(lambda *_: self.changed.emit())
            lay.addWidget(self.spin)
        else:
            self.edit = QLineEdit()
            self.edit.setValidator(
                QIntValidator() if self.is_int else QDoubleValidator()
            )
            self.edit.setPlaceholderText("integer" if self.is_int else "number")
            self.edit.textChanged.connect(lambda *_: self.changed.emit())
            lay.addWidget(self.edit)
        self.reset()

    def value(self):
        if self.use_spin:
            v = self.spin.value()
            return int(v) if self.is_int else float(v)
        text = self.edit.text().strip()
        if not text:
            return _NO_VALUE
        try:
            return int(text) if self.is_int else float(text)
        except ValueError as e:
            raise SchemaError(self.path, f"not a number: {text!r}") from e

    def set_value(self, value):
        if value is None:
            return
        if self.use_spin:
            self.spin.setValue(value)
        else:
            self.edit.setText(str(value))


class StringField(Field):
    def __init__(self, schema, path, required, parent=None, suggestions=None):
        super().__init__(schema, path, required, parent)
        self.edit = QLineEdit()
        if suggestions:
            completer = QCompleter(list(suggestions))
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.edit.setCompleter(completer)
        if "default" not in schema and not required:
            self.edit.setPlaceholderText("(not set)")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit)
        self.edit.textChanged.connect(lambda *_: self.changed.emit())
        self.reset()

    def value(self):
        text = self.edit.text()
        if text == "" and "default" not in self.schema:
            return _NO_VALUE
        return text

    def set_value(self, value):
        self.edit.setText("" if value is None else str(value))


class ListField(Field):
    """Array of scalars as a comma-separated line edit."""

    def __init__(self, schema, path, required, defs, parent=None, suggestions=None):
        super().__init__(schema, path, required, parent)
        self.item_type = _type(resolve(schema.get("items", {}), defs)) or "string"
        self.edit = QLineEdit()
        hint = "comma separated"
        if suggestions:
            hint += f", e.g. {', '.join(list(suggestions)[:3])}"
            completer = QCompleter(list(suggestions))
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.edit.setCompleter(completer)
        self.edit.setPlaceholderText(hint)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit)
        self.edit.textChanged.connect(lambda *_: self.changed.emit())
        self.reset()

    def _convert(self, text):
        if self.item_type == "integer":
            return int(text)
        if self.item_type == "number":
            return float(text)
        if self.item_type == "boolean":
            return text.lower() in ("1", "true", "yes")
        return text

    def value(self):
        text = self.edit.text().strip()
        if not text:
            return [] if "default" in self.schema else _NO_VALUE
        try:
            items = [self._convert(x.strip()) for x in text.split(",") if x.strip()]
        except ValueError as e:
            raise SchemaError(self.path, f"invalid list item ({e})") from e
        lo, hi = self.schema.get("minItems"), self.schema.get("maxItems")
        if lo is not None and len(items) < lo:
            raise SchemaError(self.path, f"needs at least {lo} item(s)")
        if hi is not None and len(items) > hi:
            raise SchemaError(self.path, f"at most {hi} item(s)")
        return items

    def set_value(self, value):
        if isinstance(value, list):
            self.edit.setText(", ".join(str(v) for v in value))


class JsonField(Field):
    """Fallback editor for anything else (e.g. lists of models)."""

    def __init__(self, schema, path, required, parent=None):
        super().__init__(schema, path, required, parent)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("JSON, e.g. [] or {}")
        self.edit.setToolTip("Enter a JSON value")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit)
        self.edit.textChanged.connect(lambda *_: self.changed.emit())
        self.reset()

    def value(self):
        text = self.edit.text().strip()
        if not text:
            return _NO_VALUE
        try:
            return json.loads(text)
        except ValueError as e:
            raise SchemaError(self.path, f"invalid JSON ({e.args[0]})") from e

    def set_value(self, value):
        self.edit.setText(json.dumps(value))


class _RowsField(Field):
    """Base for editable collections: rows with a remove button + "Add"."""

    add_text = "+ Add"

    def __init__(self, schema, path, required, defs, parent=None, suggestions=None):
        super().__init__(schema, path, required, parent)
        self.defs = defs
        self.suggestions = suggestions
        self.rows: list[tuple[QWidget, Any]] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(4)
        lay.addLayout(self.rows_layout)
        self.empty_label = QLabel("(empty)")
        self.empty_label.setStyleSheet("color: gray;")
        lay.addWidget(self.empty_label)
        add = QToolButton()
        add.setText(self.add_text)
        add.clicked.connect(lambda: (self.add_row(), self.changed.emit()))
        lay.addWidget(add, alignment=Qt.AlignLeft)
        self.reset()

    def _make_row_content(self, value) -> tuple[QWidget, Any]:
        raise NotImplementedError

    def add_row(self, value=_NO_VALUE):
        content, handle = self._make_row_content(value)
        row = QFrame()
        row.setFrameShape(QFrame.StyledPanel)
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(4, 4, 4, 4)
        row_lay.addWidget(content, 1)
        remove = QToolButton()
        remove.setText("✕")
        remove.setToolTip("Remove")
        row_lay.addWidget(remove, alignment=Qt.AlignTop)
        entry = (row, handle)
        remove.clicked.connect(lambda: (self._remove(entry), self.changed.emit()))
        self.rows.append(entry)
        self.rows_layout.addWidget(row)
        self.empty_label.setVisible(False)

    def _remove(self, entry):
        if entry in self.rows:
            self.rows.remove(entry)
            entry[0].setParent(None)
            entry[0].deleteLater()
        self.empty_label.setVisible(not self.rows)

    def clear(self):
        for entry in list(self.rows):
            self._remove(entry)

    def reset(self):
        self.clear()
        self.set_value(copy.deepcopy(self.schema.get("default")))


class ModelListField(_RowsField):
    """Array of models / unions, e.g. a list of pre-processing steps."""

    add_text = "+ Add item"

    def _make_row_content(self, value):
        field = build_field(
            self.schema.get("items", {}), self.defs, self.path, True,
            suggestions=self.suggestions,
        )
        if value is not _NO_VALUE:
            field.set_value(value)
        field.changed.connect(self.changed.emit)
        return field, field

    def value(self):
        items = [f.value() for _row, f in self.rows]
        items = [v for v in items if v is not _NO_VALUE]
        if not items and "default" not in self.schema and not self.required:
            return _NO_VALUE
        lo, hi = self.schema.get("minItems"), self.schema.get("maxItems")
        if lo is not None and len(items) < lo:
            raise SchemaError(self.path, f"needs at least {lo} item(s)")
        if hi is not None and len(items) > hi:
            raise SchemaError(self.path, f"at most {hi} item(s)")
        return items

    def set_value(self, value):
        self.clear()
        for v in value or []:
            self.add_row(v)

    def validate(self):
        errors = []
        for _row, f in self.rows:
            errors += f.validate()
        if errors:
            return errors
        return super().validate()


class MapField(_RowsField):
    """``additionalProperties`` objects: key -> value table."""

    add_text = "+ Add entry"

    def _make_row_content(self, value):
        key, val = value if value is not _NO_VALUE else ("", _NO_VALUE)
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        key_edit = QLineEdit(str(key))
        key_edit.setPlaceholderText("key")
        field = build_field(
            self.schema.get("additionalProperties", {}), self.defs,
            self.path, True, suggestions=self.suggestions,
        )
        if val is not _NO_VALUE:
            field.set_value(val)
        key_edit.textChanged.connect(lambda *_: self.changed.emit())
        field.changed.connect(self.changed.emit)
        lay.addWidget(key_edit, 1)
        lay.addWidget(QLabel("→"))
        lay.addWidget(field, 2)
        return w, (key_edit, field)

    def value(self):
        out = {}
        for _row, (key_edit, field) in self.rows:
            key = key_edit.text().strip()
            v = field.value()
            if key and v is not _NO_VALUE:
                out[key] = v
        if not out and "default" not in self.schema and not self.required:
            return _NO_VALUE
        return out

    def set_value(self, value):
        self.clear()
        for k, v in (value or {}).items():
            self.add_row((k, v))

    def validate(self):
        errors = []
        for _row, (_key, f) in self.rows:
            errors += f.validate()
        return errors or super().validate()


class ObjectField(Field):
    """Nested model: a sub-form."""

    def __init__(self, schema, path, required, defs, parent=None, suggestions=None):
        super().__init__(schema, path, required, parent)
        self.form = _FormBody(
            schema.get("properties", {}),
            set(schema.get("required", [])),
            defs,
            path,
            suggestions=suggestions,
        )
        self.form.changed.connect(self.changed.emit)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.form)
        self.reset()

    def value(self):
        return self.form.values(omit_defaults=False)

    def set_value(self, value):
        if isinstance(value, dict):
            self.form.set_values(value)

    def reset(self):
        self.form.reset()
        super().reset()

    def validate(self):
        return self.form.validate()


class OptionalField(Field):
    """``anyOf: [X, null]``: unchecked = None (null)."""

    def __init__(self, schema, inner: Field, path, parent=None):
        super().__init__(schema, path, False, parent)
        self.inner = inner
        self.toggle = QCheckBox("set")
        self.toggle.setToolTip("Unchecked: not set (None)")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.toggle)
        lay.addWidget(inner)
        self.toggle.toggled.connect(self._sync)
        inner.changed.connect(self.changed.emit)
        self.reset()

    def _sync(self, *_):
        self.inner.setVisible(self.toggle.isChecked())
        self.changed.emit()

    def value(self):
        if not self.toggle.isChecked():
            return None
        return self.inner.value()

    def set_value(self, value):
        self.toggle.setChecked(value is not None)
        if value is not None:
            self.inner.set_value(value)
        self._sync()

    def reset(self):
        self.inner.reset()
        self.set_value(self.schema.get("default"))

    def validate(self):
        if not self.toggle.isChecked():
            return []
        return self.inner.validate()


class UnionField(Field):
    """``oneOf``/``anyOf`` of several models: pick one, then fill its form."""

    def __init__(self, schema, path, required, defs, parent=None, suggestions=None):
        super().__init__(schema, path, required, parent)
        disc = (schema.get("discriminator") or {}).get("propertyName")
        mapping = (schema.get("discriminator") or {}).get("mapping", {})
        ref_to_label = {v: k for k, v in mapping.items()}

        self.combo = _ComboBox()
        self.combo.setFocusPolicy(Qt.StrongFocus)
        self.fields: list[Field] = []
        self._labels: list[Any] = []
        for i, variant in enumerate(_variants(schema)):
            sub = resolve(variant, defs)
            label = ref_to_label.get(variant.get("$ref"))
            if label is None and disc:
                p = sub.get("properties", {}).get(disc, {})
                label = p.get("const", (p.get("enum") or [None])[0])
            label = label or sub.get("title") or f"Option {i + 1}"
            self._labels.append(label)
            field = build_field(sub, defs, f"{path}", True, suggestions=suggestions)
            self.fields.append(field)
            self.combo.addItem(str(label))
            field.changed.connect(self.changed.emit)
        self.disc = disc

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo)
        for f in self.fields:
            lay.addWidget(f)
        self.combo.currentIndexChanged.connect(self._switch)
        self.reset()

    def _switch(self, idx):
        # Show only the selected model's form (a QStackedWidget would reserve
        # the height of the largest one)
        for i, f in enumerate(self.fields):
            f.setVisible(i == idx)
        self.changed.emit()

    def value(self):
        return self.fields[self.combo.currentIndex()].value()

    def set_value(self, value):
        idx = 0
        if isinstance(value, dict) and self.disc in value:
            if value[self.disc] in self._labels:
                idx = self._labels.index(value[self.disc])
        self.combo.setCurrentIndex(idx)
        self._switch(idx)
        if value is not None:
            self.fields[idx].set_value(value)

    def reset(self):
        for f in self.fields:
            f.reset()
        self.set_value(self.schema.get("default"))

    def validate(self):
        return self.fields[self.combo.currentIndex()].validate()


def build_field(
    schema: dict,
    defs: dict,
    path: str,
    required: bool,
    suggestions: Optional[dict] = None,
) -> Field:
    """Pick the widget for a (sub)schema."""
    schema = resolve(schema, defs)
    key = path.split(".")[-1]
    hints = (suggestions or {}).get(key)

    variants = _variants(schema)
    if variants:
        non_null = [v for v in variants if not _is_null(v)]
        if len(non_null) < len(variants) and len(non_null) == 1:
            # Optional[X]
            inner_schema = {
                **resolve(non_null[0], defs),
                **{k: v for k, v in schema.items() if k in ("title", "description")},
            }
            if schema.get("default") is not None:
                inner_schema["default"] = schema["default"]
            inner = build_field(inner_schema, defs, path, True, suggestions)
            # Simple optional scalars: an empty field already means "not set"
            if isinstance(inner, (StringField, ListField, NumberField)) and not getattr(
                inner, "use_spin", False
            ):
                inner.required = False
                inner.schema = {k: v for k, v in inner.schema.items() if k != "default"}
                if schema.get("default") is None:
                    inner.set_value(None)
                return inner
            return OptionalField(schema, inner, path)
        if all("$ref" in v or resolve(v, defs).get("properties") for v in non_null):
            return UnionField(schema, path, required, defs, suggestions=suggestions)
        return JsonField(schema, path, required)

    if "const" in schema or ("enum" in schema and len(schema["enum"]) == 1):
        return ConstField(schema, path, required)
    if "enum" in schema:
        return EnumField(schema, path, required)

    t = _type(schema)
    if t == "boolean":
        return BoolField(schema, path, required)
    if t in ("integer", "number"):
        return NumberField(schema, path, required)
    if t == "string":
        return StringField(schema, path, required, suggestions=hints)
    if t == "array":
        item = resolve(schema.get("items", {}), defs)
        if _type(item) in ("string", "integer", "number", "boolean") and not _variants(item):
            return ListField(schema, path, required, defs, suggestions=hints)
        if item.get("properties") or _variants(item):
            return ModelListField(schema, path, required, defs, suggestions=suggestions)
        return JsonField(schema, path, required)
    if t == "object" or "properties" in schema:
        if schema.get("properties"):
            return ObjectField(schema, path, required, defs, suggestions=suggestions)
        if isinstance(schema.get("additionalProperties"), dict) and schema["additionalProperties"]:
            return MapField(schema, path, required, defs, suggestions=suggestions)
        return JsonField(schema, path, required)
    return JsonField(schema, path, required)


# --------------------------------------------------------------------------
# Forms
# --------------------------------------------------------------------------


class _Collapsible(QWidget):
    def __init__(self, title: str, content: QWidget, expanded: bool, tooltip: str = ""):
        super().__init__()
        self.button = QToolButton()
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setChecked(expanded)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.button.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.button.setToolTip(tooltip)
        self.content = content
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(8, 4, 4, 4)
        inner.addWidget(content)
        self.frame = frame
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.button)
        lay.addWidget(frame)
        frame.setVisible(expanded)
        self.button.toggled.connect(self._toggle)

    def _toggle(self, checked):
        self.button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.frame.setVisible(checked)


def _is_group(field: Field) -> bool:
    if isinstance(field, OptionalField):
        return _is_group(field.inner)
    return isinstance(field, (ObjectField, UnionField, ModelListField, MapField))


class _FormBody(QWidget):
    changed = Signal()

    def __init__(self, properties, required, defs, prefix="", skip=(), suggestions=None):
        super().__init__()
        self.fields: dict[str, Field] = {}
        self.schemas: dict[str, dict] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        simple = QFormLayout()
        simple.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        simple.setLabelAlignment(Qt.AlignLeft)
        lay.addLayout(simple)
        groups = []

        for key, prop in properties.items():
            if key in skip:
                continue
            path = f"{prefix}.{key}" if prefix else key
            is_required = key in required
            field = build_field(prop, defs, path, is_required, suggestions)
            self.fields[key] = field
            self.schemas[key] = resolve(prop, defs)
            field.changed.connect(self.changed.emit)

            title = prop.get("title") or self.schemas[key].get("title") or key
            description = prop.get("description") or self.schemas[key].get("description", "")
            label_text = title + (" *" if is_required else "")
            if isinstance(field, ConstField):
                # Discriminator of the selected model type: shown by the
                # type drop-down already
                continue
            if _is_group(field):
                groups.append((label_text, field, description, is_required))
            else:
                label = QLabel(label_text)
                label.setToolTip(description)
                field.setToolTip(description)
                simple.addRow(label, field)

        self._sections = {}
        for label_text, field, description, is_required in groups:
            section = _Collapsible(
                label_text, field, expanded=is_required, tooltip=description
            )
            lay.addWidget(section)
            self._sections[field] = (section, label_text)

        # Values right after building = the task's defaults (also for nested
        # models without a top-level "default"); unchanged ones are omitted
        self._pristine = self._safe_values()

        # Mark collapsed sections that differ from the defaults
        for key, field in self.fields.items():
            if field in self._sections:
                field.changed.connect(lambda k=key, f=field: self._mark_modified(k, f))

    def _mark_modified(self, key, field):
        section, label_text = self._sections[field]
        try:
            modified = field.value() != self._pristine.get(key)
        except SchemaError:
            modified = True
        section.button.setText(label_text + ("  • modified" if modified else ""))

    def _safe_values(self) -> dict:
        out = {}
        for key, field in self.fields.items():
            try:
                out[key] = field.value()
            except SchemaError:
                out[key] = _NO_VALUE
        return out

    def values(self, omit_defaults: bool) -> dict:
        out = {}
        for key, field in self.fields.items():
            v = field.value()
            if v is _NO_VALUE:
                continue
            if omit_defaults:
                if "default" in self.schemas[key] and v == self.schemas[key]["default"]:
                    continue
                if not field.required and v == self._pristine.get(key, _NO_VALUE):
                    continue
            out[key] = v
        return out

    def set_values(self, values: dict):
        for key, v in values.items():
            if key in self.fields:
                self.fields[key].set_value(v)

    def reset(self):
        for f in self.fields.values():
            f.reset()

    def validate(self) -> list[SchemaError]:
        errors = []
        for f in self.fields.values():
            errors += f.validate()
        return errors


class SchemaForm(QWidget):
    """Form for a task's ``args_schema_*``."""

    changed = Signal()

    def __init__(
        self,
        schema: dict,
        skip: tuple = (),
        suggestions: Optional[dict[str, list[str]]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.schema = schema
        defs = schema.get("$defs") or schema.get("definitions") or {}
        self.body = _FormBody(
            schema.get("properties", {}),
            set(schema.get("required", [])),
            defs,
            skip=set(skip) | RESERVED_ARGS,
            suggestions=suggestions,
        )
        self.body.changed.connect(self.changed.emit)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.body)
        lay.addStretch(1)

    @property
    def fields(self) -> dict[str, Field]:
        return self.body.fields

    def get_args(self) -> dict:
        """Task arguments; raises SchemaError if a field is invalid/missing."""
        errors = self.body.validate()
        if errors:
            raise errors[0]
        return self.body.values(omit_defaults=True)

    def validate(self) -> list[SchemaError]:
        return self.body.validate()

    def set_values(self, values: dict) -> None:
        self.body.set_values(values)

    def reset(self) -> None:
        self.body.reset()


