from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    lowered = value.lower().replace(" ", "-")
    cleaned = "".join(ch for ch in lowered if ch in allowed)
    return cleaned or "item"


def ensure_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        if ";" in value:
            parts = [part.strip() for part in value.split(";")]
            return [part for part in parts if part]
        if "," in value:
            parts = [part.strip() for part in value.split(",")]
            return [part for part in parts if part]
        return [value.strip()]
    return [str(value)]


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chunk_text(text: str, *, chunk_size: int = 1200, overlap: int = 180) -> list[str]:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start = max(0, end - overlap)
    return chunks


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _safe_csv_value(row.get(key)) for key in fieldnames})


def _safe_csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def to_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def from_json(value: str | None) -> object:
    if not value:
        return None
    return json.loads(value)


def compact_lines(lines: Iterable[str], *, limit: int = 10) -> list[str]:
    out: list[str] = []
    for item in lines:
        item = " ".join(str(item).split())
        if not item:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out
