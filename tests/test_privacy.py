# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

from wingman.core import privacy

TERMS = ["ACME", "Globex Corporation"]


def test_redact_replaces_whole_words_case_insensitively():
    text = "At ACME (globex corporation before that) we used acme-style naming."
    assert privacy.redact(text, TERMS) == "At [employer] ([employer] before that) we used [employer]-style naming."


def test_redact_leaves_substrings_alone():
    assert privacy.redact("an acmeish tool; a Globex Corporations list", TERMS) == \
        "an acmeish tool; a Globex Corporations list"


def test_find_reports_each_term_present():
    assert privacy.find("worked at ACME, then Globex Corporation", TERMS) == TERMS
    assert privacy.find("worked at a large bank", TERMS) == []


def test_terms_with_regex_characters_are_literal():
    assert privacy.find("we use C++ daily", ["C++"]) == ["C++"]
    assert privacy.find("we use C daily", ["C++"]) == []
