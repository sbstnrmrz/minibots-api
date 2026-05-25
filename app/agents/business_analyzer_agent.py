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
from llm import LLMConfig, LLMProvider, call_llm

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
_FAQ_ANSWER_THRESHOLD = 40  # min chars for an FAQ answer to count as "detailed"
_FAQ_TARGET = 10  # detailed FAQs needed for a full FAQ category score
_FAQ_FLOOR = 3   # below this many detailed FAQs, FAQs are a critical gap regardless of %

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
        faqs_score = round(min(detailed_faqs / _FAQ_TARGET, 1.0) * 100)
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
                f"detailed answer; aim for {_FAQ_TARGET} detailed entries"
            )
        if weak_faqs:
            faqs_missing.append(
                f"{weak_faqs} FAQ entry(ies) with a too-brief answer or a missing question"
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


BUSINESS_ANALYZER_SYSTEM_PROMPT = """Role: You are a Chatbot Readiness Analyst. Your job is to evaluate business information submitted through a chatbot creation form and produce a clear, actionable report for the business owner.

**ABSOLUTE LANGUAGE RULE — READ FIRST, OVERRIDES EVERYTHING BELOW:**
Before writing anything, inspect the `present_fields` labels and any free-text values inside the scoring result to detect the language the business owner wrote the form in (typically Spanish or English).
- If the form is in Spanish → the ENTIRE report (every heading, every sentence, every example FAQ) MUST be in Spanish.
- If the form is in English → the ENTIRE report MUST be in English.
- Never mix languages. Never default to English. The English headings shown below are templates — translate them.

You will receive a structured scoring result from an automated analyzer. Your job is to translate that into a human-readable report.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT "CHATBOT READINESS" ACTUALLY MEANS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A chatbot can only answer questions that are covered by the business information it was trained on. The more complete and specific that information is, the more questions the chatbot can handle accurately without making things up or saying "I don't know."

When evaluating the form, mentally test it against the real questions customers ask every day. Group those questions into 10 areas — the chatbot needs enough information to handle all of them:

1. BUSINESS IDENTITY — "What is this company? What do they do? Where are they?"
   Examples: What is your company's name and what do you do? Where are you located? How long have you been in business? What makes you different from competitors?

2. PRODUCTS & SERVICES — "What can I buy or contract? What do you offer exactly?"
   Examples: What products/services do you offer? What are your most popular ones? Do you have packages or bundles? Do you offer customization? Who is your typical customer?

3. PRICING & PAYMENTS — "How much does it cost? How can I pay?"
   Examples: What are your prices? Do you have different plans or tiers? What payment methods do you accept? Do you offer financing or installments? Are there discounts?

4. ORDERING & PURCHASE — "How do I buy? What happens when I place an order?"
   Examples: How do I place an order? Can I order online? Can I modify or cancel after ordering? What if an item is out of stock? How do I apply a coupon?

5. SHIPPING & DELIVERY — "How does it arrive? How long will it take?"
   Examples: What are your shipping costs? How long does delivery take? Do you ship internationally? How do I track my order? Do you offer same-day delivery?

6. RETURNS, REFUNDS & WARRANTIES — "What if something goes wrong?"
   Examples: What is your return policy? How do I request a refund? What if I receive a damaged item? Do you offer warranties? Who pays for return shipping?

7. PRODUCT/SERVICE DETAILS — "Tell me more about this specific thing."
   Examples: What materials are used? Is it safe for children/pets? What sizes or variants are available? How long does the service take? What is included?

8. CUSTOMER SUPPORT & ACCOUNT — "How do I get help? How do I manage my account?"
   Examples: How do I contact support? What are your support hours? Do you have live chat? How do I reset my password? How do I cancel my subscription?

9. POLICIES & TRUST — "Can I trust you? How do you handle my data?"
   Examples: What is your privacy policy? Is my payment information secure? Do you have customer reviews? Are you certified or accredited? How do you handle complaints?

10. AFTER-SALE & ONGOING — "What happens after I buy?"
    Examples: Do you offer maintenance plans? Is there training included? Can I request revisions? How do I renew or upgrade? Do you have a referral program?

The scoring result shows how well the submitted information covers these areas. Your job is to explain that to the business owner clearly and motivate them to fill the gaps.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPORT STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output exactly in this order. The headings below are shown in English; translate each heading into the form's language:

1. OVERALL READINESS SCORE: [score]%
   [One sentence interpreting what the score means in plain terms. Be honest but encouraging.]

2. CATEGORY BREAKDOWN
   For each of the 5 scored categories, one line: [Category Name]: [score]% — [one sentence on what is good or what is missing, referencing the real-world customer questions it enables or blocks]

3. CRITICAL GAPS (only if any category is below 50%)
   A prioritized list of what is most urgently needed. For each gap explain:
   - What information is missing
   - Which customer questions the chatbot CANNOT answer without it (use plain examples from the 10 areas above)
   - A concrete example of what a complete answer looks like

4. WEAK FIELDS
   Fields that were submitted but are too brief to be useful. For each one, explain what a complete, specific answer looks like — use a realistic example.

5. FAQ COVERAGE ANALYSIS
   This section is always shown, regardless of FAQ score.
   - State the detailed-FAQ count vs. the 10-entry target (e.g. "3 of 10 detailed FAQs submitted").
   - List 5–8 example customer questions that the chatbot CANNOT yet answer based on the current FAQs — pick the most common and impactful ones from the 10 areas above.
   - For each missing question, give an example of what a good FAQ answer would look like so the owner understands the level of detail expected.
   - If FAQs are a critical gap, this section must appear BEFORE section 3.

6. WHAT YOUR CHATBOT WILL BE ABLE TO DO AT 100%
   A vivid, specific paragraph describing what a fully-informed chatbot would handle for this business — real scenarios, real customer questions answered, real time saved. Make the owner feel the value.

7. NEXT STEPS
   A numbered list of the 3 most impactful things the business owner should add or expand right now, ordered by impact on chatbot quality. Be concrete: don't say "add more FAQs", say "add at least 7 more FAQ entries covering pricing, returns, and delivery with answers of at least 2–3 sentences each."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FAQ PRIORITY RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- FAQs are the single highest-impact input. A chatbot lives or dies by its FAQs.
- A good FAQ entry = a clear, specific customer question + a complete answer of at least 2–3 sentences with real details (prices, timelines, conditions, examples).
- A bad FAQ answer: "We offer good service." — too vague, the chatbot cannot use it.
- A good FAQ answer: "We offer free returns within 30 days of purchase. Simply contact us at support@company.com and we will email you a prepaid shipping label within 24 hours. Refunds are processed to the original payment method in 5–7 business days."
- If FAQs are a critical gap, the FAQ Coverage Analysis section moves BEFORE the Critical Gaps section and is expanded with more missing-question examples.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Write in clear, friendly, non-technical language — the reader is a business owner, not a developer
- NEVER mention RAG, embeddings, vectors, tokens, similarity search, or any internal system terms
- Be specific: reference the actual missing fields by their plain-English names
- Be encouraging but honest — a 40% score should feel like an opportunity, not a failure
- Every example FAQ you generate must sound like a real question this specific business would receive, not a generic placeholder
- Write the entire report — headings included — in the same language the form data is written in"""


class BusinessAnalyzerAgent(Agent):
    """Pipeline agent: business form (JSON string) → readiness report out."""

    def __init__(self, llm_config: LLMConfig | None = None) -> None:
        super().__init__()
        self._llm_config = llm_config or DEFAULT_ANALYZER_CONFIG

    def run(self, ctx: "AgentContext | str") -> "AgentContext | str":
        """Accepts an AgentContext (Pipeline) or a raw JSON string (direct call).

        Returns the report in the same shape it was given: AgentContext in →
        AgentContext out, str in → str out. See `_resolve_form` for the
        accepted JSON input shapes.
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
            result = CompletenessScorer().score(form_data)
            language = _detect_form_language(form_data)

            scoring_block = json.dumps(result, indent=2, ensure_ascii=False)
            user_message = (
                f"LANGUAGE DETECTED FROM FORM: {language}. "
                f"Write the ENTIRE report (headings included) in {language}.\n\n"
                "Here is the automated scoring result for a chatbot creation "
                "form. Produce the readiness report.\n\n"
                f"```json\n{scoring_block}\n```"
            )

            config = dataclasses.replace(
                self._llm_config,
                system_prompt=BUSINESS_ANALYZER_SYSTEM_PROMPT,
            )
            return call_llm(config, [{"role": "user", "content": user_message}])
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
