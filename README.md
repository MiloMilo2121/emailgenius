# EmailGenius MVP

MVP operativo per:
- discovery web azienda via Playwright,
- estrazione segnali energetici/ESG/Industria 4.0 dal testo sito,
- scoring eleggibilita' Transizione 5.0 (regole configurate nel codice),
- generazione bozza email B2B personalizzata.

## Requisiti

- Python 3.10+
- Browser runtime Playwright (Chromium)

## Setup rapido

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Esecuzione

```bash
emailgenius analyze https://example.com --company "Azienda Demo" --show-email
```

Workflow richiesto \"nome azienda + citta + news\":

```bash
emailgenius discover --company "Acme S.p.A." --city "Vicenza" --show-news --show-email
```

Output JSON salvato in `reports/`.

## Note importanti

- Lo scoring e' una stima tecnica preliminare. Non sostituisce una perizia energetica.
- Per uso produttivo servono logging centralizzato, retry policy, monitoraggio, e review legale privacy/compliance.
