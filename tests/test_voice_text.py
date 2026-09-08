"""Amounts and visual formatting become speech without changing their meaning."""

import pytest

from recollect.engine.voice_text import spoken_text


@pytest.mark.parametrize(("text", "expected"), [
    ("It costs $400/month.", "It costs four hundred dollars per month."),
    (r"It costs $\$400$ / month.", "It costs four hundred dollars per month."),
    (r"\(\$400 / \text{month}\)", "four hundred dollars per month"),
    ("$1 and $2", "one dollar and two dollars"),
    ("$1,234.56", "one thousand two hundred thirty four dollars and fifty six cents"),
    ("-$400", "minus four hundred dollars"),
    ("$400–$600", "four hundred dollars to six hundred dollars"),
    ("$400-$600", "four hundred dollars to six hundred dollars"),
    ("US$2.5k", "two thousand five hundred dollars"),
    ("$2.4M", "two million four hundred thousand dollars"),
    ("£1.01", "one pound and one penny"),
    ("£1.25", "one pound and twenty five pence"),
    ("€400", "four hundred euros"),
    ("$0.005", "zero point zero zero five dollars"),
    ("Save 25% or $400, today.", "Save 25 percent or four hundred dollars, today."),
    ("Meet 09/08/2026 and/or tomorrow.", "Meet 09/08/2026 and or tomorrow."),
])
def test_currency_and_units(text, expected):
    assert spoken_text(text) == expected


def test_table_cells_keep_their_header_relationships():
    text = """Here are the options:
| Plan | Monthly cost | Deposit |
| :--- | ---: | --- |
| Basic | $400/month | $1,200 |
| Plus | $500/month | $1,500 |

Both include support.
"""
    assert spoken_text(text) == (
        "Here are the options: Plan: Basic; Monthly cost: four hundred dollars "
        "per month; Deposit: one thousand two hundred dollars. "
        "Plan: Plus; Monthly cost: five hundred dollars per month; Deposit: "
        "one thousand five hundred dollars. Both include support."
    )


def test_numbered_bullets_keep_contents_and_inline_code_keeps_words():
    assert spoken_text("1. **First** option\n2) Keep `this`.\n---") == (
        "First option Keep this."
    )


def test_table_with_extra_cells_does_not_drop_values():
    assert spoken_text("Item | Price\n--- | ---\nOne | $1 | Extra") == (
        "Item: One; Price: one dollar; Extra."
    )
