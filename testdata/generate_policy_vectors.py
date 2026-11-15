#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Writes the shared policy-mode cases every otel-muzzle package's tests check.

policy_threshold_vectors.json: the rejection threshold for a percentage and a precision, and for
SAMPLING_MODE_PROPORTIONAL the threshold given an arriving one.

policy_tracestate_vectors.json: how the `ot` tracestate member is read (the `th` and `rv` sub-keys) and where the
56-bit randomness comes from.

policy_regex_vectors.json: which patterns the regex predicate accepts, and what they match.

policy_decision_vectors.json: policy sets as text, the apply status of each policy, and for a span whether the
sampler keeps it.

This script holds the reference arithmetic only. What a matcher matches is declared by hand below, so that the
expectations do not depend on any one regular expression engine. See docs/design.md#policy-mode.

The output is deterministic: rerun this script after changing it and commit all five files.
"""

from __future__ import annotations

import json
import math
import random
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXAMPLE_POLICY = HERE.parent / "examples" / "policies" / "muzzle.json"

MAX = 1 << 56
DEFAULT_PRECISION = 4
HEX_TH = re.compile(r"[0-9a-f]{1,14}")
HEX_RV = re.compile(r"[0-9a-f]{14}")
LOCAL_SPAN_ID = "53995c3f42cd8ad8"
REMOTE_SPAN_ID = "00f067aa0ba902b7"
HIGH = "4bf92f3577b34da6a3"  # trace-id bytes 0-8; bytes 9-15 are the randomness


# --- reference arithmetic ---------------------------------------------------------------------------------------


def round_half_up(value: Fraction) -> int:
    floor = value.numerator // value.denominator
    return floor + (1 if value - floor >= Fraction(1, 2) else 0)


def threshold(probability: float, precision: int) -> int:
    """The rejection threshold T for a sampling probability: a span is kept when its randomness R >= T.

    T = 2**56 - round(probability * 2**56), then rounded half up to `precision` hexadecimal digits, counted after
    the leading "f" digits (the OpenTelemetry tracestate probability sampling rule), and to 14 digits at most.
    """
    if probability == 0:
        return MAX
    if probability == 1:
        return 0
    t = MAX - round_half_up(Fraction(probability) * MAX)
    _, exponent = math.frexp(probability)
    digits = min(14, precision + (-exponent) // 4)
    shift = 4 * (14 - digits)
    if shift > 0:
        t = ((t + (1 << (shift - 1))) >> shift) << shift
    return t


def proportional(arriving: int, percentage: float, precision: int) -> int:
    """SAMPLING_MODE_PROPORTIONAL: the arriving probability times the percentage, as a threshold."""
    return threshold(float(MAX - arriving) / MAX * (percentage / 100), precision)


def parse_ot(ot: str | None) -> tuple[int | None, int | None]:
    """(rv, th) from the value of the `ot` tracestate member. An invalid sub-key reads as absent."""
    if ot is None or len(ot) > 256:
        return None, None
    rv = th = None
    for member in ot.split(";"):
        if member.startswith("th:"):
            value = member[3:]
            th = int(value.ljust(14, "0"), 16) if HEX_TH.fullmatch(value) else None
        elif member.startswith("rv:"):
            value = member[3:]
            rv = int(value, 16) if HEX_RV.fullmatch(value) else None
    return rv, th


def erase_th(pairs: list[list[str]]) -> dict[str, str]:
    """The tracestate a policy DROP returns: the same members, with `th` taken out of `ot`."""
    out = {}
    for key, value in pairs:
        if key == "ot":
            value = ";".join(member for member in value.split(";") if not member.startswith("th:"))
            if not value:
                continue
        out[key] = value
    return out


def decide(matching: list[tuple[float, str, int]], trace_id: str, tracestate: list[list[str]]) -> bool:
    """Whether a span is kept, given the (percentage, mode, precision) of every policy that matches it."""
    rv, arriving = parse_ot(dict(tracestate).get("ot"))
    randomness = rv if rv is not None else int(trace_id[18:], 16)
    if arriving is not None and randomness < arriving:
        arriving = None  # inconsistent with the randomness: ignored
    limit = None
    for percentage, mode, precision in matching:
        t = threshold(percentage / 100, precision)
        if arriving is not None:
            if mode == "PROPORTIONAL":
                t = proportional(arriving, percentage, precision)
            t = max(t, arriving)
        limit = t if limit is None else max(limit, t)
    return limit is None or randomness >= limit


def hex14(value: int) -> str:
    """A threshold as text: 14 hexadecimal digits, or "100000000000000" for 2**56 (keep nothing)."""
    return f"{value:014x}"


def th_text(value: int) -> str | None:
    """The `th` encoding of a threshold: trailing zeros removed. 2**56 has none."""
    if value == MAX:
        return None
    return hex14(value).rstrip("0") or "0"


# The OpenTelemetry specification's table (tracestate-probability-sampling.md): 1-in-N at precision 3, 4 and 5.
SPEC_TABLE = {
    1: ("0", "0", "0"),
    2: ("8", "8", "8"),
    3: ("aab", "aaab", "aaaab"),
    4: ("c", "c", "c"),
    5: ("ccd", "cccd", "ccccd"),
    8: ("e", "e", "e"),
    10: ("e66", "e666", "e6666"),
    16: ("f", "f", "f"),
    100: ("fd71", "fd70a", "fd70a4"),
    1000: ("ffbe7", "ffbe77", "ffbe76d"),
    10000: ("fff972", "fff9724", "fff97247"),
    100000: ("ffff584", "ffff583a", "ffff583a5"),
    1000000: ("ffffef4", "ffffef39", "ffffef391"),
}


def check_spec_table() -> None:
    for n, expected in SPEC_TABLE.items():
        for precision, text in zip((3, 4, 5), expected):
            got = th_text(threshold(1 / n, precision))
            assert got == text, f"1-in-{n} at precision {precision}: {got} != {text}"


# --- output ------------------------------------------------------------------------------------------------------


def write(name: str, description: str, sections: dict[str, list[Any] | dict[str, Any]]) -> None:
    """One case per line, like ratio_vectors.json."""
    lines = ["{", f' "description": {json.dumps(description)},']
    names = list(sections)
    for index, section in enumerate(names):
        body = sections[section]
        comma = "," if index < len(names) - 1 else ""
        if isinstance(body, dict):
            lines.append(f' "{section}": {{')
            keys = list(body)
            lines += [
                f"  {json.dumps(key)}: {json.dumps(body[key])}{',' if i < len(keys) - 1 else ''}"
                for i, key in enumerate(keys)
            ]
            lines.append(f" }}{comma}")
        else:
            lines.append(f' "{section}": [')
            lines += [f"  {json.dumps(case)}{',' if i < len(body) - 1 else ''}" for i, case in enumerate(body)]
            lines.append(f" ]{comma}")
    lines += ["}", ""]
    (HERE / name).write_text("\n".join(lines))


# --- thresholds --------------------------------------------------------------------------------------------------

PERCENTAGES = [
    0, 100, 50, 25, 12.5, 10, 6.25, 5, 1, 0.1, 0.01, 0.001, 1e-9, 1e-15, 1e-30,
    100 / 3, 20, 75, 90, 93.75, 94, 99, 99.9, 99.99, 99.999999, 33.3, 66.6, 2.5, 7,
]  # fmt: skip
PRECISIONS = [1, 2, 3, 4, 5, 8, 13, 14]


def threshold_vectors(rng: random.Random) -> dict[str, list[Any]]:
    cases = []
    for percentage in PERCENTAGES + [round(rng.uniform(0, 100), 6) for _ in range(12)]:
        for precision in PRECISIONS:
            t = threshold(percentage / 100, precision)
            cases.append({"percentage": percentage, "precision": precision, "threshold": hex14(t), "th": th_text(t)})
    arriving_values = [0, 1 << 55, 0xC << 52, 0xE666 << 40, 0xF << 52, 0xFD70A << 36, 0xFFFFFFFFFFFFFF, 0x123456789ABCDE]
    proportional_cases = []
    for arriving in arriving_values + [rng.getrandbits(56) for _ in range(6)]:
        for percentage in (0, 100, 50, 10, 1, 100 / 3, 0.001):
            for precision in (1, 4, 14):
                t = proportional(arriving, percentage, precision)
                proportional_cases.append(
                    {"arriving": hex14(arriving), "percentage": percentage, "precision": precision, "threshold": hex14(t)}
                )
    return {"cases": cases, "proportional": proportional_cases}


# --- tracestate --------------------------------------------------------------------------------------------------

OT_VALUES = [
    None, "", "th:8", "th:0", "th:e666", "th:fd70a", "th:ffffffffffffff", "th:00000000000001",
    "rv:00000000000000", "rv:ffffffffffffff", "rv:6e6d1a75832a2f", "th:c;rv:d0000000000000", "rv:d0000000000000;th:c",
    "th:c;rv:d0000000000000;ext:1", "ext:1;th:c", "th:", "th:E666", "th:0x8", "th:g", "th:123456789abcdef",
    "rv:D0000000000000", "rv:d000000000000", "rv:d00000000000000", "rv:", "rv:zz000000000000",
    "th:8;th:c", "th:8;th:zz", "rv:00000000000000;rv:ffffffffffffff", "rv:00000000000000;rv:1",
    "th:8 ", " th:8", "th: 8", "TH:8", "th=8", "p:2;r:5", ";;", "th:8;", ";th:8",
    "th:8;" + "x" * 251, "th:8;" + "x" * 252,
]  # fmt: skip


def tracestate_vectors(rng: random.Random) -> dict[str, list[Any]]:
    ids = [HIGH + "00000000000000", HIGH + "ffffffffffffff", HIGH + "e6660000000000"]
    ids.append((bytes(4) + rng.randbytes(12)).hex())  # zero bytes 0-3
    ids.append((rng.randbytes(4) + bytes(4) + rng.randbytes(8)).hex())  # Datadog 128-bit
    ids.append((bytes(8) + rng.randbytes(8)).hex())  # a 64-bit id, zero-padded: 7 of its 8 bytes are the randomness
    ids.append("5f2b7a10" + rng.randbytes(12).hex())  # X-Ray
    ids += [rng.randbytes(16).hex() for _ in range(3)]
    cases = []
    for index, ot in enumerate(OT_VALUES):
        trace_id = ids[index % len(ids)]
        rv, th = parse_ot(ot)
        cases.append({
            "trace_id": trace_id,
            "ot": ot,
            "rv": None if rv is None else hex14(rv),
            "th": None if th is None else hex14(th),
            "randomness": hex14(rv if rv is not None else int(trace_id[18:], 16)),
        })  # fmt: skip
    for trace_id in ids:
        cases.append({"trace_id": trace_id, "ot": None, "rv": None, "th": None, "randomness": trace_id[18:]})
    return {"cases": cases}


# --- regular expressions -----------------------------------------------------------------------------------------

# (pattern, strings it matches, strings it does not). RE2 semantics: an unanchored search; ^ and $ are the ends of
# the text unless (?m); "." matches everything but "\n" unless (?s); \d, \w, \s and \b are ASCII; \s is [\t\n\f\r ].
REGEX_VALID: list[tuple[str, list[str], list[str]]] = [
    ("abc", ["abc", "xxabcxx"], ["ab", "ABC", ""]),
    ("", ["", "anything"], []),
    ("^GET /(healthz|readyz)$", ["GET /healthz", "GET /readyz"], ["GET /healthz\n", "xGET /healthz", "x\nGET /healthz", "GET /live"]),
    ("^$", [""], ["\n", "a"]),
    ("a$", ["a", "ba"], ["a\n", "ab"]),
    ("(?i)^get ", ["GET x", "get x", "Get x"], [" get x", "post"]),
    ("(?m)^b$", ["b", "a\nb\nc", "a\nb"], ["ab", "a\nbb"]),
    ("(?im)^B$", ["a\nb"], ["ab"]),
    ("a.b", ["axb", "a\rb", "a\u2028b", "a b"], ["a\nb", "ab"]),
    ("(?s)a.b", ["a\nb", "axb"], ["ab"]),
    (r"a\sb", ["a b", "a\tb", "a\nb", "a\rb", "a\fb"], ["a\vb", "a\u00a0b", "a\u2003b", "ab"]),
    (r"a\Sb", ["axb", "a\vb", "a\u00a0b"], ["a b", "a\tb"]),
    (r"[\s,]+x", [" ,x", "\tx", ",x"], ["x", "\vx"]),
    (r"\d+", ["a12", "0"], ["abc", "\u0663", ""]),
    (r"^\D+$", ["abc", "\u0663"], ["a1"]),
    (r"^\w+@", ["a@", "a_1@"], ["é@", "@", "-@"]),
    (r"^\W$", ["-", "é"], ["a", "_"]),
    (r"\bfoo\b", ["foo", "a foo b", "(foo)"], ["afoob", "foo1"]),
    (r"\Bfoo", ["afoo"], ["foo", " foo"]),
    ("colou?r", ["color", "colour"], ["colouur"]),
    ("^a+?$", ["a", "aaa"], ["", "b"]),
    ("^a*$", ["", "aaaa"], ["b"]),
    ("^a??b$", ["b", "ab"], ["aab"]),
    ("^(?:ab)+c$", ["abc", "ababc"], ["c", "abac"]),
    ("^a{2}$", ["aa"], ["a", "aaa"]),
    ("^a{2,}$", ["aa", "aaaa"], ["a"]),
    ("^a{2,3}?$", ["aa", "aaa"], ["a", "aaaa"]),
    ("^a{0,1000}$", ["", "a"], ["b"]),
    ("a|b", ["a", "b", "xb"], ["c"]),
    ("^(|a)$", ["", "a"], ["b"]),
    ("^()$", [""], ["a"]),
    ("^[a-c]x$", ["ax", "cx"], ["dx", "Ax"]),
    ("^[^a-c]x$", ["dx", "\nx"], ["ax"]),
    (r"^[\]\\\-\^\[]+$", ["]", "\\", "-", "^", "[", "]\\-^["], ["a", ""]),
    ("^[a^$.|?*+(){}-]+$", ["a^$.|?*+(){}-"], ["b"]),
    ("^[-a]$", ["-", "a"], ["b"]),
    (r"^[\d\w]$", ["1", "a"], ["-"]),
    (r"^[\D]$", ["a"], ["1"]),
    (r"^[\t\n\r\f\v\x41]$", ["\t", "\n", "\r", "\f", "\v", "A"], ["B", " "]),
    (r"^\x41\x2e$", ["A."], ["Ax"]),
    (r"^\t\n\r\f\v$", ["\t\n\r\f\v"], ["tnrfv"]),
    (r"^a\.b\/c\-d\_e\#f$", ["a.b/c-d_e#f"], ["axb/c-d_e#f"]),
    (r"\$\^\*\+\?\(\)\[\]\{\}\|\\", ["$^*+?()[]{}|\\"], ["$"]),
    ("café", ["un café"], ["cafe"]),
    ("a b#c", ["a b#c"], ["ab#c"]),
    (r"(^|,)\s*muzzle=verbose\s*(,|$)", ["muzzle=verbose", "vendor=x,muzzle=verbose", "muzzle=verbose,vendor=x", "a=b, muzzle=verbose ,c=d"], ["muzzle=verbose1", "xmuzzle=verbose", "muzzle=VERBOSE", "muzzle=quiet", ""]),
    ("^(SERVER|CONSUMER)$", ["SERVER", "CONSUMER"], ["INTERNAL", "SERVERS"]),
    ("(?s)", ["", "x"], []),
]  # fmt: skip

REGEX_INVALID = [
    "(", ")", "a)", "(a", "(?:a", "[a", "[", "[]", "[]a]", "[^]a]", "a]", "a}", "a{", "a{x}", "a{,2}", "a{2,1}",
    "a{1001}", "a{1,1001}", "*a", "+a", "?a", "a**", "a*+", "a++", "a?+", "a{1}{2}", "a+?+", "a|*", "(*a)", "^*", "$+",
    r"\b+", "(?=a)", "(?!a)", "(?<=a)b", "(?<!a)b", "(?<name>a)", "(?P<name>a)", "(?>a)", "(?#c)", "(?i:a)", "(?-i)a",
    "a(?i)b", "(?x)a", "(?U)a", "(?is", "(?)", r"(a)\1", r"\0", r"\07", r"\k<n>", r"\A", r"\z", r"\Z", r"\G", r"\K",
    r"\pL", r"\p{L}", r"\PL", r"\Qa\E", r"\C", r"\a", r"\e", r"\h", r"\R", r"\X", r"\x4", r"\x{41}", r"\xzz", r"\x80",
    r"\xff", r"\u0041", "\\", "a\\", r"\é", "[[:alpha:]]", "[a[b]]", r"[\S]", r"[\b]", r"[\B]", "[a&&b]", "[a--b]",
    "[a||b]", "[a~~b]", "[z-a]", r"[\d-z]", r"[a-\d]", r"[\q]", "[a\\",
]  # fmt: skip


def regex_vectors() -> dict[str, list[Any]]:
    valid = [{"pattern": pattern, "match": match, "no_match": no_match} for pattern, match, no_match in REGEX_VALID]
    return {"valid": valid, "invalid": [{"pattern": pattern} for pattern in REGEX_INVALID]}


# --- decisions ---------------------------------------------------------------------------------------------------


def record(field: str) -> dict[str, Any]:
    return {"recordField": f"SPAN_RECORD_FIELD_{field}"}


def attribute(*path: str) -> dict[str, Any]:
    return {"spanAttribute": {"path": list(path)}}


def matcher(target: dict[str, Any], predicate: dict[str, Any], negate: bool | None = None) -> dict[str, Any]:
    out = {"target": target, "predicate": predicate}
    if negate is not None:
        out["negate"] = negate
    return out


def sampling(policy_id: str, matches: list[Any] | None, percentage: Any = 0, **config: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"sampling": {"percentage": percentage, **config}}
    if matches is not None:
        body = {"matches": matches, **body}
    return {"id": policy_id, "spanSampling": body}


def document(*policies: Any) -> str:
    return json.dumps({"policies": list(policies)})


def span(
    randomness: int = 0,
    *,
    kind: str | None = "INTERNAL",
    parent: str = "local",
    name: str = "work",
    tracestate: list[list[str]] | None = None,
    result_tracestate: list[list[str]] | None = None,
    attributes: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id or HIGH + hex14(randomness),
        "name": name,
        "kind": kind,
        "parent": parent,
        "parent_span_id": {"none": None, "local": LOCAL_SPAN_ID, "remote": REMOTE_SPAN_ID}[parent],
        "tracestate": tracestate or [],
        "result_tracestate": result_tracestate,
        "attributes": attributes,
    }


class Decisions:
    def __init__(self) -> None:
        self.sets: dict[str, dict[str, Any]] = {}
        self.cases: list[dict[str, Any]] = []

    def policy_set(self, name: str, text: str, statuses: list[tuple[str, bool]], malformed: bool = False) -> None:
        assert name not in self.sets, name
        self.sets[name] = {
            "text": text,
            "malformed": malformed,
            "statuses": [{"id": policy_id, "applied": applied} for policy_id, applied in statuses],
        }

    def case(self, policy_set: str, name: str, span_: dict[str, Any], keep: bool) -> None:
        assert policy_set in self.sets, policy_set
        case = {"set": policy_set, "name": name, "span": span_, "keep": keep}
        if not keep:
            effective = span_["result_tracestate"]
            case["drop_tracestate"] = erase_th(effective if effective is not None else span_["tracestate"])
        self.cases.append(case)

    def matched(self, policy_set: str, name: str, span_: dict[str, Any], matched: bool) -> None:
        """For a set whose valid policies all sample at 0%: the span is dropped exactly when one matches."""
        self.case(policy_set, name, span_, not matched)

    def sampled(self, policy_set: str, name: str, span_: dict[str, Any], matching: list[tuple[float, str, int]]) -> None:
        """For always-matching policies: the reference arithmetic decides."""
        effective = span_["result_tracestate"] if span_["result_tracestate"] is not None else span_["tracestate"]
        self.case(policy_set, name, span_, decide(matching, span_["trace_id"], effective))


E666 = 0xE666 << 40  # threshold(0.1, 4)
HUGE = 10**400  # a whole number no double holds
INVALID_ESCAPE = (
    '{"policies": [{"id": "x", "spanSampling": {"matches": [{"target": {"recordField": "SPAN_RECORD_FIELD_NAME"}, '
    '"predicate": {"regex": "' + chr(92) + 'd+"}, "negate": true}], "sampling": {"percentage": 0}}}]}'
)
EQ, PROP = "EQUALIZING", "PROPORTIONAL"


def decision_vectors(rng: random.Random) -> dict[str, Any]:
    d = Decisions()

    # The shipped example: what otel-muzzle does, as a policy.
    d.policy_set("muzzle", EXAMPLE_POLICY.read_text(), [("drop-internal-spans", True)])
    assert threshold(0.1, 4) == E666
    verbose = [["muzzle", "verbose"]]
    d.matched("muzzle", "INTERNAL under a local parent, below the threshold", span(E666 - 1), True)
    d.case("muzzle", "INTERNAL under a local parent, at the threshold", span(E666), True)
    d.case("muzzle", "INTERNAL under a local parent, at the top", span(MAX - 1), True)
    d.matched("muzzle", "INTERNAL under a local parent, at zero", span(0), True)
    d.matched("muzzle", "a missing kind reads INTERNAL", span(0, kind=None), True)
    for kind in ("SERVER", "CLIENT", "PRODUCER", "CONSUMER"):
        d.matched("muzzle", f"{kind} is not matched", span(0, kind=kind), False)
    d.matched("muzzle", "a root INTERNAL span has no parent", span(0, parent="none"), False)
    d.matched("muzzle", "INTERNAL under a remote parent is matched: the specification has no selector for it",
              span(0, parent="remote"), True)  # fmt: skip
    d.matched("muzzle", "muzzle=verbose keeps the span", span(0, tracestate=verbose), False)
    d.matched("muzzle", "muzzle=verbose after another member", span(0, tracestate=[["vendor", "x"], *verbose]), False)
    d.matched("muzzle", "muzzle=verbose before another member", span(0, tracestate=[*verbose, ["vendor", "x"]]), False)
    d.matched("muzzle", "muzzle=verbose from the delegate's result",
              span(0, tracestate=[["vendor", "x"]], result_tracestate=verbose), False)  # fmt: skip
    d.matched("muzzle", "the delegate's result replaces the parent's tracestate",
              span(0, tracestate=verbose, result_tracestate=[["vendor", "x"]]), True)  # fmt: skip
    for key, value in (("muzzle", "verbose1"), ("muzzle", "VERBOSE"), ("muzzle", "quiet"), ("xmuzzle", "verbose"),
                       ("vendor", "verbose"), ("muzzle-x", "verbose")):  # fmt: skip
        d.matched("muzzle", f"{key}={value} is not verbose", span(0, tracestate=[[key, value]]), True)
    ten = [(10, PROP, 4)]
    d.sampled("muzzle", "rv replaces the trace id: low rv, high id",
              span(MAX - 1, tracestate=[["ot", "rv:00000000000000"]]), ten)  # fmt: skip
    d.sampled("muzzle", "rv replaces the trace id: high rv, low id", span(0, tracestate=[["ot", "rv:ffffffffffffff"]]), ten)
    d.sampled("muzzle", "an invalid rv is ignored", span(MAX - 1, tracestate=[["ot", "rv:0"]]), ten)
    f333 = proportional(1 << 55, 10, 4)
    assert hex14(f333) == "f3333000000000", hex14(f333)  # 5%: one leading f, so five digits
    for randomness in (f333 - 1, f333, E666, (1 << 55), (1 << 55) - 1):
        d.sampled("muzzle", f"proportional to an arriving th:8, randomness {hex14(randomness)}",
                  span(randomness, tracestate=[["ot", "th:8"], ["vendor", "x"]]), ten)  # fmt: skip
    d.sampled("muzzle", "an arriving threshold above the randomness is ignored",
              span(E666, tracestate=[["ot", "th:f"]]), ten)  # fmt: skip
    d.sampled("muzzle", "th and rv together", span(0, tracestate=[["ot", "th:8;rv:f3333000000000"]]), ten)
    d.sampled("muzzle", "th and rv together, just below", span(0, tracestate=[["ot", "rv:f3332fffffffff;th:8"]]), ten)
    d.sampled("muzzle", "th from the delegate's result",
              span(E666, tracestate=[], result_tracestate=[["ot", "th:8"]]), ten)  # fmt: skip
    for _ in range(8):
        d.sampled("muzzle", "a random trace id", span(trace_id=rng.randbytes(16).hex()), ten)

    # Modes and precedence, with policies that match every span.
    d.policy_set("equalizing", document(sampling("ten", [], 10)), [("ten", True)])
    ten_eq = [(10, EQ, 4)]
    for randomness in (0, E666 - 1, E666, MAX - 1):
        d.sampled("equalizing", f"no arriving threshold, randomness {hex14(randomness)}", span(randomness, kind="SERVER"), ten_eq)
    for th in ("8", "c", "e666", "f", "fd70a"):
        for randomness in (E666 - 1, E666, 0xF << 52, 0xFD70A << 36, MAX - 1):
            d.sampled("equalizing", f"arriving th:{th}, randomness {hex14(randomness)}",
                      span(randomness, parent="remote", kind="SERVER", tracestate=[["ot", f"th:{th}"]]), ten_eq)  # fmt: skip
    d.sampled("equalizing", "a root span with no tracestate", span(0, parent="none", kind="SERVER"), ten_eq)
    d.sampled("equalizing", "a root span with a result tracestate",
              span(MAX - 1, parent="none", kind="SERVER", result_tracestate=[["ot", "th:8"]]), ten_eq)  # fmt: skip

    d.policy_set(
        "precedence",
        document(
            sampling("half", [], 50),
            sampling("five", None, 5, mode="SAMPLING_MODE_EQUALIZING"),
            sampling("no-servers", [matcher(record("KIND"), {"equals": {"stringValue": "SERVER"}})], 0),
            sampling("all", [], 100, mode="SAMPLING_MODE_UNSPECIFIED"),
        ),
        [("half", True), ("five", True), ("no-servers", True), ("all", True)],
    )
    both = [(50, EQ, 4), (5, EQ, 4), (100, EQ, 4)]
    t5 = threshold(0.05, 4)
    for randomness in (0, (1 << 55), t5 - 1, t5, MAX - 1):
        d.sampled("precedence", f"the lowest percentage wins, randomness {hex14(randomness)}", span(randomness), both)
    d.matched("precedence", "0% beats every other policy", span(MAX - 1, kind="SERVER"), True)

    d.policy_set(
        "proportional",
        document(sampling("half", [], 50, mode="SAMPLING_MODE_PROPORTIONAL"), sampling("ten", [], 10)),
        [("half", True), ("ten", True)],
    )
    mixed = [(50, PROP, 4), (10, EQ, 4)]
    for th in ("8", "c", "e", "f8"):
        _, arriving = parse_ot(f"th:{th}")
        assert arriving is not None
        edge = max(proportional(arriving, 50, 4), E666)
        for randomness in (edge - 1, edge, MAX - 1):
            d.sampled("proportional", f"mixed modes with arriving th:{th}, randomness {hex14(randomness)}",
                      span(randomness, tracestate=[["ot", f"th:{th}"]]), mixed)  # fmt: skip

    d.policy_set("all", document(sampling("all", [], 100)), [("all", True)])
    d.sampled("all", "100% keeps a span at zero", span(0), [(100, EQ, 4)])
    d.sampled("all", "100% keeps a span below an arriving threshold", span(0, tracestate=[["ot", "th:8"]]), [(100, EQ, 4)])
    d.policy_set("none", document(sampling("none", [], 0)), [("none", True)])
    d.sampled("none", "0% drops a span at the top", span(MAX - 1), [(0, EQ, 4)])
    d.sampled("none", "0% drops a root span", span(MAX - 1, parent="none", kind="SERVER"), [(0, EQ, 4)])
    d.sampled("none", "a drop takes th out of ot and leaves the rest",
              span(MAX - 1, tracestate=[["vendor", "x"], ["ot", "th:8;rv:ffffffffffffff;ext:1"]]), [(0, EQ, 4)])  # fmt: skip
    d.sampled("none", "a drop removes an ot that held only th",
              span(MAX - 1, tracestate=[["ot", "th:8"], ["vendor", "x"]]), [(0, EQ, 4)])  # fmt: skip

    d.policy_set(
        "precision",
        document(sampling("one", [], 1, samplingPrecision=1), sampling("fine", [], "33.3", sampling_precision="14")),
        [("one", True), ("fine", True)],
    )
    fine = [(1, EQ, 1), (33.3, EQ, 14)]
    t1 = threshold(0.01, 1)
    assert th_text(t1) == "fd", th_text(t1)
    for randomness in (t1 - 1, t1, threshold(0.333, 14)):
        d.sampled("precision", f"precision 1 of 1%, randomness {hex14(randomness)}", span(randomness), fine)

    # Selectors. Every policy samples at 0%, so a span is dropped exactly when a policy matches it.
    d.policy_set(
        "record-fields",
        document(
            sampling("name", [matcher(record("NAME"), {"equals": {"stringValue": "GET /users"}})]),
            sampling("name-regex", [matcher(record("NAME"), {"regex": "^GET /(healthz|readyz)$"})]),
            sampling("trace-id", [matcher(record("TRACE_ID"), {"regex": "^0af7651916cd43dd"})]),
            sampling("parent-id", [matcher(record("PARENT_SPAN_ID"), {"equals": {"stringValue": REMOTE_SPAN_ID}}),
                                   matcher(record("NAME"), {"equals": {"stringValue": "by-parent"}})]),
            sampling("root", [matcher(record("PARENT_SPAN_ID"), {"exists": {}}, True),
                              matcher(record("NAME"), {"equals": {"stringValue": "root-only"}})]),
            sampling("tracestate", [matcher(record("TRACE_STATE"), {"equals": {"stringValue": "a=1,b=2"}})]),
            sampling("any-tracestate", [matcher(record("TRACE_STATE"), {"exists": {}}),
                                        matcher(record("NAME"), {"equals": {"stringValue": "has-tracestate"}})]),
            sampling("kinds", [matcher(record("KIND"), {"regex": "^(PRODUCER|CONSUMER)$"})]),
            sampling("kind-number", [matcher(record("KIND"), {"equals": {"intValue": 1}})]),
            sampling("empty-name", [matcher(record("NAME"), {"exists": {}}, True)]),
            sampling("name-contains", [matcher(record("NAME"), {"contains": {"stringValue": "/admin/"}})]),
            sampling("name-gt", [matcher(record("NAME"), {"gt": {"intValue": 0}})]),
        ),
        [(i, True) for i in ("name", "name-regex", "trace-id", "parent-id", "root", "tracestate", "any-tracestate",
                             "kinds", "kind-number", "empty-name", "name-contains", "name-gt")],
    )  # fmt: skip
    rf = "record-fields"
    d.matched(rf, "nothing matches a plain span", span(), False)
    d.matched(rf, "NAME equals", span(name="GET /users"), True)
    d.matched(rf, "NAME equals is case sensitive", span(name="get /users"), False)
    d.matched(rf, "NAME equals is the whole name", span(name="GET /users/1"), False)
    d.matched(rf, "NAME regex", span(name="GET /readyz"), True)
    d.matched(rf, "NAME regex is anchored by the pattern", span(name="GET /readyz/x"), False)
    d.matched(rf, "TRACE_ID reads 32 lowercase hexadecimal characters", span(trace_id="0af7651916cd43dd8448eb211c80319c"), True)
    d.matched(rf, "TRACE_ID does not match another id", span(trace_id="1af7651916cd43dd8448eb211c80319c"), False)
    d.matched(rf, "PARENT_SPAN_ID equals", span(name="by-parent", parent="remote"), True)
    d.matched(rf, "PARENT_SPAN_ID differs", span(name="by-parent", parent="local"), False)
    d.matched(rf, "PARENT_SPAN_ID of a root is empty", span(name="by-parent", parent="none"), False)
    d.matched(rf, "exists with negate selects a root", span(name="root-only", parent="none"), True)
    d.matched(rf, "exists with negate skips a child", span(name="root-only"), False)
    d.matched(rf, "TRACE_STATE reads the members in order, comma separated", span(tracestate=[["a", "1"], ["b", "2"]]), True)
    d.matched(rf, "TRACE_STATE in another order", span(tracestate=[["b", "2"], ["a", "1"]]), False)
    d.matched(rf, "TRACE_STATE exists", span(name="has-tracestate", tracestate=[["a", "1"]]), True)
    d.matched(rf, "TRACE_STATE does not exist when empty", span(name="has-tracestate"), False)
    d.matched(rf, "TRACE_STATE of the delegate's result", span(name="has-tracestate", result_tracestate=[["a", "1"]]), True)
    d.matched(rf, "KIND regex", span(kind="CONSUMER"), True)
    d.matched(rf, "KIND is text, never a number", span(kind="INTERNAL"), False)
    d.matched(rf, "an empty NAME does not exist", span(name=""), True)
    d.matched(rf, "contains on a record field is a substring", span(name="GET /admin/users"), True)

    d.policy_set(
        "attributes",
        document(
            sampling("string", [matcher(attribute("a.string"), {"equals": {"stringValue": "x"}})]),
            sampling("int", [matcher(attribute("a.int"), {"equals": {"intValue": "200"}})]),
            sampling("int-number", [matcher(attribute("a.int_number"), {"equals": {"intValue": 200}})]),
            sampling("double", [matcher(attribute("a.double"), {"equals": {"doubleValue": 0.5}})]),
            sampling("double-whole", [matcher(attribute("a.double_whole"), {"equals": {"doubleValue": 3}})]),
            sampling("bool", [matcher(attribute("a.bool"), {"equals": {"boolValue": True}})]),
            sampling("nan", [matcher(attribute("a.nan"), {"equals": {"doubleValue": "NaN"}})]),
            sampling("bytes", [matcher(attribute("a.bytes"), {"equals": {"bytesValue": "eA=="}})]),
            sampling("exists", [matcher(attribute("a.exists"), {"exists": {}})]),
            sampling("absent", [matcher(attribute("a.absent"), {"exists": {}}, True),
                                matcher(record("NAME"), {"equals": {"stringValue": "absent"}})]),
            sampling("regex", [matcher(attribute("a.regex"), {"regex": "^5[0-9]{2}$"})]),
            sampling("not-regex", [matcher(attribute("a.not_regex"), {"regex": "^ok$"}, True),
                                   matcher(record("NAME"), {"equals": {"stringValue": "not-regex"}})]),
            sampling("gt", [matcher(attribute("a.gt"), {"gt": {"intValue": 499}})]),
            sampling("gte", [matcher(attribute("a.gte"), {"gte": {"doubleValue": 0.5}})]),
            sampling("lt", [matcher(attribute("a.lt"), {"lt": {"intValue": "-1"}})]),
            sampling("lte", [matcher(attribute("a.lte"), {"lte": {"doubleValue": "1e3"}})]),
            sampling("gt-min", [matcher(attribute("a.gt_min"), {"gte": {"intValue": "-9223372036854775808"}})]),
            sampling("lt-infinity", [matcher(attribute("a.lt_inf"), {"lt": {"doubleValue": "Infinity"}})]),
            sampling("contains", [matcher(attribute("a.contains"), {"contains": {"stringValue": "err"}})]),
            sampling("contains-int", [matcher(attribute("a.contains_int"), {"contains": {"intValue": 7}})]),
            sampling("both", [matcher(attribute("a.one"), {"exists": {}}), matcher(attribute("a.two"), {"exists": {}})]),
            sampling("nested", [matcher(attribute("http", "route"), {"exists": {}})]),
            sampling("not-nested", [matcher(attribute("http", "route"), {"exists": {}}, True),
                                    matcher(record("NAME"), {"equals": {"stringValue": "not-nested"}})]),
        ),
        [(i, True) for i in ("string", "int", "int-number", "double", "double-whole", "bool", "nan", "bytes", "exists",
                             "absent", "regex", "not-regex", "gt", "gte", "lt", "lte", "gt-min", "lt-infinity",
                             "contains", "contains-int", "both", "nested", "not-nested")],
    )  # fmt: skip

    def attr(name: str, attributes: dict[str, Any] | None, matched: bool, span_name: str = "work") -> None:
        d.matched("attributes", name, span(attributes=attributes, name=span_name), matched)

    attr("no attributes", None, False)
    attr("empty attributes", {}, False)
    attr("string equals", {"a.string": "x"}, True)
    attr("string differs", {"a.string": "X"}, False)
    attr("string against a number", {"a.string": 1}, False)
    attr("string against an array holding it", {"a.string": ["x"]}, False)
    attr("int equals an int", {"a.int": 200}, True)
    attr("int equals a whole double", {"a.int": 200.0}, True)
    attr("int against a string", {"a.int": "200"}, False)
    attr("int against a boolean", {"a.int": True}, False)
    attr("int differs", {"a.int": 201}, False)
    attr("int against a fraction", {"a.int": 200.5}, False)
    attr("int given as a JSON number", {"a.int_number": 200}, True)
    attr("double equals", {"a.double": 0.5}, True)
    attr("double differs", {"a.double": 0.25}, False)
    attr("double against a string", {"a.double": "0.5"}, False)
    attr("whole double equals an int", {"a.double_whole": 3}, True)
    attr("bool equals", {"a.bool": True}, True)
    attr("bool differs", {"a.bool": False}, False)
    attr("bool against the number one", {"a.bool": 1}, False)
    attr("bool against a string", {"a.bool": "true"}, False)
    attr("NaN equals nothing", {"a.nan": 1.5}, False)
    attr("bytes never match a start attribute", {"a.bytes": "x"}, False)
    attr("exists with a string", {"a.exists": ""}, True)
    attr("exists with false", {"a.exists": False}, True)
    attr("exists with zero", {"a.exists": 0}, True)
    attr("exists with an empty array", {"a.exists": []}, True)
    attr("negated exists matches when absent", {"a.other": 1}, True, "absent")
    attr("negated exists skips when present", {"a.absent": 1}, False, "absent")
    attr("regex on a string", {"a.regex": "503"}, True)
    attr("regex that does not match", {"a.regex": "404"}, False)
    attr("regex on a number never matches", {"a.regex": 503}, False)
    attr("regex on an array never matches", {"a.regex": ["503"]}, False)
    attr("negated regex matches another string", {"a.not_regex": "bad"}, True, "not-regex")
    attr("negated regex skips a match", {"a.not_regex": "ok"}, False, "not-regex")
    attr("negated regex matches an absent attribute", None, True, "not-regex")
    attr("negated regex matches a number", {"a.not_regex": 5}, True, "not-regex")
    attr("gt above", {"a.gt": 500}, True)
    attr("gt equal", {"a.gt": 499}, False)
    attr("gt with a double", {"a.gt": 499.5}, True)
    attr("gt against a string", {"a.gt": "500"}, False)
    attr("gt against a boolean", {"a.gt": True}, False)
    attr("gt against an array", {"a.gt": [500]}, False)
    attr("gte equal", {"a.gte": 0.5}, True)
    attr("gte below", {"a.gte": 0.25}, False)
    attr("gte with an int", {"a.gte": 1}, True)
    attr("lt below", {"a.lt": -2}, True)
    attr("lt equal", {"a.lt": -1}, False)
    attr("lte equal", {"a.lte": 1000}, True)
    attr("lte above", {"a.lte": 1000.5}, False)
    attr("gte the smallest int64", {"a.gt_min": -9007199254740991}, True)
    attr("lt infinity", {"a.lt_inf": 1.5e300}, True)
    attr("contains a substring", {"a.contains": "an error"}, True)
    attr("contains no substring", {"a.contains": "fine"}, False)
    attr("contains an element", {"a.contains": ["ok", "err"]}, True)
    attr("contains is an equal element, not a substring of one", {"a.contains": ["an error"]}, False)
    attr("contains against a number", {"a.contains": 5}, False)
    attr("contains an int element", {"a.contains_int": [1, 7]}, True)
    attr("contains an int element as a whole double", {"a.contains_int": [7.0]}, True)
    attr("contains no int element", {"a.contains_int": [1, 2]}, False)
    attr("contains an int in a string never matches", {"a.contains_int": "7"}, False)
    attr("contains an int against a string element", {"a.contains_int": ["7"]}, False)
    attr("every matcher must match: one of two", {"a.one": 1}, False)
    attr("every matcher must match: both", {"a.one": 1, "a.two": 2}, True)
    attr("a nested path is absent from flat attributes", {"http.route": "/x", "http": "y"}, False)
    attr("a negated nested path matches", {"http.route": "/x"}, True, "not-nested")

    # Policies that must stay inert. Each would drop the "probe" span if it were applied.
    probe = matcher(record("NAME"), {"equals": {"stringValue": "probe"}})
    kind = record("KIND")
    internal = {"equals": {"stringValue": "INTERNAL"}}

    def bad(policy_id: str, **span_sampling: Any) -> dict[str, Any]:
        return {"id": policy_id, "spanSampling": span_sampling}

    zero = {"percentage": 0}
    invalid: list[Any] = [
        {"spanSampling": {"matches": [probe], "sampling": zero}},
        {"id": "", "spanSampling": {"matches": [probe], "sampling": zero}},
        {"id": 7, "spanSampling": {"matches": [probe], "sampling": zero}},
        sampling("twice", [probe]),
        sampling("twice", [probe]),
        {"id": "no-definition"},
        {"id": "two-definitions", "spanSampling": {"matches": [probe], "sampling": zero}, "logFilter": {}},
        {"id": "both-spellings", "spanSampling": {"matches": [probe], "sampling": zero},
         "span_sampling": {"matches": [probe], "sampling": zero}},
        {"id": "log-filter", "logFilter": {"matches": [], "action": "ACTION_DROP"}},
        {"id": "log-transform", "log_transform": {}},
        {"id": "metric-filter", "metricFilter": {}},
        {"id": "definition-not-an-object", "spanSampling": "x"},
        bad("no-sampling", matches=[probe]),
        bad("no-percentage", matches=[probe], sampling={}),
        bad("null-percentage", matches=[probe], sampling={"percentage": None}),
        bad("percentage-150", matches=[probe], sampling={"percentage": 150}),
        bad("percentage-negative", matches=[probe], sampling={"percentage": -0.5}),
        bad("percentage-nan", matches=[probe], sampling={"percentage": "NaN"}),
        bad("percentage-text", matches=[probe], sampling={"percentage": "ten"}),
        bad("percentage-bool", matches=[probe], sampling={"percentage": True}),
        bad("precision-0", matches=[probe], sampling={"percentage": 0, "samplingPrecision": 0}),
        bad("precision-15", matches=[probe], sampling={"percentage": 0, "samplingPrecision": 15}),
        bad("precision-fraction", matches=[probe], sampling={"percentage": 0, "samplingPrecision": 2.5}),
        bad("precision-both-spellings", matches=[probe],
            sampling={"percentage": 0, "samplingPrecision": 4, "sampling_precision": 4}),
        bad("mode-unknown", matches=[probe], sampling={"percentage": 0, "mode": "SAMPLING_MODE_HASH_SEED"}),
        bad("mode-number", matches=[probe], sampling={"percentage": 0, "mode": 2}),
        bad("sampling-unknown-key", matches=[probe], sampling={"percentage": 0, "hashSeed": 1}),
        bad("unknown-key", matches=[probe], sampling=zero, keep=True),
        bad("matches-not-a-list", matches={}, sampling=zero),
        bad("matches-false", matches=False, sampling=zero),
        bad("mode-false", matches=[probe], sampling={"percentage": 0, "mode": False}),
        bad("percentage-huge", matches=[probe], sampling={"percentage": HUGE}),
        bad("percentage-huge-negative", matches=[probe], sampling={"percentage": -HUGE}),
        bad("precision-huge", matches=[probe], sampling={"percentage": 0, "samplingPrecision": HUGE}),
        bad("matcher-not-an-object", matches=["x"], sampling=zero),
        bad("no-target", matches=[probe, {"predicate": internal}], sampling=zero),
        bad("empty-target", matches=[probe, {"target": {}, "predicate": internal}], sampling=zero),
        bad("two-targets", matches=[probe, matcher({**kind, **attribute("a")}, internal)], sampling=zero),
        bad("no-predicate", matches=[probe, {"target": kind}], sampling=zero),
        bad("empty-predicate", matches=[probe, matcher(kind, {})], sampling=zero),
        bad("two-predicates", matches=[probe, matcher(kind, {**internal, "exists": {}})], sampling=zero),
        bad("unknown-predicate", matches=[probe, matcher(kind, {"glob": "INT*"})], sampling=zero),
        bad("matcher-unknown-key", matches=[{**probe, "caseInsensitive": True}], sampling=zero),
        bad("negate-text", matches=[{**probe, "negate": "yes"}], sampling=zero),
        bad("field-unspecified", matches=[probe, matcher(record("UNSPECIFIED"), {"exists": {}})], sampling=zero),
        bad("field-unknown", matches=[probe, matcher(record("STATUS_CODE"), {"exists": {}})], sampling=zero),
        bad("field-number", matches=[probe, matcher({"recordField": 6}, internal)], sampling=zero),
        bad("field-span-id", matches=[probe, matcher(record("SPAN_ID"), {"exists": {}})], sampling=zero),
        bad("resource-attribute", matches=[probe, matcher({"resourceAttribute": {"path": ["service.name"]}}, {"exists": {}})], sampling=zero),
        bad("scope-attribute", matches=[probe, matcher({"scope_attribute": {"path": ["a"]}}, {"exists": {}})], sampling=zero),
        bad("scope-field", matches=[probe, matcher({"scopeField": "SCOPE_FIELD_NAME"}, {"exists": {}})], sampling=zero),
        bad("empty-path", matches=[probe, matcher({"spanAttribute": {"path": []}}, {"exists": {}})], sampling=zero),
        bad("no-path", matches=[probe, matcher({"spanAttribute": {}}, {"exists": {}})], sampling=zero),
        bad("path-not-strings", matches=[probe, matcher({"spanAttribute": {"path": [1]}}, {"exists": {}})], sampling=zero),
        bad("regex-unbalanced", matches=[probe, matcher(kind, {"regex": "("})], sampling=zero),
        bad("regex-backreference", matches=[probe, matcher(kind, {"regex": r"(a)\1"})], sampling=zero),
        bad("regex-lookahead", matches=[probe, matcher(kind, {"regex": "(?=a)"})], sampling=zero),
        bad("regex-not-text", matches=[probe, matcher(kind, {"regex": 5})], sampling=zero),
        bad("exists-not-an-object", matches=[probe, matcher(kind, {"exists": True})], sampling=zero),
        bad("equals-empty", matches=[probe, matcher(kind, {"equals": {}})], sampling=zero),
        bad("equals-two-values", matches=[probe, matcher(kind, {"equals": {"stringValue": "a", "intValue": 1}})], sampling=zero),
        bad("equals-wrong-type", matches=[probe, matcher(kind, {"equals": {"stringValue": 5}})], sampling=zero),
        bad("int-fraction", matches=[probe, matcher(kind, {"equals": {"intValue": 1.5}})], sampling=zero),
        bad("int-text", matches=[probe, matcher(kind, {"equals": {"intValue": "1.0"}})], sampling=zero),
        bad("int-too-large", matches=[probe, matcher(kind, {"equals": {"intValue": "9223372036854775808"}})], sampling=zero),
        bad("int-unsafe-number", matches=[probe, matcher(kind, {"equals": {"intValue": 9007199254740992}})], sampling=zero),
        bad("int-huge-number", matches=[probe, matcher(kind, {"equals": {"intValue": HUGE}})], sampling=zero),
        bad("int-huge-text", matches=[probe, matcher(kind, {"equals": {"intValue": str(HUGE)}})], sampling=zero),
        bad("int-very-long-text", matches=[probe, matcher(kind, {"equals": {"intValue": "9" * 5000}})], sampling=zero),
        bad("bool-text", matches=[probe, matcher(kind, {"equals": {"boolValue": "true"}})], sampling=zero),
        bad("double-text", matches=[probe, matcher(kind, {"equals": {"doubleValue": "half"}})], sampling=zero),
        bad("bytes-not-base64", matches=[probe, matcher(kind, {"equals": {"bytesValue": "!!"}})], sampling=zero),
        bad("gt-string", matches=[probe, matcher(kind, {"gt": {"stringValue": "a"}})], sampling=zero),
        bad("gt-empty", matches=[probe, matcher(kind, {"gt": {}})], sampling=zero),
        {"id": "enabled-text", "enabled": "no", "spanSampling": {"matches": [probe], "sampling": zero}},
        {"id": "disabled", "enabled": False, "spanSampling": {"matches": [probe], "sampling": {"percentage": 999}}},
        {"id": "disabled-snake", "enabled": False, "span_sampling": {"matches": [probe], "sampling": zero}},
        17,
        {"id": "valid", "name": "A valid policy among the invalid ones", "description": "d", "enabled": True,
         "labels": [{"key": "team", "value": {"stringValue": "x"}}], "futureField": 1,
         "spanSampling": {"matches": [matcher(record("NAME"), {"equals": {"stringValue": "valid-probe"}})],
                          "sampling": zero}},
    ]  # fmt: skip
    statuses = []
    for policy in invalid:
        policy_id = policy.get("id", "") if isinstance(policy, dict) else ""
        policy_id = policy_id if isinstance(policy_id, str) else ""
        statuses.append((policy_id, policy_id in ("valid", "disabled", "disabled-snake")))
    d.policy_set("invalid", json.dumps({"policies": invalid, "futureTopLevel": {}}), statuses)
    d.matched("invalid", "no invalid, unsupported or disabled policy applies", span(MAX - 1, name="probe"), False)
    d.matched("invalid", "the valid policy still applies", span(MAX - 1, name="valid-probe"), True)

    # The other spelling of every key, and numbers as text.
    snake = {
        "policies": [{
            "id": "snake",
            "span_sampling": {
                "matches": [
                    {"target": {"record_field": "SPAN_RECORD_FIELD_KIND"}, "predicate": {"equals": {"string_value": "INTERNAL"}}},
                    {"target": {"span_attribute": {"path": ["a.int"]}}, "predicate": {"gte": {"int_value": "5"}}},
                    {"target": {"span_attribute": {"path": ["a.double"]}}, "predicate": {"lt": {"double_value": "0.5"}}},
                    {"target": {"span_attribute": {"path": ["a.bool"]}}, "predicate": {"equals": {"bool_value": False}}},
                ],
                "sampling": {"percentage": "10", "mode": "SAMPLING_MODE_EQUALIZING", "sampling_precision": 4},
            },
        }]
    }  # fmt: skip
    d.policy_set("snake-case", json.dumps(snake, indent=2), [("snake", True)])
    matching_attributes = {"a.int": 5, "a.double": 0.25, "a.bool": False}
    d.sampled("snake-case", "matched, below the threshold", span(E666 - 1, attributes=matching_attributes), ten_eq)
    d.sampled("snake-case", "matched, at the threshold", span(E666, attributes=matching_attributes), ten_eq)
    d.matched("snake-case", "not matched", span(0, attributes={**matching_attributes, "a.int": 4}), False)

    # A whole number too large for a double reads as an infinity where a double is wanted.
    def huge(policy_id: str, predicate: dict[str, Any]) -> dict[str, Any]:
        probe_name = matcher(record("NAME"), {"equals": {"stringValue": policy_id}})
        return sampling(policy_id, [probe_name, matcher(attribute("a"), predicate)])

    oversized = {"policies": [huge("below-huge", {"lt": {"doubleValue": HUGE}}),
                              huge("above-negative-huge", {"gt": {"doubleValue": -HUGE}}),
                              huge("above-huge", {"gt": {"doubleValue": HUGE}})]}  # fmt: skip
    d.policy_set("oversized", json.dumps(oversized), [(policy["id"], True) for policy in oversized["policies"]])
    d.matched("oversized", "any finite number is below it", span(0, name="below-huge", attributes={"a": 1e308}), True)
    d.matched("oversized", "any finite number is above its negative", span(0, name="above-negative-huge", attributes={"a": -1e308}), True)
    d.matched("oversized", "no finite number is above it", span(0, name="above-huge", attributes={"a": 1e308}), False)

    # Documents that hold no usable policy set: nothing is dropped.
    for name, text, malformed in (
        ("empty-object", "{}", False),
        ("no-policies", '{"policies": []}', False),
        ("null-policies", '{"policies": null}', False),
        ("truncated", '{"policies": [', True),
        ("not-json", "policies:\n  - id: x\n", True),
        ("empty-text", "", True),
        ("array", "[]", True),
        ("policies-not-a-list", '{"policies": {"id": "x"}}', True),
        # The usual mistake with a regex in JSON: a backslash that is not doubled. Some JSON readers drop it.
        ("invalid-escape", INVALID_ESCAPE, True),
    ):
        d.policy_set(name, text, [], malformed)
        d.matched(name, "nothing is dropped", span(0), False)

    return {"policy_sets": d.sets, "cases": d.cases}


def main() -> None:
    check_spec_table()
    rng = random.Random(20261114)
    write(
        "policy_threshold_vectors.json",
        "otel-muzzle policy mode: the rejection threshold T of a span sampling policy. A span is kept when its "
        "56-bit randomness R >= T. cases: T = 2**56 - round(percentage / 100 * 2**56), rounded half up to "
        "`precision` hexadecimal digits counted after the leading f digits, 14 at most; `threshold` is T as 14 "
        'hexadecimal digits ("100000000000000" is 2**56: keep nothing) and `th` its tracestate encoding. '
        "proportional: T for SAMPLING_MODE_PROPORTIONAL given an arriving threshold, from the probability "
        "(2**56 - arriving) / 2**56 * percentage / 100. Generated by generate_policy_vectors.py.",
        threshold_vectors(rng),
    )
    write(
        "policy_tracestate_vectors.json",
        "otel-muzzle policy mode: reading the `ot` tracestate member. `ot` is the member's value (null: no such "
        "member). `th` is 1 to 14 lowercase hexadecimal digits, padded with zeros on the right; `rv` is exactly 14. "
        "An invalid sub-key reads as absent, a repeated one takes its last value, and a value longer than 256 "
        "characters is not read. `randomness` is rv when present, else trace-id bytes 9-15 (the last 14 "
        "hexadecimal characters). Generated by generate_policy_vectors.py.",
        tracestate_vectors(rng),
    )
    write(
        "policy_regex_vectors.json",
        "otel-muzzle policy mode: the regex predicate. valid: patterns every package accepts, with strings they "
        "match and strings they do not (an unanchored search with RE2 semantics). invalid: patterns every package "
        "rejects, because RE2 has no such construct or because it lies outside the supported subset. Generated by "
        "generate_policy_vectors.py.",
        regex_vectors(),
    )
    write(
        "policy_decision_vectors.json",
        "otel-muzzle policy mode: decisions. policy_sets: a policy file's text, whether the document is malformed "
        "(no policy set at all), and the apply status of each policy in document order. cases: with that set "
        "loaded and a delegate that records every span and returns `result_tracestate` (null: none of its own, so "
        "the parent's `tracestate` stands), whether the sampler keeps the span; `parent` is none, local or remote, "
        "and a missing `kind` is null. `drop_tracestate` is the tracestate of a DROP result, as a mapping. "
        "Generated by generate_policy_vectors.py.",
        decision_vectors(rng),
    )


if __name__ == "__main__":
    main()
