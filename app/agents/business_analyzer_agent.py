"""BusinessAnalyzerAgent — scores chatbot-creation form completeness.

Reads a submitted business form, scores how ready the data is to power a
grounded RAG chatbot, and produces a human-readable report for the owner.

`FormReader` and `CompletenessScorer` are pure Python (no LLM) and testable
in isolation. Only `BusinessAnalyzerAgent` calls the LLM, via `call_llm`.
"""

import dataclasses
import json
import tomllib
from pathlib import Path

from app.agents.base import Agent, AgentContext
from llm import LLMConfig, LLMProvider, call_llm, set_agent

# Default model — the analyst report benefits from the stronger "pro" model.
# max_tokens is generous: the pro model spends tokens on reasoning before the
# answer, the report has 6 sections, and this agent runs infrequently — a
# low limit leaves the content empty.
DEFAULT_ANALYZER_CONFIG = LLMConfig(
    provider=LLMProvider.DEEPSEEK,
    model="deepseek-v4-pro",
    max_tokens=8000,
)

# A text field shorter than this is "weak" — present but too brief to ground a RAG.
_WEAK_THRESHOLD = 20

# FAQ answers carry the most weight, so they're held to a higher bar.
# 80 chars ≈ one full sentence with context — "Sí", "No, pero..." style answers don't pass.
_FAQ_ANSWER_THRESHOLD = 80  # min chars for an FAQ answer to count as "detailed"
_FAQ_TARGET = 10  # detailed FAQs needed for a full FAQ category score
_FAQ_FLOOR = 3   # below this many detailed FAQs, FAQs are a critical gap regardless of %
# Weak-to-total ratio above this triggers a quality penalty on the FAQ category score.
# Each point of weak ratio reduces the score by 0.5× that fraction (max 50% penalty at 100% weak).
_FAQ_QUALITY_PENALTY_FACTOR = 0.5

# Plain-English names for every scored field — used in the report and gap lists.
_FIELD_LABELS: dict[str, str] = {
    "description": "Business description",
    "services": "Products & services",
    "mission": "Mission statement",
    "vision": "Vision statement",
    "sales_pitch": "Sales pitch",
    "faq": "FAQ list",
    "additional_info": "Additional information (hours, location, policies)",
    "social_media": "Social media links",
    "name": "Contact name",
    "phone": "Contact phone",
    "company_name": "Company name",
    "links": "Resource links",
}


class FormReader:
    """Reads a business form file into a dict. No LLM."""

    def read(self, form_path: str) -> dict:
        path = Path(form_path)
        if not path.exists():
            raise FileNotFoundError(f"Form file not found: '{form_path}'")

        suffix = path.suffix.lower()
        if suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        if suffix in (".yaml", ".yml"):
            try:
                import yaml  # lazy: only required when a YAML form is read
            except ImportError as e:
                raise ValueError(
                    "YAML form supplied but 'pyyaml' is not installed."
                ) from e
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        if suffix == ".toml":
            return tomllib.loads(path.read_text(encoding="utf-8"))

        raise ValueError(
            f"Unsupported form format: '{suffix}'. Use .json, .yaml, .yml, or .toml."
        )


