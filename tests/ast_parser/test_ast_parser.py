import pytest

from checkcord.ast.parser import TreeSitterParser
from checkcord.core.exceptions import ASTParsingError


def test_python_parsing():
    parser = TreeSitterParser()
    code = b"""
class PythonClass:
    def method_one(self):
        def nested_func():
            pass
        pass

def standalone_func():
    pass
"""
    nodes = parser.extract_nodes(code, "python")

    # Assert nodes extracted
    assert len(nodes) == 4

    # Class
    class_node = next(n for n in nodes if n.symbol_name == "PythonClass")
    assert class_node.node_type == "class_definition"
    assert "class PythonClass:" in class_node.content

    # Method
    method_node = next(n for n in nodes if n.symbol_name == "method_one")
    assert method_node.node_type == "method_definition"
    assert "def method_one(self):" in method_node.content

    # Nested function
    nested_node = next(n for n in nodes if n.symbol_name == "nested_func")
    assert nested_node.node_type == "function_definition"
    assert "def nested_func():" in nested_node.content

    # Standalone function
    func_node = next(n for n in nodes if n.symbol_name == "standalone_func")
    assert func_node.node_type == "function_definition"
    assert "def standalone_func():" in func_node.content


def test_typescript_parsing():
    parser = TreeSitterParser()
    code = b"""
class TypeScriptClass {
    methodOne() {
    }
}

function standaloneFunc() {
}
"""
    nodes = parser.extract_nodes(code, "typescript")

    assert len(nodes) == 3

    class_node = next(n for n in nodes if n.symbol_name == "TypeScriptClass")
    assert class_node.node_type == "class_definition"

    method_node = next(n for n in nodes if n.symbol_name == "methodOne")
    assert method_node.node_type == "method_definition"

    func_node = next(n for n in nodes if n.symbol_name == "standaloneFunc")
    assert func_node.node_type == "function_definition"


def test_javascript_parsing_uses_ts_parser():
    # JavaScript should reuse TS queries and parser under the hood
    parser = TreeSitterParser()
    code = b"""
class JSClass {
    jsMethod() {}
}
"""
    nodes = parser.extract_nodes(code, "javascript")
    assert len(nodes) == 2
    assert nodes[0].symbol_name == "JSClass"
    assert nodes[0].node_type == "class_definition"
    assert nodes[1].symbol_name == "jsMethod"
    assert nodes[1].node_type == "method_definition"


def test_go_parsing():
    parser = TreeSitterParser()
    code = b"""
package main

type GoStruct struct {
    Field string
}

func StandaloneFunc() {
}

func (g *GoStruct) MethodOne() {
}
"""
    nodes = parser.extract_nodes(code, "go")

    assert len(nodes) == 3

    struct_node = next(n for n in nodes if n.symbol_name == "GoStruct")
    assert struct_node.node_type == "class_definition"

    func_node = next(n for n in nodes if n.symbol_name == "StandaloneFunc")
    assert func_node.node_type == "function_definition"

    method_node = next(n for n in nodes if n.symbol_name == "MethodOne")
    assert method_node.node_type == "method_definition"


def test_java_parsing():
    parser = TreeSitterParser()
    code = b"""
package com.example;

@Deprecated
public class OuterClass {

    @Override
    public String toString() {
        return "outer";
    }

    public OuterClass(int x) {
        this.x = x;
    }

    private int x;

    static class InnerClass {
        void innerMethod() {
        }
    }

    interface Greeter {
        String greet(String name);
    }

    enum Color {
        RED, GREEN, BLUE;
    }

    record Point(int x, int y) {
        Point {
            if (x < 0) {
                throw new IllegalArgumentException();
            }
        }
    }
}
"""
    nodes = parser.extract_nodes(code, "java")

    # class, toString, constructor, InnerClass, innerMethod, Greeter, greet, Color, Point == 9.
    # No separate node for record's compact constructor -- see below.
    assert len(nodes) == 9

    # Class, with its leading annotation included in the captured byte range.
    class_node = next(
        n for n in nodes if n.symbol_name == "OuterClass" and n.node_type == "class_definition"
    )
    assert "@Deprecated" in class_node.content
    assert "public class OuterClass" in class_node.content

    # Method, with its leading annotation included in the captured byte range.
    method_node = next(n for n in nodes if n.symbol_name == "toString")
    assert method_node.node_type == "method_definition"
    assert "@Override" in method_node.content

    # Constructor shares its symbol_name with the class -- node_type disambiguates.
    ctor_node = next(
        n for n in nodes if n.symbol_name == "OuterClass" and n.node_type == "method_definition"
    )
    assert "public OuterClass(int x)" in ctor_node.content

    # Nested class is captured as its own node, byte-range-enclosed by the outer class.
    inner_node = next(n for n in nodes if n.symbol_name == "InnerClass")
    assert inner_node.node_type == "class_definition"
    assert class_node.start_byte <= inner_node.start_byte
    assert inner_node.end_byte <= class_node.end_byte

    inner_method_node = next(n for n in nodes if n.symbol_name == "innerMethod")
    assert inner_method_node.node_type == "method_definition"

    interface_node = next(n for n in nodes if n.symbol_name == "Greeter")
    assert interface_node.node_type == "class_definition"

    greet_node = next(n for n in nodes if n.symbol_name == "greet")
    assert greet_node.node_type == "method_definition"

    enum_node = next(n for n in nodes if n.symbol_name == "Color")
    assert enum_node.node_type == "class_definition"

    # Record: captured as a class_definition. Its compact constructor
    # (`Point { ... }`, Java 16+ validation syntax) is a distinct grammar node
    # (compact_constructor_declaration, not constructor_declaration) and is not
    # captured as its own node -- but its content is still inside the record's
    # own byte range, so it's reflected in the record's Merkle hash.
    record_node = next(n for n in nodes if n.symbol_name == "Point")
    assert record_node.node_type == "class_definition"
    assert "IllegalArgumentException" in record_node.content
    assert not any(n.symbol_name == "Point" and n.node_type == "method_definition" for n in nodes)


def test_unsupported_language_raises_error():
    parser = TreeSitterParser()
    with pytest.raises(ASTParsingError) as exc_info:
        parser.extract_nodes(b"fn main() {}", "rust")
    assert "Unsupported language" in str(exc_info.value)
