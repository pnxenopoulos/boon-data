"""Read the text KV3 and localization KV1 formats used by GameTracking.

Typed KV3 values remain explicit so catalog JSON does not discard resource or
subclass annotations. Unsupported/malformed input fails instead of yielding a
partial catalog; this is not a reader for binary KV3.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Never

_TOKEN = re.compile(
    r"(?P<skip>\s+|//[^\n]*|/\*.*?\*/|<!--.*?-->)"
    r'|(?P<string>"(?:\\.|[^"\\])*")'
    r"|(?P<punct>[{}\[\]=,:])"
    r'|(?P<bare>[^\s{}\[\]=,:"<>]+)',
    re.DOTALL,
)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


@dataclass(frozen=True)
class TypedValue:
    kind: str
    value: Any


def unwrap(value: Any) -> Any:
    """Read a typed value without altering its stored representation."""
    return value.value if isinstance(value, TypedValue) else value


def to_json(value: Any) -> str:
    def encode(item: TypedValue) -> dict:
        if not isinstance(item, TypedValue):
            raise TypeError(f"cannot serialize {type(item).__name__}")
        return {"$type": item.kind, "$value": item.value}

    return json.dumps(
        value,
        default=encode,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


class _Reader:
    def __init__(self, text: str, kv1: bool):
        self.text = text.lstrip("\ufeff")
        self.kv1 = kv1
        self.position = 0
        self.token = None
        self.advance()

    def fail(self, message: str) -> Never:
        line = self.text.count("\n", 0, self.position) + 1
        raise ValueError(f"{message} near line {line}")

    def advance(self) -> None:
        while self.position < len(self.text):
            match = _TOKEN.match(self.text, self.position)
            if match is None:
                self.fail("unsupported KeyValues syntax")
            self.position = match.end()
            if match.lastgroup != "skip":
                self.token = (match.lastgroup, match.group())
                return
        self.token = None

    def take(self, text: str) -> None:
        if self.token != ("punct", text):
            self.fail(f"expected {text!r}, got {self.token!r}")
        self.advance()

    def string(self) -> str:
        if self.token is None or self.token[0] not in ("string", "bare"):
            self.fail("expected a key or string")
        kind, text = self.token
        self.advance()
        if kind == "string":
            return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m[1], m[0]), text[1:-1])
        return text

    def object(self, wrapped: bool = True) -> dict:
        if wrapped:
            self.take("{")
        result = {}
        while self.token is not None and self.token != ("punct", "}"):
            key = self.string()
            if key in result:
                self.fail(f"duplicate key {key!r}")
            if not self.kv1:
                self.take("=")
            result[key] = self.value()
            if self.token == ("punct", ","):
                self.advance()
        if wrapped:
            self.take("}")
        return result

    def value(self) -> Any:
        if self.token == ("punct", "{"):
            return self.object()
        if self.token == ("punct", "[") and not self.kv1:
            self.advance()
            values = []
            while self.token is not None and self.token != ("punct", "]"):
                values.append(self.value())
                if self.token == ("punct", ","):
                    self.advance()
            self.take("]")
            return values
        quoted = self.token is not None and self.token[0] == "string"
        value = self.string()
        if self.kv1 or quoted:
            return value
        if self.token == ("punct", ":"):
            self.advance()
            return TypedValue(value, self.value())
        if value in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[value]
        if _NUMBER.fullmatch(value):
            return float(value) if any(c in value for c in ".eE") else int(value)
        return value


def parse(text: str, *, kv1: bool = False) -> dict:
    reader = _Reader(text, kv1)
    result = reader.object(wrapped=not kv1)
    if reader.token is not None:
        reader.fail("unexpected trailing content")
    return result