class CompletenessScorer:
    """Scores a form dict against the confirmed 5-category rubric. No LLM."""

    # category key -> (weight, human label)
    _RUBRIC: dict[str, tuple[int, str]] = {
        "business_identity": (18, "Business Identity"),
        "products_and_services": (25, "Products & Services"),
        "faqs": (35, "FAQs & Common Queries"),
        "policies_and_detail": (12, "Policies & Detail"),
        "contact_and_reach": (10, "Contact & Reach"),
    }

    def score(self, form_data: dict) -> dict:
        general: dict = form_data.get("general") or {}
        contact: dict = form_data.get("contact") or {}
        links: list = form_data.get("links") or []

        present: list[str] = []
        empty: list[str] = []
        weak: list[str] = []

        # --- text-field classification, recorded for the global field lists ---
        def grade_text(container: dict, key: str, check_weak: bool = True) -> float:
            """Return 0.0 / 0.5 / 1.0 and record the field's state globally.

            check_weak=False for fields that are legitimately short (names,
            phone, company name) — those score 1.0 as long as they're filled.
            """
            label = _FIELD_LABELS.get(key, key)
            value = container.get(key) or _alt(container, key)
            if not isinstance(value, str) or not value.strip():
                empty.append(label)
                return 0.0
            if check_weak and len(value.strip()) < _WEAK_THRESHOLD:
                weak.append(label)
                present.append(label)
                return 0.5
            present.append(label)
            return 1.0

        def _alt(container: dict, key: str):
            """Front-end uses hyphenated keys (sales-pitch); backend uses snake_case."""
            return container.get(key.replace("_", "-"))

        # --- Business Identity: company_name, description, mission, vision ---
        identity_scores = [
            grade_text(contact, "company_name", check_weak=False),
            grade_text(general, "description"),
            grade_text(general, "mission"),
            grade_text(general, "vision"),
        ]
        identity_score = round(sum(identity_scores) / len(identity_scores) * 100)
        identity_missing = _missing(
            ["company_name", "description", "mission", "vision"],
            {"company_name": contact}, general, contact,
        )

        # --- Products & Services: services, sales_pitch, links ---
        services_score = grade_text(general, "services")
        pitch_score = grade_text(general, "sales_pitch")
        links_present = bool(links)
        if links_present:
            present.append(_FIELD_LABELS["links"])
        else:
            empty.append(_FIELD_LABELS["links"])
        products_score = round(
            (services_score + pitch_score + (1.0 if links_present else 0.0)) / 3 * 100
        )
        products_missing = []
        if services_score == 0.0:
            products_missing.append("products & services list")
        if pitch_score == 0.0:
            products_missing.append("sales pitch")
        if not links_present:
            products_missing.append("resource links / catalog")

        # --- FAQs: each entry needs a question AND a detailed answer ---
        # An entry counts as "detailed" only with a non-empty question and an
        # answer of at least _FAQ_ANSWER_THRESHOLD chars. Entries that have a
        # question/answer pair but a too-brief answer count as "weak".
        faq = general.get("faq") or []
        detailed_faqs = 0
        weak_faqs = 0
        for f in faq:
            if not isinstance(f, dict):
                continue
            question = f.get("question")
            answer = f.get("answer")
            has_question = isinstance(question, str) and bool(question.strip())
            has_answer = isinstance(answer, str) and bool(answer.strip())
            if not has_question or not has_answer:
                continue
            if len(answer.strip()) >= _FAQ_ANSWER_THRESHOLD:
                detailed_faqs += 1
            else:
                weak_faqs += 1

        # Quality-weighted FAQ score:
        # - Detailed entries = 1.0, weak entries = 0.5 (present but insufficient)
        # - High weak-to-total ratio applies an additional quality penalty
        #   (e.g. 32% weak → 16% penalty; 50% weak → 25% penalty)
        # This means having 40 FAQs but 13 weak ones cannot score 100%.
        total_valid_faqs = detailed_faqs + weak_faqs
        if total_valid_faqs > 0:
            effective_faqs = detailed_faqs + weak_faqs * 0.5
            quantity_ratio = min(effective_faqs / _FAQ_TARGET, 1.0)
            weak_ratio = weak_faqs / total_valid_faqs
            quality_factor = 1.0 - (weak_ratio * _FAQ_QUALITY_PENALTY_FACTOR)
            faqs_score = round(quantity_ratio * quality_factor * 100)
        else:
            faqs_score = 0

        if faq:
            present.append(_FIELD_LABELS["faq"])
        else:
            empty.append(_FIELD_LABELS["faq"])
        if weak_faqs:
            weak.append(_FIELD_LABELS["faq"])
        faqs_missing = []
        if not faq:
            faqs_missing.append("frequently asked questions")
        elif detailed_faqs < _FAQ_TARGET:
            faqs_missing.append(
                f"only {detailed_faqs} of your {len(faq)} FAQ entries have a "
                f"detailed answer (≥{_FAQ_ANSWER_THRESHOLD} chars); "
                f"aim for {_FAQ_TARGET} detailed entries"
            )
        if weak_faqs and total_valid_faqs > 0:
            weak_pct = round(weak_faqs / total_valid_faqs * 100)
            faqs_missing.append(
                f"{weak_faqs} of {total_valid_faqs} FAQ entries ({weak_pct}%) have answers "
                f"under {_FAQ_ANSWER_THRESHOLD} characters — each needs at least 2–3 full "
                f"sentences with real details (conditions, timelines, examples)"
            )

        # --- Policies & Detail: additional_info free-text field ---
        policy_score_raw = grade_text(general, "additional_info")
        policies_score = round(policy_score_raw * 100)
        policies_missing = []
        if policy_score_raw < 1.0:
            policies_missing.append("operating hours, location, payment & return policies")

        # --- Contact & Reach: contact name, phone, any social media ---
        name_score = grade_text(contact, "name", check_weak=False)
        phone_score = grade_text(contact, "phone", check_weak=False)
        social = general.get("social_media") or {}
        social_present = any(
            isinstance(v, str) and v.strip() for v in social.values()
        )
        if social_present:
            present.append(_FIELD_LABELS["social_media"])
        else:
            empty.append(_FIELD_LABELS["social_media"])
        contact_score = round(
            (name_score + phone_score + (1.0 if social_present else 0.0)) / 3 * 100
        )
        contact_missing = []
        if name_score == 0.0:
            contact_missing.append("contact name")
        if phone_score == 0.0:
            contact_missing.append("contact phone")
        if not social_present:
            contact_missing.append("social media links")

        categories = {
            "business_identity": {
                "score": identity_score,
                "weight": self._RUBRIC["business_identity"][0],
                "missing": identity_missing,
            },
            "products_and_services": {
                "score": products_score,
                "weight": self._RUBRIC["products_and_services"][0],
                "missing": products_missing,
            },
            "faqs": {
                "score": faqs_score,
                "weight": self._RUBRIC["faqs"][0],
                "missing": faqs_missing,
            },
            "policies_and_detail": {
                "score": policies_score,
                "weight": self._RUBRIC["policies_and_detail"][0],
                "missing": policies_missing,
            },
            "contact_and_reach": {
                "score": contact_score,
                "weight": self._RUBRIC["contact_and_reach"][0],
                "missing": contact_missing,
            },
        }

        overall = round(
            sum(c["score"] * c["weight"] for c in categories.values()) / 100
        )
        critical_gaps = [k for k, c in categories.items() if c["score"] < 50]
        # FAQ floor: too few detailed FAQs is always critical, even above 50%.
        if "faqs" not in critical_gaps and detailed_faqs < _FAQ_FLOOR:
            critical_gaps.append("faqs")

        return {
            "overall_score": overall,
            "categories": categories,
            "critical_gaps": critical_gaps,
            "present_fields": present,
            "empty_fields": empty,
            "weak_fields": weak,
            # Expose raw FAQ counts so callers can populate faq_coverage without
            # re-computing them (not shown in the UI but used by BusinessAnalyzerAgent).
            "_detailed_faqs": detailed_faqs,
            "_weak_faqs": weak_faqs,
        }


