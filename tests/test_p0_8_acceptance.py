from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.lite.desktop_query import query_desktop_index
from app.lite.generator import _answer_with_llm, answer_query
from app.lite.indexer import write_chunks
from app.lite.remote_retrieval import (
    RemoteModelError,
    remote_access_enabled,
    set_remote_access,
)
from app.security import (
    DEFAULT_SERVICE,
    delete_secret,
    get_secret,
    redact_secrets,
    set_secret,
)


class OfflineGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(set_remote_access, False)

    def test_offline_gate_blocks_semantic_search(self) -> None:
        set_remote_access(False)
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(RemoteModelError) as raised:
                from app.lite.remote_retrieval import semantic_search_index

                semantic_search_index(
                    "测试",
                    temp_dir,
                    api_key="test-key",
                    base_url="https://example.test/v1",
                    model="embedding-test",
                )
        self.assertEqual(raised.exception.mode, "embedding_error")
        self.assertIn("离线", str(raised.exception))

    def test_offline_gate_blocks_reranker(self) -> None:
        from app.lite.remote_retrieval import rerank_sources

        set_remote_access(False)
        with self.assertRaises(RemoteModelError) as raised:
            rerank_sources(
                "测试",
                [{"content": "片段"}],
                top_n=1,
                api_key="test-key",
                base_url="https://example.test/v1",
                model="reranker-test",
            )
        self.assertEqual(raised.exception.mode, "reranker_error")

    def test_offline_gate_blocks_embed_texts(self) -> None:
        from app.lite.remote_retrieval import embed_texts, RemoteModelConfig

        set_remote_access(False)
        with self.assertRaises(RemoteModelError) as raised:
            embed_texts(
                ["文本"],
                RemoteModelConfig("key", "https://example.test/v1", "model"),
                mode="embedding_error",
            )
        self.assertEqual(raised.exception.mode, "embedding_error")

    def test_remote_access_flag_round_trip(self) -> None:
        set_remote_access(True)
        self.assertTrue(remote_access_enabled())
        set_remote_access(False)
        self.assertFalse(remote_access_enabled())
        set_remote_access(True)

    @patch("openai.AsyncOpenAI")
    def test_offline_gate_blocks_lite_llm(self, openai_mock) -> None:
        set_remote_access(False)
        result = asyncio.run(
            answer_query(
                "question",
                [{"filename": "policy.txt", "chunk_index": 0, "content": "local evidence"}],
                True,
                api_key="sk-test12345678",
                base_url="https://example.test/v1",
                model="model-test",
            )
        )
        self.assertEqual(result["mode"], "local_fallback")
        self.assertEqual(result["llm"]["error"], "offline_mode")
        openai_mock.assert_not_called()

    @patch("openai.AsyncOpenAI")
    @patch("app.lite.desktop_query.search_bm25_index")
    def test_desktop_request_preserves_context_model_over_shared_settings(
        self,
        search_mock,
        openai_mock,
    ) -> None:
        from app.core.config import settings

        set_remote_access(True)
        self.addCleanup(set_remote_access, False)
        search_mock.return_value = [
            {
                "filename": "policy.txt",
                "source_path": "policy.txt",
                "chunk_index": 0,
                "content": "local evidence",
                "rank": 1,
                "score": 1.0,
            }
        ]
        openai_mock.return_value.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="answer [1]")
                    )
                ],
                model="mimo-v2.5-free",
                usage=None,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            settings,
            "LLM_MODEL",
            "deepseek-v4-pro",
        ):
            write_chunks(
                Path(temp_dir),
                [
                    {
                        "id": "policy.txt:0",
                        "source_path": "policy.txt",
                        "filename": "policy.txt",
                        "chunk_index": 0,
                        "content": "local evidence",
                    }
                ],
            )
            result = asyncio.run(
                query_desktop_index(
                    "question",
                    temp_dir,
                    use_llm=True,
                    llm_api_key="sk-test12345678",
                    llm_base_url="https://example.test/v1",
                    llm_model="mimo-v2.5-free",
                    use_embedding=False,
                    use_reranker=False,
                    retrieval_api_key="",
                )
            )

        self.assertEqual(result["mode"], "llm")
        create_mock = openai_mock.return_value.chat.completions.create
        self.assertEqual(create_mock.call_args.kwargs["model"], "mimo-v2.5-free")
        self.assertEqual(result["llm"]["model"], "mimo-v2.5-free")

    @patch("openai.AsyncOpenAI")
    def test_lite_request_does_not_fallback_to_shared_model(
        self,
        openai_mock,
    ) -> None:
        from app.core.config import DEFAULT_LLM_MODEL, settings

        set_remote_access(True)
        self.addCleanup(set_remote_access, False)
        openai_mock.return_value.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="answer [1]")
                    )
                ],
                model=DEFAULT_LLM_MODEL,
                usage=None,
            )
        )

        with patch.object(settings, "LLM_MODEL", "mimo-v2.5-free"):
            result = asyncio.run(
                answer_query(
                    "question",
                    [
                        {
                            "filename": "policy.txt",
                            "chunk_index": 0,
                            "content": "local evidence",
                        }
                    ],
                    True,
                    api_key="sk-test12345678",
                    base_url="https://example.test/v1",
                    model="",
                )
            )

        self.assertEqual(result["mode"], "llm")
        create_mock = openai_mock.return_value.chat.completions.create
        self.assertEqual(create_mock.call_args.kwargs["model"], DEFAULT_LLM_MODEL)

    @patch("openai.AsyncOpenAI")
    def test_offline_gate_blocks_legacy_rag_llm(self, openai_mock) -> None:
        from app.core.config import settings
        from app.rag.generator import RAGAnswerGenerator

        set_remote_access(False)
        with patch.object(settings, "LLM_API_KEY", "sk-test12345678"):
            result = asyncio.run(
                RAGAnswerGenerator().generate(
                    "question",
                    [{"content": "local evidence"}],
                )
            )
        self.assertEqual(result["mode"], "local_fallback")
        self.assertEqual(result["llm"]["error"], "offline_mode")
        openai_mock.assert_not_called()

    @patch("app.rag.hyde.AsyncOpenAI")
    def test_offline_gate_blocks_hyde(self, openai_mock) -> None:
        from app.rag.hyde import HyDE

        set_remote_access(False)
        self.assertEqual(asyncio.run(HyDE().generate("question")), "question")
        openai_mock.assert_not_called()

    def test_offline_gate_forces_local_model_cache(self) -> None:
        sentence_transformer = Mock()
        cross_encoder = Mock()
        module = SimpleNamespace(
            SentenceTransformer=sentence_transformer,
            CrossEncoder=cross_encoder,
        )
        numpy_module = SimpleNamespace(ndarray=object)
        set_remote_access(False)
        with patch.dict(
            sys.modules,
            {
                "numpy": numpy_module,
                "sentence_transformers": module,
            },
        ):
            from app.rag.embedder import Embedder
            from app.rag.reranker import Reranker

            Embedder("embedding-test")._load_model()
            Reranker("reranker-test", use_model=True)

        sentence_transformer.assert_called_once_with(
            "embedding-test",
            local_files_only=True,
        )
        cross_encoder.assert_called_once_with(
            "reranker-test",
            local_files_only=True,
        )

    @patch("app.lite.desktop_query.search_bm25_index")
    def test_desktop_query_offline_forces_local_only(self, search_mock) -> None:
        search_mock.return_value = [
            {
                "filename": "policy.txt",
                "source_path": "policy.txt",
                "chunk_index": 0,
                "content": "差旅结束后三十天内提交报销。",
                "rank": 1,
                "score": 1.0,
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            write_chunks(
                Path(temp_dir),
                [
                    {
                        "id": "policy.txt:0",
                        "source_path": "policy.txt",
                        "filename": "policy.txt",
                        "chunk_index": 0,
                        "content": "差旅结束后三十天内提交报销。",
                    }
                ],
            )
            result = asyncio.run(
                query_desktop_index(
                    "如何报销",
                    temp_dir,
                    use_llm=True,
                    llm_api_key="",
                    llm_base_url="",
                    llm_model="",
                    use_embedding=True,
                    use_reranker=True,
                    retrieval_api_key="",
                    offline=True,
                )
            )
        self.assertTrue(result["retrieval"]["offline"])
        self.assertFalse(result["retrieval"]["remote"])
        self.assertFalse(result["llm"]["enabled"])
        self.assertEqual(result["mode"], "local_fallback")
        self.assertTrue(result["sources"])
        search_mock.assert_called_once()


class CredentialStoreTests(unittest.TestCase):
    @patch("app.security.credentials._read_credential", return_value="secret-value")
    def test_get_secret_reads_from_backend(self, read_mock) -> None:
        self.assertEqual(get_secret("svc", "acct"), "secret-value")
        read_mock.assert_called_once_with("svc", "acct")

    @patch("app.security.credentials._write_credential")
    def test_set_secret_writes_to_backend(self, write_mock) -> None:
        set_secret("svc", "acct", "value")
        write_mock.assert_called_once_with("svc", "acct", "value")

    @patch("app.security.credentials._delete_credential")
    def test_set_secret_empty_value_deletes(self, delete_mock) -> None:
        set_secret("svc", "acct", "")
        delete_mock.assert_called_once_with("svc", "acct")

    def test_empty_account_is_safe_noop(self) -> None:
        self.assertEqual(get_secret("", ""), "")
        set_secret("", "", "value")  # 不应抛错

    def test_backend_name_is_resolved(self) -> None:
        from app.security import get_backend_name

        self.assertIn(
            get_backend_name(),
            ("windows_credential_manager", "macos_keychain", "fallback_file"),
        )

    @unittest.skipUnless(sys.platform == "win32", "需要 Windows Credential Manager")
    def test_windows_credential_manager_round_trip(self) -> None:
        account = f"p0_8_test_{uuid4().hex[:8]}"
        try:
            set_secret(DEFAULT_SERVICE, account, "roundtrip-secret")
            self.assertEqual(get_secret(DEFAULT_SERVICE, account), "roundtrip-secret")
        finally:
            delete_secret(DEFAULT_SERVICE, account)
        self.assertEqual(get_secret(DEFAULT_SERVICE, account), "")

    def test_fallback_file_round_trip(self) -> None:
        with patch(
            "app.security.credentials._fallback_path",
            return_value=Path(tempfile.mkdtemp()) / "secrets.bin",
        ):
            service = "p0_8_fallback"
            account = "llm_api_key"
            set_secret(service, account, "fallback-value")
            try:
                self.assertEqual(get_secret(service, account), "fallback-value")
            finally:
                delete_secret(service, account)
            self.assertEqual(get_secret(service, account), "")


class RedactionTests(unittest.TestCase):
    def test_bearer_token_redacted(self) -> None:
        text = "Authorization: Bearer sk-abcdef1234567890"
        self.assertNotIn("sk-abcdef1234567890", redact_secrets(text))
        self.assertIn("[REDACTED]", redact_secrets(text))

    def test_bare_sk_key_redacted(self) -> None:
        self.assertIn("[REDACTED]", redact_secrets("invalid key sk-xxxxyyyy1234567890"))

    def test_api_key_equals_redacted(self) -> None:
        self.assertIn("[REDACTED]", redact_secrets('api_key=supersecretvalue123'))

    def test_plain_text_unchanged(self) -> None:
        self.assertEqual(redact_secrets("正常的中文错误信息"), "正常的中文错误信息")

    @patch("openai.AsyncOpenAI")
    def test_llm_error_message_redacts_key(self, openai_mock) -> None:
        set_remote_access(True)
        self.addCleanup(set_remote_access, False)
        openai_mock.return_value.chat.completions.create.side_effect = RuntimeError(
            "401 Unauthorized key sk-abcdef1234567890 leaked"
        )
        result = asyncio.run(
            _answer_with_llm(
                "问题",
                "资料",
                "sk-abcdef1234567890",
                "https://example.test",
                "model-test",
            )
        )
        error = result["llm"]["error"]
        self.assertNotIn("sk-abcdef1234567890", error)
        self.assertIn("[REDACTED]", error)

    @patch("openai.AsyncOpenAI")
    def test_legacy_rag_error_message_redacts_key(self, openai_mock) -> None:
        from app.core.config import settings
        from app.rag.generator import RAGAnswerGenerator

        set_remote_access(True)
        self.addCleanup(set_remote_access, False)
        openai_mock.return_value.chat.completions.create.side_effect = RuntimeError(
            "401 Unauthorized key sk-abcdef1234567890 leaked"
        )
        with patch.object(settings, "LLM_API_KEY", "configured-key"):
            result = asyncio.run(
                RAGAnswerGenerator()._generate_with_llm("question", "context")
            )
        error = result["llm"]["error"]
        self.assertNotIn("sk-abcdef1234567890", error)
        self.assertIn("[REDACTED]", error)

    @patch("app.rag.hyde.AsyncOpenAI")
    def test_hyde_error_log_redacts_key(self, openai_mock) -> None:
        from app.rag.hyde import HyDE

        set_remote_access(True)
        self.addCleanup(set_remote_access, False)
        openai_mock.return_value.chat.completions.create.side_effect = RuntimeError(
            "401 Unauthorized key sk-abcdef1234567890 leaked"
        )
        with patch("app.rag.hyde.logger.warning") as warning_mock:
            self.assertEqual(asyncio.run(HyDE().generate("question")), "question")
        logged = " ".join(str(value) for call in warning_mock.call_args_list for value in call.args)
        self.assertNotIn("sk-abcdef1234567890", logged)
        self.assertIn("[REDACTED]", logged)


class DesktopPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from PySide6.QtWidgets import QApplication
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest("desktop tests require PySide6") from exc
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        MemorySettings.values = {}
        set_remote_access(False)

    def _make_window(self, memory_values: dict):
        from app.desktop import main as desktop_main

        MemorySettings.values = dict(memory_values)
        # MainWindow 构造会初始化聊天 DB、加载会话、统计磁盘，在 offscreen/
        # 打包环境可能卡住；本测试只验证隐私/离线逻辑，将这些慢路径 patch 掉。
        with patch.object(desktop_main, "QSettings", MemorySettings), patch.object(
            desktop_main, "get_secret", return_value=""
        ), patch.object(desktop_main, "set_secret"), patch.object(
            desktop_main, "set_remote_access"
        ), patch.object(desktop_main, "cleanup_stale_temp_files"), patch.object(
            desktop_main.MainWindow, "_ensure_chat_db", return_value=None
        ), patch.object(
            desktop_main.MainWindow, "start_disk_stats", return_value=None
        ), patch.object(
            desktop_main.MainWindow, "_load_conversations", return_value=None
        ), patch.object(
            desktop_main.MainWindow, "_cleanup_old_data", return_value=None
        ):
            return desktop_main.MainWindow()

    def _close_window(self, window) -> None:
        MemorySettings.values = {}
        window.close()

    def test_offline_mode_disables_remote_checkboxes(self) -> None:
        window = self._make_window({"privacy/offline_mode": True})
        try:
            self.assertTrue(window.offline_checkbox.isChecked())
            self.assertFalse(window.use_llm_checkbox.isEnabled())
            self.assertFalse(window.use_embedding_checkbox.isEnabled())
            self.assertFalse(window.use_reranker_checkbox.isEnabled())
            window.offline_checkbox.setChecked(False)
            self.assertTrue(window.use_llm_checkbox.isEnabled())
            self.assertTrue(window.use_embedding_checkbox.isEnabled())
            self.assertTrue(window.use_reranker_checkbox.isEnabled())
        finally:
            self._close_window(window)

    def test_offline_defaults_to_on(self) -> None:
        window = self._make_window({})
        try:
            self.assertTrue(window.offline_checkbox.isChecked())
        finally:
            self._close_window(window)

    def test_ask_question_consent_cancel_does_not_query(self) -> None:
        from app.desktop import main as desktop_main

        window = self._make_window(
            {
                "privacy/offline_mode": False,
                "features/use_llm": True,
                "llm/base_url": "https://llm.example",
            }
        )
        try:
            with patch.object(desktop_main, "QueryWorker") as query_worker, patch.object(
                window, "_confirm_remote_consent", return_value=False
            ):
                window.query_input.setPlainText("测试问题")
                window.ask_question()
            query_worker.assert_not_called()
            # P1-4 后网络状态是持久状态行；取消 consent 时保持初始"模式：—"。
            self.assertEqual(window.network_status_label.text(), "模式：—")
        finally:
            self._close_window(window)

    def test_ask_question_remote_shows_network_indicator(self) -> None:
        from app.desktop import main as desktop_main

        window = self._make_window(
            {
                "privacy/offline_mode": False,
                "features/use_llm": True,
                "llm/base_url": "https://llm.example",
            }
        )
        try:
            with patch.object(desktop_main, "QueryWorker") as query_worker, patch.object(
                window, "_confirm_remote_consent", return_value=True
            ):
                window.query_input.setPlainText("测试问题")
                window.ask_question()
            query_worker.assert_called_once()
            query_worker.return_value.isRunning.return_value = False
            self.assertFalse(window.network_status_label.isHidden())
        finally:
            self._close_window(window)

    def test_consent_cancel_returns_false_and_does_not_remember(self) -> None:
        from app.desktop import main as desktop_main

        window = self._make_window({"privacy/offline_mode": False})

        class FakeBox:
            Warning = 2
            AcceptRole = 0
            RejectRole = 1

            def __init__(self, *_args, **_kwargs):
                self.remember = "remember-button"
                self.once = "once-button"
                self.cancel = "cancel-button"
                self.pressed = self.cancel

            def setWindowTitle(self, _title):
                return None

            def setIcon(self, _icon):
                return None

            def setText(self, _text):
                return None

            def setDefaultButton(self, _button):
                return None

            def addButton(self, label, _role):
                return {"同意并记住": self.remember, "仅本次同意": self.once, "取消": self.cancel}[label]

            def exec(self):
                return 0

            def clickedButton(self):
                return self.pressed

        try:
            with patch.object(desktop_main, "QMessageBox", FakeBox):
                window.base_url_input.setText("https://llm.example")
                accepted = window._confirm_remote_consent("测试问题", True, False, False)
            self.assertFalse(accepted)
            self.assertEqual(window._consented_endpoints(), set())
        finally:
            self._close_window(window)

    def test_consent_remember_persists_endpoint(self) -> None:
        from app.desktop import main as desktop_main

        window = self._make_window({"privacy/offline_mode": False})

        class FakeBox:
            Warning = 2
            AcceptRole = 0
            RejectRole = 1

            def __init__(self, *_args, **_kwargs):
                self.remember = "remember-button"
                self.once = "once-button"
                self.cancel = "cancel-button"
                self.pressed = self.remember

            def setWindowTitle(self, _title):
                return None

            def setIcon(self, _icon):
                return None

            def setText(self, _text):
                return None

            def setDefaultButton(self, _button):
                return None

            def addButton(self, label, _role):
                return {"同意并记住": self.remember, "仅本次同意": self.once, "取消": self.cancel}[label]

            def exec(self):
                return 0

            def clickedButton(self):
                return self.pressed

        try:
            with patch.object(desktop_main, "QMessageBox", FakeBox):
                window.base_url_input.setText("https://llm.example")
                accepted = window._confirm_remote_consent("测试问题", True, False, False)
            self.assertTrue(accepted)
            self.assertIn("https://llm.example", window._consented_endpoints())
        finally:
            self._close_window(window)

    def test_legacy_api_keys_migrate_to_credential_store(self) -> None:
        from app.desktop import main as desktop_main

        recorded: dict[str, str] = {}

        def capture(service, account, value):
            recorded[account] = value

        MemorySettings.values = {
            "release/schema_version": 2,
            "llm/api_key": "legacy-llm-key",
            "retrieval/api_key": "legacy-retrieval-key",
        }
        with patch.object(desktop_main, "QSettings", MemorySettings), patch.object(
            desktop_main, "set_secret", side_effect=capture
        ), patch.object(desktop_main, "get_secret", return_value=""), patch.object(
            desktop_main, "set_remote_access"
        ), patch.object(desktop_main, "cleanup_stale_temp_files"), patch.object(
            desktop_main.MainWindow, "_ensure_chat_db", return_value=None
        ), patch.object(
            desktop_main.MainWindow, "start_disk_stats", return_value=None
        ), patch.object(
            desktop_main.MainWindow, "_load_conversations", return_value=None
        ), patch.object(
            desktop_main.MainWindow, "_cleanup_old_data", return_value=None
        ):
            window = desktop_main.MainWindow()
            self.assertEqual(recorded.get("llm_api_key"), "legacy-llm-key")
            self.assertEqual(recorded.get("retrieval_api_key"), "legacy-retrieval-key")
            # 迁移后旧明文键从设置副本中移除，不再写入 QSettings。
            self.assertNotIn("llm/api_key", window.settings._values)
            self.assertNotIn("retrieval/api_key", window.settings._values)
            window.close()
        MemorySettings.values = {}

    def test_temp_cleanup_removes_only_stale_files(self) -> None:
        from app.desktop import main as desktop_main

        root = Path(tempfile.mkdtemp())
        try:
            stale = root / "stale.tmp"
            stale.write_bytes(b"x")
            fresh = root / "fresh.tmp"
            fresh.write_bytes(b"y")
            old = time.time() - 8 * 24 * 3600
            os.utime(stale, (old, old))
            with patch.object(desktop_main, "desktop_temp_dir", return_value=root):
                desktop_main.cleanup_stale_temp_files()
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)


class MemorySettings:
    values: dict[str, object] = {}

    def __init__(self, *_args) -> None:
        self._values = dict(self.values)

    def setFallbacksEnabled(self, _enabled: bool) -> None:
        return None

    def value(self, key: str, default=None, value_type=None):
        value = self._values.get(key, default)
        if value_type is bool:
            return bool(value)
        if value_type is str:
            return str(value)
        return value

    def setValue(self, key: str, value) -> None:
        self._values[key] = value

    def remove(self, key: str) -> None:
        self._values.pop(key, None)

    def sync(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
