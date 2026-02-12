from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any

from .guardrails import apply_claim_guard
from .types import DraftEmailVariant, EnrichmentDossier, LeadCompany, LeadContact, ParentProfile

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - imported lazily in environments without dependency
    OpenAI = None  # type: ignore[assignment]


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def format_email_subject(value: str) -> str:
    subject = (value or "").replace("\r", " ").replace("\n", " ").strip()
    subject = re.sub(r"\s+", " ", subject)
    return subject[:120].rstrip()


def format_email_body(value: str) -> str:
    body = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    body = "\n".join(line.rstrip() for line in body.split("\n"))
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _rewrite_targets_for_variants(variant_names: list[str]) -> dict[str, tuple[float, float]]:
    defaults = {
        "A": (0.25, 0.30),
        "B": (0.50, 0.60),
        "C": (0.35, 0.45),
    }
    out: dict[str, tuple[float, float]] = {}
    for name in variant_names:
        out[name] = defaults.get(name.upper(), (0.25, 0.40))
    return out


_HARD_QUALITY_FLAGS = {
    # Deliverability / obvious "spam" signals
    "spam_caps",
    "spam_excessive_exclamation",
    "spam_clickbait_subject",
    "subject_too_long",
    # Going far beyond rewrite target is a brand-risk signal (structure drift, new claims).
    "rewrite_over_target",
}


