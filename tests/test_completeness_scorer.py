"""Tests for the pure-Python CompletenessScorer.

These don't touch the LLM — they verify the rubric math, gap detection
and weak-field flagging stays correct when fields are missing, short,
or short-but-shouldn't-be-flagged-weak (names, phone).
"""

from app.agents.business_analyzer_agent import (
    _FAQ_ANSWER_THRESHOLD,
    _FAQ_FLOOR,
    _FAQ_TARGET,
    _WEAK_THRESHOLD,
    CompletenessScorer,
)


def _filled_paragraph(prefix: str = "text ") -> str:
    """Long enough to clear _WEAK_THRESHOLD."""
    return prefix * 10


def _detailed_answer() -> str:
    """Long enough to clear _FAQ_ANSWER_THRESHOLD."""
    return "answer " * 10


def test_empty_form_scores_zero_with_every_category_critical():
    result = CompletenessScorer().score({})
    assert result["overall_score"] == 0
    # Every category is below 50% → every category is critical
    assert set(result["critical_gaps"]) >= {
        "business_identity",
        "products_and_services",
        "faqs",
        "policies_and_detail",
        "contact_and_reach",
    }
    assert result["present_fields"] == []


def test_full_form_scores_one_hundred():
    form = {
        "general": {
            "description": _filled_paragraph(),
            "services": _filled_paragraph(),
            "mission": _filled_paragraph(),
            "vision": _filled_paragraph(),
            "sales_pitch": _filled_paragraph(),
            "additional_info": _filled_paragraph(),
            "faq": [
                {"question": f"q{i}", "answer": _detailed_answer()}
                for i in range(_FAQ_TARGET)
            ],
            "social_media": {"instagram": "@brand"},
        },
        "contact": {
            "name": "Ana",
            "phone": "+58 4140000000",
            "company_name": "Crazy Imagine",
        },
        "links": [{"label": "Catálogo", "url": "https://x.com"}],
    }
    result = CompletenessScorer().score(form)
    assert result["overall_score"] == 100
    assert result["critical_gaps"] == []


def test_short_text_marks_field_weak_not_missing():
    form = {
        "general": {
            "description": "x",  # below _WEAK_THRESHOLD but non-empty
            "services": _filled_paragraph(),
            "mission": _filled_paragraph(),
            "vision": _filled_paragraph(),
        },
        "contact": {"name": "Ana", "phone": "1", "company_name": "Co"},
        "links": [],
    }
    assert len("x") < _WEAK_THRESHOLD
    result = CompletenessScorer().score(form)
    # description is weak, not empty
    assert "Business description" in result["weak_fields"]
    assert "Business description" not in result["empty_fields"]


def test_short_contact_fields_are_not_flagged_weak():
    # `name`, `phone`, `company_name` are check_weak=False — a one-char
    # answer counts as filled.
    form = {
        "general": {},
        "contact": {"name": "A", "phone": "1", "company_name": "C"},
        "links": [],
    }
    result = CompletenessScorer().score(form)
    assert "Contact name" not in result["weak_fields"]
    assert "Contact phone" not in result["weak_fields"]
    assert "Company name" not in result["weak_fields"]


def test_faq_floor_promotes_to_critical_even_above_fifty_percent():
    # _FAQ_FLOOR is 3 — fewer detailed FAQs than the floor is always
    # listed in critical_gaps regardless of the % score.
    faq = [
        {"question": f"q{i}", "answer": _detailed_answer()}
        for i in range(_FAQ_FLOOR - 1)
    ]
    form = {
        "general": {
            "description": _filled_paragraph(),
            "services": _filled_paragraph(),
            "mission": _filled_paragraph(),
            "vision": _filled_paragraph(),
            "faq": faq,
        },
        "contact": {"name": "Ana", "phone": "+58", "company_name": "Co"},
        "links": [{"label": "L", "url": "https://x.com"}],
    }
    result = CompletenessScorer().score(form)
    assert "faqs" in result["critical_gaps"]


def test_too_brief_faq_answers_flagged_weak():
    form = {
        "general": {
            "faq": [
                {"question": "q", "answer": "short"},  # below _FAQ_ANSWER_THRESHOLD
            ],
        },
        "contact": {},
        "links": [],
    }
    assert len("short") < _FAQ_ANSWER_THRESHOLD
    result = CompletenessScorer().score(form)
    assert "FAQ list" in result["weak_fields"]


def test_hyphenated_frontend_keys_are_accepted():
    # Backend snake_case isn't required — the scorer falls back to
    # hyphenated keys (`sales-pitch` etc.) via the _alt helper.
    form = {
        "general": {
            "sales-pitch": _filled_paragraph(),
            "additional-info": _filled_paragraph(),
        },
        "contact": {},
        "links": [],
    }
    result = CompletenessScorer().score(form)
    assert "Sales pitch" in result["present_fields"]
    assert "Additional information (hours, location, policies)" in result["present_fields"]
