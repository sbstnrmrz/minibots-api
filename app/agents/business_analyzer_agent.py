"""BusinessAnalyzerAgent — scores chatbot-creation form completeness.

Reads a submitted business form, scores how ready the data is to power a
grounded RAG chatbot, and produces a human-readable report for the owner.

`FormReader`, `CompletenessScorer`, and `ContentModerator` are pure Python
(no LLM) and testable in isolation. Only `BusinessAnalyzerAgent` calls the
LLM, via `call_llm` (sync pipeline path) or `acall_llm` (streaming HTTP path).

Streaming path: `analyze_sections_async()` runs 7 concurrent LLM tasks and
yields NDJSON chunks as each completes — first chunk arrives in ~1s (pure
Python scorer), section chunks arrive as each parallel call resolves.
"""

import asyncio
import dataclasses
import json
import re
import tomllib
from collections.abc import AsyncGenerator
from pathlib import Path

from app.agents.base import Agent, AgentContext
from llm import LLMConfig, LLMProvider, acall_llm, call_llm, set_agent

# ---------------------------------------------------------------------------
# Default configs
# ---------------------------------------------------------------------------

# The analyst report benefits from the stronger "pro" model for the single
# blocking pipeline path (run()).
DEFAULT_ANALYZER_CONFIG = LLMConfig(
    provider=LLMProvider.DEEPSEEK,
    model="deepseek-v4-pro",
    max_tokens=8000,
)

# Section-level prompts are shorter and focused — flash is fast and cheap.
_SECTION_LLM_CONFIG = LLMConfig(
    provider=LLMProvider.DEEPSEEK,
    model="deepseek-v4-flash",
    max_tokens=1200,
)

# FAQs section needs more tokens: 5-8 detailed example answers plus field
# suggestions can easily exceed 1200 tokens. Use pro model to match
# the quality users expect from the previous monolithic call.
_FAQ_SECTION_LLM_CONFIG = LLMConfig(
    provider=LLMProvider.DEEPSEEK,
    model="deepseek-v4-pro",
    max_tokens=3000,
)

# The "overall" call (summary, business_type, chatbot_potential, next_steps)
# benefits from the stronger model since it synthesises everything.
_OVERALL_LLM_CONFIG = LLMConfig(
    provider=LLMProvider.DEEPSEEK,
    model="deepseek-v4-pro",
    max_tokens=3000,
)

# Moderation is fast — flash is sufficient.
_MODERATION_LLM_CONFIG = LLMConfig(
    provider=LLMProvider.DEEPSEEK,
    model="deepseek-v4-flash",
    max_tokens=600,
)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# A text field shorter than this is "weak" — present but too brief to ground a RAG.
_WEAK_THRESHOLD = 20

# FAQ answers carry the most weight, so they're held to a higher bar.
# 80 chars ≈ one full sentence with context.
_FAQ_ANSWER_THRESHOLD = 80   # min chars for "detailed"
_FAQ_TARGET = 10             # detailed FAQs for a full score
_FAQ_FLOOR = 3               # below this many detailed FAQs → critical gap regardless of %
_FAQ_QUALITY_PENALTY_FACTOR = 0.5

# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------

# Plain-English names for every scored field — used in reports and gap lists.
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

# Step number for each field key — used by the UI for navigation.
_FIELD_STEPS: dict[str, int] = {
    "description": 1,
    "services": 1,
    "mission": 1,
    "vision": 1,
    "sales_pitch": 1,
    "faq": 1,
    "additional_info": 1,
    "social_media": 1,
    "name": 2,
    "phone": 2,
    "company_name": 2,
    "links": 3,
}

# Fields belonging to each scoring category.
_CATEGORY_FIELDS: dict[str, list[str]] = {
    "business_identity": ["company_name", "description", "mission", "vision"],
    "products_and_services": ["services", "sales_pitch", "links"],
    "faqs": ["faq"],
    "policies_and_detail": ["additional_info"],
    "contact_and_reach": ["name", "phone", "social_media"],
}

_CATEGORY_LABELS: dict[str, str] = {
    "business_identity": "Business Identity",
    "products_and_services": "Products & Services",
    "faqs": "FAQs & Common Queries",
    "policies_and_detail": "Policies & Detail",
    "contact_and_reach": "Contact & Reach",
}

