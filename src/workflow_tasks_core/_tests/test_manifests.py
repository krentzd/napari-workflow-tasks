"""Reading task manifests."""

from workflow_tasks_core.manifests import docs_summary, docs_text

DOCS = """### Purpose
- **Applies** pre-calculated `registration`.
- Masks regions.

### Limitations
- Only HCS plates.
"""


def test_docs_text():
    assert docs_text(DOCS) == ("Applies pre-calculated registration. Masks "
                               "regions.\n\nLimitations: Only HCS plates.")
    assert docs_text("# Title\n\n- **Segments** nuclei\n\nMore.") == (
        "Segments nuclei\n\nMore.")
    assert docs_text("") == ""


def test_docs_summary_is_the_start_of_the_text():
    assert docs_summary(DOCS) == docs_text(DOCS)  # short: all of it
    summary = docs_summary(DOCS, length=30)
    assert summary == "Applies pre-calculated…"
    assert docs_text(DOCS).startswith(summary[:-1])
    assert docs_summary("x" * 400) == "x" * 300 + "…"
