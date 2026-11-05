"""
Fill a task's arguments for the user, as long as they have not typed their
own value: the channel fields follow the input layer's channel
(``ChannelAutofill``), and arguments whose default names other arguments
follow those (``PlaceholderAutofill``).
"""

import re

from workflow_tasks_core.outputs import CHANNEL_PLACEHOLDER, LABEL_NAME_ARGS
from workflow_tasks_core.schema import SchemaError

from ._schema_form import (
    ListField,
    ObjectField,
    OptionalField,
    StringField,
    UnionField,
)

# "{name}" in a default
PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Fields that take channel names: offer the input image's channels
CHANNEL_FIELDS = ('identifiers', 'label', 'channel_label', 'channel_labels')


def input_channel(layer_name, channels):
    """The channel an input image layer shows, or None.

    napari-ome-zarr names image layers after their channel label; layers of
    task outputs get a " [<image>]" suffix. Without channel metadata, the
    layer name is used as is."""
    if not layer_name:
        return None
    if not channels:
        return layer_name
    base = re.sub(r' \[[^\]]*\]$', '', layer_name)
    for name in (layer_name, base):
        if name in channels:
            return name
    return None


def _unedited(field, last_filled):
    """True while the user has not typed their own value: the text field is
    empty, at its default or holds ``last_filled``."""
    text = field.edit.text().strip()
    default = field.schema.get('default')
    if isinstance(default, list):
        default = ', '.join(map(str, default))
    auto = {'', last_filled}
    if isinstance(default, str):
        auto.add(default)
    return text in auto


class ChannelAutofill:
    """Keep a task form's channel identifiers and output label name in step
    with the selected input channel.

    Filled, outside optional nested models: ``identifiers`` lists that need
    a value (required, or with ``minItems``), a single ``identifier`` string
    of a channel model (e.g. ``channel.identifier``; the ``identifier`` of
    other models, e.g. masking, names a label or table), and ``label_name`` /
    ``output_label_name`` when their default uses ``{channel_identifier}`` or
    they have no default ("<channel>_segmented"). The label name uses the
    first identifier. A field is only overwritten while it is empty, at its
    default, or still holds the value last filled in, so edits are kept."""

    def __init__(self, fields):
        self._identifiers = []   # ListFields (several) / StringFields (one)
        self._labels = []        # (StringField, template)
        self._filled = {}        # id(field) -> text last filled in
        self._channel = None
        self._updating = False
        self._collect(fields)
        for field in self._identifiers:
            field.changed.connect(self._fill_labels)

    def _collect(self, fields, parent=''):
        for key, field in fields.items():
            inner = getattr(field, 'form', None)
            if inner is not None:
                if not getattr(field, 'optional', False):
                    self._collect(inner.fields, parent=key)
            elif (
                key == 'identifiers' and isinstance(field, ListField)
                and (field.required or field.schema.get('minItems'))
            ) or (
                key == 'identifier' and isinstance(field, StringField)
                and field.required and 'channel' in parent.lower()
            ):
                self._identifiers.append(field)
            elif key in LABEL_NAME_ARGS and isinstance(field, StringField):
                default = field.schema.get('default')
                if default is None:
                    template = f'{CHANNEL_PLACEHOLDER}_segmented'
                elif isinstance(default, str) and CHANNEL_PLACEHOLDER in default:
                    template = default
                else:
                    continue  # fixed default name: leave it
                self._labels.append((field, template))

    def _is_auto(self, field):
        """True while the user has not typed their own value."""
        return _unedited(field, self._filled.get(id(field)))

    def _fill(self, field, value):
        if not self._is_auto(field):
            return
        self._updating = True
        try:
            field.set_value(value)
        finally:
            self._updating = False
        self._filled[id(field)] = field.edit.text().strip()

    def _label_channel(self):
        for field in self._identifiers:
            try:
                ids = field.value()
            except SchemaError:
                continue
            if isinstance(ids, list) and ids:
                return str(ids[0])
            if isinstance(ids, str) and ids:
                return ids
        return self._channel

    def _fill_labels(self, *_):
        if self._updating:
            return
        channel = self._label_channel()
        if not channel:
            return
        for field, template in self._labels:
            self._fill(field, template.replace(CHANNEL_PLACEHOLDER, channel))

    def set_channel(self, channel):
        """Fill the fields for ``channel``; None leaves them as they are."""
        if not channel:
            return
        self._channel = channel
        for field in self._identifiers:
            self._fill(field, [channel] if isinstance(field, ListField) else channel)
        self._fill_labels()


def _text_fields(fields):
    """(key, field) of every text field among ``fields``, also inside
    argument models."""
    for key, field in fields.items():
        if isinstance(field, StringField):
            yield key, field
        elif isinstance(field, OptionalField):
            yield from _text_fields({key: field.inner})
        elif isinstance(field, ObjectField):
            yield from _text_fields(field.form.fields)
        elif isinstance(field, UnionField):
            for variant in field.fields:
                yield from _text_fields({key: variant})


def _text_value(field):
    """The value of a field as text; None if it has none, or if it is a
    template itself that is not filled in yet."""
    try:
        value = field.value()
    except SchemaError:
        return None
    if not isinstance(value, (str, int, float)) or str(value) == '':
        return None
    if PLACEHOLDER.search(str(value)):
        return None
    return str(value)


class PlaceholderAutofill:
    """Fill the text fields whose default names other arguments of the task,
    also inside argument models: e.g. Find Registration Consensus's
    ``registered_roi_table = "{input_roi_table}_registered"`` follows
    ``input_roi_table``. A field is only overwritten while it is empty, at
    its default, or still holds the value last filled in.

    ``fields``: the arguments of the task (the top-level fields of its
    forms); placeholders that are no argument (e.g. ``{image_name}``, which
    the task fills per image) are left to the task."""

    def __init__(self, fields):
        self._arguments = fields
        self._templates = []  # (field, template, argument names)
        self._filled = {}     # id(field) -> text last filled in
        for key, field in _text_fields(fields):
            default = field.schema.get('default')
            if not isinstance(default, str):
                continue
            names = set(PLACEHOLDER.findall(default))
            if names and names <= set(fields) - {key}:
                self._templates.append((field, default, names))
        for name in {n for _field, _template, names in self._templates
                     for n in names}:
            fields[name].changed.connect(self.fill)
        self.fill()

    def fill(self, *_):
        for field, template, names in self._templates:
            values = {name: _text_value(self._arguments[name]) for name in names}
            if None in values.values():
                continue
            if _unedited(field, self._filled.get(id(field))):
                text = template.format(**values)
                field.set_value(text)
                self._filled[id(field)] = text