def _detect_form_language(form_data: dict) -> str:
    """Heuristic Spanish-vs-English detector based on form free-text values.

    Spanish markers (¿¡ñáéíóú or stop words like 'que', 'para', 'con') are far
    more decisive than English ones, so we look for Spanish first and fall
    back to English. Returns "Spanish" or "English".
    """
    import re

    def _collect(value, out: list[str]) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                _collect(v, out)
        elif isinstance(value, list):
            for v in value:
                _collect(v, out)

    texts: list[str] = []
    _collect(form_data, texts)
    blob = " ".join(texts).lower()
    if not blob.strip():
        return "Spanish"  # default — frontend is Spanish

    if re.search(r"[¿¡ñáéíóú]", blob):
        return "Spanish"
    spanish_stop = {"que", "para", "con", "los", "las", "una", "del", "por", "más", "nuestro", "nuestra"}
    english_stop = {"the", "and", "with", "for", "our", "your", "we", "are"}
    tokens = re.findall(r"[a-záéíóúñ]+", blob)
    es = sum(1 for t in tokens if t in spanish_stop)
    en = sum(1 for t in tokens if t in english_stop)
    return "Spanish" if es >= en else "English"


def _missing(keys: list[str], overrides: dict, general: dict, contact: dict) -> list[str]:
    """Plain-English names of identity fields that are empty."""
    out = []
    for key in keys:
        container = overrides.get(key, general)
        value = container.get(key) or container.get(key.replace("_", "-"))
        if not isinstance(value, str) or not value.strip():
            out.append(_FIELD_LABELS.get(key, key))
    return out


