# EmailGenius Drive-Native Go-Live Playbook (Step-by-Step)

Data: 2026-03-03
Ambiente target: produzione/operativo
Obiettivo: mettere online il flusso `io_mode=drive` in modo affidabile.

## 1) Cosa devi avere prima di iniziare

Devi avere questi 8 elementi:

1. URL Postgres reale (`EMAILGENIUS_DATABASE_URL`) con DB raggiungibile.
2. Estensione `pgvector` attiva sul DB.
3. File JSON del Service Account Google (`GOOGLE_SERVICE_ACCOUNT_JSON`) su disco locale.
4. Cartella root Google Drive per workspace (`EMAILGENIUS_WORKSPACE_FOLDER_ID`).
5. API GCP abilitate: Drive API, Docs API, Sheets API.
6. Almeno una key LLM (`OPENAI_API_KEY` oppure `EMAILGENIUS_OPENAI_FALLBACK_API_KEY`) se vuoi strict reale.
7. Colonna `parent_slug` nei lead, se userai piu parent.
8. Conferma per patchare subito i 3 finding aperti (vedi sezione 10).

## 2) Setup dipendenze Python (nuove)

Nel repo `emailgenius` esegui:

```bash
cd /Users/marcomilanello/Desktop/emailgenius
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verifica moduli richiesti:

```bash
python - <<'PY'
import importlib
mods=[
  'googleapiclient',
  'langgraph',
  'langgraph.checkpoint.postgres',
  'gspread',
  'openai',
  'psycopg'
]
for m in mods:
    try:
        importlib.import_module(m)
        print(f"{m}=OK")
    except Exception:
        print(f"{m}=MISSING")
PY
```

Tutti devono risultare `OK`.

## 3) Setup Postgres + pgvector

Se il DB non e pronto, esegui su Postgres:

```sql
CREATE DATABASE emailgenius;
\c emailgenius
CREATE EXTENSION IF NOT EXISTS vector;
```

Poi verifica connessione dal progetto:

```bash
export EMAILGENIUS_DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/emailgenius"
python - <<'PY'
from emailgenius.storage import PostgresStore
import os
s = PostgresStore(os.environ['EMAILGENIUS_DATABASE_URL'])
s.migrate()
print('DB_OK')
PY
```

Output atteso: `DB_OK`.

## 4) Setup Google Cloud (obbligatorio)

Nel progetto GCP del Service Account:

1. Abilita API:
   - Google Drive API
   - Google Docs API
   - Google Sheets API
2. Crea (o usa) Service Account.
3. Crea una key JSON e salvala localmente, esempio:
   - `/Users/marcomilanello/.secrets/emailgenius-service-account.json`

Esporta env:

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON="/Users/marcomilanello/.secrets/emailgenius-service-account.json"
```

## 5) Setup Google Drive Data Room

Crea una cartella root su Drive, esempio `EmailGenius_Workspace`.

Dentro la root crea esattamente queste sottocartelle:

- `Profiles`
- `Knowledge`
- `Knowledge/Processed`
- `Input Leads`
- `Output Sequences`

Condividi la root in `Editor` con la mail del Service Account (quella `...iam.gserviceaccount.com`).

Ricava il Folder ID dalla URL Drive e imposta:

```bash
export EMAILGENIUS_WORKSPACE_FOLDER_ID="<FOLDER_ID>"
```

## 6) Config ambiente applicativo

Imposta env minime per run drive-native:

```bash
export EMAILGENIUS_IO_MODE="drive"
export EMAILGENIUS_DRIVE_POLL_INTERVAL_SECONDS="60"
export EMAILGENIUS_DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/emailgenius"
export GOOGLE_SERVICE_ACCOUNT_JSON="/Users/marcomilanello/.secrets/emailgenius-service-account.json"
export EMAILGENIUS_WORKSPACE_FOLDER_ID="<FOLDER_ID>"

# LLM strict (almeno una)
export OPENAI_API_KEY="<KEY>"
# oppure fallback
export EMAILGENIUS_OPENAI_FALLBACK_API_KEY="<KEY>"
```

