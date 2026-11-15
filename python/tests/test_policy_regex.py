# SPDX-License-Identifier: Apache-2.0
"""Policy mode: the regex predicate's validation and translation."""

from __future__ import annotations

import re
import warnings
from typing import Any

import pytest

from otel_muzzle.policy._regex import compile_regex
from tests.helpers import policy_vectors


class TestRegex:
    def test_valid_patterns_match_as_the_shared_vectors_say(self) -> None:
        cases = policy_vectors("regex")["valid"]
        assert len(cases) > 40
        for case in cases:
            # No translation makes `re` warn: compile_regex does not look for warnings itself.
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                pattern = compile_regex(case["pattern"])
            for text in case["match"]:
                assert pattern.search(text) is not None, (case["pattern"], text)
            for text in case["no_match"]:
                assert pattern.search(text) is None, (case["pattern"], text)

    def test_invalid_patterns_are_rejected(self) -> None:
        cases = policy_vectors("regex")["invalid"]
        assert len(cases) > 80
        for case in cases:
            with pytest.raises(ValueError, match="regex"):
                compile_regex(case["pattern"])

    def test_compiling_leaves_the_warning_filters_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The filters belong to the process. Changed while a pattern compiles, they would turn a warning that
        # another thread raises into an exception.
        during: list[list[Any]] = []
        compile_ = re.compile

        def spy(*args: Any, **kwargs: Any) -> re.Pattern[str]:
            during.append(list(warnings.filters))
            return compile_(*args, **kwargs)

        monkeypatch.setattr(re, "compile", spy)
        before = list(warnings.filters)
        compile_regex("a+")
        assert during == [before]

    def test_dollar_does_not_match_before_a_trailing_newline(self) -> None:
        assert compile_regex("a$").search("a\n") is None

    def test_backreferences_and_lookaround_are_rejected(self) -> None:
        for pattern in (r"(a)\1", "(?=a)", "(?<!a)b"):
            with pytest.raises(ValueError):
                compile_regex(pattern)

    def test_a_leading_flag_group_is_honoured(self) -> None:
        assert compile_regex("(?i)^get").search("GET /") is not None
        assert compile_regex("^get").search("GET /") is None

    def test_the_error_names_the_position(self) -> None:
        with pytest.raises(ValueError, match=r"at 3"):
            compile_regex(r"(a)\1")
