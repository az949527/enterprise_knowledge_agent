from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Iterable, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import settings
from app.core.database import async_session_factory, init_db
from app.rag.embedder import Embedder
from app.rag.generator import RAGAnswerGenerator
from app.rag.reranker import Reranker
from app.rag.retriever import RAGRetriever
from app.rag.vector_store import VectorStore


def load_dataset(path: Path, limit: int | None = None) -> List[dict]:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    return dataset[:limit] if limit else dataset


async def evaluate(args: argparse.Namespace) -> Dict[str, Any]:
    if args.no_llm:
        settings.LLM_API_KEY = ""

    await init_db()
    dataset = load_dataset(Path(args.dataset), args.limit)
    embedder = Embedder(settings.EMBEDDING_MODEL)
    vector_store = VectorStore(settings.FAISS_INDEX_PATH)
    reranker = Reranker() if args.use_reranker else None
    generator = RAGAnswerGenerator()

    cases = []
    async with async_session_factory() as db:
        retriever = RAGRetriever(
            embedder=embedder,
            vector_store=vector_store,
            db=db,
            use_hyde=False,
            use_reranker=args.use_reranker,
            reranker=reranker,
        )
        for item in dataset:
            started = perf_counter()
            retrieve_started = perf_counter()
            chunks = await retriever.retrieve(
                item["question"],
                top_k=args.candidate_k if args.use_reranker else args.top_k,
                top_n=args.top_k,
                user_id=item.get("user_id", 1),
            )
            retrieve_elapsed_ms = int((perf_counter() - retrieve_started) * 1000)
            generate_started = perf_counter()
            answer_payload = await generator.generate(item["question"], chunks)
            generate_elapsed_ms = int((perf_counter() - generate_started) * 1000)
            elapsed_ms = int((perf_counter() - started) * 1000)
            cases.append(
                score_case(
                    item,
                    chunks,
                    answer_payload,
                    elapsed_ms,
                    args.min_answer_coverage,
                    timings={
                        "retrieve_ms": retrieve_elapsed_ms,
                        "generate_ms": generate_elapsed_ms,
                        "total_ms": elapsed_ms,
                    },
                )
            )

    return summarize(cases, args)


def score_case(
    item: dict,
    chunks: List[dict],
    answer_payload: dict,
    elapsed_ms: int,
    min_answer_coverage: float,
    timings: Dict[str, int] | None = None,
) -> dict:
    expected_doc = item.get("expected_document_contains", "")
    expected_chunk_indices = set(item.get("expected_chunk_indices", []))
    answer = answer_payload.get("answer", "")

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

    answer_terms = score_answer_terms(answer, item.get("expected_answer_terms", []))
    citations = extract_citations(answer)
    citation_valid = bool(citations) and all(1 <= citation <= len(chunks) for citation in citations)
    answer_complete = answer_terms["coverage"] >= min_answer_coverage
    failure_type = classify_failure(chunk_hit_rank is not None, answer_complete, citation_valid)

    return {
        "id": item["id"],
        "question": item["question"],
        "reference_answer": item.get("reference_answer", ""),
        "answer": answer,
        "answer_mode": answer_payload.get("mode"),
        "answer_strategy": answer_payload.get("strategy"),
        "llm": answer_payload.get("llm"),
        "elapsed_ms": elapsed_ms,
        "timings": timings or {"total_ms": elapsed_ms},
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
        "answer_term_hits": answer_terms["hits"],
        "answer_term_misses": answer_terms["misses"],
        "answer_term_coverage": answer_terms["coverage"],
        "answer_complete": answer_complete,
        "citations": citations,
        "has_citation": bool(citations),
        "citation_valid": citation_valid,
        "failure_type": failure_type,
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


def score_answer_terms(answer: str, expected_terms: Iterable[Any]) -> dict:
    hits = []
    misses = []
    for expected in expected_terms:
        alternatives = expected if isinstance(expected, list) else [expected]
        if any(contains_term(answer, alternative) for alternative in alternatives):
            hits.append(expected)
        else:
            misses.append(expected)

    total = len(hits) + len(misses)
    coverage = len(hits) / total if total else 1.0
    return {"hits": hits, "misses": misses, "coverage": coverage}


def contains_term(text: str, term: str) -> bool:
    return normalize_for_match(str(term)) in normalize_for_match(text)


def normalize_for_match(text: str) -> str:
    normalized = str(text).lower()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.replace("－", "-").replace("—", "-")
    normalized = normalized.replace("_", "-")
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff%]+", "", normalized)
    return normalized


def extract_citations(answer: str) -> List[int]:
    return [int(value) for value in re.findall(r"\[(\d+)\]", answer or "")]


