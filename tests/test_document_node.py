from __future__ import annotations

import unittest

from app.documents import (
    BoundingBox,
    DocumentNode,
    NodeType,
    content_sha256,
    document_id_from_source,
)


class DocumentNodeTests(unittest.TestCase):
    def test_desktop_runtime_uses_slots(self) -> None:
        self.assertTrue(hasattr(DocumentNode, "__slots__"))

    def test_node_types_cover_p0_2_contract(self) -> None:
        self.assertEqual(
            {item.value for item in NodeType},
            {
                "text",
                "table",
                "figure",
                "workbook_summary",
                "sheet_summary",
                "row_group",
            },
        )

    def test_ids_and_content_hash_are_deterministic(self) -> None:
        kwargs = {
            "document_id": document_id_from_source("policies/leave.md"),
            "content": "员工请假应提前提交申请。",
            "parser_version": "test_parser_v1",
            "source_anchor": {"source_path": "policies/leave.md"},
        }

        first = DocumentNode(**kwargs)
        second = DocumentNode(**kwargs)

        self.assertEqual(first.node_id, second.node_id)
        self.assertEqual(first.content_hash, content_sha256(first.content))

    def test_plain_text_does_not_duplicate_display_content(self) -> None:
        node = DocumentNode(
            document_id="doc_text",
            content="same content",
            display_content="same content",
            parser_version="test_parser_v1",
        )

        self.assertIsNone(node.display_content)
        self.assertEqual(node.effective_display_content, node.content)

    def test_structured_node_round_trip_preserves_location(self) -> None:
        node = DocumentNode(
            document_id="doc_table",
            content="部门 第二季度预算",
            display_content="| 部门 | 第二季度预算 |",
            parser_version="xlsx_parser_v1",
            node_type=NodeType.ROW_GROUP,
            page_or_sheet="预算",
            section_path=("年度预算", "部门预算"),
            sequence=3,
            bbox=BoundingBox(10, 20, 100, 80),
            row_start=2,
            row_end=6,
            column_start=1,
            column_end=4,
            parent_id="node_parent",
            source_anchor={
                "source_path": "budget.xlsx",
                "sheet": "预算",
                "row_start": 2,
                "row_end": 6,
            },
            metadata={"merged_ranges": ["A1:D1"]},
        )

        restored = DocumentNode.from_record(node.to_record())

        self.assertEqual(restored.to_record(), node.to_record())
        self.assertEqual(restored.node_type, NodeType.ROW_GROUP)
        self.assertEqual(restored.bbox, BoundingBox(10, 20, 100, 80))

    def test_invalid_ranges_and_hashes_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DocumentNode(
                document_id="doc_invalid",
                content="content",
                parser_version="test_parser_v1",
                row_start=5,
                row_end=2,
            )
        with self.assertRaises(ValueError):
            DocumentNode(
                document_id="doc_invalid",
                content="content",
                content_hash="incorrect",
                parser_version="test_parser_v1",
            )


if __name__ == "__main__":
    unittest.main()
