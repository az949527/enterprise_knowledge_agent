from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.lite.indexer import DEFAULT_INDEX_DIR, build_index


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a lightweight local knowledge index.")
    parser.add_argument("--source-dir", default="demo_documents")
    parser.add_argument("--index-dir", default=str(DEFAULT_INDEX_DIR))
    parser.add_argument("--chunk-size", type=int, default=900)
    parser.add_argument("--chunk-overlap", type=int, default=120)
    args = parser.parse_args()

    stats = build_index(args.source_dir, args.index_dir, args.chunk_size, args.chunk_overlap)
    print(f"Source: {stats.source_dir}")
    print(f"Index: {stats.index_dir}")
    print(f"Files: {stats.file_count}")
    print(f"Chunks: {stats.chunk_count}")


if __name__ == "__main__":
    main()
