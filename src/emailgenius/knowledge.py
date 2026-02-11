from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from .llm import LLMGateway
from .storage import PostgresStore
from .utils import chunk_text, sha256_of_bytes


@dataclass(slots=True)
class KnowledgeIngestResult:
    parent_slug: str
    source_path: str
    kind: str
    chunks_total: int
    embeddings_used: bool


def ingest_knowledge_file(
    *,
    store: PostgresStore,
    llm: LLMGateway,
    parent_slug: str,
    file_path: str,
    kind: str = "marketing",
) -> KnowledgeIngestResult:
    path = Path(file_path)
    raw_bytes = path.read_bytes()
    source_hash = sha256_of_bytes(raw_bytes)
    text = _extract_text(path)
    chunks = chunk_text(text, chunk_size=1300, overlap=220)

    document_id = store.upsert_knowledge_document(
        parent_slug=parent_slug,
        kind=kind,
        source_path=str(path),
        source_hash=source_hash,
        metadata={"filename": path.name, "suffix": path.suffix.lower()},
    )

    embeddings = llm.embed_texts(chunks) if chunks else []
    store.insert_knowledge_chunks(
        document_id=document_id,
        parent_slug=parent_slug,
        kind=kind,
        chunks=chunks,
        embeddings=embeddings,
        metadata={"source": str(path)},
    )

    return KnowledgeIngestResult(
        parent_slug=parent_slug,
        source_path=str(path),
        kind=kind,
        chunks_total=len(chunks),
        embeddings_used=bool(embeddings),
    )


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)
    if suffix == ".docx":
        document = Document(str(path))
        lines = [paragraph.text for paragraph in document.paragraphs]
        return "\n".join(lines)

    raise ValueError(f"Unsupported file type: {suffix}. Use PDF, DOCX, or Markdown/TXT.")
