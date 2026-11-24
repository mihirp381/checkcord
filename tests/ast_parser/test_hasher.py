from checkcord.ast.hasher import SemanticHasher, strip_comments_and_whitespace
from checkcord.ast.parser import TreeSitterParser


def test_strip_comments_and_whitespace_python():
    # Helper to test strip utility directly
    py_code = """
def func():
    # This is a line comment
    x = 1  # inline comment
    '''This is a standalone triple quote comment'''
    y = 2
    \"\"\"This is also standalone docstring\"\"\"
    s = "hello # not a comment"
    return s
"""
    cleaned = strip_comments_and_whitespace(py_code, "python")
    # Verify comments and docstrings are stripped but string literals are intact
    assert "Thisisalinecomment" not in cleaned
    assert "inlinecomment" not in cleaned
    assert "Thisisastandalonetriplequotecomment" not in cleaned
    assert "Thisisalsostandalonedocstring" not in cleaned
    assert 's="hello#notacomment"' in cleaned


def test_strip_comments_and_whitespace_go():
    go_code = """
func main() {
    // line comment
    x := 1 // inline comment
    /* block
       comment */
    s := "http://example.com//not-comment"
    return s
}
"""
    cleaned = strip_comments_and_whitespace(go_code, "go")
    assert "linecomment" not in cleaned
    assert "inlinecomment" not in cleaned
    assert "blockcomment" not in cleaned
    assert 's:="http://example.com//not-comment"' in cleaned


def test_strip_comments_and_whitespace_typescript():
    ts_code = """
class A {
    // line comment
    method() {
        /* block comment */
        let x = `template // not comment`;
    }
}
"""
    cleaned = strip_comments_and_whitespace(ts_code, "typescript")
    assert "linecomment" not in cleaned
    assert "blockcomment" not in cleaned
    assert "template//notcomment" in cleaned


def test_strip_comments_and_whitespace_java():
    java_code = """
public class Demo {
    // line comment
    void method() {
        int x = 1; // inline comment
        /* block
           comment */
        String s = "http://example.com//not-comment";
    }

    String textBlock() {
        return \"\"\"
            hello // not a real comment, this is text block content
            world
            \"\"\";
    }
}
"""
    cleaned = strip_comments_and_whitespace(java_code, "java")
    assert "linecomment" not in cleaned
    assert "inlinecomment" not in cleaned
    assert "blockcomment" not in cleaned
    assert 's="http://example.com//not-comment"' in cleaned
    # Java text blocks are ordinary string-literal content, not docstrings --
    # unlike Python's triple-quote heuristic, nothing here should be stripped.
    assert "hello" in cleaned
    assert "world" in cleaned
    assert "notarealcomment" in cleaned


def test_hasher_invariance_python():
    parser = TreeSitterParser()

    code_v1 = b"""
class DemoClass:
    \"\"\"This is a docstring\"\"\"
    def method_one(self):
        # Line comment
        x = 1
        return x
"""

    code_v2 = b"""
class DemoClass:
    def method_one(self):
        x = 1
        return x
"""

    nodes_v1 = parser.extract_nodes(code_v1, "python")
    nodes_v2 = parser.extract_nodes(code_v2, "python")

    hashed_v1 = SemanticHasher.generate_hashes(nodes_v1, "python")
    hashed_v2 = SemanticHasher.generate_hashes(nodes_v2, "python")

    # Hashes should be identical because comments and docstrings are stripped
    assert len(hashed_v1) == len(hashed_v2)
    for h1, h2 in zip(hashed_v1, hashed_v2, strict=True):
        assert h1.symbol_name == h2.symbol_name
        assert h1.merkle_hash == h2.merkle_hash


def test_hasher_logic_change_python():
    parser = TreeSitterParser()

    code_v1 = b"""
class DemoClass:
    def method_one(self):
        x = 1
        return x
"""

    code_v2 = b"""
class DemoClass:
    def method_one(self):
        x = 2
        return x
"""

    nodes_v1 = parser.extract_nodes(code_v1, "python")
    nodes_v2 = parser.extract_nodes(code_v2, "python")

    hashed_v1 = SemanticHasher.generate_hashes(nodes_v1, "python")
    hashed_v2 = SemanticHasher.generate_hashes(nodes_v2, "python")

    # Method logic changed, so its hash must change
    m1 = next(n for n in hashed_v1 if n.symbol_name == "method_one")
    m2 = next(n for n in hashed_v2 if n.symbol_name == "method_one")
    assert m1.merkle_hash != m2.merkle_hash

    # Class hash encloses the method hash, so class hash must also change
    c1 = next(n for n in hashed_v1 if n.symbol_name == "DemoClass")
    c2 = next(n for n in hashed_v2 if n.symbol_name == "DemoClass")
    assert c1.merkle_hash != c2.merkle_hash


