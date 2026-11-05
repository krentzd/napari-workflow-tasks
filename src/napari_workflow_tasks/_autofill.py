"""
Keep a task form's channel fields in step with the input channel selected in
the plugin's Main tab.
"""

import re

from workflow_tasks_core.outputs import CHANNEL_PLACEHOLDER, LABEL_NAME_ARGS
from workflow_tasks_core.schema import SchemaError

from ._schema_form import ListField, StringField

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
        text = field.edit.text().strip()
        default = field.schema.get('default')
        if isinstance(default, list):
            default = ', '.join(map(str, default))
        auto = {'', self._filled.get(id(field))}
        if isinstance(default, str):
            auto.add(default)
        return text in auto

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
