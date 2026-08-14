from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import settings
from app.core.database import async_session_factory, init_db
from app.rag.embedder import Embedder
from app.rag.reranker import Reranker
from app.rag.retriever import RAGRetriever
from app.rag.vector_store import VectorStore


def load_dataset(path: Path, limit: int = None) -> List[dict]:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    return dataset[:limit] if limit else dataset


async def evaluate(dataset: List[dict], top_k: int, candidate_k: int, use_reranker: bool) -> Dict[str, Any]:
    await init_db()
    embedder = Embedder(settings.EMBEDDING_MODEL)
    vector_store = VectorStore(settings.FAISS_INDEX_PATH)
    reranker = Reranker() if use_reranker else None

    cases = []
    async with async_session_factory() as db:
        retriever = RAGRetriever(
            embedder=embedder,
            vector_store=vector_store,
            db=db,
            use_hyde=False,
            use_reranker=use_reranker,
            reranker=reranker,
        )
        for item in dataset:
            started = perf_counter()
            chunks = await retriever.retrieve(
                item["question"],
                top_k=candidate_k,
                top_n=top_k,
                user_id=item.get("user_id", 1),
            )
            elapsed_ms = int((perf_counter() - started) * 1000)
            cases.append(score_case(item, chunks, elapsed_ms))

    return summarize(cases, top_k, candidate_k, use_reranker)


def score_case(item: dict, chunks: List[dict], elapsed_ms: int) -> dict:
    expected_doc = item.get("expected_document_contains", "")
    expected_chunk_indices = set(item.get("expected_chunk_indices", []))

    doc_hit_rank = None
    chunk_hit_rank = None
    top1 = chunks[0] if chunks else None

    for rank, chunk in enumerate(chunks, start=1):
        filename = chunk.get("filename") or ""
        chunk_index = chunk.get("chunk_index")
        if doc_hit_rank is None and expected_doc and expected_doc in filename:
            doc_hit_rank = rank
        if chunk_hit_rank is None and _expected_chunk_hit(filename, chunk_index, expected_doc, expected_chunk_indices):
            chunk_hit_rank = rank

    return {
        "id": item["id"],
        "question": item["question"],
        "elapsed_ms": elapsed_ms,
        "retrieved_count": len(chunks),
        "doc_hit": doc_hit_rank is not None,
        "doc_hit_rank": doc_hit_rank,
        "chunk_hit": chunk_hit_rank is not None,
        "chunk_hit_rank": chunk_hit_rank,
        "top1_doc_hit": bool(top1 and expected_doc and expected_doc in (top1.get("filename") or "")),
        "top1_chunk_hit": bool(
            top1
            and _expected_chunk_hit(
                top1.get("filename") or "",
                top1.get("chunk_index"),
                expected_doc,
                expected_chunk_indices,
            )
        ),
        "mrr_chunk": 1 / chunk_hit_rank if chunk_hit_rank else 0.0,
        "top_results": [
            {
                "rank": rank,
                "filename": chunk.get("filename"),
                "chunk_id": chunk.get("chunk_id"),
                "chunk_index": chunk.get("chunk_index"),
                "score": chunk.get("score"),
                "rerank_score": chunk.get("rerank_score"),
                "preview": preview(chunk.get("content", "")),
            }
            for rank, chunk in enumerate(chunks, start=1)
        ],
    }


def summarize(cases: List[dict], top_k: int, candidate_k: int, use_reranker: bool) -> Dict[str, Any]:
    total = len(cases)
    if total == 0:
        raise ValueError("Evaluation dataset is empty.")

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "top_k": top_k,
        "candidate_k": candidate_k,
        "use_reranker": use_reranker,
        "strategy": "faiss_plus_reranker" if use_reranker else "faiss_only",
        "case_count": total,
        "recall_doc_at_k": average(case["doc_hit"] for case in cases),
        "recall_chunk_at_k": average(case["chunk_hit"] for case in cases),
        "top1_doc_hit_rate": average(case["top1_doc_hit"] for case in cases),
        "top1_chunk_hit_rate": average(case["top1_chunk_hit"] for case in cases),
        "mrr_chunk": sum(case["mrr_chunk"] for case in cases) / total,
        "avg_latency_ms": sum(case["elapsed_ms"] for case in cases) / total,
    }
    return {"summary": summary, "cases": cases}


