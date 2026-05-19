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
        "business_identity": (20, "Business Identity"),
        "products_and_services": (30, "Products & Services"),
        "faqs": (25, "FAQs & Common Queries"),
        "policies_and_detail": (15, "Policies & Detail"),
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

        # --- FAQs: count of FAQ entries with a detailed answer (target 3+) ---
        faq = general.get("faq") or []
        detailed_faqs = sum(
            1 for f in faq
            if isinstance(f, dict)
            and isinstance(f.get("answer"), str)
            and len(f["answer"].strip()) >= _WEAK_THRESHOLD
        )
        faqs_score = round(min(detailed_faqs / 3, 1.0) * 100)
        if faq:
            present.append(_FIELD_LABELS["faq"])
        else:
            empty.append(_FIELD_LABELS["faq"])
        faqs_missing = []
        if not faq:
            faqs_missing.append("frequently asked questions")
        elif detailed_faqs < 3:
            faqs_missing.append(
                f"more detailed FAQ answers ({detailed_faqs}/3 are detailed enough)"
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

        return {
            "overall_score": overall,
            "categories": categories,
            "critical_gaps": critical_gaps,
            "present_fields": present,
            "empty_fields": empty,
            "weak_fields": weak,
        }


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

You will receive a structured scoring result from an automated analyzer. Your job is to translate that into a human-readable report.

Report structure — output exactly in this order:

1. OVERALL READINESS SCORE: [score]%
   [One sentence interpreting the score: what it means in plain terms]

2. CATEGORY BREAKDOWN
   For each category, one line: [Category Name]: [score]% — [one sentence on what's good or what's missing]

3. CRITICAL GAPS (if any categories are below 50%)
   A prioritized list of what is most urgently needed, explained in plain language for a non-technical business owner.
   For each gap: what information is missing, why it matters for the chatbot, and a concrete example of what good information looks like.

4. WEAK FIELDS
   Fields that were filled in but are too brief to be useful. Explain what a complete answer looks like for each.

5. WHAT HAPPENS AT 100%
   A short paragraph explaining what a fully-informed chatbot would be able to do for this business — paint the picture of the end state to motivate them to complete the information.

6. NEXT STEPS
   A numbered list of the 3 most impactful things the business owner should add or expand, ordered by impact on chatbot quality.

Constraints:
- Write in clear, friendly, non-technical language — the reader is a business owner, not a developer
- NEVER mention RAG, embeddings, vectors, tokens, or internal system terms
- Be specific: reference the actual missing fields by their plain-English names
- Be encouraging but honest — a 40% score should feel like an opportunity, not a failure
- Respond in the same language the form data appears to be written in"""


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

        - {"form_path": "/path/to/form.json"}  → read the file from disk
        - {"form_data": {...}}                 → use the inline form dict
        - {"general": {...}, "contact": {...}} → the payload IS the form
        """
        if "form_path" in payload:
            return FormReader().read(payload["form_path"])
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

            scoring_block = json.dumps(result, indent=2, ensure_ascii=False)
            user_message = (
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
