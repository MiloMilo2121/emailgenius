# Sintesi Esecutiva (Versione Operativa)

## Decisione architetturale consigliata

- **Browser:** Playwright come standard; Puppeteer solo per esigenze CDP specialistiche.
- **Orchestrazione:** LangGraph con checkpoint persistenti e human-in-the-loop.
- **Dati:** PostgreSQL + pgvector (fase iniziale), tuning successivo con pgvectorscale solo dopo benchmark interni.
- **LLM:** routing dinamico per task, non scelta monolitica di un solo modello.
- **Unblocking:** managed browser/proxy stack con policy anti-lock-in.

## Perche' e' la scelta migliore nel 2026

1. Bilancia resilienza tecnica e time-to-value.
2. Riduce debito operativo su anti-bot.
3. Mantiene controllo su costi e compliance.
4. Resta evolutiva rispetto al cambio rapido dei modelli AI.

## Correzioni prioritarie al whitepaper originale

1. Correggere Puppeteer (non piu' mono-engine).
2. Rimuovere benchmark LLM non confermati da fonti primarie.
3. Segnare i benchmark proxy come vendor claims.
4. Aggiornare stato Transizione 5.0 (risorse esaurite al 7 novembre 2025).
5. Rafforzare compliance outreach B2B su ePrivacy/art.130 soft-spam.

## Fonti chiave

- Playwright docs: https://playwright.dev/docs/actionability
- Puppeteer FAQ: https://pptr.dev/faq
- Cloudflare bot docs: https://developers.cloudflare.com/bots/additional-configurations/ja3-ja4-fingerprint/
- OpenAI GPT-5: https://openai.com/index/introducing-gpt-5/
- Anthropic models overview: https://docs.anthropic.com/en/docs/models-overview
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- MIMIT Transizione 5.0: https://www.mimit.gov.it/it/incentivi/piano-transizione-5-0
- MIMIT esaurimento risorse: https://www.mimit.gov.it/it/notizie-stampa/mimit-esaurite-le-risorse-transizione-5-0
- AI Act timeline: https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai
- ePrivacy Directive: https://eur-lex.europa.eu/eli/dir/2002/58/2009-12-19/eng
