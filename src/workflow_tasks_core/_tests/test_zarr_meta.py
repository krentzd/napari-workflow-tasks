"""Label and channel names read from OME-Zarr metadata (v0.4 and v0.5)."""

import json

from workflow_tasks_core.zarr_meta import channel_labels, list_labels


def test_v04(tmp_path):
    (tmp_path / "labels").mkdir()
    (tmp_path / "labels" / ".zattrs").write_text(
        json.dumps({"labels": ["nuclei"]}))
    (tmp_path / ".zattrs").write_text(json.dumps(
        {"omero": {"channels": [{"label": "DAPI"}, {"label": "nanog"}]}}))
    assert list_labels(str(tmp_path)) == ["nuclei"]
    assert channel_labels(str(tmp_path)) == ["DAPI", "nanog"]


def test_v05(tmp_path):
    (tmp_path / "labels").mkdir()
    (tmp_path / "labels" / "zarr.json").write_text(
        json.dumps({"attributes": {"ome": {"labels": ["cells"]}}}))
    (tmp_path / "zarr.json").write_text(json.dumps({"attributes": {"ome": {
        "omero": {"channels": [{"label": "GFP"}, {}]}}}}))
    assert list_labels(str(tmp_path)) == ["cells"]
    assert channel_labels(str(tmp_path)) == ["GFP"]


def test_no_metadata(tmp_path):
    assert list_labels(str(tmp_path)) == []
    assert channel_labels(str(tmp_path)) == []
