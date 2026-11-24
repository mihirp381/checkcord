from checkcord.core.viewer_links import build_node_url


def test_build_node_url_basic() -> None:
    url = build_node_url("https://checkcord.example.com", "acme/service-a", "context:abc-123")
    assert (
        url == "https://checkcord.example.com/embed/graph/acme%2Fservice-a?node=context%3Aabc-123"
    )


def test_build_node_url_strips_trailing_slash_on_base() -> None:
    url = build_node_url("https://checkcord.example.com/", "acme/service-a", "context:abc-123")
    assert url.startswith("https://checkcord.example.com/embed/graph/")
    assert "//embed" not in url


def test_build_node_url_encodes_special_characters_in_repo_identifier() -> None:
    url = build_node_url("http://localhost:3000", "acme/service a?b", "symbol:xyz")
    # Slash, space, and query-string-breaking characters must all be percent-encoded
    # so the repo identifier stays a single path segment, matching what
    # web/src/app/embed/graph/[repoId]/page.tsx expects to decodeURIComponent.
    assert "acme%2Fservice%20a%3Fb" in url
    assert "?node=" in url
    assert url.count("?") == 1


def test_build_node_url_no_auth_token_present() -> None:
    url = build_node_url("http://localhost:3000", "acme/repo", "context:abc")
    assert "token" not in url
