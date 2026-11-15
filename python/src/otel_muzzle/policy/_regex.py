# SPDX-License-Identifier: Apache-2.0
"""The regex predicate: a subset of RE2 syntax, checked and translated for ``re``.

The specification names RE2 syntax. ``re`` is a backtracking engine with a different syntax, so a pattern is read
token by token against an allowlist, and rewritten where ``re`` would give a construct another meaning: ``$`` matches
only at the end of the text, ``\\s`` is ``[\\t\\n\\f\\r ]``, and the classes are ASCII. Whatever RE2 lacks
(backreferences, lookaround) or lies outside the subset is an error. See docs/policy-conformance.md#regex.
"""

from __future__ import annotations

import re
import string
from typing import Final

__all__ = ["compile_regex"]

_FLAGS: Final = re.compile(r"\(\?([ims]+)\)")
_REPEAT: Final = re.compile(r"\{([0-9]+)(?:(,)([0-9]*))?\}")
_MAX_REPEAT: Final = 1000
_PUNCTUATION: Final = frozenset(string.punctuation)
_HEX: Final = frozenset("0123456789abcdefABCDEF")
_CLASS_ESCAPES: Final = frozenset("dDwW")
_CONTROL: Final = {"t": "\t", "n": "\n", "r": "\r", "f": "\f", "v": "\v"}
_SPACE: Final = "\\t\\n\\f\\r\\x20"
_DOUBLED: Final = frozenset("-&|~")


