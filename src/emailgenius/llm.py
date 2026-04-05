from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from .guardrails import apply_claim_guard
from .search import is_event_news_hit
from .types import (
    DraftEmailVariant,
    EnrichmentDossier,
    InstantlyDraft,
    LeadCompany,
    LeadContact,
    ParentProfile,
    ResearchDossier,
    ResearchSource,
)

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


def apply_template_replacements(
    value: str,
    *,
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
) -> str:
    first_name = _contact_first_name(contact) or "tu"
    replacements = {
        "{{first_name}}": first_name,
        "{{firstName}}": first_name,
        "{{firstname}}": first_name,
        "{{company_name}}": company.company_name,
        "{{companyName}}": company.company_name,
        "{{sender_name}}": parent.sender_name or parent.company_name,
        "{{sender_company}}": parent.sender_company or parent.company_name,
        "{{sender_phone}}": parent.sender_phone or "",
        "{{sender_booking_url}}": parent.sender_booking_url or "",
    }

    rendered = value or ""
    for key, replacement in replacements.items():
        rendered = rendered.replace(key, replacement)

    # Remove any remaining mustache placeholders to avoid sending unresolved templates.
    rendered = re.sub(r"\{\{[^}]+\}\}", "", rendered)
    return rendered


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
    def __init__(
        self,
        *,
        api_key: str | None,
        chat_model: str,
        embedding_model: str,
        api_base_url: str | None = None,
        fallback_api_key: str | None = None,
        fallback_chat_model: str | None = None,
        fallback_api_base_url: str | None = None,
        research_model: str | None = None,
        writer_model: str | None = None,
        writer_fallback_model: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._research_model = (research_model or chat_model).strip()
        self._writer_model = (writer_model or chat_model).strip()
        self._writer_fallback_model = (writer_fallback_model or fallback_chat_model or self._writer_model).strip()
        self._chat_timeout_s = 90.0
        self._embedding_timeout_s = 45.0
        self._embedding_client = self._build_client(api_key=api_key, base_url=api_base_url)
        self._chat_targets: list[tuple[Any, str]] = []
        self._research_targets: list[tuple[Any, str]] = []
        self._writer_targets: list[tuple[Any, str]] = []
        if self._embedding_client is not None:
            self._chat_targets.append((self._embedding_client, self._writer_model))
            self._research_targets.append((self._embedding_client, self._research_model))
            self._writer_targets.append((self._embedding_client, self._writer_model))
            if self._writer_fallback_model and self._writer_fallback_model != self._writer_model:
                self._writer_targets.append((self._embedding_client, self._writer_fallback_model))

        resolved_fallback_model = (fallback_chat_model or chat_model).strip()
        fallback_client = self._build_client(api_key=fallback_api_key, base_url=fallback_api_base_url)
        if fallback_client is not None and resolved_fallback_model:
            same_key = (api_key or "").strip() == (fallback_api_key or "").strip()
            same_base_url = (api_base_url or "").strip() == (fallback_api_base_url or "").strip()
            same_model = resolved_fallback_model == chat_model
            if not (same_key and same_base_url and same_model):
                self._chat_targets.append((fallback_client, self._writer_fallback_model or resolved_fallback_model))
                self._research_targets.append((fallback_client, self._research_model or resolved_fallback_model))
                self._writer_targets.append((fallback_client, self._writer_fallback_model or resolved_fallback_model))

    def _build_client(self, *, api_key: str | None, base_url: str | None) -> Any | None:
        key = (api_key or "").strip()
        if not key or OpenAI is None:
            return None
        kwargs: dict[str, Any] = {
            "api_key": key,
            "timeout": self._chat_timeout_s,
            "max_retries": 0,
        }
        resolved_base_url = (base_url or "").strip()
        if resolved_base_url:
            kwargs["base_url"] = resolved_base_url
        return OpenAI(**kwargs)

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3), reraise=True)
    def _call_embeddings_api(self, *, client: Any, model: str, texts: list[str]) -> Any:
        return client.embeddings.create(
            model=model,
            input=texts,
            timeout=self._embedding_timeout_s,
        )

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3), reraise=True)
    def _call_chat_api(self, *, client: Any, model: str, system_prompt: str, user_prompt: str) -> Any:
        return client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=self._chat_timeout_s,
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        embed_model = (self._embedding_model or "").strip()
        if embed_model.lower() in {"", "hash", "local", "none", "disabled", "hash-local"}:
            return [_hash_embedding(text) for text in texts]
        if self._embedding_client is None:
            return [_hash_embedding(text) for text in texts]

        try:
            response = self._call_embeddings_api(
                client=self._embedding_client,
                model=embed_model,
                texts=texts,
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
        real_insights = _extract_real_insights(company, dossier)
        if not self._chat_targets:
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
            "real_personalization_insights": [
                {"fact": fact, "meaning": meaning}
                for fact, meaning in real_insights
            ],
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
                "personalization_rules": {
                    "use_real_insights_if_available": bool(real_insights),
                    "real_insights_min": 1 if real_insights else 0,
                    "real_insights_max": 2,
                    "connect_fact_to_meaning": True,
                },
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
            "Se nel JSON trovi real_personalization_insights, usa 1-2 fatti reali massimo e collega ogni fatto "
            "a un significato operativo concreto per il prospect. "
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
                    subject_raw = str(item.get("subject") or _fallback_subject(company=company, contact=contact))
                    body_raw = str(item.get("body") or render_seed_template(parent, company, contact))
                    subject = format_email_subject(
                        apply_template_replacements(
                            subject_raw,
                            parent=parent,
                            company=company,
                            contact=contact,
                        )
                    )
                    body = format_email_body(
                        apply_template_replacements(
                            body_raw,
                            parent=parent,
                            company=company,
                            contact=contact,
                        )
                    )
                    cta = str(item.get("cta") or parent.cta_policy)

                    cleaned_text, claim_flags = apply_claim_guard(
                        f"Oggetto: {subject}\n\n{body}",
                        parent.no_go_claims,
                    )
                    if "\n\n" in cleaned_text:
                        subject_line, body_text = cleaned_text.split("\n\n", 1)
                        subject = apply_template_replacements(
                            subject_line.replace("Oggetto:", "").strip() or subject,
                            parent=parent,
                            company=company,
                            contact=contact,
                        )
                        body = apply_template_replacements(
                            body_text.strip() or body,
                            parent=parent,
                            company=company,
                            contact=contact,
                        )
                        subject = format_email_subject(subject)
                        body = format_email_body(body)

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
                            repaired_subject = apply_template_replacements(
                                repaired_subject,
                                parent=parent,
                                company=company,
                                contact=contact,
                            )
                            repaired_body = apply_template_replacements(
                                repaired_body,
                                parent=parent,
                                company=company,
                                contact=contact,
                            )
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
        return self._call_chat_json_targets(
            targets=self._chat_targets,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    def _call_chat_json_targets(
        self,
        *,
        targets: list[tuple[Any, str]],
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        if not targets:
            raise RuntimeError("LLM client unavailable")
        last_exc: Exception | None = None
        for client, model in targets:
            try:
                response = self._call_chat_api(
                    client=client,
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                raw_content = response.choices[0].message.content or "{}"
                parsed = json.loads(raw_content)
                if not isinstance(parsed, dict):
                    raise RuntimeError("Unexpected LLM response format")
                return parsed
            except Exception as exc:
                last_exc = exc

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("LLM client unavailable")

    def generate_research_dossier(
        self,
        *,
        parent: ParentProfile,
        company: LeadCompany,
        contact: LeadContact | None,
        research_bundle: dict[str, object],
        llm_policy: str = "strict",
    ) -> ResearchDossier:
        if not self._research_targets:
            if llm_policy == "strict":
                raise RuntimeError("Research model unavailable: configure OPENROUTER_API_KEY or fallback provider")
            return _fallback_research_dossier(company=company, research_bundle=research_bundle)

        system_prompt = (
            "Sei un analyst B2B. "
            "Ricevi dati grezzi su un'azienda e devi restituire SOLO JSON valido. "
            "Non inventare nulla. Usa solo fatti osservabili nei risultati. "
            "I risultati possono includere web, Instagram e LinkedIn pubblici. "
            "Scegli un trigger concreto se esiste; altrimenti lascia trigger_event vuoto. "
            "La personalizzazione deve essere utile per una cold email B2B italiana, non generica."
        )
        payload = {
            "parent": {
                "company_name": parent.company_name,
                "tone": parent.tone,
                "offer_catalog": parent.offer_catalog,
                "proof_points": parent.proof_points,
                "icp": parent.icp,
            },
            "company": asdict(company),
            "contact": asdict(contact) if contact else None,
            "research_bundle": research_bundle,
            "output_schema": {
                "company_summary": "string",
                "trigger_event": "string",
                "pain_hypothesis": "string",
                "personalization_angle": "string",
                "key_facts": ["string"],
                "recent_news": [
                    {
                        "title": "string",
                        "url": "string",
                        "snippet": "string",
                        "published_at": "string|null",
                    }
                ],
                "citations": ["string"],
                "confidence": "float 0..1",
            },
        }
        try:
            parsed = self._call_chat_json_targets(
                targets=self._research_targets,
                system_prompt=system_prompt,
                user_prompt=json.dumps(payload, ensure_ascii=False),
            )
            return _research_dossier_from_payload(parsed, company=company, research_bundle=research_bundle)
        except Exception as exc:
            if llm_policy == "strict":
                raise RuntimeError(f"Research dossier generation failed: {exc}") from exc
            return _fallback_research_dossier(company=company, research_bundle=research_bundle)

    def generate_instantly_draft(
        self,
        *,
        parent: ParentProfile,
        company: LeadCompany,
        contact: LeadContact | None,
        research_dossier: ResearchDossier,
        llm_policy: str = "strict",
    ) -> InstantlyDraft:
        if not self._writer_targets:
            if llm_policy == "strict":
                raise RuntimeError("Writer model unavailable: configure OPENROUTER_API_KEY or fallback provider")
            return _fallback_instantly_draft(parent=parent, company=company, contact=contact, research_dossier=research_dossier)

        intro_line = format_email_body(
            apply_template_replacements(
                parent.instantly_intro_template or render_seed_template(parent, company, contact),
                parent=parent,
                company=company,
                contact=contact,
            )
        )
        cta_line = format_email_body(
            apply_template_replacements(
                parent.instantly_cta_template or parent.cta_policy,
                parent=parent,
                company=company,
                contact=contact,
            )
        )

        system_prompt = (
            "Sei un copywriter B2B senior per cold email italiane. "
            "Devi scrivere SOLO JSON valido con subject_line e personalization. "
            "La personalization e' il blocco variabile centrale per Instantly: 2-4 frasi, concreta, credibile, no hype. "
            "Non ripetere CTA o firma. Non inventare fatti. Se il trigger e' debole, resta sobrio."
        )
        payload = {
            "parent": {
                "company_name": parent.company_name,
                "tone": parent.tone,
                "offer_catalog": parent.offer_catalog,
                "proof_points": parent.proof_points,
                "no_go_claims": parent.no_go_claims,
            },
            "company": asdict(company),
            "contact": asdict(contact) if contact else None,
            "research_dossier": asdict(research_dossier),
            "fixed_intro": intro_line,
            "fixed_cta": cta_line,
            "rules": {
                "language": "italiano",
                "subject_max_chars": 70,
                "subject_max_words": 9,
                "personalization_focus": [
                    "trigger_event",
                    "pain_hypothesis",
                    "personalization_angle",
                    "1-2 key facts massimo",
                ],
                "forbidden": [
                    "claim assoluti",
                    "garantito",
                    "100%",
                    "urgenza artificiale",
                    "toni spam",
                ],
            },
        }
        try:
            parsed = self._call_chat_json_targets(
                targets=self._writer_targets,
                system_prompt=system_prompt,
                user_prompt=json.dumps(payload, ensure_ascii=False),
            )
            return _instantly_draft_from_payload(
                parsed,
                parent=parent,
                company=company,
                contact=contact,
                intro_line=intro_line,
                cta_line=cta_line,
            )
        except Exception as exc:
            if llm_policy == "strict":
                raise RuntimeError(f"Instantly draft generation failed: {exc}") from exc
            return _fallback_instantly_draft(parent=parent, company=company, contact=contact, research_dossier=research_dossier)

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
        if not self._chat_targets:
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


def _extract_real_insights(company: LeadCompany, dossier: EnrichmentDossier) -> list[tuple[str, str]]:
    insights: list[tuple[str, str]] = []
    seen_facts: set[str] = set()

    def _push(fact: str, meaning: str) -> None:
        fact_clean = " ".join((fact or "").split()).strip()
        meaning_clean = " ".join((meaning or "").split()).strip()
        if not fact_clean or not meaning_clean:
            return
        key = fact_clean.lower()
        if key in seen_facts:
            return
        seen_facts.add(key)
        insights.append((fact_clean, meaning_clean))

    if dossier.news_items:
        event_hit = next((item for item in dossier.news_items if is_event_news_hit(item)), None)
        selected_hit = event_hit or dossier.news_items[0]
        title = str(selected_hit.title or "").strip()
        if title:
            if event_hit is not None:
                _push(
                    f"Evento recente aziendale: {title}",
                    "indica una fase attiva (investimenti, fiere o partnership) in cui ha senso proporre una verifica operativa su opportunita finanziabili.",
                )
            else:
                _push(
                    f"Notizia recente: {title}",
                    "significa che c'e' movimento strategico in corso e conviene proporre un confronto operativo ora.",
                )

    industry = (company.industry or "").strip()
    if industry:
        _push(
            f"Settore rilevato: {industry}",
            "per personalizzare l'email su casi, linguaggio e priorita' specifiche del settore.",
        )

    homepage_evidence = ""
    for item in dossier.evidence:
        text = str(item).strip()
        if text.lower().startswith("homepage title:"):
            homepage_evidence = text
            break
    if homepage_evidence:
        _push(
            homepage_evidence,
            "indica il posizionamento pubblico attuale dell'azienda e aiuta a rendere l'incipit credibile.",
        )

    if dossier.pain_hypotheses:
        pain = str(dossier.pain_hypotheses[0] or "").strip()
        if pain:
            _push(
                f"Pain operativo ipotizzato: {pain}",
                "consente di proporre una micro-soluzione concreta invece di un messaggio generico.",
            )

    return insights[:2]


def _render_insight_block(company: LeadCompany, dossier: EnrichmentDossier) -> str:
    insights = _extract_real_insights(company, dossier)
    if not insights:
        return ""

    lines = ["Dai dati pubblici emergono due punti utili:"]
    for fact, meaning in insights:
        lines.append(f"- {fact}. Significato: {meaning}")
    return "\n".join(lines).strip()


def _fallback_variants(
    *,
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    dossier: EnrichmentDossier,
    variant_names: list[str],
    rewrite_targets: dict[str, tuple[float, float]],
) -> tuple[list[DraftEmailVariant], str, list[str]]:
    rendered = render_seed_template(parent, company, contact)
    insight_block = _render_insight_block(company, dossier)
    rendered_with_insights = (
        f"{rendered}\n\n{insight_block}".strip() if insight_block else rendered
    )
    insight_section = f"{insight_block}\n\n" if insight_block else ""
    contact_name = contact.full_name if contact and contact.full_name else "Team"
    subject_a = _fallback_subject(company=company, contact=contact)
    subject_b = f"{company.company_name}: confronto operativo su opportunita concrete"
    subject_c = f"{company.company_name}: proposta di analisi preliminare"

    templates = {
        "A": rendered_with_insights,
        "B": (
            f"Ciao {contact_name},\n\n"
            f"ti propongo un confronto rapido su {company.company_name}. "
            "Molte aziende simili stanno finanziando investimenti con contributi che riducono "
            "l'esborso iniziale e liberano cassa operativa.\n\n"
            f"{insight_section}"
            "Possiamo verificare insieme se nel tuo caso ci sono opportunita concrete, "
            "con un'analisi mirata ai prossimi investimenti e alle priorita reali del business.\n\n"
            f"{parent.sender_name or parent.company_name}\n"
            f"{parent.sender_company or parent.company_name}"
        ),
        "C": (
            f"Ciao {contact_name},\n\n"
            f"partendo da informazioni pubbliche su {company.company_name}, "
            "abbiamo identificato alcune opportunita da verificare in modo operativo.\n\n"
            f"{rendered_with_insights}"
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


def render_instantly_email(
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    personalization: str,
) -> str:
    intro_line = format_email_body(
        apply_template_replacements(
            parent.instantly_intro_template or render_seed_template(parent, company, contact),
            parent=parent,
            company=company,
            contact=contact,
        )
    )
    cta_line = format_email_body(
        apply_template_replacements(
            parent.instantly_cta_template or parent.cta_policy,
            parent=parent,
            company=company,
            contact=contact,
        )
    )
    blocks = [intro_line.strip(), format_email_body(personalization or "").strip(), cta_line.strip()]
    return format_email_body("\n\n".join(block for block in blocks if block))


def render_instantly_body_template(
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    personalization_placeholder: str = "{{Personalization}}",
) -> str:
    intro_line = format_email_body(
        apply_template_replacements(
            parent.instantly_intro_template or render_seed_template(parent, company, contact),
            parent=parent,
            company=company,
            contact=contact,
        )
    )
    cta_line = format_email_body(
        apply_template_replacements(
            parent.instantly_cta_template or parent.cta_policy,
            parent=parent,
            company=company,
            contact=contact,
        )
    )
    blocks = [intro_line.strip(), personalization_placeholder.strip(), cta_line.strip()]
    return format_email_body("\n\n".join(block for block in blocks if block))


def render_seed_template(parent: ParentProfile, company: LeadCompany, contact: LeadContact | None) -> str:
    template = parent.outreach_seed_template or ""
    rendered = apply_template_replacements(
        template,
        parent=parent,
        company=company,
        contact=contact,
    )
    return rendered.strip()


def _contact_first_name(contact: LeadContact | None) -> str:
    if not contact or not contact.full_name:
        return ""
    return contact.full_name.split()[0].strip()


def _research_dossier_from_payload(
    payload: dict[str, Any],
    *,
    company: LeadCompany,
    research_bundle: dict[str, object],
) -> ResearchDossier:
    news_payload = payload.get("recent_news")
    recent_news: list[ResearchSource] = []
    if isinstance(news_payload, list):
        for item in news_payload:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title or not url:
                continue
            recent_news.append(
                ResearchSource(
                    title=title,
                    url=url,
                    snippet=str(item.get("snippet") or "").strip(),
                    source_type="news",
                    published_at=str(item.get("published_at") or "").strip() or None,
                )
            )

    if not recent_news:
        recent_news = _research_sources_from_bundle(research_bundle, "news_results")

    citations = payload.get("citations")
    key_facts = payload.get("key_facts")
    selected_sources = research_bundle.get("selected_sources")
    normalized_sources = (
        [str(item).strip() for item in selected_sources if str(item).strip()]
        if isinstance(selected_sources, list)
        else []
    )
    resolved_citations = [str(item).strip() for item in citations if str(item).strip()] if isinstance(citations, list) else []
    if not resolved_citations:
        fallback_sources = (
            recent_news[:2]
            + _research_sources_from_bundle(research_bundle, "official_pages")[:1]
            + _research_sources_from_bundle(research_bundle, "instagram_profiles")[:1]
            + _research_sources_from_bundle(research_bundle, "linkedin_profiles")[:1]
        )
        resolved_citations = [item.url for item in fallback_sources if item.url]
    return ResearchDossier(
        company_name=company.company_name,
        domain=_domain_from_company(company),
        company_summary=str(payload.get("company_summary") or "").strip(),
        trigger_event=str(payload.get("trigger_event") or "").strip(),
        pain_hypothesis=str(payload.get("pain_hypothesis") or "").strip(),
        personalization_angle=str(payload.get("personalization_angle") or "").strip(),
        key_facts=[str(item).strip() for item in key_facts if str(item).strip()] if isinstance(key_facts, list) else [],
        recent_news=recent_news,
        citations=resolved_citations,
        research_sources=normalized_sources,
        confidence=_clamp(float(payload.get("confidence") or 0.65)),
    )


def _fallback_research_dossier(
    *,
    company: LeadCompany,
    research_bundle: dict[str, object],
) -> ResearchDossier:
    official_pages = _research_sources_from_bundle(research_bundle, "official_pages")
    news_results = _research_sources_from_bundle(research_bundle, "news_results")
    instagram_profiles = _research_sources_from_bundle(research_bundle, "instagram_profiles")
    instagram_posts = _research_sources_from_bundle(research_bundle, "instagram_posts")
    linkedin_profiles = _research_sources_from_bundle(research_bundle, "linkedin_profiles")
    linkedin_posts = _research_sources_from_bundle(research_bundle, "linkedin_posts")
    facts: list[str] = []
    if company.industry:
        facts.append(f"Settore: {company.industry}")
    if company.location:
        facts.append(f"Location: {company.location}")
    if company.employee_count:
        facts.append(f"Dimensione stimata: {company.employee_count} dipendenti")
    if official_pages:
        facts.append(f"Pagina rilevata: {official_pages[0].title}")
    if news_results:
        facts.append(f"Notizia recente: {news_results[0].title}")
    if instagram_profiles:
        facts.append(f"Profilo Instagram pubblico rilevato: {instagram_profiles[0].title}")
    if instagram_posts:
        facts.append(f"Contenuto Instagram rilevato: {instagram_posts[0].title}")
    if linkedin_profiles:
        facts.append(f"Profilo LinkedIn pubblico rilevato: {linkedin_profiles[0].title}")
    if linkedin_posts:
        facts.append(f"Aggiornamento LinkedIn rilevato: {linkedin_posts[0].title}")

    trigger_event = news_results[0].title if news_results else ""
    if not trigger_event and linkedin_posts:
        trigger_event = linkedin_posts[0].title
    if not trigger_event and instagram_posts:
        trigger_event = instagram_posts[0].title
    pain = "possibile bisogno di rendere piu' efficace il posizionamento commerciale su un momento aziendale concreto"
    angle = facts[0] if facts else f"Azienda osservata: {company.company_name}"
    citations = [
        item.url
        for item in (
            news_results[:2]
            + official_pages[:2]
            + instagram_profiles[:1]
            + instagram_posts[:1]
            + linkedin_profiles[:1]
            + linkedin_posts[:1]
        )
        if item.url
    ]
    selected_sources = research_bundle.get("selected_sources")
    return ResearchDossier(
        company_name=company.company_name,
        domain=_domain_from_company(company),
        company_summary=f"Profilo sintetico costruito da fonti esterne per {company.company_name}.",
        trigger_event=trigger_event,
        pain_hypothesis=pain,
        personalization_angle=angle,
        key_facts=facts[:5],
        recent_news=news_results[:4],
        citations=citations,
        research_sources=[str(item).strip() for item in selected_sources if str(item).strip()] if isinstance(selected_sources, list) else [],
        confidence=0.45,
    )


def _instantly_draft_from_payload(
    payload: dict[str, Any],
    *,
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    intro_line: str,
    cta_line: str,
) -> InstantlyDraft:
    subject_line = format_email_subject(
        apply_template_replacements(
            str(payload.get("subject_line") or _fallback_subject(company=company, contact=contact)),
            parent=parent,
            company=company,
            contact=contact,
        )
    )
    personalization = format_email_body(
        apply_template_replacements(
            str(payload.get("personalization") or "").strip(),
            parent=parent,
            company=company,
            contact=contact,
        )
    )
    if not personalization:
        personalization = f"Ho notato {company.company_name} e credo abbia senso aprire un confronto concreto sul vostro contesto attuale."
    full_body = render_instantly_email(parent, company, contact, personalization)
    _, claim_flags = apply_claim_guard(
        f"Oggetto: {subject_line}\n\n{full_body}",
        parent.no_go_claims,
    )
    risk_flags = sorted(set(claim_flags + _instantly_quality_flags(subject=subject_line, personalization=personalization)))
    return InstantlyDraft(
        subject_line=subject_line,
        personalization=personalization,
        intro_line=intro_line,
        cta_line=cta_line,
        body_template=render_instantly_body_template(parent, company, contact),
        confidence=_clamp(float(payload.get("confidence") or 0.7)),
        risk_flags=risk_flags,
    )


def _fallback_instantly_draft(
    *,
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    research_dossier: ResearchDossier,
) -> InstantlyDraft:
    intro_line = format_email_body(
        apply_template_replacements(
            parent.instantly_intro_template or render_seed_template(parent, company, contact),
            parent=parent,
            company=company,
            contact=contact,
        )
    )
    cta_line = format_email_body(
        apply_template_replacements(
            parent.instantly_cta_template or parent.cta_policy,
            parent=parent,
            company=company,
            contact=contact,
        )
    )
    parts: list[str] = []
    if research_dossier.trigger_event:
        parts.append(f"Ho visto un segnale recente su {company.company_name}: {research_dossier.trigger_event}.")
    if research_dossier.personalization_angle:
        parts.append(research_dossier.personalization_angle)
    elif research_dossier.pain_hypothesis:
        parts.append(research_dossier.pain_hypothesis)
    personalization = format_email_body(" ".join(parts).strip() or f"Mi sembra che su {company.company_name} abbia senso aprire un confronto concreto e molto mirato.")
    subject_line = _fallback_subject(company=company, contact=contact)
    risk_flags = _instantly_quality_flags(subject=subject_line, personalization=personalization)
    return InstantlyDraft(
        subject_line=subject_line,
        personalization=personalization,
        intro_line=intro_line,
        cta_line=cta_line,
        body_template=render_instantly_body_template(parent, company, contact),
        confidence=0.52,
        risk_flags=risk_flags,
    )


def _research_sources_from_bundle(research_bundle: dict[str, object], key: str) -> list[ResearchSource]:
    section = research_bundle.get(key)
    if not isinstance(section, dict):
        return []
    results = section.get("results")
    if not isinstance(results, list):
        return []
    out: list[ResearchSource] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        out.append(
            ResearchSource(
                title=title,
                url=url,
                snippet=str(item.get("snippet") or "").strip(),
                source_type=str(item.get("source_type") or "").strip(),
                published_at=str(item.get("published_at") or "").strip() or None,
            )
        )
    return out


def _domain_from_company(company: LeadCompany) -> str | None:
    website = (company.website or "").strip()
    if not website:
        return None
    match = re.match(r"^https?://([^/]+)", website, flags=re.IGNORECASE)
    if not match:
        return None
    host = match.group(1).lower()
    return host[4:] if host.startswith("www.") else host


def _instantly_quality_flags(*, subject: str, personalization: str) -> list[str]:
    flags: list[str] = []
    if len(subject.strip()) > 70:
        flags.append("subject_too_long")
    if personalization.count("!") > 1:
        flags.append("spam_excessive_exclamation")
    if len(personalization.strip()) > 900:
        flags.append("personalization_too_long")
    return sorted(set(flags))


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

    # Detect old-school manual line wrapping (ragged plaintext) and ask the repair pass to reflow.
    raw_lines = [line.strip() for line in (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    non_empty_lines = [line for line in raw_lines if line]
    if non_empty_lines and len(non_empty_lines) >= 12 and normalized_body.count("\n\n") < 2:
        bullet_markers = ("-", "*", "+", "•")
        bulletish = sum(1 for line in non_empty_lines if line.startswith(bullet_markers))
        if bulletish / max(len(non_empty_lines), 1) < 0.30:
            avg_len = sum(len(line) for line in non_empty_lines) / len(non_empty_lines)
            if avg_len < 70:
                flags.append("format_manual_wrap")

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
