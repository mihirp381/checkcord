from checkcord.main import app


def test_every_operation_has_one_tag() -> None:
    tags = {
        (method.upper(), path): op.get("tags", [])
        for path, ops in app.openapi()["paths"].items()
        for method, op in ops.items()
    }
    assert {k: v for k, v in tags.items() if len(v) != 1} == {}