# ---------------------------------------------------------------------------
# Profanity word-list (Pass 1 of content moderation — pure Python, no LLM)
# Common Spanish groserias + universal English profanity.
# Kept minimal — the LLM pass catches subtler cases.
# ---------------------------------------------------------------------------
_PROFANITY_WORDS: set[str] = {
    # Spanish
    "puta", "puto", "putа", "mierda", "cabrón", "cabron", "pendejo", "pendeja",
    "chingar", "chinga", "chingada", "verga", "coño", "coño", "culo", "pene",
    "vagina", "sexo", "follar", "joder", "gilipollas", "maricón", "maricon",
    "hijueputa", "hijoputa", "gonorrea", "malparido", "malparida",
    "hp", "ptm", "wtf",
    # English
    "fuck", "shit", "asshole", "bitch", "cunt", "cock", "dick", "pussy",
    "nigger", "faggot", "whore", "slut",
}
_PROFANITY_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _PROFANITY_WORDS) + r")\b",
    re.IGNORECASE | re.UNICODE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alt(container: dict, key: str):
    """Front-end uses hyphenated keys (sales-pitch); backend uses snake_case."""
    return container.get(key.replace("_", "-"))


def _missing(keys: list[str], overrides: dict, general: dict, contact: dict) -> list[str]:
    """Plain-English names of identity fields that are empty."""
    out = []
    for key in keys:
        container = overrides.get(key, general)
        value = container.get(key) or container.get(key.replace("_", "-"))
        if not isinstance(value, str) or not value.strip():
            out.append(_FIELD_LABELS.get(key, key))
    return out


