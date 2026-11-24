from scripts import export_openapi


def test_committed_openapi_document_is_current() -> None:
    assert export_openapi.PATH.read_text() == export_openapi.render(), (
        "docs/openapi.json is out of date: run `uv run python scripts/export_openapi.py`"
    )