def average(values) -> float:
    items = list(values)
    return sum(1 for value in items if value) / len(items)


def _expected_chunk_hit(filename: str, chunk_index: int | None, expected_doc: str, expected_chunk_indices: set) -> bool:
    if chunk_index not in expected_chunk_indices:
        return False
    if expected_doc:
        return expected_doc in filename
    return True


def save_reports(result: Dict[str, Any], output_dir: Path) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"eval_retrieval_{stamp}.json"
    md_path = output_dir / f"eval_retrieval_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def render_markdown(result: Dict[str, Any]) -> str:
    if "runs" in result:
        return render_compare_markdown(result)

    summary = result["summary"]
    lines = [
        "# Retrieval Evaluation Report",
        "",
        f"- Created at: {summary['created_at']}",
        f"- Strategy: {summary['strategy']}",
        f"- Cases: {summary['case_count']}",
        f"- Top K: {summary['top_k']}",
        f"- Candidate K: {summary['candidate_k']}",
        f"- Use Reranker: {summary['use_reranker']}",
        f"- Recall Doc@K: {summary['recall_doc_at_k']:.2%}",
        f"- Recall Chunk@K: {summary['recall_chunk_at_k']:.2%}",
        f"- Top1 Doc Hit Rate: {summary['top1_doc_hit_rate']:.2%}",
        f"- Top1 Chunk Hit Rate: {summary['top1_chunk_hit_rate']:.2%}",
        f"- MRR Chunk: {summary['mrr_chunk']:.3f}",
        f"- Avg Latency: {summary['avg_latency_ms']:.0f} ms",
        "",
        "## Cases",
        "",
    ]
    for case in result["cases"]:
        lines.extend(
            [
                f"### {case['id']}",
                "",
                f"- Question: {case['question']}",
                f"- Doc hit rank: {case['doc_hit_rank']}",
                f"- Chunk hit rank: {case['chunk_hit_rank']}",
                f"- Top1 doc hit: {case['top1_doc_hit']}",
                f"- Top1 chunk hit: {case['top1_chunk_hit']}",
                f"- Latency: {case['elapsed_ms']} ms",
                "",
                "| Rank | Chunk | FAISS Score | Rerank Score | Preview |",
                "|------|-------|-------------|--------------|---------|",
            ]
        )
        for item in case["top_results"]:
            lines.append(
                f"| {item['rank']} | {item['chunk_index']} | {item['score']:.4f} | {item['rerank_score']:.4f} | {item['preview']} |"
            )
        lines.append("")
    return "\n".join(lines)


def render_compare_markdown(result: Dict[str, Any]) -> str:
    lines = [
        "# Retrieval Evaluation Comparison",
        "",
        f"- Created at: {result['created_at']}",
        "",
        "| Strategy | Recall Doc@K | Recall Chunk@K | Top1 Chunk | MRR Chunk | Avg Latency |",
        "|----------|---------------|----------------|------------|-----------|-------------|",
    ]
    for run in result["runs"]:
        summary = run["summary"]
        lines.append(
            "| {strategy} | {doc:.2%} | {chunk:.2%} | {top1:.2%} | {mrr:.3f} | {latency:.0f} ms |".format(
                strategy=summary["strategy"],
                doc=summary["recall_doc_at_k"],
                chunk=summary["recall_chunk_at_k"],
                top1=summary["top1_chunk_hit_rate"],
                mrr=summary["mrr_chunk"],
                latency=summary["avg_latency_ms"],
            )
        )

    lines.extend(["", "## Run Details", ""])
    for run in result["runs"]:
        summary = run["summary"]
        lines.extend(
            [
                f"### {summary['strategy']}",
                "",
                f"- Top K: {summary['top_k']}",
                f"- Candidate K: {summary['candidate_k']}",
                f"- Use Reranker: {summary['use_reranker']}",
                "",
            ]
        )
        for case in run["cases"]:
            lines.extend(
                [
                    f"#### {case['id']}",
                    "",
                    f"- Question: {case['question']}",
                    f"- Chunk hit rank: {case['chunk_hit_rank']}",
                    f"- Top1 chunk hit: {case['top1_chunk_hit']}",
                    "",
                    "| Rank | Chunk | FAISS Score | Rerank Score | Preview |",
                    "|------|-------|-------------|--------------|---------|",
                ]
            )
            for item in case["top_results"]:
                lines.append(
                    f"| {item['rank']} | {item['chunk_index']} | {item['score']:.4f} | {item['rerank_score']:.4f} | {item['preview']} |"
                )
            lines.append("")
    return "\n".join(lines)


