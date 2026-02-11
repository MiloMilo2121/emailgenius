from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from typing import Any

from .guardrails import apply_claim_guard
from .types import DraftEmailVariant, EnrichmentDossier, LeadCompany, LeadContact, ParentProfile

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - imported lazily in environments without dependency
    OpenAI = None  # type: ignore[assignment]


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class LLMGateway:
    def __init__(self, *, api_key: str | None, chat_model: str, embedding_model: str) -> None:
        self._api_key = api_key
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._client = OpenAI(api_key=api_key) if (api_key and OpenAI is not None) else None

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        if self._client is None:
            return [_hash_embedding(text) for text in texts]

        try:
            response = self._client.embeddings.create(model=self._embedding_model, input=texts)
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
    ) -> tuple[list[DraftEmailVariant], str, list[str]]:
        if self._client is None:
            return _fallback_variants(parent, company, contact, dossier)

        payload = {
            "parent_profile": asdict(parent),
            "target_company": asdict(company),
            "target_contact": asdict(contact) if contact else None,
            "dossier": {
                **asdict(dossier),
                "news_items": [asdict(item) for item in dossier.news_items],
            },
            "retrieved_marketing_knowledge": marketing_snippets,
            "constraints": {
                "language": "italiano",
                "tone": "formale-consulenziale",
                "variants_required": ["A", "B", "C"],
                "cta": "call conoscitiva 20-30 minuti",
                "no_absolute_claims": True,
                "no_ai_disclosure": True,
            },
        }

        system_prompt = (
            "Sei un copywriter B2B senior. Genera email outbound in italiano, stile formale-consulenziale. "
            "Niente promesse assolute o claim non verificabili. "
            "Output SOLO JSON valido con chiavi: variants, recommended_variant, notes."
        )
        user_prompt = json.dumps(payload, ensure_ascii=False)

        try:
            response = self._client.chat.completions.create(
                model=self._chat_model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw_content = response.choices[0].message.content or "{}"
            parsed = json.loads(raw_content)
            variants_raw = parsed.get("variants", [])
            recommended = str(parsed.get("recommended_variant") or "A").upper()

            variants: list[DraftEmailVariant] = []
            global_flags: list[str] = []
            for index, item in enumerate(variants_raw):
                variant_name = str(item.get("variant") or chr(ord("A") + index)).upper()
                subject = str(item.get("subject") or "Proposta di confronto operativo")
                body = str(item.get("body") or "")
                cta = str(item.get("cta") or parent.cta_policy)
                cleaned_text, flags = apply_claim_guard(
                    f"Oggetto: {subject}\n\n{body}",
                    parent.no_go_claims,
                )
                if "\n\n" in cleaned_text:
                    subject_line, body_text = cleaned_text.split("\n\n", 1)
                    subject = subject_line.replace("Oggetto:", "").strip() or subject
                    body = body_text.strip() or body
                confidence = _clamp(float(item.get("confidence") or 0.65))
                variants.append(
                    DraftEmailVariant(
                        variant=variant_name,
                        subject=subject,
                        body=body,
                        cta=cta,
                        risk_flags=flags,
                        confidence=confidence,
                    )
                )
                global_flags.extend(flags)

            variants = _ensure_three_variants(variants, parent, company, contact, dossier)
            recommended = _normalize_recommended(recommended, variants)
            return variants, recommended, sorted(set(global_flags))
        except Exception:
            return _fallback_variants(parent, company, contact, dossier)


def _normalize_recommended(value: str, variants: list[DraftEmailVariant]) -> str:
    names = {variant.variant for variant in variants}
    value = (value or "A").strip().upper()
    if value in names:
        return value
    return variants[0].variant if variants else "A"


def _ensure_three_variants(
    variants: list[DraftEmailVariant],
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    dossier: EnrichmentDossier,
) -> list[DraftEmailVariant]:
    names = {variant.variant for variant in variants}
    defaults, _, _ = _fallback_variants(parent, company, contact, dossier)
    for fallback in defaults:
        if fallback.variant not in names:
            variants.append(fallback)
    variants = sorted(variants, key=lambda item: item.variant)
    return variants[:3]


def _fallback_variants(
    parent: ParentProfile,
    company: LeadCompany,
    contact: LeadContact | None,
    dossier: EnrichmentDossier,
) -> tuple[list[DraftEmailVariant], str, list[str]]:
    contact_name = contact.full_name if contact and contact.full_name else "Team"
    company_name = company.company_name

    pain_line = dossier.pain_hypotheses[0] if dossier.pain_hypotheses else "ottimizzazione operativa e priorita' commerciali"
    opp_line = dossier.opportunity_hypotheses[0] if dossier.opportunity_hypotheses else "migliorare performance e prevedibilita'"

    templates = [
        (
            "A",
            f"Confronto operativo per {company_name}",
            (
                f"Gentile {contact_name},\n\n"
                f"seguiamo aziende {company.industry or 'manifatturiere'} come {company_name} e notiamo spesso che {pain_line}. "
                f"Con il team {parent.company_name} stiamo supportando aziende simili su {opp_line}.\n\n"
                f"Se utile, possiamo fissare una call conoscitiva di 20-30 minuti per valutare priorita', vincoli e possibili quick win.\n\n"
                "Cordiali saluti"
            ),
        ),
        (
            "B",
            f"Idea concreta per {company_name}",
            (
                f"Buongiorno {contact_name},\n\n"
                f"dalle informazioni pubbliche su {company_name} emerge un contesto interessante su {opp_line}. "
                f"{parent.company_name} lavora su iniziative pratiche con un approccio graduale e misurabile.\n\n"
                "Possiamo condividere in una call di 20-30 minuti un framework operativo adattato al vostro contesto.\n\n"
                "Resto a disposizione"
            ),
        ),
        (
            "C",
            f"Proposta di allineamento: {company_name}",
            (
                f"Gentile {contact_name},\n\n"
                f"scrivo per proporre un breve confronto: su aziende comparabili a {company_name} vediamo valore nel lavorare su {pain_line}. "
                f"Il team {parent.company_name} puo' supportarvi con un perimetro iniziale molto concreto.\n\n"
                "Se ha senso, organizziamo una call conoscitiva da 20-30 minuti.\n\n"
                "Grazie del tempo"
            ),
        ),
    ]

    variants: list[DraftEmailVariant] = []
    all_flags: list[str] = []
    for name, subject, body in templates:
        cleaned, flags = apply_claim_guard(f"Oggetto: {subject}\n\n{body}", parent.no_go_claims)
        subject_line, body_text = cleaned.split("\n\n", 1)
        variants.append(
            DraftEmailVariant(
                variant=name,
                subject=subject_line.replace("Oggetto:", "").strip(),
                body=body_text.strip(),
                cta=parent.cta_policy,
                risk_flags=flags,
                confidence=0.62 if name == "A" else 0.58,
            )
        )
        all_flags.extend(flags)

    return variants, "A", sorted(set(all_flags))


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
