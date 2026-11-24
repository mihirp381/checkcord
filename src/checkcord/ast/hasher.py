"""Hashes of AST nodes that ignore comments and whitespace, combined up the tree."""

import hashlib
import re

from checkcord.ast.parser import ExtractedNode
from checkcord.core.exceptions import ASTHashingError


class HashedNode(ExtractedNode):
    """An AST node with its Merkle hash."""

    merkle_hash: str


def strip_comments_and_whitespace(content: str, language: str) -> str:
    """Strip all comments, docstrings, and whitespace for language-agnostic semantic hashing."""
    lang_lower = language.lower()

    # Regex pattern to match string literals (group 1) or comments (group 2)
    pattern = re.compile(
        r"("
        r'"""[\s\S]*?"""|'
        r"'''[\s\S]*?'''|"
        r'"(?:\\.|[^"\\])*"|'
        r"'(?:\\.|[^'\\])*'|"
        r"`(?:\\.|[^`\\])*`"
        r")|"
        r"("
        r"/\*[\s\S]*?\*/|"
        r"//.*|"
        r"#.*"
        r")"
    )

    def replace(match: re.Match) -> str:
        val = match.group(1)
        if val is not None:
            assert isinstance(val, str)
            # Special heuristic for Python docstrings (triple-quoted strings that act as comments)
            if lang_lower == "python" and (val.startswith('"""') or val.startswith("'''")):
                start_idx = match.start(1)
                end_idx = match.end(1)
                before = content[:start_idx]
                after = content[end_idx:]

                # Clean comments from surrounding contexts first to prevent them from interfering
                # with our line/block detection.
                cleaned_before = re.sub(r"#.*|//.*|/\*[\s\S]*?\*/", "", before)
                cleaned_after = re.sub(r"#.*|//.*|/\*[\s\S]*?\*/", "", after)

                is_after_colon = cleaned_before.rstrip().endswith(":")

                # Standalone check: does the line before and after containing this string
                # only have whitespace?
                last_newline = max(cleaned_before.rfind("\n"), cleaned_before.rfind("\r"))
                if last_newline == -1:
                    is_standalone_before = cleaned_before.strip() == ""
                else:
                    is_standalone_before = cleaned_before[last_newline + 1 :].strip() == ""

                first_newline = cleaned_after.find("\n")
                if first_newline == -1:
                    is_standalone_after = cleaned_after.strip() == ""
                else:
                    is_standalone_after = cleaned_after[:first_newline].strip() == ""

                if is_after_colon or (is_standalone_before and is_standalone_after):
                    return ""
            return val
        return ""

    try:
        no_comments = pattern.sub(replace, content)
        # Aggressively remove all remaining whitespace
        return re.sub(r"\s+", "", no_comments)
    except Exception as e:
        raise ASTHashingError(f"Failed to strip comments/whitespace: {e}") from e


class SemanticHasher:
    """Merkle hashes for a file's AST nodes."""

    @staticmethod
    def generate_hashes(nodes: list[ExtractedNode], language: str) -> list[HashedNode]:
        """Build parent-child relationships and generate SHA-256 Merkle hashes for all nodes."""
        if not nodes:
            return []

        # Establish parent-child hierarchy using byte-range inclusion.
        # A node A is a parent of node B if A's byte range encloses B's byte range.
        # To find immediate parent-child relationships, we sort nodes by size (span) descending.
        sorted_nodes = sorted(nodes, key=lambda n: n.end_byte - n.start_byte, reverse=True)

        # Build parent-to-children mapping
        # parent_map: index of parent in sorted_nodes -> list of indices of immediate children
        parent_map: dict[int, list[int]] = {i: [] for i in range(len(sorted_nodes))}

        for i, child in enumerate(sorted_nodes):
            # Find the smallest node that encloses this child (excluding itself)
            immediate_parent_idx = None
            min_parent_span = float("inf")

            for j, potential_parent in enumerate(sorted_nodes):
                if i == j:
                    continue
                # Byte inclusion check
                if (
                    potential_parent.start_byte <= child.start_byte
                    and child.end_byte <= potential_parent.end_byte
                ):
                    span = potential_parent.end_byte - potential_parent.start_byte
                    if span < min_parent_span:
                        min_parent_span = span
                        immediate_parent_idx = j

            if immediate_parent_idx is not None:
                parent_map[immediate_parent_idx].append(i)

        # Compute hashes bottom-up.
        # To do this safely, we resolve hashes starting from nodes with no children.
        hashes: dict[int, str] = {}

        def compute_node_hash(idx: int) -> str:
            if idx in hashes:
                return hashes[idx]

            node = sorted_nodes[idx]
            normalized_content = strip_comments_and_whitespace(node.content, language)

            child_indices = parent_map[idx]
            if child_indices:
                # Compute child hashes first
                child_hashes = [compute_node_hash(c_idx) for c_idx in child_indices]
                # Sort child hashes by child start_byte to maintain structural order
                sorted_children_with_hashes = sorted(
                    zip(child_indices, child_hashes, strict=True),
                    key=lambda x: sorted_nodes[x[0]].start_byte,
                )
                child_hash_str = "".join(h for _, h in sorted_children_with_hashes)
                data_to_hash = f"{normalized_content}{node.symbol_name}{child_hash_str}"
            else:
                data_to_hash = f"{normalized_content}{node.symbol_name}"

            node_hash = hashlib.sha256(data_to_hash.encode("utf-8")).hexdigest()
            hashes[idx] = node_hash
            return node_hash

        # Run hash computation for all nodes
        for i in range(len(sorted_nodes)):
            compute_node_hash(i)

        # Construct HashedNode list, retaining original order of input nodes
        hashed_node_map = {}
        for i, node in enumerate(sorted_nodes):
            hashed_node_map[node.start_byte] = HashedNode(
                node_type=node.node_type,
                symbol_name=node.symbol_name,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                content=node.content,
                merkle_hash=hashes[i],
            )

        return [hashed_node_map[n.start_byte] for n in nodes]
