"""Deep-link URL construction into the web graph viewer.

Links never carry an auth token: they end up in GitHub PR comments and in
prompts sent to an LLM provider. A link works only for someone who already
has viewer access.
"""

from urllib.parse import quote


def build_node_url(web_base_url: str, repo_identifier: str, node_id: str) -> str:
    """Build a no-auth deep link to a specific node in the embed graph viewer."""
    base = web_base_url.rstrip("/")
    repo_part = quote(repo_identifier, safe="")
    node_part = quote(node_id, safe="")
    return f"{base}/embed/graph/{repo_part}?node={node_part}"
