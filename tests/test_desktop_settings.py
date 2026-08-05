from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("desktop tests require PySide6") from exc

from app.desktop import main as desktop_main
from app.lite.remote_retrieval import set_remote_access


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


class DesktopSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        # MainWindow 会按离线设置更新模块级远程门禁，测试后恢复在线。
        set_remote_access(True)

    @patch.object(desktop_main, "QSettings", MemorySettings)
    def test_save_button_shows_dirty_and_saved_states(self) -> None:
        window = desktop_main.MainWindow()
        try:
            self.assertFalse(window.save_button.isEnabled())
            self.assertEqual(window.save_button.text(), "设置已保存")

            window.embedding_model_input.setText("embedding-test")
            self.assertTrue(window.save_button.isEnabled())
            self.assertEqual(window.save_button.text(), "保存设置")
            self.assertEqual(window.save_feedback.text(), "有未保存的更改")

            window.save_button.click()
            self.assertFalse(window.save_button.isEnabled())
            self.assertEqual(window.save_button.text(), "设置已保存")
            self.assertIn("已保存", window.save_feedback.text())
        finally:
            window.close()

    def test_file_selector_covers_all_supported_formats(self) -> None:
        for extension in (".txt", ".md", ".pdf", ".csv", ".xlsx"):
            self.assertIn(f"*{extension}", desktop_main.DESKTOP_FILE_FILTER)

    @patch.object(
        desktop_main.QInputDialog,
        "getItem",
        return_value=("latin-1", True),
    )
    @patch.object(desktop_main.MainWindow, "start_indexing")
    @patch.object(desktop_main, "QSettings", MemorySettings)
    def test_csv_encoding_choice_retries_same_files(
        self,
        start_indexing,
        _get_item,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.csv"
            path.write_bytes(b"name\ncaf\xe9\n")
            window = desktop_main.MainWindow()
            try:
                window._choose_csv_encoding(
                    [path],
                    {},
                    str(path),
                    "encoding failed",
                )
                start_indexing.assert_called_once_with(
                    [path],
                    {str(path.resolve()): "latin-1"},
                    source_root=None,
                    force_reparse=False,
                    replace_all=False,
                )
            finally:
                window.close()

    def test_large_index_worker_keeps_event_loop_responsive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            document = root / "large.txt"
            document.write_text(
                "enterprise policy responsiveness line\n" * 120_000,
                encoding="utf-8",
            )
            worker = desktop_main.IndexWorker([document], root / "index")
            loop = QEventLoop()
            timer = QTimer()
            ticks = 0
            errors: list[str] = []

            def record_tick() -> None:
                nonlocal ticks
                ticks += 1

            timer.setInterval(5)
            timer.timeout.connect(record_tick)
            worker.failed.connect(errors.append)
            worker.finished.connect(loop.quit)
            QTimer.singleShot(15_000, loop.quit)

            timer.start()
            worker.start()
            loop.exec()
            timer.stop()
            worker.wait(15_000)

            self.assertFalse(worker.isRunning())
            self.assertFalse(errors)
            self.assertGreater(ticks, 2)


if __name__ == "__main__":
    unittest.main()
