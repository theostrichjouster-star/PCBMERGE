"""A reader for the S-expression files KiCad writes.

KiCad stores designs as nested lists rather than XML, so before anything can be
converted it has to be parsed.  The grammar is small: parentheses, bare atoms,
and double-quoted strings with backslash escapes.
"""

from __future__ import annotations

from typing import Iterator

Node = list  # a parsed form is a list whose first item is usually its tag


class SexpError(Exception):
    """Raised when a file is not readable as S-expressions."""


def parse(text: str) -> Node:
    """Parse one whole document into nested lists."""
    forms, index = _parse_forms(text, 0, top=True)
    if not forms:
        raise SexpError("no S-expression found")
    return forms[0]


def _parse_forms(text: str, index: int, top: bool = False) -> tuple[list, int]:
    out: list = []
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
        elif char == "(":
            form, index = _parse_forms(text, index + 1)
            out.append(form)
            if top:
                return out, index
        elif char == ")":
            return out, index + 1
        elif char == '"':
            token, index = _parse_string(text, index + 1)
            out.append(token)
        else:
            start = index
            while index < length and not text[index].isspace() and text[index] not in "()":
                index += 1
            out.append(text[start:index])
    if not top:
        raise SexpError("unbalanced parentheses")
    return out, index


def _parse_string(text: str, index: int) -> tuple[str, int]:
    chunks: list[str] = []
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            nxt = text[index + 1]
            chunks.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            index += 2
        elif char == '"':
            return "".join(chunks), index + 1
        else:
            chunks.append(char)
            index += 1
    raise SexpError("unterminated string")


# --------------------------------------------------------------------------
# navigation
# --------------------------------------------------------------------------

def tag(node) -> str:
    """The head of a form, or an empty string for a bare atom."""
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return ""


def children(node, name: str) -> Iterator[Node]:
    """Every direct child form with the given tag."""
    if not isinstance(node, list):
        return
    for item in node[1:]:
        if tag(item) == name:
            yield item


def first(node, name: str) -> Node | None:
    for item in children(node, name):
        return item
    return None


def descendants(node, name: str) -> Iterator[Node]:
    """Every form with the given tag, at any depth."""
    if not isinstance(node, list):
        return
    if tag(node) == name:
        yield node
    for item in node:
        if isinstance(item, list):
            yield from descendants(item, name)


def value(node, name: str, index: int = 1, default=None):
    """One positional value from a named child, e.g. the 1 in `(width 1)`."""
    found = first(node, name)
    if found is None or len(found) <= index:
        return default
    return found[index]


def number(node, name: str, index: int = 1, default: float = 0.0) -> float:
    return as_float(value(node, name, index), default)


def as_float(raw, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def atoms(node) -> list[str]:
    """The bare (non-list) items of a form, after its tag."""
    return [item for item in node[1:] if isinstance(item, str)]
