"""Tree-sitter parsing into AST nodes."""

from typing import Literal

import tree_sitter
import tree_sitter_go
import tree_sitter_java
import tree_sitter_python
import tree_sitter_typescript
from pydantic import BaseModel

from checkcord.ast.queries import GO_QUERY, JAVA_QUERY, PYTHON_QUERY, TYPESCRIPT_QUERY
from checkcord.core.exceptions import ASTParsingError


def get_language_from_filename(filename: str) -> str | None:
    """Map a file path's extension to a TreeSitterParser-supported language name.

    PR evaluation and the AST writer both call this, so they agree on a
    file's language.
    """
    if filename.endswith(".py"):
        return "python"
    elif filename.endswith(".ts") or filename.endswith(".tsx"):
        return "typescript"
    elif filename.endswith(".js") or filename.endswith(".jsx"):
        return "javascript"
    elif filename.endswith(".go"):
        return "go"
    elif filename.endswith(".java"):
        return "java"
    return None


class ExtractedNode(BaseModel):
    """A code symbol found by the parser."""

    node_type: Literal["class_definition", "function_definition", "method_definition"]
    symbol_name: str
    start_byte: int
    end_byte: int
    content: str


def _is_python_method(node: tree_sitter.Node) -> bool:
    """Determine if a Python function definition is a method inside a class."""
    curr = node.parent
    while curr:
        if curr.type == "class_definition":
            return True
        if curr.type in ("function_definition", "generator_function_definition"):
            # Enclosed by a function scope first, so it's a nested/helper function
            return False
        curr = curr.parent
    return False


class TreeSitterParser:
    """Finds code symbols with Tree-sitter."""

    def __init__(self) -> None:
        try:
            self.py_lang = tree_sitter.Language(tree_sitter_python.language())
            self.ts_lang = tree_sitter.Language(tree_sitter_typescript.language_typescript())
            self.go_lang = tree_sitter.Language(tree_sitter_go.language())
            self.java_lang = tree_sitter.Language(tree_sitter_java.language())
        except Exception as e:
            raise ASTParsingError(f"Failed to initialize tree-sitter languages: {e}") from e

        # Precompile queries for supported languages
        try:
            self.queries = {
                "python": tree_sitter.Query(self.py_lang, PYTHON_QUERY),
                "typescript": tree_sitter.Query(self.ts_lang, TYPESCRIPT_QUERY),
                "javascript": tree_sitter.Query(self.ts_lang, TYPESCRIPT_QUERY),  # Reuse TS query
                "go": tree_sitter.Query(self.go_lang, GO_QUERY),
                "java": tree_sitter.Query(self.java_lang, JAVA_QUERY),
            }
        except Exception as e:
            raise ASTParsingError(f"Failed to compile tree-sitter queries: {e}") from e

    def extract_nodes(self, file_content: bytes, language: str) -> list[ExtractedNode]:
        """Parse source file bytes and extract structural nodes for the specified language."""
        lang_lower = language.lower()
        if lang_lower not in self.queries:
            raise ASTParsingError(f"Unsupported language: {language}")

        if lang_lower in ("typescript", "javascript"):
            lang_obj = self.ts_lang
        elif lang_lower == "python":
            lang_obj = self.py_lang
        elif lang_lower == "go":
            lang_obj = self.go_lang
        elif lang_lower == "java":
            lang_obj = self.java_lang
        else:
            raise ASTParsingError(f"Unsupported language: {language}")

        try:
            parser = tree_sitter.Parser(lang_obj)
            tree = parser.parse(file_content)
        except Exception as e:
            raise ASTParsingError(f"Failed to parse source file: {e}") from e

        query = self.queries[lang_lower]
        cursor = tree_sitter.QueryCursor(query)
        matches = cursor.matches(tree.root_node)

        nodes = []
        for _, match_dict in matches:
            def_node = None
            name_node = None
            capture_type = None

            for key, val_nodes in match_dict.items():
                if not val_nodes:
                    continue
                if key.endswith(".def"):
                    def_node = val_nodes[0]
                    capture_type = key.split(".")[0]
                elif key.endswith(".name"):
                    name_node = val_nodes[0]

            if not def_node or not name_node or not capture_type:
                continue

            # Map the capture type to standard output types
            node_type: Literal["class_definition", "function_definition", "method_definition"]
            if capture_type == "class":
                node_type = "class_definition"
            elif capture_type == "method":
                node_type = "method_definition"
            elif capture_type == "function":
                if lang_lower == "python" and _is_python_method(def_node):
                    node_type = "method_definition"
                else:
                    node_type = "function_definition"
            else:
                continue

            try:
                symbol_name = file_content[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8"
                )
                content = file_content[def_node.start_byte : def_node.end_byte].decode("utf-8")
            except UnicodeDecodeError:
                # Fallback to replace decoding if invalid UTF-8 (safeguard)
                symbol_name = file_content[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                content = file_content[def_node.start_byte : def_node.end_byte].decode(
                    "utf-8", errors="replace"
                )

            nodes.append(
                ExtractedNode(
                    node_type=node_type,
                    symbol_name=symbol_name,
                    start_byte=def_node.start_byte,
                    end_byte=def_node.end_byte,
                    content=content,
                )
            )

        return nodes