def classify_failure(chunk_hit: bool, answer_complete: bool, citation_valid: bool) -> str:
    if not chunk_hit:
        return "retrieval_miss"
    if not answer_complete:
        return "answer_incomplete"
    if not citation_valid:
        return "citation_invalid"
    return "pass"


def _expected_chunk_hit(
    filename: str,
    chunk_index: int | None,
    expected_doc: str,
    expected_chunk_indices: set,
) -> bool:
    if chunk_index not in expected_chunk_indices:
        return False
    if expected_doc:
        return expected_doc in filename
    return True


def summarize(cases: List[dict], args: argparse.Namespace) -> Dict[str, Any]:
    total = len(cases)
    if total == 0:
        raise ValueError("Evaluation dataset is empty.")

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": args.dataset,
        "case_count": total,
        "top_k": args.top_k,
        "candidate_k": args.candidate_k if args.use_reranker else args.top_k,
        "use_reranker": args.use_reranker,
        "llm_enabled": bool(settings.LLM_API_KEY),
        "min_answer_coverage": args.min_answer_coverage,
        "recall_doc_at_k": average(case["doc_hit"] for case in cases),
        "recall_chunk_at_k": average(case["chunk_hit"] for case in cases),
        "top1_chunk_hit_rate": average(case["top1_chunk_hit"] for case in cases),
        "mrr_chunk": sum(case["mrr_chunk"] for case in cases) / total,
        "answer_complete_rate": average(case["answer_complete"] for case in cases),
        "avg_answer_term_coverage": sum(case["answer_term_coverage"] for case in cases) / total,
        "answer_with_citation_rate": average(case["has_citation"] for case in cases),
        "citation_valid_rate": average(case["citation_valid"] for case in cases),
        "avg_latency_ms": sum(case["elapsed_ms"] for case in cases) / total,
        "avg_retrieve_ms": sum(case["timings"].get("retrieve_ms", 0) for case in cases) / total,
        "avg_generate_ms": sum(case["timings"].get("generate_ms", 0) for case in cases) / total,
        "llm_usage": summarize_llm_usage(cases),
        "failure_counts": count_by(cases, "failure_type"),
    }
    return {"summary": summary, "cases": cases}


def average(values: Iterable[bool]) -> float:
    items = list(values)
    return sum(1 for value in items if value) / len(items)


def count_by(items: Iterable[dict], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        value = str(item.get(key))
        counts[value] = counts.get(value, 0) + 1
    return counts


def summarize_llm_usage(cases: Iterable[dict]) -> dict:
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls_with_usage": 0,
    }
    for case in cases:
        usage = ((case.get("llm") or {}).get("usage") or {})
        if not usage:
            continue
        totals["calls_with_usage"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] += int(usage.get(key) or 0)
    return totals