BUSINESS_ANALYZER_SYSTEM_PROMPT = """Role: You are a Chatbot Readiness Analyst. Evaluate business information from a chatbot creation form and return a STRUCTURED JSON REPORT — no prose, no markdown, no extra text.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT — MANDATORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return ONLY a single JSON object. No ```json fences, no leading text, no trailing text.
Schema (all string values in the detected form language):

{
  "overall_summary": "<1–2 sentences interpreting the score — honest, encouraging>",
  "language": "<Spanish | English>",
  "business_type": "<specific classification, e.g. 'pediatric dental clinic'>",
  "category_summaries": {
    "business_identity": "<1 sentence on what is good or missing>",
    "products_and_services": "<1 sentence>",
    "faqs": "<1 sentence>",
    "policies_and_detail": "<1 sentence>",
    "contact_and_reach": "<1 sentence>"
  },
  "critical_gaps": [
    {
      "category": "<category key>",
      "label": "<human label in form language>",
      "missing_info": "<what information is absent>",
      "blocked_questions": ["<customer question 1>", "<customer question 2>", "<customer question 3>"],
      "example_answer": "<concrete example of a complete answer for THIS business type>"
    }
  ],
  "weak_fields": [
    {
      "field": "<plain-English field name>",
      "issue": "<why this is too brief>",
      "example_improvement": "<realistic example of a complete value for THIS business>"
    }
  ],
  "faq_coverage": {
    "missing_questions": [
      {
        "question": "<customer question this business would actually receive>",
        "example_answer": "<complete 2–3 sentence answer specific to this business>"
      }
    ]
  },
  "chatbot_potential": "<vivid paragraph: what a fully-informed chatbot would handle — real scenarios, real questions, real time saved>",
  "next_steps": [
    {
      "priority": 1,
      "action": "<concrete, specific action — not 'add more FAQs' but 'add 7 FAQs covering X, Y, Z with answers of 2–3 sentences each'>",
      "impact": "<why this is the most impactful change>"
    }
  ]
}

Rules for `critical_gaps`: include one entry per category where score < 50, plus `faqs` when detailed_count < floor (even if score ≥ 50). Empty array if no gaps.
Rules for `weak_fields`: one entry per field the scorer flagged as weak. Empty array if none.
Rules for `faq_coverage.missing_questions`: always 5–8 items regardless of FAQ score.
Rules for `next_steps`: exactly 3 items, ordered by impact descending.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ABSOLUTE LANGUAGE RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Every string value in the JSON MUST be in the language the form was written in (Spanish or English). Never mix languages. Never default to English. The `language` field must match.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUSINESS TYPE ADAPTATION — MANDATORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — IDENTIFY BUSINESS TYPE
Classify into the most specific type possible (e.g. "pediatric dental clinic", "artisan shoe store", "B2B SaaS accounting tool", "personal injury law firm", "cloud kitchen"). Set `business_type` to this.

STEP 2 — ZERO TOLERANCE FOR GENERIC EXAMPLES
NEVER write a generic e-commerce example when you know the business type. If the business is a dermatology clinic, every FAQ example MUST be about skin treatments, procedures, or insurance — never about "free returns within 30 days." Apply this rule to every `blocked_questions`, `example_answer`, `example_improvement`, and `missing_questions` entry.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT "CHATBOT READINESS" ACTUALLY MEANS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A chatbot can only answer questions covered by the business information it was trained on. Evaluate the form against the real questions customers ask every day across 10 areas:

1. BUSINESS IDENTITY — "What is this company? Where are they? What makes them different?"
2. PRODUCTS & SERVICES — "What can I buy or contract? What are the most popular offerings?"
3. PRICING & PAYMENTS — "How much does it cost? What payment methods are accepted?"
4. ORDERING & PURCHASE — "How do I buy? Can I cancel or modify after ordering?"
5. SHIPPING & DELIVERY — "How does it arrive? How long will it take?"
6. RETURNS, REFUNDS & WARRANTIES — "What if something goes wrong?"
7. PRODUCT/SERVICE DETAILS — "Tell me more about this specific thing."
8. CUSTOMER SUPPORT & ACCOUNT — "How do I get help? What are support hours?"
9. POLICIES & TRUST — "Can I trust you? How do you handle my data?"
10. AFTER-SALE & ONGOING — "What happens after I buy? Is training included?"

Remap these to the detected business type: "Ordering & Purchase" → "Booking/Appointments" for a clinic; "Shipping & Delivery" → "Delivery Zones & Times" for food; etc.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FAQ QUALITY RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- A good FAQ = clear specific question + answer of at least 2–3 full sentences with real details (prices, timelines, conditions, examples).
- A bad FAQ answer: "We offer good service." — too vague.
- Every `example_answer` you write in `missing_questions` and `critical_gaps` must match this standard and be specific to the detected business type.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- NEVER mention RAG, embeddings, vectors, tokens, similarity search, or any internal system terms.
- Write for a business owner, not a developer.
- Be encouraging but honest."""