def _safe_parse_llm_json(raw: str) -> dict | list | None:
    """Strip markdown fences and parse JSON, with auto-repair fallback."""
    from json_repair import repair_json  # lazy import

    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()
    for candidate in (text, repair_json(text)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _detect_form_language(form_data: dict) -> str:
    """Heuristic Spanish-vs-English detector based on form free-text values."""
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


# ---------------------------------------------------------------------------
# FormReader
# ---------------------------------------------------------------------------

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
                import yaml
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


# ---------------------------------------------------------------------------
# CompletenessScorer
# ---------------------------------------------------------------------------

class CompletenessScorer:
    """Scores a form dict against the confirmed 5-category rubric. No LLM.

    Extended output now includes ``field_details`` (per-field quality/step)
    and ``_faq_items`` (per-FAQ-entry quality) so the streaming path can
    render targeted feedback without a second scoring pass.
    """

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

        # Per-field quality tracker: field_key -> "good" | "weak" | "empty"
        field_details: dict[str, dict] = {}

        def _record_quality(key: str, quality: str) -> None:
            field_details[key] = {
                "quality": quality,
                "step": _FIELD_STEPS.get(key, 1),
            }

        def grade_text(container: dict, key: str, check_weak: bool = True) -> float:
            """Return 0.0 / 0.5 / 1.0 and record the field's state globally.

            check_weak=False for legitimately short fields (names, phone,
            company name) — those score 1.0 as long as they're non-empty.
            """
            label = _FIELD_LABELS.get(key, key)
            value = container.get(key) or _alt(container, key)
            if not isinstance(value, str) or not value.strip():
                empty.append(label)
                _record_quality(key, "empty")
                return 0.0
            if check_weak and len(value.strip()) < _WEAK_THRESHOLD:
                weak.append(label)
                present.append(label)
                _record_quality(key, "weak")
                return 0.5
            present.append(label)
            _record_quality(key, "good")
            return 1.0

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
            _record_quality("links", "good")
        else:
            empty.append(_FIELD_LABELS["links"])
            _record_quality("links", "empty")
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

        # --- FAQs ---
        faq = general.get("faq") or []
        detailed_faqs = 0
        weak_faqs = 0
        faq_items: list[dict] = []
        for idx, f in enumerate(faq):
            if not isinstance(f, dict):
                continue
            question = f.get("question")
            answer = f.get("answer")
            has_question = isinstance(question, str) and bool(question.strip())
            has_answer = isinstance(answer, str) and bool(answer.strip())
            if not has_question or not has_answer:
                faq_items.append({"index": idx, "quality": "empty"})
                continue
            if len(answer.strip()) >= _FAQ_ANSWER_THRESHOLD:
                detailed_faqs += 1
                faq_items.append({"index": idx, "quality": "good"})
            else:
                weak_faqs += 1
                faq_items.append({"index": idx, "quality": "weak"})

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
            if weak_faqs:
                weak.append(_FIELD_LABELS["faq"])
            faq_quality = "weak" if weak_faqs > 0 else "good"
            _record_quality("faq", faq_quality)
        else:
            empty.append(_FIELD_LABELS["faq"])
            _record_quality("faq", "empty")

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

        # --- Policies & Detail: additional_info ---
        policy_score_raw = grade_text(general, "additional_info")
        policies_score = round(policy_score_raw * 100)
        policies_missing = []
        if policy_score_raw < 1.0:
            policies_missing.append("operating hours, location, payment & return policies")

        # --- Contact & Reach: name, phone, social_media ---
        name_score = grade_text(contact, "name", check_weak=False)
        phone_score = grade_text(contact, "phone", check_weak=False)
        social = general.get("social_media") or {}
        social_present = any(
            isinstance(v, str) and v.strip() for v in social.values()
        )
        if social_present:
            present.append(_FIELD_LABELS["social_media"])
            _record_quality("social_media", "good")
        else:
            empty.append(_FIELD_LABELS["social_media"])
            _record_quality("social_media", "empty")
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
        if "faqs" not in critical_gaps and detailed_faqs < _FAQ_FLOOR:
            critical_gaps.append("faqs")

        return {
            "overall_score": overall,
            "categories": categories,
            "critical_gaps": critical_gaps,
            "field_details": field_details,
            "present_fields": present,
            "empty_fields": empty,
            "weak_fields": weak,
            "_detailed_faqs": detailed_faqs,
            "_weak_faqs": weak_faqs,
            "_faq_items": faq_items,
        }


# ---------------------------------------------------------------------------
# ContentModerator
# ---------------------------------------------------------------------------

class ContentModerator:
    """Two-pass content moderation for business form text.

    Pass 1 (pure Python): regex word-list — catches obvious profanity instantly.
    Pass 2 (LLM): lightweight async call — catches subtle issues (sexual
    references, offensive slang, brand-damaging content).

    Call `check_sync()` from the synchronous pipeline path or
    `check_async()` from the streaming path.
    """

    # Fields to inspect: (container_key, field_key, step)
    _TEXT_FIELDS: list[tuple[str, str, int]] = [
        ("general", "description", 1),
        ("general", "services", 1),
        ("general", "mission", 1),
        ("general", "vision", 1),
        ("general", "sales_pitch", 1),
        ("general", "additional_info", 1),
    ]

    @staticmethod
    def _collect_texts(form_data: dict) -> list[tuple[str, int, str]]:
        """Return (field_key, step, text) for every non-empty inspectable field."""
        general = form_data.get("general") or {}
        results: list[tuple[str, int, str]] = []

        for _container_key, field_key, step in ContentModerator._TEXT_FIELDS:
            val = general.get(field_key) or _alt(general, field_key)
            if isinstance(val, str) and val.strip():
                results.append((field_key, step, val.strip()))

        # FAQ questions and answers
        for i, f in enumerate(general.get("faq") or []):
            if not isinstance(f, dict):
                continue
            q = f.get("question", "")
            a = f.get("answer", "")
            if isinstance(q, str) and q.strip():
                results.append((f"faq[{i}].question", 1, q.strip()))
            if isinstance(a, str) and a.strip():
                results.append((f"faq[{i}].answer", 1, a.strip()))

        return results

    @staticmethod
    def _pass1_flags(texts: list[tuple[str, int, str]]) -> list[dict]:
        """Fast regex profanity scan — no LLM."""
        flags = []
        seen_fields: set[str] = set()
        for field_key, step, text in texts:
            base_field = field_key.split("[")[0]  # collapse faq[0].answer → faq
            if base_field in seen_fields:
                continue
            if _PROFANITY_PATTERN.search(text):
                seen_fields.add(base_field)
                flags.append({
                    "field": base_field,
                    "step": step,
                    "issue": "Contiene lenguaje inapropiado / Contains inappropriate language",
                })
        return flags

    @staticmethod
    def _build_moderation_prompt(texts: list[tuple[str, int, str]], language: str) -> str:
        lines = []
        for field_key, step, text in texts[:30]:  # cap at 30 entries to keep prompt small
            lines.append(f'[{field_key} / step {step}]: "{text[:300]}"')
        joined = "\n".join(lines)
        return (
            f"LANGUAGE: {language}\n\n"
            "Review each field below for:\n"
            "- Profanity or crude language (groserias)\n"
            "- Sexual references or innuendo (referencias sexuales)\n"
            "- Offensive, discriminatory, or hateful content\n"
            "- Content that would damage the image of the business or the chatbot platform\n\n"
            "Fields:\n"
            f"{joined}\n\n"
            "Return ONLY a JSON array. Each item: "
            '{"field": "<field_key>", "step": <number>, "issue": "<short description in form language>"}. '
            "Empty array [] if all content is clean. No markdown, no extra text."
        )

    _MODERATION_SYSTEM = (
        "You are a content safety reviewer for a business chatbot platform. "
        "Identify inappropriate content that would embarrass the business owner or the platform. "
        "Be precise — flag real problems, not mild language. "
        "Return ONLY a JSON array as instructed."
    )

    def check_sync(self, form_data: dict, language: str = "Spanish") -> list[dict]:
        """Synchronous moderation — used by the single-call pipeline path."""
        texts = self._collect_texts(form_data)
        flags = self._pass1_flags(texts)
        flagged_fields = {f["field"] for f in flags}

        # Only call LLM if there are long enough fields to warrant it.
        has_long_text = any(len(t) > 50 for _, _, t in texts)
        if has_long_text:
            prompt = self._build_moderation_prompt(texts, language)
            config = dataclasses.replace(
                _MODERATION_LLM_CONFIG,
                system_prompt=self._MODERATION_SYSTEM,
            )
            set_agent("ContentModerator")
            try:
                raw = call_llm(config, [{"role": "user", "content": prompt}])
                parsed = _safe_parse_llm_json(raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and item.get("field") not in flagged_fields:
                            flags.append(item)
                            flagged_fields.add(item.get("field", ""))
            except Exception:
                pass  # moderation failure is non-fatal

        return flags

    async def check_async(self, form_data: dict, language: str = "Spanish") -> list[dict]:
        """Async moderation — used by the streaming path."""
        texts = self._collect_texts(form_data)
        flags = self._pass1_flags(texts)
        flagged_fields = {f["field"] for f in flags}

        has_long_text = any(len(t) > 50 for _, _, t in texts)
        if has_long_text:
            prompt = self._build_moderation_prompt(texts, language)
            config = dataclasses.replace(
                _MODERATION_LLM_CONFIG,
                system_prompt=self._MODERATION_SYSTEM,
            )
            set_agent("ContentModerator")
            try:
                raw = await acall_llm(config, [{"role": "user", "content": prompt}])
                parsed = _safe_parse_llm_json(raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and item.get("field") not in flagged_fields:
                            flags.append(item)
                            flagged_fields.add(item.get("field", ""))
            except Exception:
                pass  # moderation failure is non-fatal

        return flags


# ---------------------------------------------------------------------------
# Section prompts
# ---------------------------------------------------------------------------

_SECTION_SYSTEM = """You are a Chatbot Readiness Analyst evaluating ONE section of a business form.

Return ONLY a JSON object. No markdown fences, no extra text.

All string values MUST be in the form's detected language (Spanish or English). Never mix languages.
Examples must be specific to the detected business type — never generic e-commerce examples.

FIELD QUALITY RULES:
- field_suggestions[key] = null → field is already good (quality = "good"), no action needed
- field_suggestions[key] = {"suggestion": "...", "example": "..."} → field is weak or empty

NEVER mention RAG, embeddings, vectors, tokens, or any internal system terms.
Write for a business owner, not a developer."""


def _build_section_prompt(
    section_key: str,
    form_data: dict,
    scorer_result: dict,
    language: str,
    business_type_hint: str,
) -> str:
    """Build a focused user message for a single section LLM call."""
    general = form_data.get("general") or {}
    contact = form_data.get("contact") or {}
    links = form_data.get("links") or []
    field_details = scorer_result.get("field_details") or {}
    cat = scorer_result["categories"][section_key]
    field_keys = _CATEGORY_FIELDS[section_key]

    # Extract only the fields relevant to this section.
    section_form: dict = {}
    for key in field_keys:
        if key == "links":
            section_form["links"] = links
        elif key in ("name", "phone", "company_name"):
            val = contact.get(key)
            if val:
                section_form[key] = val
        elif key == "social_media":
            val = general.get("social_media")
            if val:
                section_form["social_media"] = val
        elif key == "faq":
            val = general.get("faq")
            if val:
                section_form["faq"] = val
        else:
            val = general.get(key) or _alt(general, key)
            if val:
                section_form[key] = val

    # Per-field quality for this section's fields.
    quality_map = {k: field_details.get(k, {}).get("quality", "empty") for k in field_keys}

    is_gap = section_key in scorer_result.get("critical_gaps", [])
    faq_extra = ""
    if section_key == "faqs":
        faq_items = scorer_result.get("_faq_items") or []
        faq_extra = (
            f"\nFAQ items quality: {json.dumps(faq_items, ensure_ascii=False)}"
            f"\nDetailed FAQ count: {scorer_result.get('_detailed_faqs', 0)}"
            f"\nWeak FAQ count: {scorer_result.get('_weak_faqs', 0)}"
            f"\nTarget: {_FAQ_TARGET} detailed FAQs"
        )

    schema_hint = _section_output_schema(section_key, field_keys, is_gap)

    return (
        f"LANGUAGE: {language}. All strings in JSON MUST be in {language}.\n"
        f"BUSINESS TYPE HINT: {business_type_hint or 'unknown (infer from form data)'}\n\n"
        f"SECTION: {_CATEGORY_LABELS[section_key]} (score: {cat['score']}/100, weight: {cat['weight']}%)\n"
        f"FIELD QUALITIES: {json.dumps(quality_map, ensure_ascii=False)}\n"
        f"CATEGORY GAPS: {json.dumps(cat['missing'], ensure_ascii=False)}"
        f"{faq_extra}\n\n"
        f"RELEVANT FORM DATA:\n```json\n{json.dumps(section_form, indent=2, ensure_ascii=False)}\n```\n\n"
        f"Return this exact JSON schema:\n{schema_hint}\n\n"
        "Rules:\n"
        "- field_suggestions[key] = null if quality is 'good', else give suggestion + example\n"
        "- suggestion: what to add or improve (1-2 sentences, specific to this business)\n"
        "- example: a realistic, complete example value specific to THIS business type\n"
        + (
            "- blocked_questions: 3 real customer questions this gap blocks answering\n"
            if is_gap else ""
        )
        + (
            "- missing_faq_questions: 5-8 FAQ pairs this business actually needs\n"
            if section_key == "faqs" else ""
        )
        + "- summary: 1 sentence describing section status\n"
        "- No markdown, no extra text, pure JSON only."
    )


def _section_output_schema(
    section_key: str, field_keys: list[str], is_gap: bool
) -> str:
    """JSON schema snippet for the section's LLM output."""
    fields_schema = ", ".join(
        f'"{k}": <null | {{"suggestion": "...", "example": "..."}}>'
        for k in field_keys
    )
    base = (
        "{\n"
        f'  "summary": "...",\n'
        f'  "field_suggestions": {{ {fields_schema} }}'
    )
    if is_gap:
        base += ',\n  "blocked_questions": ["...", "...", "..."]'
    if section_key == "faqs":
        base += (
            ',\n  "missing_faq_questions": ['
            '\n    {"question": "...", "example_answer": "..."},'
            "\n    ...\n  ]"
        )
    base += "\n}"
    return base


# Overall LLM call prompt (summary, business_type, chatbot_potential, next_steps).
_OVERALL_SYSTEM = """You are a Chatbot Readiness Analyst. Given a complete business form scoring result, return a STRUCTURED JSON REPORT.

Return ONLY a JSON object. No markdown fences, no extra text.
All string values MUST be in the form's detected language. Never mix languages.
Examples and recommendations must be specific to the detected business type.
NEVER mention RAG, embeddings, vectors, tokens, or any internal system terms.
Write for a business owner, not a developer. Be encouraging but honest."""


def _build_overall_prompt(
    form_data: dict,
    scorer_result: dict,
    language: str,
) -> str:
    scoring_block = json.dumps(scorer_result, indent=2, ensure_ascii=False)
    form_block = json.dumps(form_data, indent=2, ensure_ascii=False)
    return (
        f"LANGUAGE: {language}. All strings MUST be in {language}.\n\n"
        "## RAW FORM DATA\n\n"
        f"```json\n{form_block}\n```\n\n"
        "## SCORING RESULT\n"
        "(Use scores and gap lists verbatim — do NOT invent numbers.)\n\n"
        f"```json\n{scoring_block}\n```\n\n"
        "Return this exact JSON schema:\n"
        "{\n"
        '  "language": "<Spanish | English>",\n'
        '  "business_type": "<specific classification, e.g. \'pediatric dental clinic\'>",\n'
        '  "overall_summary": "<1-2 sentences interpreting the score — honest, encouraging>",\n'
        '  "chatbot_potential": "<vivid paragraph: what a fully-informed chatbot would handle>",\n'
        '  "next_steps": [\n'
        '    {\n'
        '      "priority": 1,\n'
        '      "action": "<concrete specific action>",\n'
        '      "impact": "<why this is the most impactful change>",\n'
        '      "field_keys": ["<field key>"],\n'
        '      "step": <1 | 2 | 3>\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        "Rules for next_steps: exactly 3 items, ordered by impact descending.\n"
        "Rules for business_type: as specific as possible.\n"
        "No markdown, no extra text, pure JSON only."
    )


# ---------------------------------------------------------------------------
# Full analysis prompt (for the synchronous single-call pipeline path)
# ---------------------------------------------------------------------------

BUSINESS_ANALYZER_SYSTEM_PROMPT = """Role: You are a Chatbot Readiness Analyst. Evaluate business information from a chatbot creation form and return a STRUCTURED JSON REPORT — no prose, no markdown, no extra text.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORM FIELD REGISTRY — MEMORIZE THIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The form has 3 steps. Every `field_keys` and `field_key` you emit MUST only contain keys from this exact list (use these verbatim — no variants):

Step 1 — General Info:
  "description"    → Business description
  "services"       → Products & services
  "mission"        → Mission statement
  "vision"         → Vision statement
  "sales_pitch"    → Sales pitch
  "faq"            → FAQ list
  "additional_info"→ Hours, location, policies
  "social_media"   → Social media links

Step 2 — Contact:
  "name"           → Contact name
  "phone"          → Contact phone
  "company_name"   → Company name

Step 3 — Links & Files:
  "links"          → Resource links / catalog

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
  "field_suggestions": {
    "description": <null | {"suggestion": "...", "example": "..."}>,
    "services": <null | {"suggestion": "...", "example": "..."}>,
    "mission": <null | {"suggestion": "...", "example": "..."}>,
    "vision": <null | {"suggestion": "...", "example": "..."}>,
    "sales_pitch": <null | {"suggestion": "...", "example": "..."}>,
    "faq": <null | {"suggestion": "...", "example": "..."}>,
    "additional_info": <null | {"suggestion": "...", "example": "..."}>,
    "social_media": <null | {"suggestion": "...", "example": "..."}>,
    "name": <null | {"suggestion": "...", "example": "..."}>,
    "phone": <null | {"suggestion": "...", "example": "..."}>,
    "company_name": <null | {"suggestion": "...", "example": "..."}>,
    "links": <null | {"suggestion": "...", "example": "..."}>
  },
  "content_flags": [
    {"field": "<field_key>", "step": <1|2|3>, "issue": "<description in form language>"}
  ],
  "critical_gaps": [
    {
      "category": "<category key>",
      "label": "<human label in form language>",
      "missing_info": "<what information is absent>",
      "field_keys": ["<field key from registry>"],
      "blocked_questions": ["<customer question 1>", "<customer question 2>", "<customer question 3>"],
      "example_answer": "<concrete example of a complete answer for THIS business type>"
    }
  ],
  "weak_fields": [
    {
      "field": "<plain-English field name>",
      "field_key": "<exact key from the registry above>",
      "step": <1 | 2 | 3>,
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
      "action": "<concrete, specific action>",
      "impact": "<why this is the most impactful change>",
      "field_keys": ["<field key from registry>"],
      "step": <1 | 2 | 3>
    }
  ]
}

Rules for field_suggestions: null if field quality is "good". Non-null with suggestion+example if field is empty or weak.
Rules for content_flags: list every field with inappropriate content (profanity, sexual references, brand-damaging language). Empty array if clean.
Rules for critical_gaps: include one entry per category where score < 50, plus `faqs` when detailed_count < floor. Empty array if no gaps.
Rules for weak_fields: one entry per field the scorer flagged as weak. Empty array if none.
Rules for faq_coverage.missing_questions: always 5–8 items regardless of FAQ score.
Rules for next_steps: exactly 3 items, ordered by impact descending.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ABSOLUTE LANGUAGE RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Every string value in the JSON MUST be in the language the form was written in (Spanish or English). Never mix languages. Never default to English. The `language` field must match.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUSINESS TYPE ADAPTATION — MANDATORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — IDENTIFY BUSINESS TYPE
Classify into the most specific type possible. Set `business_type` to this.

STEP 2 — ZERO TOLERANCE FOR GENERIC EXAMPLES
NEVER write a generic e-commerce example when you know the business type. Every example_answer, example_improvement, and suggestion must be specific to the detected business type.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTENT MODERATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Check all text fields for profanity, sexual references, offensive language, or content that would damage the business's or the platform's image. Add any findings to content_flags. Empty array if clean.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- NEVER mention RAG, embeddings, vectors, tokens, similarity search, or any internal system terms.
- Write for a business owner, not a developer.
- Be encouraging but honest."""


# ---------------------------------------------------------------------------
# BusinessAnalyzerAgent
# ---------------------------------------------------------------------------

class BusinessAnalyzerAgent(Agent):
    """Pipeline agent: business form (JSON string) → structured JSON report out.

    Two execution paths:
    - `run()` — synchronous, single LLM call, for the chat pipeline.
    - `analyze_sections_async()` — async generator, 7 parallel LLM calls,
      for the streaming HTTP endpoint.
    """

    def __init__(self, llm_config: LLMConfig | None = None) -> None:
        super().__init__()
        self._llm_config = llm_config or DEFAULT_ANALYZER_CONFIG

    # ------------------------------------------------------------------ #
    # Pipeline path (sync, single call)                                    #
    # ------------------------------------------------------------------ #

    def run(self, ctx: "AgentContext | str") -> "AgentContext | str":
        """Accepts an AgentContext (Pipeline) or a raw JSON string (direct call)."""
        raw = ctx.input if isinstance(ctx, AgentContext) else ctx
        report = self._analyze(raw)
        if isinstance(ctx, AgentContext):
            return dataclasses.replace(ctx, input=report)
        return report

    @staticmethod
    def _resolve_form(payload: dict) -> dict:
        """Resolve the form dict from the parsed JSON input.

        Accepted shapes:
        - {"form_data": {...}}                 → use the inline form dict
        - {"general": {...}, "contact": {...}} → the payload IS the form
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
        """Single blocking LLM call — used by the pipeline / chat path."""
        try:
            payload = json.loads(raw_input)
            if not isinstance(payload, dict):
                raise ValueError("Input JSON must be an object.")

            form_data = self._resolve_form(payload)
            scorer_result = CompletenessScorer().score(form_data)
            language = _detect_form_language(form_data)

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

            llm_data = _safe_parse_llm_json(llm_raw)
            if llm_data is None:
                return json.dumps(
                    {"error": "LLM returned non-JSON output", "raw_report": llm_raw},
                    ensure_ascii=False,
                )

            llm_summaries: dict = llm_data.get("category_summaries") or {}
            categories_out: dict = {}
            for key, cat in scorer_result["categories"].items():
                categories_out[key] = {
                    "score": cat["score"],
                    "weight": cat["weight"],
                    "label": _CATEGORY_LABELS.get(key, key),
                    "summary": llm_summaries.get(key, ""),
                    "missing": cat["missing"],
                    "field_suggestions": {
                        k: (llm_data.get("field_suggestions") or {}).get(k)
                        for k in _CATEGORY_FIELDS[key]
                    },
                }

            faq_llm: dict = llm_data.get("faq_coverage") or {}
            faq_coverage_out = {
                "detailed_count": scorer_result.get("_detailed_faqs", 0),
                "weak_count": scorer_result.get("_weak_faqs", 0),
                "target": _FAQ_TARGET,
                "faq_items": scorer_result.get("_faq_items") or [],
                "missing_questions": faq_llm.get("missing_questions") or [],
            }

            report = {
                "overall_score": scorer_result["overall_score"],
                "overall_summary": llm_data.get("overall_summary", ""),
                "language": llm_data.get("language", language),
                "business_type": llm_data.get("business_type", ""),
                "categories": categories_out,
                "field_details": scorer_result["field_details"],
                "content_flags": llm_data.get("content_flags") or [],
                "critical_gaps": llm_data.get("critical_gaps") or [],
                "weak_fields": llm_data.get("weak_fields") or [],
                "faq_coverage": faq_coverage_out,
                "chatbot_potential": llm_data.get("chatbot_potential", ""),
                "next_steps": llm_data.get("next_steps") or [],
                "present_fields": scorer_result["present_fields"],
                "empty_fields": scorer_result["empty_fields"],
                "weak_field_names": scorer_result["weak_fields"],
                "critical_gap_keys": scorer_result["critical_gaps"],
            }
            return json.dumps(report, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    # Streaming path (async, 7 parallel LLM calls)                        #
    # ------------------------------------------------------------------ #

    async def analyze_sections_async(
        self, raw_input: str
    ) -> AsyncGenerator[dict, None]:
        """Async generator that yields NDJSON chunks as each section completes.

        Chunk types (in emission order):
          1. {"type": "scored", "overall_score": int, "categories": {...},
                "field_details": {...}}          — instant (pure Python)
          2–6. {"type": "section", "key": "<cat>", "data": {...}}
                                                  — as each parallel LLM call resolves
          7. {"type": "content_flags", "flags": [...]}
          8. {"type": "complete", "language": "...", "business_type": "...",
               "overall_summary": "...", "chatbot_potential": "...",
               "next_steps": [...], "faq_coverage": {...}}
        """
        try:
            payload = json.loads(raw_input)
            if not isinstance(payload, dict):
                raise ValueError("Input JSON must be an object.")
            form_data = self._resolve_form(payload)
        except Exception as e:
            yield {"type": "error", "message": str(e)}
            return

        # Step 1: pure Python — instant.
        scorer_result = CompletenessScorer().score(form_data)
        language = _detect_form_language(form_data)

        yield {
            "type": "scored",
            "overall_score": scorer_result["overall_score"],
            "categories": scorer_result["categories"],
            "field_details": scorer_result["field_details"],
            "critical_gap_keys": scorer_result["critical_gaps"],
        }

        # Step 2: infer a rough business type hint from whatever the model can see
        # early — we'll use the company name + description if available.
        general = form_data.get("general") or {}
        contact = form_data.get("contact") or {}
        business_type_hint = (
            f"{contact.get('company_name', '')} — {(general.get('description') or '')[:120]}"
        ).strip(" —")

        # Step 3: launch all async tasks in parallel.
        section_keys = list(_CATEGORY_LABELS.keys())

        async def _run_section(key: str) -> tuple[str, dict]:
            prompt = _build_section_prompt(
                key, form_data, scorer_result, language, business_type_hint
            )
            # FAQs section generates 5-8 detailed example answers — needs more
            # tokens and the stronger model to avoid truncation.
            base_cfg = _FAQ_SECTION_LLM_CONFIG if key == "faqs" else _SECTION_LLM_CONFIG
            config = dataclasses.replace(
                base_cfg,
                system_prompt=_SECTION_SYSTEM,
            )
            set_agent(f"BusinessAnalyzerAgent.{key}")
            try:
                raw = await acall_llm(config, [{"role": "user", "content": prompt}])
                parsed = _safe_parse_llm_json(raw)
                if isinstance(parsed, dict):
                    return key, parsed
            except Exception as e:
                pass
            return key, {"summary": "", "field_suggestions": {}}

        async def _run_overall() -> dict:
            prompt = _build_overall_prompt(form_data, scorer_result, language)
            config = dataclasses.replace(
                _OVERALL_LLM_CONFIG,
                system_prompt=_OVERALL_SYSTEM,
            )
            set_agent("BusinessAnalyzerAgent.overall")
            try:
                raw = await acall_llm(config, [{"role": "user", "content": prompt}])
                parsed = _safe_parse_llm_json(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
            return {}

        async def _run_moderation() -> list[dict]:
            moderator = ContentModerator()
            return await moderator.check_async(form_data, language)

        # Wrap each coroutine to carry an identifier.
        async def _tagged(tag: str, coro):
            result = await coro
            return tag, result

        tasks = [
            asyncio.create_task(_tagged(key, _run_section(key)))
            for key in section_keys
        ]
        tasks.append(asyncio.create_task(_tagged("__overall__", _run_overall())))
        tasks.append(asyncio.create_task(_tagged("__moderation__", _run_moderation())))

        overall_data: dict = {}
        moderation_flags: list[dict] = []

        # Yield chunks as each task completes.
        for future in asyncio.as_completed(tasks):
            tag, result = await future
            if tag == "__overall__":
                overall_data = result if isinstance(result, dict) else {}
            elif tag == "__moderation__":
                moderation_flags = result if isinstance(result, list) else []
                yield {"type": "content_flags", "flags": moderation_flags}
            else:
                # Section result — merge scorer numbers with LLM text.
                key = tag
                cat = scorer_result["categories"][key]
                section_data = result if isinstance(result, dict) else {}
                merged = {
                    "score": cat["score"],
                    "weight": cat["weight"],
                    "label": _CATEGORY_LABELS.get(key, key),
                    "summary": section_data.get("summary", ""),
                    "missing": cat["missing"],
                    "field_suggestions": section_data.get("field_suggestions") or {},
                }
                if "blocked_questions" in section_data:
                    merged["blocked_questions"] = section_data["blocked_questions"]
                if key == "faqs" and "missing_faq_questions" in section_data:
                    merged["missing_faq_questions"] = section_data["missing_faq_questions"]
                yield {"type": "section", "key": key, "data": merged}

        # Final complete chunk — aggregates everything.
        faq_section_data: dict = {}  # collected from section chunks above; re-derive
        # (We don't keep section results in memory above — just yield them.
        # The faq_coverage numeric fields come from the scorer.)
        faq_coverage_out = {
            "detailed_count": scorer_result.get("_detailed_faqs", 0),
            "weak_count": scorer_result.get("_weak_faqs", 0),
            "target": _FAQ_TARGET,
            "faq_items": scorer_result.get("_faq_items") or [],
        }

        yield {
            "type": "complete",
            "overall_score": scorer_result["overall_score"],
            "language": overall_data.get("language", language),
            "business_type": overall_data.get("business_type", ""),
            "overall_summary": overall_data.get("overall_summary", ""),
            "chatbot_potential": overall_data.get("chatbot_potential", ""),
            "next_steps": overall_data.get("next_steps") or [],
            "faq_coverage": faq_coverage_out,
            "critical_gap_keys": scorer_result["critical_gaps"],
            "present_fields": scorer_result["present_fields"],
            "empty_fields": scorer_result["empty_fields"],
            "weak_field_names": scorer_result["weak_fields"],
        }
