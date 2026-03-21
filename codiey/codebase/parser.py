"""Tree-sitter AST parsing for Python, JavaScript, and TypeScript files.

Parses a single file and returns structured data:
  - functions (name, line, params, return_type, docstring, body_lines)
  - classes (name, line, methods, bases, docstring)
  - imports (module, names, line, kind)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tree_sitter_python as ts_python
import tree_sitter_javascript as ts_javascript
import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Parser


# ── Language setup ──

PY_LANGUAGE = Language(ts_python.language())
JS_LANGUAGE = Language(ts_javascript.language())
TS_LANGUAGE = Language(ts_typescript.language_typescript())
TSX_LANGUAGE = Language(ts_typescript.language_tsx())


EXTENSION_TO_LANGUAGE: dict[str, Language] = {
    ".py": PY_LANGUAGE,
    ".js": JS_LANGUAGE,
    ".jsx": JS_LANGUAGE,
    ".ts": TSX_LANGUAGE,
    ".tsx": TSX_LANGUAGE,
    ".mjs": JS_LANGUAGE,
    ".cjs": JS_LANGUAGE,
}

SUPPORTED_EXTENSIONS = set(EXTENSION_TO_LANGUAGE.keys())


def get_parser(language: Language) -> Parser:
    """Create a tree-sitter parser for the given language."""
    parser = Parser(language)
    return parser


def parse_file(file_path: Path) -> dict[str, Any] | None:
    """Parse a single file and return structured data.

    Returns None if the file extension is unsupported or the file can't be read.

    Returns:
        {
            "path": str,
            "language": str,
            "functions": [...],
            "classes": [...],
            "imports": [...],
            "line_count": int,
        }
    """
    ext = file_path.suffix.lower()
    if ext not in EXTENSION_TO_LANGUAGE:
        return None

    try:
        source = file_path.read_bytes()
    except (OSError, PermissionError):
        return None

    language = EXTENSION_TO_LANGUAGE[ext]
    parser = get_parser(language)
    tree = parser.parse(source)

    lang_name = _ext_to_lang_name(ext)

    if lang_name == "python":
        return _parse_python(tree, source, file_path)
    else:
        return _parse_js_ts(tree, source, file_path, lang_name)


def _ext_to_lang_name(ext: str) -> str:
    if ext == ".py":
        return "python"
    if ext in (".ts", ".tsx"):
        return "typescript"
    return "javascript"


# ══════════════════════════════════════════════════════════════
# Python Parsing
# ══════════════════════════════════════════════════════════════

def _parse_python(tree, source: bytes, file_path: Path) -> dict[str, Any]:
    root = tree.root_node
    source_lines = source.decode("utf-8", errors="replace").split("\n")

    functions = []
    classes = []
    imports = []

    for node in root.children:
        if node.type == "function_definition":
            functions.append(_extract_python_function(node, source_lines))
        elif node.type == "decorated_definition":
            inner = _get_decorated_inner(node)
            if inner and inner.type == "function_definition":
                functions.append(_extract_python_function(inner, source_lines, decorated=True))
            elif inner and inner.type == "class_definition":
                classes.append(_extract_python_class(inner, source_lines, decorated=True))
        elif node.type == "class_definition":
            classes.append(_extract_python_class(node, source_lines))
        elif node.type in ("import_statement", "import_from_statement"):
            imports.append(_extract_python_import(node, source_lines))

    return {
        "path": str(file_path),
        "language": "python",
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "line_count": len(source_lines),
    }


def _get_decorated_inner(node):
    """Get the actual definition inside a decorated_definition."""
    for child in node.children:
        if child.type in ("function_definition", "class_definition"):
            return child
    return None


def _extract_python_function(node, source_lines: list[str], decorated: bool = False) -> dict:
    name = ""
    params = []
    return_type = None
    docstring = None

    for child in node.children:
        if child.type == "identifier":
            name = child.text.decode("utf-8")
        elif child.type == "parameters":
            params = _extract_python_params(child)
        elif child.type == "type":
            return_type = child.text.decode("utf-8")
        elif child.type == "block":
            docstring = _extract_python_docstring(child)

    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1

    return {
        "name": name,
        "line": start_line,
        "end_line": end_line,
        "params": params,
        "return_type": return_type,
        "docstring": docstring,
        "decorated": decorated,
    }


def _extract_python_params(params_node) -> list[str]:
    result = []
    for child in params_node.children:
        if child.type in ("identifier", "typed_parameter", "default_parameter",
                          "typed_default_parameter", "list_splat_pattern",
                          "dictionary_splat_pattern"):
            text = child.text.decode("utf-8")
            if text not in ("(", ")", ","):
                result.append(text)
    return result


def _extract_python_docstring(block_node) -> str | None:
    for child in block_node.children:
        if child.type == "expression_statement":
            for inner in child.children:
                if inner.type == "string":
                    raw = inner.text.decode("utf-8")
                    # Strip triple quotes
                    for q in ('"""', "'''"):
                        if raw.startswith(q) and raw.endswith(q):
                            return raw[3:-3].strip()
                    return raw.strip("\"'").strip()
        break  # Only check the first statement
    return None


