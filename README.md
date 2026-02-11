# EmailGenius

Sistema CLI per campagne email B2B multi-azienda madre con:
- contesto persistente per parent company (`slug`),
- ingest CSV lead + dedup azienda + scelta contatto primario,
- enrichment pubblico (sito, news, link LinkedIn pubblici),
- RAG marketing su PostgreSQL + pgvector,
- generazione 3 varianti email (A/B/C),
- coda approvazione su Google Sheet + export CSV locale,
- retention automatica dati campagna (default 90 giorni).

## Requisiti

- Python 3.10+
- PostgreSQL con estensione `pgvector`
- (opzionale) Chromium Playwright per enrichment web profondo
- (opzionale) credenziali Google Service Account per publish su Sheet

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Variabili ambiente

```bash
export EMAILGENIUS_DATABASE_URL="postgresql://postgres:postgres@localhost:5432/emailgenius"
export OPENAI_API_KEY="..."
export EMAILGENIUS_OPENAI_CHAT_MODEL="gpt-5"
export EMAILGENIUS_OPENAI_EMBED_MODEL="text-embedding-3-small"
export GOOGLE_SERVICE_ACCOUNT_JSON="/absolute/path/service-account.json"
export EMAILGENIUS_RETENTION_DAYS="90"
```

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
```

## Comandi principali

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
  --stages all
```

```bash
emailgenius campaign status --campaign-id <campaign_id>
emailgenius campaign export --campaign-id <campaign_id> --format csv --out reports/campaigns/export.csv
```

## Colonne output approvazione

`campaign_id`, `parent_slug`, `company_name`, `contact_name`, `contact_title`, `contact_email`,
`variant_a_subject`, `variant_a_body`, `variant_b_subject`, `variant_b_body`, `variant_c_subject`,
`variant_c_body`, `recommended_variant`, `evidence_summary`, `risk_flags`, `status`,
`reviewer_notes`, `approved_variant`, `updated_at`.

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
- In assenza `OPENAI_API_KEY`, il sistema usa fallback locale deterministico per embedding e copy.
