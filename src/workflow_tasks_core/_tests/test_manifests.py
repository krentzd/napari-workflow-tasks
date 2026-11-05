"""Reading task manifests."""

from workflow_tasks_core.manifests import docs_summary


def test_docs_summary():
    docs = "# Title\n\n- **Segments** nuclei\n\nwith `cellpose`.\n\nMore."
    assert docs_summary(docs) == "Segments nuclei with cellpose. More."
    assert docs_summary("x" * 400) == "x" * 300 + "…"
