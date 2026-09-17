"""The capture-time entity regex."""

import pytest

from app.kb.entities import CVE_PATTERN, extract_entities


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("nothing here", []),
        ("Tracked as CVE-2024-3094.", [("cve", "CVE-2024-3094")]),
        ("cve-2024-3094", [("cve", "CVE-2024-3094")]),
        (
            "CVE-2021-44228 and CVE-2021-45046",
            [("cve", "CVE-2021-44228"), ("cve", "CVE-2021-45046")],
        ),
        # Five-digit and longer ids are real (CVE-2014-100001 exists).
        ("CVE-2014-100001", [("cve", "CVE-2014-100001")]),
        # Three digits is not a CVE id.
        ("CVE-2024-309", []),
    ],
)
def test_extract_entities(text: str, expected: list[tuple[str, str]]):
    assert extract_entities(text) == expected


def test_duplicates_collapse_and_order_is_first_appearance():
    text = "CVE-2021-45046 then CVE-2021-44228 then cve-2021-45046 again"

    assert extract_entities(text) == [
        ("cve", "CVE-2021-45046"),
        ("cve", "CVE-2021-44228"),
    ]


def test_the_pattern_does_not_match_inside_a_longer_token():
    assert extract_entities("XCVE-2024-3094") == [("cve", "CVE-2024-3094")]
    assert CVE_PATTERN.search("CVE-2024-30941234") is not None