class _Translator:
    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.pos = 0
        self.flags = re.ASCII
        self.multiline = False
        self.out: list[str] = []

    def fail(self, reason: str, at: int | None = None) -> ValueError:
        return ValueError(f"unsupported regex: {reason} at {self.pos if at is None else at}")

    def translate(self) -> re.Pattern[str]:
        pattern = self.pattern
        leading = _FLAGS.match(pattern)
        if leading:
            for flag in leading.group(1):
                self.flags |= {"i": re.IGNORECASE, "s": re.DOTALL, "m": re.MULTILINE}[flag]
            self.multiline = "m" in leading.group(1)
            self.pos = leading.end()
        depth = 0
        atom = False  # whether the previous token can take a quantifier
        while self.pos < len(pattern):
            char = pattern[self.pos]
            if char == "\\":
                atom = self.escape()
            elif char == "[":
                self.char_class()
                atom = True
            elif char == "(":
                if pattern.startswith("(?:", self.pos):
                    self.emit("(?:", 3)
                elif pattern.startswith("(?", self.pos):
                    raise self.fail("group syntax")
                else:
                    self.emit("(", 1)
                depth += 1
                atom = False
            elif char == ")":
                if depth == 0:
                    raise self.fail("unbalanced parenthesis")
                depth -= 1
                self.emit(")", 1)
                atom = True
            elif char == "|":
                self.emit("|", 1)
                atom = False
            elif char in "*+?" or char == "{":
                if not atom:
                    raise self.fail("nothing to repeat")
                self.quantifier()
                atom = False
            elif char in "}]":
                raise self.fail(f"unescaped {char}")
            elif char == "^":
                self.emit("^", 1)
                atom = False
            elif char == "$":
                self.emit("$" if self.multiline else "\\Z", 1)
                atom = False
            elif char == ".":
                self.emit(".", 1)
                atom = True
            else:
                self.emit(_literal(char), 1)
                atom = True
        if depth:
            raise self.fail("unbalanced parenthesis")
        # The translation holds nothing `re` warns about: punctuation is written as \xHH, and a class that opens
        # with `[` or holds `--`, `&&`, `||` or `~~` is rejected above. The warning filters are left alone, because
        # they belong to the whole process.
        try:
            return re.compile("".join(self.out), self.flags)
        except (re.error, OverflowError, RecursionError) as error:
            raise ValueError(f"unsupported regex: {error}") from error

    def emit(self, text: str, width: int) -> None:
        self.out.append(text)
        self.pos += width

    def quantifier(self) -> None:
        pattern = self.pattern
        if pattern[self.pos] == "{":
            repeat = _REPEAT.match(pattern, self.pos)
            if repeat is None:
                raise self.fail("repetition")
            low = int(repeat.group(1))
            high = int(repeat.group(3)) if repeat.group(3) else low
            if low > _MAX_REPEAT or high > _MAX_REPEAT or (repeat.group(3) and high < low):
                raise self.fail("repetition count")
            self.emit(repeat.group(0), len(repeat.group(0)))
        else:
            self.emit(pattern[self.pos], 1)
        if pattern.startswith("?", self.pos):
            self.emit("?", 1)

    def single(self) -> str | None:
        """Reads an escape that stands for one character, and returns its translation. None: not such an escape."""
        pattern = self.pattern
        char = pattern[self.pos + 1 : self.pos + 2]
        if char in _CONTROL:
            self.pos += 2
            return "\\" + char
        if char == "x":
            digits = pattern[self.pos + 2 : self.pos + 4]
            if len(digits) != 2 or not set(digits) <= _HEX or int(digits, 16) > 0x7F:
                raise self.fail("\\x escape")
            self.pos += 4
            return "\\x" + digits.lower()
        if char in _PUNCTUATION:
            self.pos += 2
            return _literal(char)
        return None

    def escape(self) -> bool:
        """An escape outside a class. Returns whether it can take a quantifier."""
        char = self.pattern[self.pos + 1 : self.pos + 2]
        if char in _CLASS_ESCAPES:
            self.emit("\\" + char, 2)
            return True
        if char == "s":
            self.emit(f"[{_SPACE}]", 2)
            return True
        if char == "S":
            self.emit(f"[^{_SPACE}]", 2)
            return True
        if char in ("b", "B"):
            self.emit("\\" + char, 2)
            return False
        single = self.single()
        if single is None:
            raise self.fail("escape")
        self.out.append(single)
        return True

    def char_class(self) -> None:
        pattern = self.pattern
        start = self.pos
        self.pos += 1
        out = ["["]
        if pattern.startswith("^", self.pos):
            out.append("^")
            self.pos += 1
        if pattern.startswith("]", self.pos):
            raise self.fail("character class starting with ]")
        while True:
            if self.pos >= len(pattern):
                raise self.fail("unterminated character class", start)
            if pattern[self.pos] == "]":
                self.pos += 1
                break
            low = self.class_item(out)
            if low is None:
                continue
            if pattern.startswith("-", self.pos) and not pattern.startswith("]", self.pos + 1):
                if self.pos + 1 >= len(pattern):
                    raise self.fail("unterminated character class", start)
                self.pos += 1
                high = self.class_item(None)
                if high is None or _code(high) < _code(low):
                    raise self.fail("character class range")
                out.append(f"{low}-{high}")
            else:
                out.append(low)
        out.append("]")
        self.out.append("".join(out))

    def class_item(self, out: list[str] | None) -> str | None:
        """Reads one class member. Returns a single character's translation, or None after appending a set to out."""
        pattern = self.pattern
        char = pattern[self.pos]
        if char == "\\":
            escaped = pattern[self.pos + 1 : self.pos + 2]
            if escaped in _CLASS_ESCAPES or escaped == "s":
                if out is None:
                    raise self.fail("character class range")
                out.append(_SPACE if escaped == "s" else "\\" + escaped)
                self.pos += 2
                if pattern.startswith("-", self.pos) and not pattern.startswith("]", self.pos + 1):
                    raise self.fail("character class range")
                return None
            single = self.single()
            if single is None:
                raise self.fail("escape in character class")
            return single
        if char == "[":
            raise self.fail("unescaped [ in character class")
        if char in _DOUBLED and pattern.startswith(char, self.pos + 1):
            raise self.fail(f"{char}{char} in character class")
        self.pos += 1
        return _literal(char)


def _literal(char: str) -> str:
    """A character that stands for itself, written so that no engine reads it as syntax."""
    return f"\\x{ord(char):02x}" if char in _PUNCTUATION else char


def _code(item: str) -> int:
    if item.startswith("\\x"):
        return int(item[2:], 16)
    if item.startswith("\\"):
        return ord(_CONTROL[item[1]])
    return ord(item)


def compile_regex(pattern: str) -> re.Pattern[str]:
    """Compiles an RE2-syntax pattern from the supported subset. Use ``search`` on the result.

    Raises:
        ValueError: the pattern uses a construct RE2 lacks, or one outside the supported subset.
    """
    return _Translator(pattern).translate()