def _extract_python_class(node, source_lines: list[str], decorated: bool = False) -> dict:
    name = ""
    bases = []
    methods = []
    docstring = None

    for child in node.children:
        if child.type == "identifier":
            name = child.text.decode("utf-8")
        elif child.type == "argument_list":
            bases = [
                c.text.decode("utf-8")
                for c in child.children
                if c.type not in ("(", ")", ",")
            ]
        elif child.type == "block":
            docstring = _extract_python_docstring(child)
            for block_child in child.children:
                if block_child.type == "function_definition":
                    methods.append(_extract_python_function(block_child, source_lines))
                elif block_child.type == "decorated_definition":
                    inner = _get_decorated_inner(block_child)
                    if inner and inner.type == "function_definition":
                        methods.append(_extract_python_function(inner, source_lines, decorated=True))

    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1

    return {
        "name": name,
        "line": start_line,
        "end_line": end_line,
        "bases": bases,
        "methods": methods,
        "docstring": docstring,
        "decorated": decorated,
    }


def _extract_python_import(node, source_lines: list[str]) -> dict:
    line = node.start_point[0] + 1
    text = node.text.decode("utf-8")

    if node.type == "import_statement":
        # import foo, bar
        names = []
        for child in node.children:
            if child.type == "dotted_name":
                names.append(child.text.decode("utf-8"))
            elif child.type == "aliased_import":
                names.append(child.text.decode("utf-8"))
        return {"kind": "import", "module": None, "names": names, "line": line, "text": text}
    else:
        # from foo import bar, baz
        module = None
        names = []
        for child in node.children:
            if child.type in ("dotted_name", "relative_import"):
                module = child.text.decode("utf-8")
            elif child.type == "import_prefix":
                module = child.text.decode("utf-8")
            elif child.type == "wildcard_import":
                names.append("*")
            elif child.type == "import_from_list":  # Unused but safe
                pass

        # Extract imported names from the import list
        for child in node.children:
            if child.type == "import_from_list":
                for item in child.children:
                    if item.type in ("dotted_name", "aliased_import"):
                        names.append(item.text.decode("utf-8"))

        return {"kind": "from", "module": module, "names": names, "line": line, "text": text}


# ══════════════════════════════════════════════════════════════
# JavaScript / TypeScript Parsing
# ══════════════════════════════════════════════════════════════

def _parse_js_ts(tree, source: bytes, file_path: Path, lang_name: str) -> dict[str, Any]:
    root = tree.root_node
    source_lines = source.decode("utf-8", errors="replace").split("\n")

    functions = []
    classes = []
    imports = []

    _walk_js_ts(root, functions, classes, imports, source_lines)

    return {
        "path": str(file_path),
        "language": lang_name,
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "line_count": len(source_lines),
    }