## 7) Caricamento dati in Drive

### 7.1 Parent Profiles

Metti in `Profiles` uno o piu file YAML validi.

### 7.2 Knowledge

Metti in `Knowledge` file `.pdf`/`.docx`/`.md`.
Dopo sync corretta verranno spostati in `Knowledge/Processed`.

### 7.3 Input Leads

Metti almeno un Google Sheet in `Input Leads`.

Colonne minime consigliate:

- `Email`
- `First Name`
- `Last Name`
- `companyName` (o `Company Name`)
- `website` (se disponibile)

Se usi piu parent, aggiungi `parent_slug` per ogni riga.

## 8) Primo avvio (smoke test obbligatorio)

### 8.1 Sync singolo

```bash
cd /Users/marcomilanello/Desktop/emailgenius
source .venv/bin/activate
PYTHONPATH=src emailgenius workspace sync-once --slug <fallback-parent-slug> --workspace-folder-id "$EMAILGENIUS_WORKSPACE_FOLDER_ID" --gsheets-auth service_account
```

Controlla:

1. CSV export locale creato in `reports/campaigns/`.
2. Google Doc creati in `Output Sequences`.
3. `EmailGenius Master Status` aggiornato.
4. File knowledge spostati in `Knowledge/Processed`.

### 8.2 Daemon continuo

Quando lo smoke test e OK:

```bash
PYTHONPATH=src emailgenius workspace daemon --slug <fallback-parent-slug> --workspace-folder-id "$EMAILGENIUS_WORKSPACE_FOLDER_ID" --poll-interval-seconds 60 --gsheets-auth service_account
```

## 9) Comandi diagnostici rapidi

Verifica env realmente caricate:

```bash
python - <<'PY'
import os
for k in [
  'EMAILGENIUS_DATABASE_URL',
  'GOOGLE_SERVICE_ACCOUNT_JSON',
  'EMAILGENIUS_WORKSPACE_FOLDER_ID',
  'EMAILGENIUS_IO_MODE',
  'EMAILGENIUS_DRIVE_POLL_INTERVAL_SECONDS',
  'OPENAI_API_KEY',
  'EMAILGENIUS_OPENAI_FALLBACK_API_KEY',
]:
    print(k, 'set' if (os.getenv(k) or '').strip() else 'missing')
PY
```

Verifica branch e allineamento remoto:

```bash
git -C /Users/marcomilanello/Desktop/emailgenius status -sb
git -C /Users/marcomilanello/Desktop/emailgenius log --oneline -n 3
```

## 10) Finding aperti da patchare subito

Ci sono 3 fix consigliati prima del go-live pieno:

1. `P1` strict enforcement nel nuovo motore agentico:
   - oggi, in errore LLM, puo degradare in fallback implicito.
2. `P2` cost cap nel percorso `io_mode=drive`:
   - oggi non blocca il run su stima costi alta.
3. `P2` mapping knowledge multi-parent:
   - oggi usa parent di default se non hai mapping esplicito.

## 11) Cosa inviarmi adesso (formato esatto)

Rispondimi con questi 8 punti, uno per riga:

1. `DATABASE_URL=...`
2. `SERVICE_ACCOUNT_JSON=...`
3. `WORKSPACE_FOLDER_ID=...`
4. `DRIVE_SHARED_WITH_SA=yes/no`
5. `GCP_APIS_ENABLED=drive,docs,sheets (yes/no)`
6. `LLM_KEY_CONFIGURED=yes/no`
7. `INPUT_LEADS_HAS_PARENT_SLUG=yes/no`
8. `APPROVE_PATCH_FINDINGS=yes/no`

Con questi 8 valori posso chiudere il go-live assistito end-to-end senza ambiguita.
