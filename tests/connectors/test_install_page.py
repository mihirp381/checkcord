from checkcord.connectors import install_page


def test_failed_page_escapes_the_message() -> None:
    resp = install_page.failed("Slack", "<script>x</script>", 400)
    body = bytes(resp.body).decode()
    assert resp.status_code == 400
    assert "<script>" not in body
    assert "&lt;script&gt;x&lt;/script&gt;" in body
    assert "Open the Checkcord console" in body


def test_connected_page() -> None:
    resp = install_page.connected("Linear")
    assert resp.status_code == 200
    assert "Linear is connected to Checkcord. You can close this tab." in bytes(resp.body).decode()
