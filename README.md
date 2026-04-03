# EmailGenius

Sistema CLI + app locale per campagne email B2B multi-azienda madre con:
- contesto persistente per parent company (`slug`),
- ingest CSV lead con canonicalizzazione header + preflight validazione,
- enrichment pubblico (sito, news, link LinkedIn pubblici),
- RAG marketing su PostgreSQL + pgvector,
- generazione varianti email (`A/B` default, `A/B/C` legacy),
- quality gates (claim guard, anti-spam, rewrite-budget) con repair pass,
- coda approvazione su Google Sheet + export CSV send-ready (outer join input+output),
- retention automatica dati campagna (default 90 giorni).

Direzione consigliata:
- `App-first`: lavori sempre dalla UI.
- `PostgreSQL locale`: salva parent, knowledge, campagne e output.
- `Google Drive`: opzionale, solo se vuoi import/export o collaborazione Workspace.
- `Exa + OpenRouter`: stack consigliato per ricerca low-cost + scrittura personalizzata.

Non serve Supabase per questa fase: l'app usa gia `PostgreSQL` come source of truth e puo` vivere interamente in locale.

## Requisiti

- Python 3.10+
- PostgreSQL con estensione `pgvector`
- (opzionale) Chromium Playwright per enrichment web profondo
- (opzionale) credenziali Google Service Account per publish su Sheet

## Setup consigliato

Setup a sbattimento minimo:

1. Installa Docker Desktop.
2. Copia `.env.example` in `.env.local`.
3. Inserisci almeno `OPENAI_API_KEY` in `.env.local`.
4. Avvia tutto con lo script locale.

```bash
cp .env.example .env.local
./scripts/start-local.sh
```

