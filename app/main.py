from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import init_db
from app.rag.embedder import Embedder
from app.rag.generator import RAGAnswerGenerator
from app.rag.reranker import Reranker
from app.rag.vector_store import VectorStore
from app.routers.document import router as document_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("data", exist_ok=True)
    os.makedirs(settings.DOCUMENTS_DIR, exist_ok=True)
    os.makedirs(settings.TRACE_DIR, exist_ok=True)
    await init_db()
    app.state.settings = settings
    app.state.embedder = Embedder(settings.EMBEDDING_MODEL)
    app.state.answer_generator = RAGAnswerGenerator()
    app.state.reranker = Reranker() if settings.USE_RERANKER else None
    app.state.vector_store = VectorStore(settings.FAISS_INDEX_PATH)
    yield
    app.state.vector_store.save()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.include_router(document_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.APP_NAME}


@app.get("/")
async def workspace():
    return FileResponse("app/static/index.html")
