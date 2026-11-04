"""Tree-sitter language loading with graceful fallback.

If tree-sitter + grammars are installed we get real ASTs (enabling `ast_pattern`
rules). If not, the rest of the system still runs using the regex fallback in
``extractors.py`` — the AST-quality rules simply become unavailable and are
reported as skipped rather than failing.
"""
from __future__ import annotations

import re
from typing import Any

TREE_SITTER_AVAILABLE = False
try:  # pragma: no cover - environment dependent
    from tree_sitter import Language, Parser
    from tree_sitter_c import language as c_language
    from tree_sitter_cpp import language as cpp_language

    _LANG_CACHE: dict[str, Any] = {}

    def _make_language(lang_fn) -> Any:
        try:
            return Language(lang_fn())
        except TypeError:  # newer API: Language(abi, name) not used here
            return lang_fn()

    def get_language(name: str):
        if name in _LANG_CACHE:
            return _LANG_CACHE[name]
        if name in ("c", "h"):
            lang = _make_language(c_language)
        elif name in ("cpp", "cxx", "hpp", "cc", "c++"):
            lang = _make_language(cpp_language)
        else:
            return None
        _LANG_CACHE[name] = lang
        return lang

    def get_parser(name: str):
        lang = get_language(name)
        if lang is None:
            return None
        try:
            parser = Parser(language=lang)
        except TypeError:
            parser = Parser()
            parser.set_language(lang)
        return parser

    TREE_SITTER_AVAILABLE = True
except Exception:  # pragma: no cover
    TREE_SITTER_AVAILABLE = False

    def get_language(name: str):  # type: ignore
        return None

    def get_parser(name: str):  # type: ignore
        return None


# Map file extensions -> internal language tag.
# NOTE: tree-sitter grammars are only bundled for c/cpp, so ast_pattern rules
# only work for those. python/javascript are indexed (so regex rules can run on
# them) but have no tree-sitter parser — ast_pattern/semantic matchers skip them.
EXT_LANG = {
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cxx": "cpp", ".cc": "cpp", ".hpp": "cpp", ".h++": "cpp",
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "javascript", ".tsx": "javascript",  # TS shares the JS rule pack for now
}


def lang_for_path(path: str) -> str | None:
    ext = re.sub(r"^.*(\.[^.]+)$", r"\1", path).lower()
    return EXT_LANG.get(ext)