Poi apri [http://127.0.0.1:8080](http://127.0.0.1:8080).

Questo flusso:
- avvia PostgreSQL locale con volume persistente Docker,
- usa `.emailgenius/` per file runtime, upload, report e backup,
- lascia Drive completamente opzionale.

## Setup manuale

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Variabili ambiente

```bash
export EMAILGENIUS_DATABASE_URL="postgresql://postgres:postgres@localhost:5432/emailgenius"
export OPENAI_API_KEY="..."                              # provider primario (OpenAI-compatible)
export OPENAI_BASE_URL="https://open.bigmodel.cn/api/paas/v4/"
export OPENROUTER_API_KEY="..."                         # consigliato per routing multi-modello low-cost
export OPENROUTER_BASE_URL="https://openrouter.ai/api/v1"
export EXA_API_KEY="..."                                # consigliato per research aziendale/news
export EMAILGENIUS_OPENAI_CHAT_MODEL="glm-4.7-flashx"
export EMAILGENIUS_OPENAI_EMBED_MODEL="hash-local"      # embedding locale gratuito
export EMAILGENIUS_OPENAI_FALLBACK_API_KEY="..."        # opzionale provider secondario
export EMAILGENIUS_OPENAI_FALLBACK_BASE_URL="https://api.deepseek.com"
export EMAILGENIUS_OPENAI_FALLBACK_CHAT_MODEL="deepseek-chat"
export EMAILGENIUS_RESEARCH_MODEL="deepseek/deepseek-chat-v3.1"
export EMAILGENIUS_WRITER_MODEL="anthropic/claude-3.5-haiku"
export EMAILGENIUS_WRITER_FALLBACK_MODEL="anthropic/claude-sonnet-4.5"
export GOOGLE_SERVICE_ACCOUNT_JSON="/absolute/path/service-account.json"
export EMAILGENIUS_RETENTION_DAYS="90"
export EMAILGENIUS_WORKSPACE_FOLDER_ID="<GOOGLE_DRIVE_FOLDER_ID>"
export EMAILGENIUS_DRIVE_POLL_INTERVAL_SECONDS="60"
export EMAILGENIUS_IO_MODE="local"
```

`OPENAI_BASE_URL`/`EMAILGENIUS_OPENAI_BASE_URL` permette di usare endpoint OpenAI-compatible
(es. Z.AI, DeepSeek, Kimi). Se il provider primario fallisce, `EMAILGENIUS_OPENAI_FALLBACK_*`
viene usato come backup.

Stack consigliato per il flusso UI-first:
- `EXA_API_KEY` per recuperare dati e news aziendali
- `OPENROUTER_API_KEY` per orchestrare:
  - modello research economico (`EMAILGENIUS_RESEARCH_MODEL`)
  - modello writer (`EMAILGENIUS_WRITER_MODEL`)

Con questa configurazione la UI genera:
- dossier JSON aziendale
- subject line
- blocco `Personalization` per Instantly
- CSV pronto per Instantly

`EMAILGENIUS_HOME` default: `.emailgenius/`

Dentro `EMAILGENIUS_HOME` trovi:
- `web-uploads/` per file caricati dalla UI
- `web-reports/` per export e report UI
- `backups/` per dump database
- `google-oauth-token.json` se usi OAuth Google

## Parent profile (YAML)

Esempio `parent_profile.yaml`:

```yaml
slug: azienda-a
company_name: Azienda A Srl
tone: formale-consulenziale
offer_catalog:
  - Audit commerciale B2B
  - Ottimizzazione outreach
icp:
  - PMI manifatturiere Nord Italia
proof_points:
  - Caso studio settore machinery
objections:
  - Budget limitato
  - Team commerciale piccolo
cta_policy: call conoscitiva 20-30 min
no_go_claims:
  - garantito
  - 100%
compliance_notes:
  - usa solo fonti pubbliche
sender_name: Ivan Lorenzoni
sender_company: Contributo Facile
sender_phone: "+39 347 283 0680"
sender_booking_url: "https://calendly.com/ivan-lorenzoni/preparere"
outreach_seed_template: |
  Si {{firstName}}, ci sono oltre 86.000 aziende, più o meno come la Tua, solo in Lombardia.
  Oggi hai l'occasione di fare il primo passo: fissiamo un confronto da 30 minuti.
  {{sender_name}}
  {{sender_company}}
```

## Comandi principali

### App locale

Avvia la UI locale:

```bash
./scripts/start-local.sh
```

La UI copre:
- creazione e attivazione parent profile senza scrivere YAML a mano,
- upload knowledge per parent,
- lancio campagne locali da CSV,
- export CSV Instantly-ready con `SubjectLine` e `Personalization`,
- sync Drive-native dal workspace,
- monitoraggio job e campagne recenti.

Per fermare il database locale:

```bash
./scripts/stop-local.sh
```

Backup del database locale:

```bash
./scripts/backup-db.sh
```

Restore da backup:

```bash
./scripts/restore-db.sh .emailgenius/backups/<backup>.sql --yes
```

### Parent context

```bash
emailgenius parent register --slug azienda-a --profile parent_profile.yaml --set-active
emailgenius parent use --slug azienda-a
emailgenius parent list
```

### Knowledge (RAG)

```bash
emailgenius knowledge ingest --slug azienda-a --file marketing-playbook.pdf --kind marketing
emailgenius knowledge list --slug azienda-a
```

Supporto ingest: `PDF`, `DOCX`, `Markdown/TXT`.

### Campagne

```bash
emailgenius campaign run \
  --slug azienda-a \
  --leads "/path/leads.csv" \
  --sheet-id "GOOGLE_SHEET_ID" \
  --out-dir reports/campaigns \
  --stages all \
  --recipient-mode row \
  --variant-mode ab \
  --output-schema ab \
  --llm-policy strict \
  --enrichment-mode auto \
  --max-concurrency 5 \
  --max-retries 3 \
  --backoff-base-seconds 1.0 \
  --cost-cap-eur 50
```

Drive-native (opzionale):

```bash
emailgenius campaign run \
  --slug azienda-a \
  --io-mode drive \
  --workspace-folder-id "<GOOGLE_DRIVE_FOLDER_ID>" \
  --out-dir reports/campaigns \
  --llm-policy strict \
  --gsheets-auth service_account
```

Daemon continuo:

```bash
emailgenius workspace daemon \
  --slug azienda-a \
  --workspace-folder-id "<GOOGLE_DRIVE_FOLDER_ID>" \
  --poll-interval-seconds 60 \
  --gsheets-auth service_account
```

Sync singolo:

```bash
emailgenius workspace sync-once \
  --slug azienda-a \
  --workspace-folder-id "<GOOGLE_DRIVE_FOLDER_ID>"
```

Modello Drive opzionale:

```text
PARENTS/
  azienda-a/
    PROFILE/profile.yaml
    KNOWLEDGE/
    LEADS/
    OUTPUT/
```

Con questa struttura:
- il nome cartella `azienda-a` diventa il `parent_slug`,
- i documenti in `KNOWLEDGE/` vengono indicizzati su quel parent,
- gli Sheet in `LEADS/` ereditano automaticamente quel parent,
- i documenti generati finiscono in `OUTPUT/`.

```bash
emailgenius campaign status --campaign-id <campaign_id>
emailgenius campaign export \
  --campaign-id <campaign_id> \
  --format csv \
  --output-schema auto \
  --out reports/campaigns/export.csv

# Fast path: publish an existing CSV to Google Sheets (no re-run)
emailgenius campaign publish-sheet \
  --csv reports/campaigns/campaign-<campaign_id>.csv \
  --sheet-title "EmailGenius Drafts" \
  --drive-folder-id "<DRIVE_FOLDER_ID_OR_URL>" \
  --gsheets-auth oauth
```

## Colonne output approvazione

`campaign_id`, `parent_slug`, `company_name`, `contact_name`, `contact_title`, `contact_email`,
`variant_a_subject`, `variant_a_body`, `variant_b_subject`, `variant_b_body`, `recommended_variant`,
`final_subject`, `final_body`, `selected_variant`, `generation_status`, `generation_warning`, `error_code`,
`evidence_summary`, `risk_flags`, `status`, `reviewer_notes`, `approved_variant`, `updated_at`.

Schema legacy `A/B/C` disponibile con `--variant-mode abc --output-schema abc`.

## Comandi legacy utili

```bash
emailgenius analyze https://example.com --company "Azienda Demo" --show-email
emailgenius discover --company "Acme S.p.A." --city "Vicenza" --show-news --show-email
```

## Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## Note operative

- LinkedIn: solo link pubblici, nessun login/scraping autenticato.
- Nessun invio automatico email in questa release.
- Default `--llm-policy strict`: senza almeno una key (`OPENAI_API_KEY` o `EMAILGENIUS_OPENAI_FALLBACK_API_KEY`) la campagna si ferma.
- Usa `--llm-policy fallback` per degradare a copy deterministico locale.
- La persistenza locale vera sta in due posti:
  - volume Docker `emailgenius_pg_data` per PostgreSQL
  - cartella `.emailgenius/` per runtime file
- Se chiudi l'app e la riapri, i dati restano.
- Drive-native Data Room e` opzionale: usalo solo se ti serve integrazione Google.
- Se vuoi Drive: condividi la cartella workspace in Editor con l'email del service account (`...iam.gserviceaccount.com`) e abilita Google Drive API, Google Docs API e Google Sheets API.
