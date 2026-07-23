from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.lite.generator import answer_query
from app.lite.indexer import DEFAULT_INDEX_DIR
from app.lite.search import search_index


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Query the lightweight local knowledge index.")
    parser.add_argument("query")
    parser.add_argument("--index-dir", default=str(DEFAULT_INDEX_DIR))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    sources = search_index(args.query, args.index_dir, args.top_k)
    result = await answer_query(args.query, sources, use_llm=not args.no_llm)

    print(result["answer"])
    print("\nSources:")
    for source in sources:
        print(
            f"[{source['rank']}] {source['filename']} "
            f"chunk={source['chunk_index']} score={source['score']:.4f}"
        )
    usage = (result.get("llm") or {}).get("usage")
    if usage:
        print(f"\nLLM total tokens: {usage.get('total_tokens')}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
