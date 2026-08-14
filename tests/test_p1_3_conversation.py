"""P1-3 Conversation & Memory tests"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.config import Settings
from app.core.database import (
    Base,
    async_session_factory,
    engine,
    init_db,
)
from app.services.conversation_service import ConversationService
from app.lite.followup_rewriter import rewrite_followup


async def _setup_db():
    """Create tables in a temp SQLite database."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _teardown_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class ConversationDBCrudTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        asyncio.run(_setup_db())

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(_teardown_db())

    def setUp(self) -> None:
        self._cleanup()

    def tearDown(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                await ConversationService.clear_all_conversations(db)
                await db.commit()

        asyncio.run(_run())

    def test_create_conversation(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                conv = await ConversationService.create_conversation(db)
                await db.commit()
                self.assertEqual(conv.title, "新对话")
                self.assertFalse(conv.is_archived)
                self.assertEqual(conv.message_count, 0)

        asyncio.run(_run())

    def test_create_conversation_with_title(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                conv = await ConversationService.create_conversation(
                    db, "报销政策咨询"
                )
                await db.commit()
                self.assertEqual(conv.title, "报销政策咨询")

        asyncio.run(_run())

    def test_list_conversations(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                await ConversationService.create_conversation(db, "A")
                await ConversationService.create_conversation(db, "B")
                await db.commit()
                convs = await ConversationService.list_conversations(db)
                self.assertEqual(len(convs), 2)

        asyncio.run(_run())

    def test_archive_conversation(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                conv = await ConversationService.create_conversation(db)
                await db.commit()
                ok = await ConversationService.archive_conversation(db, conv.id)
                await db.commit()
                self.assertTrue(ok)
                refreshed = await ConversationService.get_conversation(db, conv.id)
                self.assertTrue(refreshed.is_archived)

        asyncio.run(_run())

    def test_delete_conversation_cascades(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                conv = await ConversationService.create_conversation(db)
                await ConversationService.add_message(
                    db,
                    conv.id,
                    role="user",
                    original_query="test",
                )
                await ConversationService.add_message(
                    db,
                    conv.id,
                    role="assistant",
                    answer="response",
                )
                await db.commit()
                deleted = await ConversationService.delete_conversation(db, conv.id)
                await db.commit()
                self.assertTrue(deleted)
                # Verify cascade
                conv2 = await ConversationService.get_conversation(db, conv.id)
                self.assertIsNone(conv2)
                msgs = await ConversationService.get_messages(db, conv.id)
                self.assertEqual(len(msgs), 0)

        asyncio.run(_run())

    def test_add_message_updates_conversation(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                conv = await ConversationService.create_conversation(db)
                await ConversationService.add_message(
                    db,
                    conv.id,
                    role="user",
                    original_query="如何报销差旅费？",
                )
                await db.commit()
                refreshed = await ConversationService.get_conversation(db, conv.id)
                self.assertEqual(refreshed.message_count, 1)
                self.assertEqual(refreshed.title, "如何报销差旅费？")

        asyncio.run(_run())

    def test_get_active_context(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                conv = await ConversationService.create_conversation(db)
                await ConversationService.add_message(
                    db, conv.id, role="user", original_query="Q1"
                )
                await ConversationService.add_message(
                    db, conv.id, role="assistant", answer="A1"
                )
                await ConversationService.add_message(
                    db, conv.id, role="user", original_query="Q2"
                )
                await db.commit()
                summary, msgs = await ConversationService.get_active_context(
                    db, conv.id, max_recent=5
                )
                self.assertIsNone(summary)  # No summary yet
                self.assertEqual(len(msgs), 3)

        asyncio.run(_run())

    def test_search_conversations(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                await ConversationService.create_conversation(db, "报销")
                await ConversationService.create_conversation(db, "请假")
                await db.commit()
                results = await ConversationService.search_conversations(db, "报销")
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].title, "报销")

        asyncio.run(_run())

    def test_export_conversation(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                conv = await ConversationService.create_conversation(db, "test")
                await ConversationService.add_message(
                    db, conv.id, role="user", original_query="hello"
                )
                await ConversationService.add_message(
                    db,
                    conv.id,
                    role="assistant",
                    answer="world",
                    model="deepseek-v4-flash",
                    token_usage={"total_tokens": 100},
                    citations=[{"filename": "doc.txt", "content": "x"}],
                )
                await db.commit()
                data = await ConversationService.export_conversation(db, conv.id)
                self.assertEqual(data["id"], conv.id)
                self.assertEqual(data["title"], "test")
                self.assertEqual(len(data["messages"]), 2)
                self.assertEqual(data["messages"][1]["model"], "deepseek-v4-flash")
                self.assertEqual(data["messages"][1]["token_usage"]["total_tokens"], 100)
                self.assertEqual(len(data["messages"][1]["citations"]), 1)

        asyncio.run(_run())

    def test_clear_all_conversations(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                await ConversationService.create_conversation(db)
                await ConversationService.create_conversation(db)
                await db.commit()
                count = await ConversationService.clear_all_conversations(db)
                await db.commit()
                self.assertEqual(count, 2)
                convs = await ConversationService.list_conversations(db)
                self.assertEqual(len(convs), 0)

        asyncio.run(_run())


class RetrievalCacheTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        asyncio.run(_setup_db())

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(_teardown_db())

    def setUp(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                await ConversationService.invalidate_retrieval_cache(db)
                await db.commit()

        asyncio.run(_run())

    def test_cache_set_and_get(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                key = "test-cache-key"
                data = [{"filename": "a.txt", "content": "hello"}]
                await ConversationService.set_cached_retrieval(
                    db, key, data, index_version="v1"
                )
                await db.commit()
                cached = await ConversationService.get_cached_retrieval(db, key, "v1")
                self.assertEqual(len(cached), 1)
                self.assertEqual(cached[0]["filename"], "a.txt")

        asyncio.run(_run())

    def test_cache_version_mismatch(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                key = "version-key"
                await ConversationService.set_cached_retrieval(
                    db, key, [{"x": 1}], index_version="v1"
                )
                await db.commit()
                # Different version should miss
                cached = await ConversationService.get_cached_retrieval(db, key, "v2")
                self.assertIsNone(cached)

        asyncio.run(_run())

    def test_cache_invalidation(self) -> None:
        async def _run():
            async with async_session_factory() as db:
                await ConversationService.set_cached_retrieval(
                    db, "k1", [{}], index_version="v1"
                )
                await ConversationService.set_cached_retrieval(
                    db, "k2", [{}], index_version="v1"
                )
                await db.commit()
                count = await ConversationService.invalidate_retrieval_cache(db)
                await db.commit()
                self.assertEqual(count, 2)
                cached = await ConversationService.get_cached_retrieval(db, "k1", "v1")
                self.assertIsNone(cached)

        asyncio.run(_run())


class FollowupRewriteTests(unittest.TestCase):

    def test_no_history_returns_original(self) -> None:
        result = asyncio.run(rewrite_followup("How to submit?", []))
        self.assertEqual(result, "How to submit?")

    def test_no_rewrite_indicator_returns_original(self) -> None:
        result = asyncio.run(
            rewrite_followup(
                "How to submit expense?",
                [{"role": "user", "content": "Hello"}],
            )
        )
        self.assertEqual(result, "How to submit expense?")

    def test_detect_pronoun(self) -> None:
        # Without API key, should fall back to original
        result = asyncio.run(
            rewrite_followup(
                "What about that one?",
                [{"role": "user", "content": "What is the leave policy?"}],
                api_key="",
            )
        )
        # Should return original since no LLM available
        self.assertEqual(result, "What about that one?")

    def test_offline_returns_original(self) -> None:
        from app.security.remote_access import set_remote_access

        set_remote_access(False)
        try:
            result = asyncio.run(
                rewrite_followup(
                    "Where is that file?",
                    [{"role": "user", "content": "Which document has the policy?"}],
                    api_key="sk-test",
                )
            )
            self.assertEqual(result, "Where is that file?")
        finally:
            set_remote_access(True)

    def test_why_failed_triggers_rewrite(self) -> None:
        from app.lite.followup_rewriter import _should_rewrite

        for query in (
            "为什么失败",
            "为什么报错",
            "怎么回事",
            "失败的原因是什么",
            "为什么不回答",
            "怎么不回答",
        ):
            self.assertTrue(
                _should_rewrite(query),
                f"{query!r} 应触发改写以结合历史消解",
            )
        self.assertFalse(_should_rewrite("完全独立的问题"))


class HistoryAwareGeneratorTests(unittest.TestCase):

    def test_history_aware_prompt_includes_history(self) -> None:
        from app.lite.generator import HISTORY_AWARE_PROMPT

        prompt = HISTORY_AWARE_PROMPT.format(
            query="test query",
            context="test context",
            history="[用户]: 之前的问题\n[助手]: 之前的回答",
        )
        self.assertIn("之前的问题", prompt)
        self.assertIn("test query", prompt)
        self.assertIn("test context", prompt)

    def test_lite_prompt_no_history(self) -> None:
        from app.lite.generator import LITE_ANSWER_PROMPT

        prompt = LITE_ANSWER_PROMPT.format(query="q", context="c")
        self.assertIn("当前资料不足以回答", prompt)
        self.assertNotIn("对话历史", prompt)


if __name__ == "__main__":
    unittest.main()