def _walk_js_ts(node, functions: list, classes: list, imports: list, source_lines: list[str]):
    """Walk the AST and extract top-level constructs."""
    for child in node.children:
        if child.type in ("function_declaration", "generator_function_declaration"):
            functions.append(_extract_js_function(child, source_lines))
        elif child.type in ("export_statement", "internal_module", "module", "namespace_declaration", "program"):
            _walk_js_ts(child, functions, classes, imports, source_lines)
        elif child.type == "class_declaration":
            classes.append(_extract_js_class(child, source_lines))
        elif child.type in ("import_statement", "import_declaration"):
            imports.append(_extract_js_import(child))
        elif child.type in ("lexical_declaration", "variable_declaration"):
            for decl in child.children:
                if decl.type == "variable_declarator":
                    _try_extract_arrow_or_fn(decl, functions, source_lines)


def _extract_js_function(node, source_lines: list[str]) -> dict:
    name = ""
    params = []

    for child in node.children:
        if child.type == "identifier":
            name = child.text.decode("utf-8")
        elif child.type == "formal_parameters":
            params = _extract_js_params(child)

    return {
        "name": name,
        "line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "params": params,
        "kind": "function",
    }


def _try_extract_arrow_or_fn(declarator, functions: list, source_lines: list[str]):
    """Extract arrow functions and function expressions assigned to variables."""
    name = ""
    for child in declarator.children:
        if child.type == "identifier":
            name = child.text.decode("utf-8")
        elif child.type in ("arrow_function", "function_expression", "generator_function"):
            params = []
            for inner in child.children:
                if inner.type == "formal_parameters":
                    params = _extract_js_params(inner)
                elif inner.type == "identifier" and not params:
                    # Single param arrow: x => ...
                    params = [inner.text.decode("utf-8")]

            functions.append({
                "name": name,
                "line": declarator.start_point[0] + 1,
                "end_line": declarator.end_point[0] + 1,
                "params": params,
                "kind": "arrow" if child.type == "arrow_function" else "function_expression",
            })


def _extract_js_params(params_node) -> list[str]:
    result = []
    for child in params_node.children:
        if child.type not in ("(", ")", ",", "comment"):
            result.append(child.text.decode("utf-8"))
    return result


def _extract_js_class(node, source_lines: list[str]) -> dict:
    name = ""
    bases = []
    methods = []

    for child in node.children:
        if child.type == "identifier":
            name = child.text.decode("utf-8")
        elif child.type == "class_heritage":
            for inner in child.children:
                if inner.type == "identifier":
                    bases.append(inner.text.decode("utf-8"))
        elif child.type == "class_body":
            for member in child.children:
                if member.type == "method_definition":
                    methods.append(_extract_js_method(member))

    return {
        "name": name,
        "line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "bases": bases,
        "methods": methods,
    }


def _extract_js_method(node) -> dict:
    name = ""
    params = []
    is_static = False

    for child in node.children:
        if child.type == "property_identifier":
            name = child.text.decode("utf-8")
        elif child.type == "formal_parameters":
            params = _extract_js_params(child)
        elif child.text == b"static":
            is_static = True

    return {
        "name": name,
        "line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "params": params,
        "static": is_static,
    }


def _extract_js_import(node) -> dict:
    text = node.text.decode("utf-8")
    line = node.start_point[0] + 1

    # Extract the module/source string
    module = None
    names = []
    for child in node.children:
        if child.type == "string":
            module = child.text.decode("utf-8").strip("\"'")
        elif child.type == "import_clause":
            for inner in child.children:
                if inner.type == "identifier":
                    names.append(inner.text.decode("utf-8"))
                elif inner.type == "named_imports":
                    for spec in inner.children:
                        if spec.type == "import_specifier":
                            names.append(spec.text.decode("utf-8"))
                elif inner.type == "namespace_import":
                    names.append(inner.text.decode("utf-8"))

    return {
        "kind": "import",
        "module": module,
        "names": names,
        "line": line,
        "text": text,
    }
