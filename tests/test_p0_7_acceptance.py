from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz

from app.documents import DocumentNode
from app.lite import indexer
from app.lite.index_diagnostics import diagnose_index


INDEX_FILES = (
    "nodes.jsonl",
    "parents.jsonl",
    "chunks.jsonl",
    "manifest.json",
)


class P07AcceptanceTests(unittest.TestCase):
    def test_unchanged_document_skips_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            document = source_dir / "policy.txt"
            document.write_text("unchanged policy content", encoding="utf-8")
            indexer.build_index(source_dir, index_dir)
            before = self._snapshot(index_dir)

            with patch.object(
                indexer,
                "iter_document_nodes",
                side_effect=AssertionError("unchanged file must not be parsed"),
            ):
                stats = indexer.build_index(source_dir, index_dir)

            self.assertEqual(stats.skipped_count, 1)
            self.assertEqual(stats.added_count, 0)
            self.assertEqual(stats.updated_count, 0)
            self.assertEqual(self._snapshot(index_dir), before)
            document_record = stats.documents[0]
            self.assertEqual(len(document_record["source_sha256"]), 64)

    def test_incremental_sync_adds_updates_and_removes_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            first = source_dir / "first.txt"
            removed = source_dir / "removed.txt"
            first.write_text("old first content", encoding="utf-8")
            removed.write_text("content to remove", encoding="utf-8")
            indexer.build_index(source_dir, index_dir)

            first.write_text("new first content", encoding="utf-8")
            removed.unlink()
            (source_dir / "added.txt").write_text(
                "newly added content",
                encoding="utf-8",
            )
            stats = indexer.build_index(source_dir, index_dir)
            content = "\n".join(
                str(chunk.get("content") or "")
                for chunk in indexer.read_chunks(index_dir)
            )

            self.assertEqual(stats.added_count, 1)
            self.assertEqual(stats.updated_count, 1)
            self.assertEqual(stats.removed_count, 1)
            self.assertEqual(stats.file_count, 2)
            self.assertIn("new first content", content)
            self.assertIn("newly added content", content)
            self.assertNotIn("old first content", content)
            self.assertNotIn("content to remove", content)

    def test_document_shards_only_replace_changed_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            changed = source_dir / "changed.txt"
            stable = source_dir / "stable.txt"
            changed.write_text("changed version one", encoding="utf-8")
            stable.write_text("stable content", encoding="utf-8")
            indexer.build_index(source_dir, index_dir)
            first_manifest = self._manifest(index_dir)
            self.assertEqual(
                first_manifest[indexer.STORAGE_LAYOUT_FIELD],
                indexer.SHARDED_STORAGE_LAYOUT,
            )
            records = {
                document["filename"]: document
                for document in first_manifest["documents"]
            }
            changed_shard = index_dir / records["changed.txt"][
                indexer.SHARD_PATH_FIELD
            ]
            stable_shard = index_dir / records["stable.txt"][
                indexer.SHARD_PATH_FIELD
            ]
            changed_before = self._directory_snapshot(changed_shard)
            stable_before = self._directory_snapshot(stable_shard)

            changed.write_text("changed version two", encoding="utf-8")
            stats = indexer.build_index(source_dir, index_dir)

            self.assertEqual(stats.updated_count, 1)
            self.assertEqual(stats.skipped_count, 1)
            self.assertNotEqual(
                self._directory_snapshot(changed_shard),
                changed_before,
            )
            self.assertEqual(
                self._directory_snapshot(stable_shard),
                stable_before,
            )
            for name in ("nodes.jsonl", "parents.jsonl", "chunks.jsonl"):
                self.assertEqual((index_dir / name).read_bytes(), b"")

    def test_sharded_delete_removes_only_target_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            (source_dir / "delete.txt").write_text(
                "delete this content",
                encoding="utf-8",
            )
            (source_dir / "keep.txt").write_text(
                "keep this content",
                encoding="utf-8",
            )
            indexer.build_index(source_dir, index_dir)
            manifest = self._manifest(index_dir)
            records = {
                document["filename"]: document
                for document in manifest["documents"]
            }
            deleted_shard = index_dir / records["delete.txt"][
                indexer.SHARD_PATH_FIELD
            ]
            kept_shard = index_dir / records["keep.txt"][
                indexer.SHARD_PATH_FIELD
            ]
            kept_before = self._directory_snapshot(kept_shard)

            stats = indexer.delete_index_document("delete.txt", index_dir)

            self.assertEqual(stats.removed_count, 1)
            self.assertFalse(deleted_shard.exists())
            self.assertEqual(self._directory_snapshot(kept_shard), kept_before)
            content = "\n".join(
                str(chunk.get("content") or "")
                for chunk in indexer.read_chunks(index_dir)
            )
            self.assertIn("keep this content", content)
            self.assertNotIn("delete this content", content)

    def test_append_to_sharded_index_preserves_existing_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            (source_dir / "existing.txt").write_text(
                "existing shard content",
                encoding="utf-8",
            )
            indexer.build_index(source_dir, index_dir)
            manifest = self._manifest(index_dir)
            existing_record = manifest["documents"][0]
            existing_shard = (
                index_dir / existing_record[indexer.SHARD_PATH_FIELD]
            )
            existing_before = self._directory_snapshot(existing_shard)

            stats = indexer.build_index_from_nodes(
                [self._node("appended.txt", "appended shard content")],
                index_dir,
            )
            updated_manifest = self._manifest(index_dir)

            self.assertEqual(stats.added_count, 1)
            self.assertEqual(
                self._directory_snapshot(existing_shard),
                existing_before,
            )
            self.assertEqual(
                updated_manifest[indexer.STORAGE_LAYOUT_FIELD],
                indexer.SHARDED_STORAGE_LAYOUT,
            )
            self.assertTrue(
                all(
                    document.get(indexer.SHARD_PATH_FIELD)
                    for document in updated_manifest["documents"]
                )
            )

    def test_sharded_commit_hard_interruption_restores_old_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            changed = source_dir / "changed.txt"
            changed.write_text("old sharded content", encoding="utf-8")
            indexer.build_index(source_dir, index_dir)
            manifest_before = (
                index_dir / indexer.INDEX_MANIFEST_FILE
            ).read_bytes()
            record = self._manifest(index_dir)["documents"][0]
            shard_path = index_dir / record[indexer.SHARD_PATH_FIELD]
            shard_before = self._directory_snapshot(shard_path)
            changed.write_text("new sharded content", encoding="utf-8")
            real_replace = indexer._replace_index_file
            commit_calls = 0

            def hard_interrupt(source: Path, target: Path) -> None:
                nonlocal commit_calls
                is_top_commit = (
                    target == index_dir / indexer.INDEX_MANIFEST_FILE
                    or target.parent == index_dir / indexer.SHARDS_DIR
                )
                if is_top_commit:
                    commit_calls += 1
                    if commit_calls == 2:
                        raise KeyboardInterrupt(
                            "simulated sharded process termination"
                        )
                real_replace(source, target)

            with (
                patch.object(
                    indexer,
                    "_replace_index_file",
                    side_effect=hard_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                indexer.build_index(source_dir, index_dir)

            self.assertTrue(
                (index_dir / indexer.INDEX_TRANSACTION_FILE).exists()
            )
            self.assertTrue(indexer.recover_index_transaction(index_dir))
            self.assertEqual(
                (index_dir / indexer.INDEX_MANIFEST_FILE).read_bytes(),
                manifest_before,
            )
            self.assertEqual(
                self._directory_snapshot(shard_path),
                shard_before,
            )

    def test_legacy_monolith_migrates_only_changed_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            changed = source_dir / "changed.txt"
            stable = source_dir / "stable.txt"
            changed.write_text("legacy changed one", encoding="utf-8")
            stable.write_text("legacy stable", encoding="utf-8")
            document_sources = {
                path.name.casefold(): indexer._source_record_for_path(
                    path,
                    path.name,
                )
                for path in (changed, stable)
            }
            indexer.write_node_index(
                (
                    node
                    for path in (changed, stable)
                    for node in indexer.iter_document_nodes(
                        path,
                        source_path=path.name,
                    )
                ),
                source_label=source_dir.as_posix(),
                index_dir=index_dir,
                chunk_size=900,
                chunk_overlap=120,
                document_sources=document_sources,
            )
            monolith_before = {
                name: (index_dir / name).read_bytes()
                for name in ("nodes.jsonl", "parents.jsonl", "chunks.jsonl")
            }

            changed.write_text("legacy changed two", encoding="utf-8")
            stats = indexer.build_index(source_dir, index_dir)
            manifest = self._manifest(index_dir)
            records = {
                document["filename"]: document
                for document in manifest["documents"]
            }

            self.assertEqual(stats.updated_count, 1)
            self.assertEqual(stats.skipped_count, 1)
            self.assertIn(indexer.SHARD_PATH_FIELD, records["changed.txt"])
            self.assertNotIn(indexer.SHARD_PATH_FIELD, records["stable.txt"])
            self.assertEqual(
                {
                    name: (index_dir / name).read_bytes()
                    for name in ("nodes.jsonl", "parents.jsonl", "chunks.jsonl")
                },
                monolith_before,
            )
            content = "\n".join(
                str(chunk.get("content") or "")
                for chunk in indexer.read_chunks(index_dir)
            )
            self.assertIn("legacy changed two", content)
            self.assertIn("legacy stable", content)
            self.assertNotIn("legacy changed one", content)

    def test_bad_modified_file_keeps_old_version_and_other_files_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            protected = source_dir / "protected.txt"
            protected.write_text("last known good content", encoding="utf-8")
            indexer.build_index(source_dir, index_dir)

            protected.write_text("broken replacement", encoding="utf-8")
            added = source_dir / "added.txt"
            added.write_text("valid added content", encoding="utf-8")
            original_parser = indexer.iter_document_nodes

            def selective_parser(path, **kwargs):
                if Path(path).name == "protected.txt":
                    raise RuntimeError("simulated damaged document")
                yield from original_parser(path, **kwargs)

            with patch.object(
                indexer,
                "iter_document_nodes",
                side_effect=selective_parser,
            ):
                stats = indexer.build_index(source_dir, index_dir)

            content = "\n".join(
                str(chunk.get("content") or "")
                for chunk in indexer.read_chunks(index_dir)
            )
            self.assertEqual(stats.failed_count, 1)
            self.assertEqual(stats.added_count, 1)
            self.assertIn("simulated damaged document", stats.failed_files[0]["error"])
            self.assertIn("last known good content", content)
            self.assertIn("valid added content", content)
            self.assertNotIn("broken replacement", content)

    def test_cancellation_keeps_old_index_and_cleans_staging_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            document = source_dir / "large.txt"
            document.write_text("original content", encoding="utf-8")
            indexer.build_index(source_dir, index_dir)
            before = self._snapshot(index_dir)
            document.write_text("replacement content\n" * 1000, encoding="utf-8")
            checks = 0

            def should_cancel() -> bool:
                nonlocal checks
                checks += 1
                return checks >= 3

            with self.assertRaises(indexer.IndexCancelledError):
                indexer.sync_index_paths(
                    [document],
                    index_dir=index_dir,
                    source_root=source_dir,
                    source_label=source_dir.as_posix(),
                    remove_missing=True,
                    should_cancel=should_cancel,
                )

            self.assertEqual(self._snapshot(index_dir), before)
            self.assertFalse(list(index_dir.glob("*.tmp")))
            self.assertFalse((index_dir / indexer.INDEX_TRANSACTION_FILE).exists())

    def test_hard_interruption_is_recovered_on_next_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            indexer.write_node_index(
                [self._node("original.txt", "original content")],
                source_label="test",
                index_dir=index_dir,
                chunk_size=900,
                chunk_overlap=120,
            )
            before = self._snapshot(index_dir)
            real_replace = indexer._replace_index_file
            calls = 0

            def hard_interrupt(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt("simulated process termination")
                real_replace(source, target)

            with (
                patch.object(
                    indexer,
                    "_replace_index_file",
                    side_effect=hard_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                indexer.write_node_index(
                    [self._node("replacement.txt", "replacement content")],
                    source_label="test",
                    index_dir=index_dir,
                    chunk_size=900,
                    chunk_overlap=120,
                )

            self.assertTrue(
                (index_dir / indexer.INDEX_TRANSACTION_FILE).exists()
            )
            self.assertTrue(indexer.recover_index_transaction(index_dir))
            self.assertEqual(self._snapshot(index_dir), before)
            self.assertFalse(list(index_dir.glob("*.tmp")))
            self.assertFalse(list(index_dir.glob(".*.bak")))

    def test_diagnostics_detects_broken_lineage_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            (source_dir / "policy.txt").write_text(
                "diagnostic content",
                encoding="utf-8",
            )
            indexer.build_index(source_dir, index_dir)

            healthy = diagnose_index(index_dir)
            self.assertEqual(healthy["status"], "healthy")
            self.assertTrue(healthy["ready"])

            manifest = json.loads(
                (index_dir / indexer.INDEX_MANIFEST_FILE).read_text(
                    encoding="utf-8"
                )
            )
            parents_path = (
                index_dir
                / manifest["documents"][0][indexer.SHARD_PATH_FIELD]
                / indexer.PARENTS_FILE
            )
            parent = json.loads(
                parents_path.read_text(encoding="utf-8").splitlines()[0]
            )
            parent["content_node_id"] = "node_missing"
            parents_path.write_text(
                json.dumps(parent, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (index_dir / "bm25_index.sqlite3").write_bytes(b"not-sqlite")

            broken = diagnose_index(index_dir)
            codes = {issue["code"] for issue in broken["issues"]}
            warning_codes = {
                warning["code"] for warning in broken["warnings"]
            }
            self.assertEqual(broken["status"], "corrupt")
            self.assertIn("parent_node_missing", codes)
            self.assertIn("bm25_corrupt", warning_codes)

    def test_encrypted_and_damaged_files_have_clear_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            pdf_path = source_dir / "encrypted.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((40, 40), "protected content")
            document.save(
                pdf_path,
                encryption=fitz.PDF_ENCRYPT_AES_256,
                owner_pw="owner-password",
                user_pw="user-password",
            )
            document.close()
            (source_dir / "damaged.xlsx").write_bytes(b"not-an-xlsx-archive")

            stats = indexer.build_index(source_dir, index_dir)

            self.assertEqual(stats.failed_count, 2)
            errors = "\n".join(item["error"] for item in stats.failed_files)
            self.assertIn("XLSX is damaged or encrypted", errors)
            self.assertIn("PDF is encrypted and requires a password", errors)

    @staticmethod
    def _node(filename: str, content: str) -> DocumentNode:
        return DocumentNode(
            document_id=f"doc_{filename.replace('.', '_')}",
            content=content,
            parser_version="p0_7_test_v1",
            source_anchor={"source_path": filename},
            metadata={"filename": filename},
        )

    @staticmethod
    def _snapshot(index_dir: Path) -> dict[str, bytes]:
        return {
            name: (index_dir / name).read_bytes()
            for name in INDEX_FILES
        }

    @staticmethod
    def _manifest(index_dir: Path) -> dict:
        return json.loads(
            (index_dir / indexer.INDEX_MANIFEST_FILE).read_text(
                encoding="utf-8"
            )
        )

    @staticmethod
    def _directory_snapshot(path: Path) -> dict[str, bytes]:
        return {
            item.relative_to(path).as_posix(): item.read_bytes()
            for item in path.rglob("*")
            if item.is_file()
        }


if __name__ == "__main__":
    unittest.main()
