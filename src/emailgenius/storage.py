from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .profiles import parent_profile_from_dict, parent_profile_to_dict
from .types import CampaignCompanyResult, CampaignSummary, ParentProfile
from .utils import utc_now_iso


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)

    def migrate(self) -> None:
        ddl = [
            "CREATE EXTENSION IF NOT EXISTS vector",
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS parent_profiles (
                slug TEXT PRIMARY KEY,
                profile_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id UUID PRIMARY KEY,
                parent_slug TEXT NOT NULL REFERENCES parent_profiles(slug) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_document_unique
            ON knowledge_documents(parent_slug, kind, source_hash)
            """,
            """
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id UUID PRIMARY KEY,
                document_id UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                parent_slug TEXT NOT NULL REFERENCES parent_profiles(slug) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                chunk_index INT NOT NULL,
                content TEXT NOT NULL,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                embedding VECTOR(1536),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_parent_kind
            ON knowledge_chunks(parent_slug, kind)
            """,
            """
            CREATE TABLE IF NOT EXISTS campaigns (
                id UUID PRIMARY KEY,
                parent_slug TEXT NOT NULL REFERENCES parent_profiles(slug) ON DELETE RESTRICT,
                leads_file TEXT NOT NULL,
                sheet_id TEXT,
                status TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                summary_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS campaign_company_records (
                id UUID PRIMARY KEY,
                campaign_id UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                parent_slug TEXT NOT NULL,
                company_key TEXT NOT NULL,
                company_name TEXT NOT NULL,
                contact_name TEXT,
                contact_title TEXT,
                contact_email TEXT,
                payload_json JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                reviewer TEXT,
                reviewer_notes TEXT,
                approved_variant TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_campaign_records_campaign_status
            ON campaign_company_records(campaign_id, status)
            """,
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                for stmt in ddl:
                    cur.execute(stmt)

    def upsert_parent_profile(self, profile: ParentProfile, *, set_active: bool = False) -> None:
        payload = json.dumps(parent_profile_to_dict(profile), ensure_ascii=False)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO parent_profiles(slug, profile_json)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (slug)
                    DO UPDATE SET
                        profile_json = EXCLUDED.profile_json,
                        updated_at = NOW()
                    """,
                    (profile.slug, payload),
                )
                if set_active:
                    cur.execute(
                        """
                        INSERT INTO app_settings(key, value)
                        VALUES ('active_parent_slug', %s)
                        ON CONFLICT (key)
                        DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                        """,
                        (profile.slug,),
                    )

    def set_active_parent(self, slug: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT slug FROM parent_profiles WHERE slug=%s", (slug,))
                if cur.fetchone() is None:
                    raise ValueError(f"Parent slug not found: {slug}")
                cur.execute(
                    """
                    INSERT INTO app_settings(key, value)
                    VALUES ('active_parent_slug', %s)
                    ON CONFLICT (key)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    (slug,),
                )

    def get_active_parent_slug(self) -> str | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key='active_parent_slug'")
                row = cur.fetchone()
                return str(row["value"]) if row else None

    def get_parent_profile(self, slug: str) -> ParentProfile | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT profile_json FROM parent_profiles WHERE slug=%s", (slug,))
                row = cur.fetchone()
                if row is None:
                    return None
                payload = row["profile_json"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                return parent_profile_from_dict(payload)

    def list_parent_profiles(self) -> list[ParentProfile]:
        out: list[ParentProfile] = []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT profile_json FROM parent_profiles ORDER BY slug")
                for row in cur.fetchall():
                    payload = row["profile_json"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    out.append(parent_profile_from_dict(payload))
        return out

    def upsert_knowledge_document(
        self,
        *,
        parent_slug: str,
        kind: str,
        source_path: str,
        source_hash: str,
        metadata: dict[str, object] | None = None,
    ) -> str:
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM knowledge_documents
                    WHERE parent_slug=%s AND kind=%s AND source_hash=%s
                    """,
                    (parent_slug, kind, source_hash),
                )
                row = cur.fetchone()
                if row:
                    doc_id = str(row["id"])
                    cur.execute("DELETE FROM knowledge_chunks WHERE document_id=%s", (doc_id,))
                    return doc_id

                doc_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO knowledge_documents(id, parent_slug, kind, source_path, source_hash, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (doc_id, parent_slug, kind, source_path, source_hash, metadata_json),
                )
                return doc_id

    def insert_knowledge_chunks(
        self,
        *,
        document_id: str,
        parent_slug: str,
        kind: str,
        chunks: list[str],
        embeddings: list[list[float]] | None,
        metadata: dict[str, object] | None = None,
    ) -> int:
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        inserted = 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                for idx, chunk in enumerate(chunks):
                    embedding_sql = _vector_literal(embeddings[idx]) if embeddings else None
                    cur.execute(
                        """
                        INSERT INTO knowledge_chunks(
                            id, document_id, parent_slug, kind, chunk_index,
                            content, metadata_json, embedding
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s::jsonb, %s::vector
                        )
                        """,
                        (
                            str(uuid.uuid4()),
                            document_id,
                            parent_slug,
                            kind,
                            idx,
                            chunk,
                            metadata_json,
                            embedding_sql,
                        ),
                    )
                    inserted += 1
        return inserted

    def list_knowledge_documents(self, parent_slug: str) -> list[dict[str, object]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, kind, source_path, source_hash, created_at
                    FROM knowledge_documents
                    WHERE parent_slug=%s
                    ORDER BY created_at DESC
                    """,
                    (parent_slug,),
                )
                return [dict(row) for row in cur.fetchall()]

    def search_knowledge_chunks(
        self,
        *,
        parent_slug: str,
        kind: str,
        query_embedding: list[float],
        top_k: int = 6,
    ) -> list[dict[str, object]]:
        if not query_embedding:
            return []
        vector_query = _vector_literal(query_embedding)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content, metadata_json,
                           (1 - (embedding <=> %s::vector)) AS similarity
                    FROM knowledge_chunks
                    WHERE parent_slug=%s AND kind=%s AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (vector_query, parent_slug, kind, vector_query, top_k),
                )
                rows = [dict(row) for row in cur.fetchall()]
        return rows

    def create_campaign(self, *, parent_slug: str, leads_file: str, sheet_id: str | None) -> str:
        campaign_id = str(uuid.uuid4())
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO campaigns(id, parent_slug, leads_file, sheet_id, status)
                    VALUES (%s, %s, %s, %s, 'RUNNING')
                    """,
                    (campaign_id, parent_slug, leads_file, sheet_id),
                )
        return campaign_id

    def finalize_campaign(self, campaign_id: str, summary: CampaignSummary) -> None:
        summary_json = json.dumps(asdict(summary), ensure_ascii=False)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE campaigns
                    SET status=%s, finished_at=NOW(), summary_json=%s::jsonb
                    WHERE id=%s
                    """,
                    (summary.status, summary_json, campaign_id),
                )

    def insert_campaign_company_result(self, result: CampaignCompanyResult) -> str:
        record_id = str(uuid.uuid4())
        payload = json.dumps(
            {
                "company": asdict(result.company),
                "contact": asdict(result.contact) if result.contact else None,
                "dossier": {
                    **asdict(result.dossier),
                    "news_items": [asdict(item) for item in result.dossier.news_items],
                },
                "variants": [asdict(item) for item in result.variants],
                "recommended_variant": result.recommended_variant,
                "approval": asdict(result.approval),
                "risk_flags": result.risk_flags,
                "created_at": utc_now_iso(),
            },
            ensure_ascii=False,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO campaign_company_records(
                        id, campaign_id, parent_slug, company_key, company_name,
                        contact_name, contact_title, contact_email, payload_json,
                        status, reviewer, reviewer_notes, approved_variant
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s::jsonb,
                        %s, %s, %s, %s
                    )
                    """,
                    (
                        record_id,
                        result.campaign_id,
                        result.parent_slug,
                        result.company.company_key,
                        result.company.company_name,
                        result.contact.full_name if result.contact else None,
                        result.contact.title if result.contact else None,
                        result.contact.email if result.contact else None,
                        payload,
                        result.approval.status,
                        result.approval.reviewer,
                        result.approval.notes,
                        result.approval.approved_variant,
                    ),
                )
        return record_id

    def get_campaign_summary(self, campaign_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, parent_slug, leads_file, sheet_id, status,
                           started_at, finished_at, summary_json
                    FROM campaigns
                    WHERE id=%s
                    """,
                    (campaign_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                summary_json = row.get("summary_json")
                if isinstance(summary_json, str):
                    summary_json = json.loads(summary_json)
                out = dict(row)
                out["summary_json"] = summary_json
                return out

    def list_campaign_records(self, campaign_id: str) -> list[dict[str, object]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, parent_slug, company_key, company_name, contact_name,
                           contact_title, contact_email, status,
                           reviewer, reviewer_notes, approved_variant,
                           payload_json, created_at, updated_at
                    FROM campaign_company_records
                    WHERE campaign_id=%s
                    ORDER BY company_name
                    """,
                    (campaign_id,),
                )
                rows = []
                for row in cur.fetchall():
                    payload = row.get("payload_json")
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    item = dict(row)
                    item["payload_json"] = payload
                    rows.append(item)
                return rows

    def purge_expired_campaign_data(self, retention_days: int) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM campaigns
                    WHERE started_at < (NOW() - (%s::text || ' days')::interval)
                    """,
                    (retention_days,),
                )
                return cur.rowcount


def _vector_literal(values: list[float] | None) -> str | None:
    if not values:
        return None
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"
