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


def test_list_tables_v04_and_v05(tmp_path):
    from workflow_tasks_core.zarr_meta import list_tables

    v04 = tmp_path / "a"
    (v04 / "tables").mkdir(parents=True)
    (v04 / "tables" / ".zattrs").write_text(json.dumps({"tables": ["FOV_ROI_table"]}))
    v05 = tmp_path / "b"
    (v05 / "tables").mkdir(parents=True)
    (v05 / "tables" / "zarr.json").write_text(
        json.dumps({"attributes": {"tables": ["nuclei_ROI_table"]}}))
    v05_ome = tmp_path / "c"
    (v05_ome / "tables").mkdir(parents=True)
    (v05_ome / "tables" / "zarr.json").write_text(
        json.dumps({"attributes": {"ome": {"tables": ["t"]}}}))
    assert list_tables(str(v04)) == ["FOV_ROI_table"]
    assert list_tables(str(v05)) == ["nuclei_ROI_table"]
    assert list_tables(str(v05_ome)) == ["t"]
    assert list_tables(str(tmp_path / "none")) == []