def preview(text: str, max_chars: int = 90) -> str:
    normalized = " ".join(str(text).replace("|", " ").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "..."


async def async_main(args: argparse.Namespace) -> None:
    dataset = load_dataset(Path(args.dataset), args.limit)
    if args.compare_reranker:
        baseline = await evaluate(dataset, top_k=args.top_k, candidate_k=args.top_k, use_reranker=False)
        reranked = await evaluate(dataset, top_k=args.top_k, candidate_k=args.candidate_k, use_reranker=True)
        result = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "runs": [baseline, reranked],
        }
        paths = save_reports(result, Path(args.output_dir))
        print_comparison(result)
        print(f"Markdown report: {paths['markdown'].as_posix()}")
        print(f"JSON report: {paths['json'].as_posix()}")
        return

    result = await evaluate(
        dataset,
        top_k=args.top_k,
        candidate_k=args.candidate_k if args.use_reranker else args.top_k,
        use_reranker=args.use_reranker,
    )
    paths = save_reports(result, Path(args.output_dir))
    summary = result["summary"]

    print(f"Cases: {summary['case_count']}")
    print(f"Strategy: {summary['strategy']}")
    print(f"Candidate K: {summary['candidate_k']}")
    print(f"Recall Doc@{summary['top_k']}: {summary['recall_doc_at_k']:.2%}")
    print(f"Recall Chunk@{summary['top_k']}: {summary['recall_chunk_at_k']:.2%}")
    print(f"Top1 Chunk Hit Rate: {summary['top1_chunk_hit_rate']:.2%}")
    print(f"MRR Chunk: {summary['mrr_chunk']:.3f}")
    print(f"Avg Latency: {summary['avg_latency_ms']:.0f} ms")
    print(f"Markdown report: {paths['markdown'].as_posix()}")
    print(f"JSON report: {paths['json'].as_posix()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval quality against a JSON dataset.")
    parser.add_argument("--dataset", default="", help="评估数据集 JSON 路径（必填）")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=10)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--use-reranker", action="store_true")
    parser.add_argument("--compare-reranker", action="store_true")
    parser.add_argument("--output-dir", default="outputs/evals")
    args = parser.parse_args()
    asyncio.run(async_main(args))


def print_comparison(result: Dict[str, Any]) -> None:
    for run in result["runs"]:
        summary = run["summary"]
        print(f"Strategy: {summary['strategy']}")
        print(f"  Recall Doc@{summary['top_k']}: {summary['recall_doc_at_k']:.2%}")
        print(f"  Recall Chunk@{summary['top_k']}: {summary['recall_chunk_at_k']:.2%}")
        print(f"  Top1 Chunk Hit Rate: {summary['top1_chunk_hit_rate']:.2%}")
        print(f"  MRR Chunk: {summary['mrr_chunk']:.3f}")
        print(f"  Avg Latency: {summary['avg_latency_ms']:.0f} ms")


if __name__ == "__main__":
    main()
