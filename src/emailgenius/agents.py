from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .guardrails import apply_claim_guard
from .llm import LLMGateway, format_email_body, format_email_subject, render_seed_template
from .types import EnrichmentDossier, LeadCompany, LeadContact, ParentProfile, SequenceResult, SequenceStep

try:
    from langgraph.graph import END, StateGraph
except Exception:  # pragma: no cover - optional dependency
    END = "__end__"
    StateGraph = None  # type: ignore[assignment]


class CampaignAgentEngine:
    def __init__(
        self,
        *,
        llm: LLMGateway,
        max_compliance_retries: int = 2,
        checkpointer: Any | None = None,
    ) -> None:
        self._llm = llm
        self._max_compliance_retries = max(0, int(max_compliance_retries))
        self._checkpointer = checkpointer

    def generate_sequence(
        self,
        *,
        parent: ParentProfile,
        company: LeadCompany,
        contact: LeadContact | None,
        dossier: EnrichmentDossier,
        marketing_snippets: list[str],
        llm_policy: str = "strict",
    ) -> SequenceResult:
        if StateGraph is None:
            state = self._run_state_machine_fallback(
                parent=parent,
                company=company,
                contact=contact,
                dossier=dossier,
                marketing_snippets=marketing_snippets,
                llm_policy=llm_policy,
            )
            return self._to_sequence_result(state)

        graph = StateGraph(dict)
        graph.add_node("strategist", self._strategist_node)
        graph.add_node("icebreaker", self._icebreaker_writer_node)
        graph.add_node("followup", self._followup_writer_node)
        graph.add_node("compliance", self._compliance_guard_node)

        graph.set_entry_point("strategist")
        graph.add_edge("strategist", "icebreaker")
        graph.add_edge("icebreaker", "followup")
        graph.add_edge("followup", "compliance")
        graph.add_conditional_edges(
            "compliance",
            self._after_compliance,
            {
                "rewrite": "icebreaker",
                "done": END,
            },
        )

        initial_state: dict[str, Any] = {
            "parent": parent,
            "company": company,
            "contact": contact,
            "dossier": dossier,
            "marketing_snippets": marketing_snippets,
            "llm_policy": llm_policy,
            "retry_count": 0,
            "attack_angle": "",
            "trigger_facts": [],
            "sequence_steps": [],
            "compliance_flags": [],
            "needs_rewrite": False,
        }
        try:
            app = graph.compile(checkpointer=self._checkpointer)
            final_state = app.invoke(initial_state)
            return self._to_sequence_result(final_state)
        except Exception:
            state = self._run_state_machine_fallback(
                parent=parent,
                company=company,
                contact=contact,
                dossier=dossier,
                marketing_snippets=marketing_snippets,
                llm_policy=llm_policy,
            )
            return self._to_sequence_result(state)

    def _run_state_machine_fallback(
        self,
        *,
        parent: ParentProfile,
        company: LeadCompany,
        contact: LeadContact | None,
        dossier: EnrichmentDossier,
        marketing_snippets: list[str],
        llm_policy: str,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "parent": parent,
            "company": company,
            "contact": contact,
            "dossier": dossier,
            "marketing_snippets": marketing_snippets,
            "llm_policy": llm_policy,
            "retry_count": 0,
            "attack_angle": "",
            "trigger_facts": [],
            "sequence_steps": [],
            "compliance_flags": [],
            "needs_rewrite": False,
        }

        state = self._strategist_node(state)
        while True:
            state = self._icebreaker_writer_node(state)
            state = self._followup_writer_node(state)
            state = self._compliance_guard_node(state)
            if self._after_compliance(state) == "done":
                break
        return state

    def _strategist_node(self, state: dict[str, Any]) -> dict[str, Any]:
        dossier: EnrichmentDossier = state["dossier"]
        company: LeadCompany = state["company"]

        trigger_facts: list[str] = []
        if dossier.news_items:
            first_news = dossier.news_items[0]
            trigger_facts.append(f"Notizia recente: {first_news.title}")
        if dossier.pain_hypotheses:
            trigger_facts.append(f"Pain: {dossier.pain_hypotheses[0]}")
        if company.industry:
            trigger_facts.append(f"Settore: {company.industry}")

        if trigger_facts:
            attack_angle = "Trigger-based outreach su evento recente con impatto operativo"
        else:
            attack_angle = "Outreach consultivo con ipotesi operativa"

        state["attack_angle"] = attack_angle
        state["trigger_facts"] = trigger_facts[:3]
        return state

    def _icebreaker_writer_node(self, state: dict[str, Any]) -> dict[str, Any]:
        parent: ParentProfile = state["parent"]
        company: LeadCompany = state["company"]
        contact: LeadContact | None = state["contact"]
        trigger_facts: list[str] = list(state.get("trigger_facts") or [])

        default_subject = format_email_subject(f"{company.company_name}: spunto operativo")
        default_body = format_email_body(render_seed_template(parent, company, contact))

        prompt_payload = {
            "company": asdict(company),
            "contact": asdict(contact) if contact else None,
            "attack_angle": state.get("attack_angle") or "",
            "trigger_facts": trigger_facts,
            "goal": "Aprire la conversazione con una prima riga iper-personalizzata sul trigger.",
            "constraints": {
                "language": "italiano",
                "tone": parent.tone,
                "max_words_subject": 9,
                "max_chars_subject": 70,
                "body_max_words": 110,
                "single_cta": True,
            },
        }

        parsed = self._safe_llm_json(
            system_prompt=(
                "Scrivi esclusivamente EMAIL 1 di cold outreach B2B. "
                "Output solo JSON con chiavi: subject, body, goal."
            ),
            payload=prompt_payload,
        )

        subject = format_email_subject(str(parsed.get("subject") or default_subject))
        body = format_email_body(str(parsed.get("body") or default_body))
        goal = str(parsed.get("goal") or "Aprire conversazione su trigger rilevato")

        step = SequenceStep(
            step_id="E1",
            subject=subject,
            body=body,
            goal=goal,
            confidence=0.7,
        )
        steps: list[SequenceStep] = list(state.get("sequence_steps") or [])
        steps = [item for item in steps if item.step_id != "E1"]
        steps.append(step)
        state["sequence_steps"] = steps
        return state

    def _followup_writer_node(self, state: dict[str, Any]) -> dict[str, Any]:
        parent: ParentProfile = state["parent"]
        company: LeadCompany = state["company"]
        contact: LeadContact | None = state["contact"]
        snippets: list[str] = list(state.get("marketing_snippets") or [])
        trigger_facts: list[str] = list(state.get("trigger_facts") or [])

        existing_steps: list[SequenceStep] = list(state.get("sequence_steps") or [])
        e1 = next((item for item in existing_steps if item.step_id == "E1"), None)

        prompt_payload = {
            "company": asdict(company),
            "contact": asdict(contact) if contact else None,
            "attack_angle": state.get("attack_angle") or "",
            "trigger_facts": trigger_facts,
            "email1": asdict(e1) if e1 else None,
            "retrieved_snippets": snippets[:8],
            "constraints": {
                "language": "italiano",
                "single_cta": True,
                "step_ids": ["E2", "E3", "BREAKUP"],
            },
        }

        parsed = self._safe_llm_json(
            system_prompt=(
                "Scrivi follow-up sequence per cold outreach. "
                "Output JSON con chiave steps (array di oggetti con step_id, subject, body, goal)."
            ),
            payload=prompt_payload,
        )
        generated_steps = parsed.get("steps") if isinstance(parsed.get("steps"), list) else []

        out_steps: list[SequenceStep] = [item for item in existing_steps if item.step_id == "E1"]
        for step_id in ["E2", "E3", "BREAKUP"]:
            source = next(
                (item for item in generated_steps if isinstance(item, dict) and str(item.get("step_id") or "").upper() == step_id),
                None,
            )
            if source is None:
                source = self._fallback_step(step_id=step_id, parent=parent, company=company, contact=contact)
            subject = format_email_subject(str(source.get("subject") or ""))
            body = format_email_body(str(source.get("body") or ""))
            goal = str(source.get("goal") or "Follow-up operativo")
            out_steps.append(
                SequenceStep(
                    step_id=step_id,
                    subject=subject,
                    body=body,
                    goal=goal,
                    confidence=0.68,
                )
            )

        state["sequence_steps"] = out_steps
        return state

    def _compliance_guard_node(self, state: dict[str, Any]) -> dict[str, Any]:
        parent: ParentProfile = state["parent"]
        steps: list[SequenceStep] = list(state.get("sequence_steps") or [])

        all_flags: list[str] = []
        cleaned_steps: list[SequenceStep] = []
        for step in steps:
            cleaned, flags = apply_claim_guard(
                f"Oggetto: {step.subject}\n\n{step.body}",
                parent.no_go_claims,
            )
            if "\n\n" in cleaned:
                subject_line, body_text = cleaned.split("\n\n", 1)
                step.subject = format_email_subject(subject_line.replace("Oggetto:", "").strip() or step.subject)
                step.body = format_email_body(body_text.strip() or step.body)
            step.risk_flags = sorted(set(list(step.risk_flags) + list(flags)))
            all_flags.extend(flags)
            cleaned_steps.append(step)

        retry_count = int(state.get("retry_count") or 0)
        hard_flags = [flag for flag in all_flags if flag.startswith("claim_") or flag.startswith("no_go:")]
        state["sequence_steps"] = cleaned_steps
        state["compliance_flags"] = sorted(set(all_flags))
        state["needs_rewrite"] = bool(hard_flags and retry_count < self._max_compliance_retries)
        if state["needs_rewrite"]:
            state["retry_count"] = retry_count + 1
        return state

    def _after_compliance(self, state: dict[str, Any]) -> str:
        return "rewrite" if bool(state.get("needs_rewrite")) else "done"

    def _safe_llm_json(self, *, system_prompt: str, payload: dict[str, object]) -> dict[str, Any]:
        call = getattr(self._llm, "_call_chat_json", None)
        if not callable(call):
            return {}
        try:
            response = call(system_prompt=system_prompt, user_prompt=json.dumps(payload, ensure_ascii=False))
        except Exception:
            return {}
        return response if isinstance(response, dict) else {}

    def _fallback_step(
        self,
        *,
        step_id: str,
        parent: ParentProfile,
        company: LeadCompany,
        contact: LeadContact | None,
    ) -> dict[str, str]:
        first_name = (contact.full_name.split()[0].strip() if contact and contact.full_name else "")
        hello = f"Ciao {first_name}," if first_name else "Ciao,"
        if step_id == "E2":
            return {
                "step_id": "E2",
                "subject": f"{company.company_name}: mini case study operativo",
                "body": (
                    f"{hello}\n\n"
                    "ti condivido un caso numerico simile: una PMI manifatturiera ha ridotto tempi di avvio "
                    "commerciale con un percorso di pre-qualifica piu rigoroso.\n\n"
                    "Se vuoi, in 15 minuti verifichiamo se il pattern e replicabile anche da voi."
                ),
                "goal": "Rendere credibile la proposta con prova numerica.",
            }
        if step_id == "E3":
            return {
                "step_id": "E3",
                "subject": f"{company.company_name}: domanda diretta",
                "body": (
                    f"{hello}\n\n"
                    "domanda secca: in questo trimestre la priorita e aumentare velocita di acquisizione o margine per commessa?\n\n"
                    "Ti rispondo in modo pratico sul punto che scegli."
                ),
                "goal": "Qualificare la priorita principale del prospect.",
            }
        return {
            "step_id": "BREAKUP",
            "subject": f"{company.company_name}: chiudo qui",
            "body": (
                f"{hello}\n\n"
                "chiudo il thread senza insistere. Se il tema torna prioritario, possiamo ripartire con un check rapido.\n\n"
                f"{parent.sender_name or parent.company_name}"
            ),
            "goal": "Chiusura professionale lasciando porta aperta.",
        }

    def _to_sequence_result(self, state: dict[str, Any]) -> SequenceResult:
        steps = list(state.get("sequence_steps") or [])
        normalized: list[SequenceStep] = []
        for step in steps:
            if isinstance(step, SequenceStep):
                normalized.append(step)
                continue
            if isinstance(step, dict):
                normalized.append(
                    SequenceStep(
                        step_id=str(step.get("step_id") or ""),
                        subject=str(step.get("subject") or ""),
                        body=str(step.get("body") or ""),
                        goal=str(step.get("goal") or ""),
                        risk_flags=list(step.get("risk_flags") or []),
                        confidence=float(step.get("confidence") or 0.0),
                    )
                )

        order = {"E1": 0, "E2": 1, "E3": 2, "BREAKUP": 3}
        normalized.sort(key=lambda item: order.get(item.step_id.upper(), 9))
        return SequenceResult(
            attack_angle=str(state.get("attack_angle") or "Outreach consultivo"),
            trigger_facts=[str(item) for item in list(state.get("trigger_facts") or [])][:3],
            steps=normalized,
            recommended_next_step="E2" if normalized else None,
            global_risk_flags=[str(item) for item in list(state.get("compliance_flags") or [])],
        )
