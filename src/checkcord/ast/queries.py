"""Tree-sitter queries per language."""

PYTHON_QUERY = """
(class_definition
  name: (identifier) @class.name) @class.def

(function_definition
  name: (identifier) @function.name) @function.def
"""

TYPESCRIPT_QUERY = """
(class_declaration
  name: (type_identifier) @class.name) @class.def

(function_declaration
  name: (identifier) @function.name) @function.def

(method_definition
  name: (property_identifier) @method.name) @method.def
"""

GO_QUERY = """
(function_declaration
  name: (identifier) @function.name) @function.def

(method_declaration
  name: (field_identifier) @method.name) @method.def

(type_spec
  name: (type_identifier) @class.name
  type: (struct_type)) @class.def
"""

JAVA_QUERY = """
(class_declaration
  name: (identifier) @class.name) @class.def

(interface_declaration
  name: (identifier) @class.name) @class.def

(enum_declaration
  name: (identifier) @class.name) @class.def

(record_declaration
  name: (identifier) @class.name) @class.def

(method_declaration
  name: (identifier) @method.name) @method.def

(constructor_declaration
  name: (identifier) @method.name) @method.def
"""
