from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import async_session_factory, init_db
from app.documents import DocumentNode, NodeType, document_id_from_source
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag.chunker import TextChunker
from app.rag.embedder import Embedder
from app.rag.vector_store import VectorStore


def collect_demo_files(source_dir: Path) -> list[Path]:
    return sorted(path for path in source_dir.glob("*.md") if path.is_file())


async def load_demo_documents(args: argparse.Namespace) -> None:
    await init_db()
    source_dir = Path(args.source_dir)
    files = collect_demo_files(source_dir)
    if not files:
        raise FileNotFoundError(f"No .md demo documents found in {source_dir}")

    embedder = Embedder(settings.EMBEDDING_MODEL)
    vector_store = VectorStore(settings.FAISS_INDEX_PATH)

    print(f"Demo files: {len(files)}")
    print(f"User ID: {args.user_id}")
    print(f"Mode: {'load' if args.load else 'dry-run'}")
    print(f"Force replace: {args.force}")

    async with async_session_factory() as db:
        for path in files:
            existing = (
                await db.execute(
                    select(Document)
                    .where(Document.user_id == args.user_id)
                    .where(Document.filename == path.name)
                )
            ).scalars().all()

            if existing and not args.force:
                print(f"SKIP existing: {path.name}")
                continue

            node = DocumentNode(
                document_id=document_id_from_source(path.name),
                content=path.read_text(encoding="utf-8"),
                parser_version="demo_markdown_v1",
                node_type=NodeType.TEXT,
                source_anchor={"source_path": path.name},
                metadata={"filename": path.name, "file_type": ".md"},
            )
            chunks = TextChunker.chunk_node(
                node,
                settings.CHUNK_SIZE,
                settings.CHUNK_OVERLAP,
            )
            print(
                f"{path.name}: {len(node.content)} chars -> "
                f"{len(chunks)} chunks"
            )

            if not args.load:
                continue

            for doc in existing:
                await delete_document_records(db, vector_store, doc)

            target_dir = Path(settings.DOCUMENTS_DIR)
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / f"{uuid4()}_{path.name}"
            shutil.copyfile(path, target_path)

            doc = Document(
                user_id=args.user_id,
                filename=path.name,
                file_path=str(target_path),
                file_size=target_path.stat().st_size,
                file_type="md",
                status="processing",
            )
            db.add(doc)
            await db.flush()

            chunk_records = []
            for index, chunk_text in enumerate(chunks):
                chunk = Chunk(
                    document_id=doc.id,
                    chunk_index=index,
                    content=chunk_text,
                    token_count=len(chunk_text),
                )
                db.add(chunk)
                chunk_records.append(chunk)
            await db.flush()

            vectors = embedder.embed([chunk.content for chunk in chunk_records])
            vector_store.add(vectors, [chunk.id for chunk in chunk_records])
            doc.status = "ready"
            doc.chunk_count = len(chunk_records)
            await db.commit()
            vector_store.save()
            print(f"LOADED: {path.name} document_id={doc.id}")

    print("Done.")


async def delete_document_records(db, vector_store: VectorStore, doc: Document) -> None:
    result = await db.execute(select(Chunk.id).where(Chunk.document_id == doc.id))
    chunk_ids = result.scalars().all()
    if chunk_ids:
        vector_store.delete(chunk_ids)
    await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    await db.delete(doc)
    await db.commit()
    if doc.file_path and Path(doc.file_path).exists():
        Path(doc.file_path).unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load demo enterprise policy documents into the local knowledge base.")
    parser.add_argument("--source-dir", default="demo_documents")
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--load", action="store_true", help="Actually write documents/chunks/vectors. Default is dry-run.")
    parser.add_argument("--force", action="store_true", help="Replace existing demo documents with the same filename.")
    asyncio.run(load_demo_documents(parser.parse_args()))


if __name__ == "__main__":
    main()