def _coerce_variants_raw(value: object, *, preferred_order: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return _coerce_variants_raw(parsed, preferred_order=preferred_order)

    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(item)
                continue
            if isinstance(item, str):
                # Some models may emit array of JSON strings or plain bodies.
                try:
                    parsed = json.loads(item)
                except Exception:
                    out.append({"body": item})
                    continue
                if isinstance(parsed, dict):
                    out.append(parsed)
        return out

    if isinstance(value, dict):
        # If it's already a single-variant object.
        if any(key in value for key in ("variant", "subject", "body", "cta")):
            return [value]

        # Otherwise treat as map { "A": {...}, "B": {...} } or similar.
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _push(name: str, payload: object) -> None:
            key = name.strip().upper()
            if not key or key in seen:
                return
            seen.add(key)
            if isinstance(payload, dict):
                item = dict(payload)
                item.setdefault("variant", key)
                out.append(item)
                return
            if isinstance(payload, str):
                out.append({"variant": key, "body": payload})
                return
            out.append({"variant": key})

        for key in preferred_order:
            if key in value:
                _push(key, value.get(key))
        for key, payload in value.items():
            if str(key).upper() in seen:
                continue
            _push(str(key), payload)
        return out

    return []


class LLMGateway:
    def __init__(self, *, api_key: str | None, chat_model: str, embedding_model: str) -> None:
        self._api_key = api_key
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._chat_timeout_s = 90.0
        self._embedding_timeout_s = 45.0
        self._client = (
            OpenAI(
                api_key=api_key,
                timeout=self._chat_timeout_s,
                max_retries=0,
            )
            if (api_key and OpenAI is not None)
            else None
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        if self._client is None:
            return [_hash_embedding(text) for text in texts]

        try:
            response = self._client.embeddings.create(
                model=self._embedding_model,
                input=texts,
                timeout=self._embedding_timeout_s,
            )
            return [item.embedding for item in response.data]
        except Exception:
            return [_hash_embedding(text) for text in texts]

    def generate_campaign_variants(
        self,
        *,
        parent: ParentProfile,
        company: LeadCompany,
        contact: LeadContact | None,
        dossier: EnrichmentDossier,
        marketing_snippets: list[str],
        variant_mode: str = "ab",
        llm_policy: str = "strict",
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
    ) -> tuple[list[DraftEmailVariant], str, list[str]]:
        requested_variants = _variant_names_for_mode(variant_mode)
        rewrite_targets = _rewrite_targets_for_variants(requested_variants)
        if self._client is None:
            if llm_policy == "strict":
                raise RuntimeError("LLM unavailable: configure OPENAI_API_KEY or set --llm-policy fallback")
            return _fallback_variants(
                parent=parent,
                company=company,
                contact=contact,
                dossier=dossier,
                variant_names=requested_variants,
                rewrite_targets=rewrite_targets,
            )

        payload = {
            "parent_profile": asdict(parent),
            "target_company": asdict(company),
            "target_contact": asdict(contact) if contact else None,
            "dossier": {
                **asdict(dossier),
                "news_items": [asdict(item) for item in dossier.news_items],
            },
            "retrieved_marketing_knowledge": marketing_snippets,
            "seed_template": parent.outreach_seed_template,
            "constraints": {
                "language": "italiano",
                "tone": parent.tone,
                "variants_required": requested_variants,
                "rewrite_budget_per_variant": {
                    name: {
                        "target_range_pct": f"{int(bounds[0] * 100)}-{int(bounds[1] * 100)}",
                        "min_pct": int(bounds[0] * 100),
                        "max_pct": int(bounds[1] * 100),
                    }
                    for name, bounds in rewrite_targets.items()
                },
                "keep_seed_structure": True,
                "personalization_scope": [
                    "incipit",
                    "riferimento ruolo/azienda",
                    "micro-angolo valore",
                    "subject",
                ],
                "anti_spam": {
                    "no_all_caps_aggressive": True,
                    "max_exclamation_marks": 1,
                    "no_artificial_urgency": True,
                    "no_clickbait_subject": True,
                },
                "subject_line": {
                    "max_words": 9,
                    "max_chars": 70,
                },
                "formatting": {
                    "short_paragraphs": True,
                    "blank_line_between_paragraphs": True,
                    "no_manual_line_wrapping": True,
                    "signature_as_final_block": True,
                    "use_bullets_for_long_lists": True,
                },
                "no_invented_facts": True,
                "no_absolute_claims": True,
                "no_ai_disclosure": True,
            },
        }

        system_prompt = (
            "Sei un copywriter B2B senior in italiano. "
            "Devi rispettare in modo rigido il seed template e mantenerne la struttura complessiva. "
            "Applica budget diversi per variante: A con riscrittura contenuta (25-30%), "
            "B con riscrittura ampia ma controllata (50-60%), C intermedia quando richiesta. "
            "Personalizza solo incipit, riferimento ruolo/azienda, micro-angolo valore e subject. "
            "Evita toni spam, clickbait, urgenza artificiale, MAIUSCOLO aggressivo, claim assoluti/non verificabili. "
            "Oggetto: specifico e corto (idealmente <= 70 caratteri, <= 9 parole). "
            "Formato: paragrafi brevi (1-2 frasi), una riga vuota tra paragrafi e tra blocchi tematici; "
            "non andare a capo manualmente dentro un paragrafo; firma come blocco finale separato. "
            "Se devi elencare piu di 3 elementi, usa bullet '-' (una voce per riga) con una riga vuota prima e dopo il blocco. "
            "Non inventare dettagli: usa solo dati presenti nel JSON (company/contact/dossier/snippets); se mancano, resta generico. "
            "Output SOLO JSON valido con chiavi: variants, recommended_variant, quality_notes."
        )
        user_prompt = json.dumps(payload, ensure_ascii=False)

        attempt = 0
        while attempt <= max_retries:
            try:
                parsed = self._call_chat_json(system_prompt=system_prompt, user_prompt=user_prompt)
                variants_raw = _coerce_variants_raw(parsed.get("variants", []), preferred_order=requested_variants)
                recommended = str(parsed.get("recommended_variant") or requested_variants[0]).upper()

                variants: list[DraftEmailVariant] = []
                global_flags: list[str] = []
                for index, item in enumerate(variants_raw):
                    variant_name = str(item.get("variant") or requested_variants[min(index, len(requested_variants) - 1)]).upper()
                    subject = format_email_subject(str(item.get("subject") or _fallback_subject(company=company, contact=contact)))
                    body = format_email_body(str(item.get("body") or _render_seed_template(parent, company, contact)))
                    cta = str(item.get("cta") or parent.cta_policy)

                    cleaned_text, claim_flags = apply_claim_guard(
                        f"Oggetto: {subject}\n\n{body}",
                        parent.no_go_claims,
                    )
                    if "\n\n" in cleaned_text:
                        subject_line, body_text = cleaned_text.split("\n\n", 1)
                        subject = format_email_subject(subject_line.replace("Oggetto:", "").strip() or subject)
                        body = format_email_body(body_text.strip() or body)

                    quality_flags = _quality_gate_flags(
                        subject=subject,
                        body=body,
                        seed_template=parent.outreach_seed_template,
                        variant_name=variant_name,
                        rewrite_targets=rewrite_targets,
                    )

                    if quality_flags:
                        repaired = self._repair_variant(
                            seed_template=parent.outreach_seed_template,
                            subject=subject,
                            body=body,
                            variant_name=variant_name,
                            rewrite_targets=rewrite_targets,
                            quality_flags=quality_flags,
                        )
                        if repaired is not None:
                            repaired_subject, repaired_body = repaired
                            repaired_subject = format_email_subject(repaired_subject)
                            repaired_body = format_email_body(repaired_body)
                            repaired_flags = _quality_gate_flags(
                                subject=repaired_subject,
                                body=repaired_body,
                                seed_template=parent.outreach_seed_template,
                                variant_name=variant_name,
                                rewrite_targets=rewrite_targets,
                            )
                            if not repaired_flags:
                                subject, body = repaired_subject, repaired_body
                                quality_flags = ["quality_repaired"]
                            else:
                                hard_flags = [flag for flag in repaired_flags if flag in _HARD_QUALITY_FLAGS]
                                if hard_flags:
                                    quality_flags = sorted(set(quality_flags + repaired_flags + ["failed_copy_guard"]))
                                else:
                                    # Soft misses (e.g. rewrite budget slightly off, formatting preferences) are warnings,
                                    # not blockers.
                                    subject, body = repaired_subject, repaired_body
                                    quality_flags = sorted(set(repaired_flags + ["quality_repaired"]))
                        else:
                            hard_flags = [flag for flag in quality_flags if flag in _HARD_QUALITY_FLAGS]
                            if hard_flags:
                                quality_flags = sorted(set(quality_flags + ["failed_copy_guard"]))

                    confidence = _clamp(float(item.get("confidence") or 0.65))
                    all_flags = sorted(set(claim_flags + quality_flags))
                    variants.append(
                        DraftEmailVariant(
                            variant=variant_name,
                            subject=subject,
                            body=body,
                            cta=cta,
                            risk_flags=all_flags,
                            confidence=confidence,
                        )
                    )
                    global_flags.extend(all_flags)

                variants = _ensure_variants(
                    variants,
                    parent,
                    company,
                    contact,
                    dossier,
                    requested_variants,
                    rewrite_targets,
                )
                recommended = _normalize_recommended(recommended, variants)
                return variants, recommended, sorted(set(global_flags))
            except Exception as exc:
                kind = _classify_exception(exc)
                if kind == "fatal":
                    raise RuntimeError(f"LLM fatal error: {exc}") from exc

                if attempt >= max_retries:
                    if llm_policy == "strict":
                        raise RuntimeError(f"LLM retry exhausted: {exc}") from exc
                    return _fallback_variants(
                        parent=parent,
                        company=company,
                        contact=contact,
                        dossier=dossier,
                        variant_names=requested_variants,
                        rewrite_targets=rewrite_targets,
                    )

                sleep_s = backoff_base_seconds * (2**attempt)
                time.sleep(max(0.0, sleep_s))
                attempt += 1

        if llm_policy == "strict":
            raise RuntimeError("LLM retry exhausted")
        return _fallback_variants(
            parent=parent,
            company=company,
            contact=contact,
            dossier=dossier,
            variant_names=requested_variants,
            rewrite_targets=rewrite_targets,
        )

    def _call_chat_json(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("LLM client unavailable")
        response = self._client.chat.completions.create(
            model=self._chat_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=self._chat_timeout_s,
        )
        raw_content = response.choices[0].message.content or "{}"
        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict):
            raise RuntimeError("Unexpected LLM response format")
        return parsed

    def _repair_variant(
        self,
        *,
        seed_template: str,
        subject: str,
        body: str,
        variant_name: str,
        rewrite_targets: dict[str, tuple[float, float]],
        quality_flags: list[str],
    ) -> tuple[str, str] | None:
        if self._client is None:
            return None
        min_rewrite, max_rewrite = rewrite_targets.get(variant_name.upper(), (0.25, 0.40))

        repair_prompt = {
            "seed_template": seed_template,
            "candidate_subject": subject,
            "candidate_body": body,
            "variant": variant_name.upper(),
            "rewrite_target_pct": {
                "min": int(min_rewrite * 100),
                "max": int(max_rewrite * 100),
            },
            "quality_flags": quality_flags,
            "instructions": [
                "mantieni la struttura del seed template",
                "rispetta il range di riscrittura richiesto per la variante",
                "rimuovi pattern spam/clickbait",
                "mantieni tono business-safe",
                "usa paragrafi brevi (1-2 frasi) con UNA riga vuota tra i paragrafi",
                "non andare a capo manualmente dentro un paragrafo; usa il newline solo tra paragrafi/blocchi",
                "firma come blocco finale separato da una riga vuota",
            ],
        }
        system_prompt = (
            "Correggi il testo email mantenendo struttura e intenti. "
            "Output SOLO JSON con chiavi subject e body."
        )
        try:
            parsed = self._call_chat_json(
                system_prompt=system_prompt,
                user_prompt=json.dumps(repair_prompt, ensure_ascii=False),
            )
        except Exception:
            return None
        repaired_subject = str(parsed.get("subject") or "").strip()
        repaired_body = str(parsed.get("body") or "").strip()
        if not repaired_subject or not repaired_body:
            return None
        return repaired_subject, repaired_body


def _variant_names_for_mode(variant_mode: str) -> list[str]:
    if variant_mode.lower() == "abc":
        return ["A", "B", "C"]
    return ["A", "B"]


def _normalize_recommended(value: str, variants: list[DraftEmailVariant]) -> str:
    names = {variant.variant for variant in variants}
    value = (value or "A").strip().upper()
    if value in names:
        return value
    return variants[0].variant if variants else "A"


def _ensure_variants(
    variants: list[DraftEmailVariant],
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    dossier: EnrichmentDossier,
    requested_variants: list[str],
    rewrite_targets: dict[str, tuple[float, float]],
) -> list[DraftEmailVariant]:
    names = {variant.variant for variant in variants}
    defaults, _, _ = _fallback_variants(
        parent=parent,
        company=company,
        contact=contact,
        dossier=dossier,
        variant_names=requested_variants,
        rewrite_targets=rewrite_targets,
    )
    for fallback in defaults:
        if fallback.variant not in names:
            variants.append(fallback)
    variants = sorted((item for item in variants if item.variant in requested_variants), key=lambda item: item.variant)
    return variants[: len(requested_variants)]


def _fallback_variants(
    *,
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    dossier: EnrichmentDossier,
    variant_names: list[str],
    rewrite_targets: dict[str, tuple[float, float]],
) -> tuple[list[DraftEmailVariant], str, list[str]]:
    rendered = _render_seed_template(parent, company, contact)
    contact_name = contact.full_name if contact and contact.full_name else "Team"
    subject_a = _fallback_subject(company=company, contact=contact)
    subject_b = f"{company.company_name}: confronto operativo su opportunita concrete"
    subject_c = f"{company.company_name}: proposta di analisi preliminare"

    templates = {
        "A": rendered,
        "B": (
            f"Ciao {contact_name},\n\n"
            f"ti propongo un confronto rapido su {company.company_name}. "
            "Molte aziende simili stanno finanziando investimenti con contributi che riducono "
            "l'esborso iniziale e liberano cassa operativa.\n\n"
            "Possiamo verificare insieme se nel tuo caso ci sono opportunita concrete, "
            "con un'analisi mirata ai prossimi investimenti e alle priorita reali del business.\n\n"
            f"{parent.sender_name or parent.company_name}\n"
            f"{parent.sender_company or parent.company_name}"
        ),
        "C": (
            f"Ciao {contact_name},\n\n"
            f"partendo da informazioni pubbliche su {company.company_name}, "
            "abbiamo identificato alcune opportunita da verificare in modo operativo.\n\n"
            f"{rendered}"
        ),
    }
    subjects = {"A": subject_a, "B": subject_b, "C": subject_c}

    variants: list[DraftEmailVariant] = []
    global_flags: list[str] = []
    for name in variant_names:
        text = templates.get(name, rendered)
        subject = subjects.get(name, subject_a)
        cleaned, flags = apply_claim_guard(f"Oggetto: {subject}\n\n{text}", parent.no_go_claims)
        subject_line, body_text = cleaned.split("\n\n", 1)
        quality_flags = _quality_gate_flags(
            subject=subject_line.replace("Oggetto:", "").strip(),
            body=body_text.strip(),
            seed_template=parent.outreach_seed_template,
            variant_name=name,
            rewrite_targets=rewrite_targets,
        )
        all_flags = sorted(set(flags + quality_flags))
        variants.append(
            DraftEmailVariant(
                variant=name,
                subject=subject_line.replace("Oggetto:", "").strip(),
                body=body_text.strip(),
                cta=parent.cta_policy,
                risk_flags=all_flags,
                confidence=0.58,
            )
        )
        global_flags.extend(all_flags)

    recommended = variant_names[0] if variant_names else "A"
    return variants, recommended, sorted(set(global_flags))


def _fallback_subject(*, company: LeadCompany, contact: LeadContact | None) -> str:
    first_name = _contact_first_name(contact)
    if first_name:
        return f"{first_name}, opportunita concrete per {company.company_name}"
    return f"Opportunita concrete per {company.company_name}"


def render_seed_template(parent: ParentProfile, company: LeadCompany, contact: LeadContact | None) -> str:
    first_name = _contact_first_name(contact) or "tu"
    template = parent.outreach_seed_template or ""
    replacements = {
        "{{first_name}}": first_name,
        "{{firstName}}": first_name,
        "{{company_name}}": company.company_name,
        "{{sender_name}}": parent.sender_name or parent.company_name,
        "{{sender_company}}": parent.sender_company or parent.company_name,
        "{{sender_phone}}": parent.sender_phone or "",
        "{{sender_booking_url}}": parent.sender_booking_url or "",
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered.strip()


def _render_seed_template(parent: ParentProfile, company: LeadCompany, contact: LeadContact | None) -> str:
    # Back-compat internal name used across the codebase.
    return render_seed_template(parent, company, contact)


def _contact_first_name(contact: LeadContact | None) -> str:
    if not contact or not contact.full_name:
        return ""
    return contact.full_name.split()[0].strip()


def _quality_gate_flags(
    *,
    subject: str,
    body: str,
    seed_template: str,
    variant_name: str,
    rewrite_targets: dict[str, tuple[float, float]],
) -> list[str]:
    flags: list[str] = []
    combined = f"{subject}\n{body}"
    all_caps_words = re.findall(r"\b[A-Z]{5,}\b", combined)
    if all_caps_words:
        flags.append("spam_caps")

    exclamation_count = combined.count("!")
    if exclamation_count > 1:
        flags.append("spam_excessive_exclamation")

    subject_l = subject.lower()
    clickbait_tokens = ("gratis", "imperdibile", "solo oggi", "urgente", "subito")
    if any(token in subject_l for token in clickbait_tokens):
        flags.append("spam_clickbait_subject")

    if len(subject.strip()) > 70:
        flags.append("subject_too_long")

    # Encourage scan-friendly formatting: enough whitespace between thematic blocks.
    normalized_body = format_email_body(body)
    if len(normalized_body) > 240 and normalized_body.count("\n\n") < 2:
        flags.append("format_needs_whitespace")

    norm_seed = _normalize_similarity_text(seed_template)
    norm_body = _normalize_similarity_text(body)
    if norm_seed and norm_body:
        similarity = SequenceMatcher(a=norm_seed[:2400], b=norm_body[:2400]).ratio()
        rewrite_ratio = 1.0 - similarity
        min_rewrite, max_rewrite = rewrite_targets.get(variant_name.upper(), (0.25, 0.40))
        tolerance = 0.08
        if rewrite_ratio < max(0.0, min_rewrite - tolerance):
            flags.append("rewrite_under_target")
        if rewrite_ratio > min(1.0, max_rewrite + tolerance):
            flags.append("rewrite_over_target")

    return sorted(set(flags))


def _normalize_similarity_text(value: str) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip().lower()
    compact = re.sub(r"\{\{[^}]+\}\}", "", compact)
    return compact


def _classify_exception(exc: Exception) -> str:
    message = str(exc).lower()
    fatal_tokens = (
        "api key",
        "authentication",
        "invalid_api_key",
        "insufficient_quota",
        "billing",
        "model not found",
        "permission",
    )
    if any(token in message for token in fatal_tokens):
        return "fatal"

    transient_tokens = (
        "429",
        "rate limit",
        "timeout",
        "temporarily",
        "500",
        "502",
        "503",
        "504",
        "connection",
    )
    if any(token in message for token in transient_tokens):
        return "transient"
    return "transient"


def _hash_embedding(text: str, dim: int = 1536) -> list[float]:
    vector = [0.0] * dim
    tokens = re.findall(r"[a-zA-Z0-9_]{2,}", text.lower())
    if not tokens:
        return vector

    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:2], "big") % dim
        sign = 1.0 if (digest[2] % 2 == 0) else -1.0
        vector[idx] += sign

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector

    return [value / norm for value in vector]