def test_merkle_bubble_up_nested():
    parser = TreeSitterParser()

    code = b"""
class OuterClass:
    def method_one(self):
        def nested_func():
            return 1
        return nested_func()
"""
    nodes = parser.extract_nodes(code, "python")
    hashed = SemanticHasher.generate_hashes(nodes, "python")

    assert len(hashed) == 3
    outer = next(n for n in hashed if n.symbol_name == "OuterClass")
    method = next(n for n in hashed if n.symbol_name == "method_one")
    nested = next(n for n in hashed if n.symbol_name == "nested_func")

    # Let's modify the nested func logic
    code_mod = b"""
class OuterClass:
    def method_one(self):
        def nested_func():
            return 2
        return nested_func()
"""
    nodes_mod = parser.extract_nodes(code_mod, "python")
    hashed_mod = SemanticHasher.generate_hashes(nodes_mod, "python")

    outer_mod = next(n for n in hashed_mod if n.symbol_name == "OuterClass")
    method_mod = next(n for n in hashed_mod if n.symbol_name == "method_one")
    nested_mod = next(n for n in hashed_mod if n.symbol_name == "nested_func")

    # Verify bubble-up:
    # 1. Nested function logic change changes its hash
    assert nested.merkle_hash != nested_mod.merkle_hash
    # 2. Bubbles up to method_one
    assert method.merkle_hash != method_mod.merkle_hash
    # 3. Bubbles up to OuterClass
    assert outer.merkle_hash != outer_mod.merkle_hash


def test_hasher_invariance_java():
    parser = TreeSitterParser()

    code_v1 = b"""
public class DemoClass {
    // A comment that should not affect the hash
    void methodOne() {
        int x = 1;
    }
}
"""

    code_v2 = b"""
public class DemoClass {
    void methodOne() {
        int x = 1;
    }
}
"""

    nodes_v1 = parser.extract_nodes(code_v1, "java")
    nodes_v2 = parser.extract_nodes(code_v2, "java")

    hashed_v1 = SemanticHasher.generate_hashes(nodes_v1, "java")
    hashed_v2 = SemanticHasher.generate_hashes(nodes_v2, "java")

    assert len(hashed_v1) == len(hashed_v2)
    for h1, h2 in zip(hashed_v1, hashed_v2, strict=True):
        assert h1.symbol_name == h2.symbol_name
        assert h1.merkle_hash == h2.merkle_hash


def test_hasher_logic_change_java():
    parser = TreeSitterParser()

    code_v1 = b"""
public class DemoClass {
    void methodOne() {
        int x = 1;
    }
}
"""

    code_v2 = b"""
public class DemoClass {
    void methodOne() {
        int x = 2;
    }
}
"""

    nodes_v1 = parser.extract_nodes(code_v1, "java")
    nodes_v2 = parser.extract_nodes(code_v2, "java")

    hashed_v1 = SemanticHasher.generate_hashes(nodes_v1, "java")
    hashed_v2 = SemanticHasher.generate_hashes(nodes_v2, "java")

    m1 = next(n for n in hashed_v1 if n.symbol_name == "methodOne")
    m2 = next(n for n in hashed_v2 if n.symbol_name == "methodOne")
    assert m1.merkle_hash != m2.merkle_hash

    c1 = next(n for n in hashed_v1 if n.symbol_name == "DemoClass")
    c2 = next(n for n in hashed_v2 if n.symbol_name == "DemoClass")
    assert c1.merkle_hash != c2.merkle_hash


def test_merkle_bubble_up_nested_java():
    parser = TreeSitterParser()

    code = b"""
public class OuterClass {
    static class InnerClass {
        void innerMethod() {
            int x = 1;
        }
    }
}
"""
    nodes = parser.extract_nodes(code, "java")
    hashed = SemanticHasher.generate_hashes(nodes, "java")

    assert len(hashed) == 3
    outer = next(n for n in hashed if n.symbol_name == "OuterClass")
    inner = next(n for n in hashed if n.symbol_name == "InnerClass")
    method = next(n for n in hashed if n.symbol_name == "innerMethod")

    code_mod = b"""
public class OuterClass {
    static class InnerClass {
        void innerMethod() {
            int x = 2;
        }
    }
}
"""
    nodes_mod = parser.extract_nodes(code_mod, "java")
    hashed_mod = SemanticHasher.generate_hashes(nodes_mod, "java")

    outer_mod = next(n for n in hashed_mod if n.symbol_name == "OuterClass")
    inner_mod = next(n for n in hashed_mod if n.symbol_name == "InnerClass")
    method_mod = next(n for n in hashed_mod if n.symbol_name == "innerMethod")

    # 1. Innermost method logic change changes its own hash
    assert method.merkle_hash != method_mod.merkle_hash
    # 2. Bubbles up to InnerClass
    assert inner.merkle_hash != inner_mod.merkle_hash
    # 3. Bubbles up to OuterClass
    assert outer.merkle_hash != outer_mod.merkle_hash