# Human-readable category labels for the structured output.
_CATEGORY_LABELS: dict[str, str] = {
    "business_identity": "Business Identity",
    "products_and_services": "Products & Services",
    "faqs": "FAQs & Common Queries",
    "policies_and_detail": "Policies & Detail",
    "contact_and_reach": "Contact & Reach",
}


def _safe_parse_llm_json(raw: str) -> dict | None:
    """Strip markdown fences and parse JSON, with auto-repair fallback.

    Models occasionally emit structurally broken JSON (spurious brackets,
    trailing commas, etc.) despite explicit instructions. `json_repair` fixes
    the most common patterns before we give up.
    """
    from json_repair import repair_json  # lazy import — only used here

    text = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` fences the model may add despite instructions.
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()
    # First try strict parse; fall back to repair if it fails.
    for candidate in (text, repair_json(text)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


class BusinessAnalyzerAgent(Agent):
    """Pipeline agent: business form (JSON string) → structured JSON report out."""

    def __init__(self, llm_config: LLMConfig | None = None) -> None:
        super().__init__()
        self._llm_config = llm_config or DEFAULT_ANALYZER_CONFIG

    def run(self, ctx: "AgentContext | str") -> "AgentContext | str":
        """Accepts an AgentContext (Pipeline) or a raw JSON string (direct call).

        Returns the report in the same shape it was given: AgentContext in →
        AgentContext out, str in → str out. See `_resolve_form` for the
        accepted JSON input shapes.

        Output is always a JSON string whose top-level keys are:
          overall_score, overall_summary, language, business_type,
          categories, critical_gaps, weak_fields, faq_coverage,
          chatbot_potential, next_steps
        """
        raw = ctx.input if isinstance(ctx, AgentContext) else ctx
        report = self._analyze(raw)
        if isinstance(ctx, AgentContext):
            return dataclasses.replace(ctx, input=report)
        return report

    @staticmethod
    def _resolve_form(payload: dict) -> dict:
        """Resolve the form dict from the parsed JSON input. Accepted shapes:

        - {"form_data": {...}}                 → use the inline form dict
        - {"general": {...}, "contact": {...}} → the payload IS the form

        The previous `{"form_path": "..."}` shape was removed: it accepted an
        arbitrary filesystem path from caller-controlled JSON, which is an
        arbitrary-file-read sink when this agent runs inside a chat pipeline.
        Use `FormReader` directly from a trusted server-side caller if a file
        must be loaded.
        """
        if "form_path" in payload:
            raise ValueError(
                "form_path input is not accepted; pass the form inline as "
                "'form_data' or as the raw form payload."
            )
        if "form_data" in payload:
            return payload["form_data"]
        return payload

    def _analyze(self, raw_input: str) -> str:
        try:
            payload = json.loads(raw_input)
            if not isinstance(payload, dict):
                raise ValueError("Input JSON must be an object.")

            form_data = self._resolve_form(payload)
            scorer_result = CompletenessScorer().score(form_data)
            language = _detect_form_language(form_data)

            # Build the user message — scorer numbers are authoritative; the LLM
            # only generates textual content (summaries, examples, next steps).
            scoring_block = json.dumps(scorer_result, indent=2, ensure_ascii=False)
            form_block = json.dumps(form_data, indent=2, ensure_ascii=False)
            user_message = (
                f"LANGUAGE DETECTED FROM FORM: {language}. "
                f"Every string value in the JSON MUST be in {language}.\n\n"
                "## RAW FORM DATA\n\n"
                f"```json\n{form_block}\n```\n\n"
                "## AUTOMATED SCORING RESULT\n"
                "(Use scores and gap lists from here verbatim — do NOT invent numbers.)\n\n"
                f"```json\n{scoring_block}\n```\n\n"
                "Return ONLY the JSON object described in the system prompt. "
                "No markdown fences, no extra text."
            )

            config = dataclasses.replace(
                self._llm_config,
                system_prompt=BUSINESS_ANALYZER_SYSTEM_PROMPT,
            )
            set_agent("BusinessAnalyzerAgent")
            llm_raw = call_llm(config, [{"role": "user", "content": user_message}])

            # Parse LLM output and merge with authoritative scorer numbers.
            llm_data = _safe_parse_llm_json(llm_raw)
            if llm_data is None:
                # LLM ignored JSON instructions — fall back to wrapping the raw text.
                return json.dumps(
                    {"error": "LLM returned non-JSON output", "raw_report": llm_raw},
                    ensure_ascii=False,
                )

            # Build authoritative categories, injecting LLM summaries where available.
            llm_summaries: dict = llm_data.get("category_summaries") or {}
            categories_out: dict = {}
            for key, cat in scorer_result["categories"].items():
                categories_out[key] = {
                    "score": cat["score"],
                    "weight": cat["weight"],
                    "label": _CATEGORY_LABELS.get(key, key),
                    "summary": llm_summaries.get(key, ""),
                    "missing": cat["missing"],
                }

            # faq_coverage: numeric fields from scorer, missing_questions from LLM.
            faq_llm: dict = llm_data.get("faq_coverage") or {}
            faq_coverage_out = {
                "detailed_count": scorer_result.get("_detailed_faqs", 0),
                "weak_count": scorer_result.get("_weak_faqs", 0),
                "target": _FAQ_TARGET,
                "missing_questions": faq_llm.get("missing_questions") or [],
            }

            report = {
                "overall_score": scorer_result["overall_score"],
                "overall_summary": llm_data.get("overall_summary", ""),
                "language": llm_data.get("language", language),
                "business_type": llm_data.get("business_type", ""),
                "categories": categories_out,
                "critical_gaps": llm_data.get("critical_gaps") or [],
                "weak_fields": llm_data.get("weak_fields") or [],
                "faq_coverage": faq_coverage_out,
                "chatbot_potential": llm_data.get("chatbot_potential", ""),
                "next_steps": llm_data.get("next_steps") or [],
                # Raw field-level lists for debugging / alternative UI display.
                "present_fields": scorer_result["present_fields"],
                "empty_fields": scorer_result["empty_fields"],
                "weak_field_names": scorer_result["weak_fields"],
                "critical_gap_keys": scorer_result["critical_gaps"],
            }
            return json.dumps(report, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