def save_reports(result: Dict[str, Any], output_dir: Path) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"eval_rag_{stamp}.json"
    md_path = output_dir / f"eval_rag_{stamp}.md"
    json_path.write_text(json.dumps(json_safe(result), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def render_markdown(result: Dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# RAG Evaluation Report",
        "",
        f"- Created at: {summary['created_at']}",
        f"- Dataset: {summary['dataset']}",
        f"- Cases: {summary['case_count']}",
        f"- Top K: {summary['top_k']}",
        f"- Candidate K: {summary['candidate_k']}",
        f"- Use Reranker: {summary['use_reranker']}",
        f"- LLM Enabled: {summary['llm_enabled']}",
        f"- Recall Doc@K: {summary['recall_doc_at_k']:.2%}",
        f"- Recall Chunk@K: {summary['recall_chunk_at_k']:.2%}",
        f"- Top1 Chunk Hit Rate: {summary['top1_chunk_hit_rate']:.2%}",
        f"- MRR Chunk: {summary['mrr_chunk']:.3f}",
        f"- Answer Complete Rate: {summary['answer_complete_rate']:.2%}",
        f"- Avg Answer Term Coverage: {summary['avg_answer_term_coverage']:.2%}",
        f"- Answer With Citation Rate: {summary['answer_with_citation_rate']:.2%}",
        f"- Citation Valid Rate: {summary['citation_valid_rate']:.2%}",
        f"- Avg Latency: {summary['avg_latency_ms']:.0f} ms",
        f"- Avg Retrieve Latency: {summary['avg_retrieve_ms']:.0f} ms",
        f"- Avg Generate Latency: {summary['avg_generate_ms']:.0f} ms",
        f"- LLM Usage Calls: {summary['llm_usage']['calls_with_usage']}",
        f"- LLM Prompt Tokens: {summary['llm_usage']['prompt_tokens']}",
        f"- LLM Completion Tokens: {summary['llm_usage']['completion_tokens']}",
        f"- LLM Total Tokens: {summary['llm_usage']['total_tokens']}",
        "",
        "## Failure Counts",
        "",
        "| Type | Count |",
        "|------|-------|",
    ]
    for failure_type, count in sorted(summary["failure_counts"].items()):
        lines.append(f"| {failure_type} | {count} |")
    lines.extend([
        "",
        "## Cases",
        "",
    ])

    for case in result["cases"]:
        lines.extend(
            [
                f"### {case['id']}",
                "",
                f"- Question: {case['question']}",
                f"- Answer mode: {case['answer_mode']}",
                f"- Chunk hit rank: {case['chunk_hit_rank']}",
                f"- Answer term coverage: {case['answer_term_coverage']:.2%}",
                f"- Answer complete: {case['answer_complete']}",
                f"- Failure type: {case['failure_type']}",
                f"- Citations: {case['citations']}",
                f"- Citation valid: {case['citation_valid']}",
                f"- Latency: {case['elapsed_ms']} ms",
                f"- Retrieve latency: {case['timings'].get('retrieve_ms')} ms",
                f"- Generate latency: {case['timings'].get('generate_ms')} ms",
                f"- LLM usage: {json.dumps(((case.get('llm') or {}).get('usage') or None), ensure_ascii=False)}",
                "",
                "**Reference Answer**",
                "",
                case["reference_answer"],
                "",
                "**Actual Answer**",
                "",
                case["answer"],
                "",
                f"- Missing terms: {format_terms(case['answer_term_misses'])}",
                "",
                "| Rank | Chunk | FAISS Score | Rerank Score | Preview |",
                "|------|-------|-------------|--------------|---------|",
            ]
        )
        for item in case["top_results"]:
            lines.append(
                "| {rank} | {chunk} | {score} | {rerank} | {preview} |".format(
                    rank=item["rank"],
                    chunk=item["chunk_index"],
                    score=format_float(item["score"]),
                    rerank=format_float(item["rerank_score"]),
                    preview=item["preview"],
                )
            )
        lines.append("")
    return "\n".join(lines)


def format_terms(terms: List[Any]) -> str:
    if not terms:
        return "None"
    return ", ".join(json.dumps(term, ensure_ascii=False) for term in terms)


def format_float(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.4f}"


def preview(text: str, max_chars: int = 110) -> str:
    normalized = " ".join(str(text).replace("|", " ").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "..."


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            return str(value)
    return value


async def async_main(args: argparse.Namespace) -> None:
    result = await evaluate(args)
    paths = save_reports(result, Path(args.output_dir))
    summary = result["summary"]

    print(f"Cases: {summary['case_count']}")
    print(f"Use Reranker: {summary['use_reranker']}")
    print(f"LLM Enabled: {summary['llm_enabled']}")
    print(f"Recall Chunk@{summary['top_k']}: {summary['recall_chunk_at_k']:.2%}")
    print(f"Top1 Chunk Hit Rate: {summary['top1_chunk_hit_rate']:.2%}")
    print(f"Answer Complete Rate: {summary['answer_complete_rate']:.2%}")
    print(f"Avg Answer Term Coverage: {summary['avg_answer_term_coverage']:.2%}")
    print(f"Citation Valid Rate: {summary['citation_valid_rate']:.2%}")
    print(f"Avg Latency: {summary['avg_latency_ms']:.0f} ms")
    print(f"Avg Retrieve Latency: {summary['avg_retrieve_ms']:.0f} ms")
    print(f"Avg Generate Latency: {summary['avg_generate_ms']:.0f} ms")
    print(f"LLM Usage Calls: {summary['llm_usage']['calls_with_usage']}")
    print(f"LLM Total Tokens: {summary['llm_usage']['total_tokens']}")
    print(f"Markdown report: {paths['markdown'].as_posix()}")
    print(f"JSON report: {paths['json'].as_posix()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate end-to-end RAG answers against a JSON dataset.")
    parser.add_argument("--dataset", default="", help="评估数据集 JSON 路径（必填）")
    parser.add_argument("--top-k", type=int, default=settings.TOP_K_RETRIEVAL)
    parser.add_argument("--candidate-k", type=int, default=settings.RERANK_CANDIDATE_K)
    parser.add_argument("--output-dir", default="outputs/evals")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--min-answer-coverage", type=float, default=0.7)
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM generation for a no-cost local check.")
    parser.set_defaults(use_reranker=settings.USE_RERANKER)
    parser.add_argument("--use-reranker", dest="use_reranker", action="store_true")
    parser.add_argument("--no-reranker", dest="use_reranker", action="store_false")
    asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    main()
