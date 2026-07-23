from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.lite.generator import answer_query
from app.lite.indexer import (
    SUPPORTED_EXTENSIONS,
    build_index_from_uploads,
    delete_index_document,
    extract_text,
    list_index_documents,
)
from app.lite.search import search_index


APP_NAME = "Local Knowledge Tool"
ORGANIZATION = "EnterpriseKnowledgeAgent"
SETTINGS_APP_NAME = "Local Knowledge Tool Desktop 1.0"
SETTINGS_SCHEMA_VERSION = 1


def desktop_index_dir() -> Path:
    override = os.getenv("DESKTOP_INDEX_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return Path("data/lite_index").resolve()
    app_data = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    return Path(app_data).resolve() / "lite_index"


def filter_sources_by_answer(answer: str, sources: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    if mode == "llm_error":
        return []
    cited_ranks = []
    for value in re.findall(r"\[(\d+)\]", answer or ""):
        rank = int(value)
        if 1 <= rank <= len(sources) and rank not in cited_ranks:
            cited_ranks.append(rank)
    if not cited_ranks:
        return sources
    return [sources[rank - 1] for rank in cited_ranks]


class IndexWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, paths: List[Path], index_dir: Path) -> None:
        super().__init__()
        self.paths = paths
        self.index_dir = index_dir

    def run(self) -> None:
        try:
            documents = []
            for path in self.paths:
                text = extract_text(path)
                if text.strip():
                    documents.append((path.name, text))
            stats = build_index_from_uploads(documents, self.index_dir)
            self.completed.emit(stats.__dict__)
        except Exception as exc:
            self.failed.emit(str(exc))


class QueryWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        query: str,
        index_dir: Path,
        use_llm: bool,
        api_key: str,
        base_url: str,
        model: str,
    ) -> None:
        super().__init__()
        self.query = query
        self.index_dir = index_dir
        self.use_llm = use_llm
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    def run(self) -> None:
        try:
            sources = search_index(self.query, self.index_dir, top_k=5)
            result = asyncio.run(
                answer_query(
                    self.query,
                    sources,
                    self.use_llm,
                    api_key=self.api_key,
                    base_url=self.base_url,
                    model=self.model,
                )
            )
            display_sources = filter_sources_by_answer(result["answer"], sources, result["mode"])
            self.completed.emit({
                "answer": result["answer"],
                "mode": result["mode"],
                "sources": display_sources,
                "llm": result.get("llm") or {},
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.index_dir = desktop_index_dir()
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(ORGANIZATION, SETTINGS_APP_NAME)
        self.settings.setFallbacksEnabled(False)
        self._initialize_release_settings()
        self._workers: List[QThread] = []

        self.setWindowTitle("本地知识库")
        self.setMinimumSize(780, 620)
        self.resize(980, 760)
        self.setStyleSheet(APP_STYLE)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._build_chat_tab(), "对话")
        tabs.addTab(self._build_knowledge_tab(), "知识库")
        tabs.addTab(self._build_settings_tab(), "设置")
        self.setCentralWidget(tabs)

        self.statusBar().showMessage("就绪")
        self._load_settings()
        self.refresh_documents()

    def _initialize_release_settings(self) -> None:
        current_version = int(self.settings.value("release/schema_version", 0) or 0)
        if current_version >= SETTINGS_SCHEMA_VERSION:
            return
        self.settings.remove("llm/api_key")
        self.settings.setValue("release/schema_version", SETTINGS_SCHEMA_VERSION)
        self.settings.sync()

    def _build_chat_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        heading = QLabel("知识库问答")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        self.query_input = QPlainTextEdit()
        self.query_input.setPlaceholderText("输入与已添加文档相关的问题")
        self.query_input.setMaximumHeight(110)
        layout.addWidget(self.query_input)

        actions = QHBoxLayout()
        self.use_llm_checkbox = QCheckBox("使用 LLM 汇总答案")
        self.use_llm_checkbox.setChecked(True)
        actions.addWidget(self.use_llm_checkbox)
        actions.addStretch()

        self.ask_button = QPushButton("查询")
        self.ask_button.setIcon(self.style().standardIcon(QStyle.SP_ArrowForward))
        self.ask_button.clicked.connect(self.ask_question)
        actions.addWidget(self.ask_button)
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Vertical)

        answer_panel = QWidget()
        answer_layout = QVBoxLayout(answer_panel)
        answer_layout.setContentsMargins(0, 0, 0, 0)
        answer_label = QLabel("答案")
        answer_label.setObjectName("sectionTitle")
        answer_layout.addWidget(answer_label)
        self.answer_output = QPlainTextEdit()
        self.answer_output.setReadOnly(True)
        self.answer_output.setPlaceholderText("答案会显示在这里")
        answer_layout.addWidget(self.answer_output)
        splitter.addWidget(answer_panel)

        sources_panel = QWidget()
        sources_layout = QVBoxLayout(sources_panel)
        sources_layout.setContentsMargins(0, 0, 0, 0)
        sources_label = QLabel("引用来源")
        sources_label.setObjectName("sectionTitle")
        sources_layout.addWidget(sources_label)

        self.sources_container = QWidget()
        self.sources_layout = QVBoxLayout(self.sources_container)
        self.sources_layout.setContentsMargins(0, 0, 0, 0)
        self.sources_layout.setSpacing(8)
        self.sources_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.sources_container)
        sources_layout.addWidget(scroll)
        splitter.addWidget(sources_panel)
        splitter.setSizes([360, 230])

        layout.addWidget(splitter, 1)
        return page

    def _build_knowledge_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        heading = QLabel("知识库文档")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        actions = QHBoxLayout()
        self.add_files_button = QPushButton("添加文件")
        self.add_files_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.add_files_button.clicked.connect(self.choose_files)
        actions.addWidget(self.add_files_button)

        self.add_folder_button = QPushButton("添加文件夹")
        self.add_folder_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.add_folder_button.clicked.connect(self.choose_folder)
        actions.addWidget(self.add_folder_button)

        actions.addStretch()

        self.delete_button = QPushButton("删除所选")
        self.delete_button.setObjectName("dangerButton")
        self.delete_button.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        self.delete_button.clicked.connect(self.delete_selected_document)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)

        self.documents_table = QTableWidget(0, 3)
        self.documents_table.setHorizontalHeaderLabels(["文件名", "片段", "字符数"])
        self.documents_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.documents_table.setSelectionMode(QTableWidget.SingleSelection)
        self.documents_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.documents_table.verticalHeader().setVisible(False)
        header = self.documents_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.documents_table, 1)

        self.index_path_label = QLabel()
        self.index_path_label.setObjectName("mutedText")
        self.index_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.index_path_label)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        heading = QLabel("LLM 设置")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("填写你的 LLM API Key")
        form.addRow("API Key", self.api_key_input)

        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("https://api.deepseek.com")
        form.addRow("Base URL", self.base_url_input)

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("deepseek-v4-flash")
        form.addRow("Model", self.model_input)
        layout.addLayout(form)

        actions = QHBoxLayout()
        actions.addStretch()
        save_button = QPushButton("保存设置")
        save_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        save_button.clicked.connect(self.save_settings)
        actions.addWidget(save_button)
        layout.addLayout(actions)

        note = QLabel("设置保存在当前系统用户配置中。使用 LLM 时，API Key 会发送到配置的服务地址。")
        note.setWordWrap(True)
        note.setObjectName("mutedText")
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _load_settings(self) -> None:
        self.api_key_input.setText(self.settings.value("llm/api_key", "", str))
        self.base_url_input.setText(
            self.settings.value("llm/base_url", "https://api.deepseek.com", str)
        )
        self.model_input.setText(
            self.settings.value("llm/model", "deepseek-v4-flash", str)
        )

    def save_settings(self) -> None:
        self.settings.setValue("llm/api_key", self.api_key_input.text().strip())
        self.settings.setValue("llm/base_url", self.base_url_input.text().strip())
        self.settings.setValue("llm/model", self.model_input.text().strip())
        self.settings.sync()
        self.statusBar().showMessage("LLM 设置已保存", 4000)

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择知识文档",
            "",
            "Knowledge files (*.pdf *.txt *.md)",
        )
        if paths:
            self.start_indexing([Path(path) for path in paths])

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择知识文档文件夹")
        if not folder:
            return
        paths = [
            path
            for path in sorted(Path(folder).rglob("*"))
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not paths:
            QMessageBox.information(self, "没有文档", "所选文件夹中没有 PDF、TXT 或 MD 文件。")
            return
        self.start_indexing(paths)

    def start_indexing(self, paths: List[Path]) -> None:
        self._set_busy(True, "正在读取并构建索引...")
        worker = IndexWorker(paths, self.index_dir)
        worker.completed.connect(self._indexing_completed)
        worker.failed.connect(self._task_failed)
        worker.finished.connect(lambda: self._release_worker(worker))
        self._workers.append(worker)
        worker.start()

    def _indexing_completed(self, payload: Dict[str, Any]) -> None:
        self._set_busy(False)
        self.refresh_documents()
        added = int(payload.get("added_count") or 0)
        skipped = payload.get("skipped_files") or []
        message = f"新增 {added} 个文档"
        if skipped:
            message += f"，跳过重复文件：{'、'.join(skipped)}"
        self.statusBar().showMessage(message, 7000)

    def refresh_documents(self) -> None:
        documents = list_index_documents(self.index_dir)
        self.documents_table.setRowCount(len(documents))
        for row, document in enumerate(documents):
            filename_item = QTableWidgetItem(str(document.get("filename") or ""))
            filename_item.setData(Qt.UserRole, str(document.get("filename") or ""))
            self.documents_table.setItem(row, 0, filename_item)
            self.documents_table.setItem(
                row, 1, QTableWidgetItem(str(document.get("chunk_count") or 0))
            )
            self.documents_table.setItem(
                row, 2, QTableWidgetItem(str(document.get("content_chars") or 0))
            )
        self.index_path_label.setText(f"索引位置：{self.index_dir}")
        self.ask_button.setEnabled(bool(documents))
        self.delete_button.setEnabled(bool(documents))

    def delete_selected_document(self) -> None:
        row = self.documents_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "请选择文档", "请先选择要删除的文档。")
            return
        item = self.documents_table.item(row, 0)
        filename = str(item.data(Qt.UserRole) or item.text())
        answer = QMessageBox.question(
            self,
            "删除文档",
            f"删除“{filename}”及其索引？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            delete_index_document(filename, self.index_dir)
            self.refresh_documents()
            self.clear_sources()
            self.answer_output.setPlainText("文档已删除。")
            self.statusBar().showMessage("文档及索引已删除", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))

    def ask_question(self) -> None:
        query = self.query_input.toPlainText().strip()
        if not query:
            QMessageBox.information(self, "请输入问题", "请先输入要查询的问题。")
            return

        self.save_settings()
        self._set_busy(True, "正在检索并生成答案...")
        self.answer_output.setPlainText("正在查询...")
        self.clear_sources()

        worker = QueryWorker(
            query=query,
            index_dir=self.index_dir,
            use_llm=self.use_llm_checkbox.isChecked(),
            api_key=self.api_key_input.text().strip(),
            base_url=self.base_url_input.text().strip(),
            model=self.model_input.text().strip(),
        )
        worker.completed.connect(self._query_completed)
        worker.failed.connect(self._task_failed)
        worker.finished.connect(lambda: self._release_worker(worker))
        self._workers.append(worker)
        worker.start()

    def _query_completed(self, payload: Dict[str, Any]) -> None:
        self._set_busy(False)
        self.answer_output.setPlainText(str(payload.get("answer") or "没有返回答案。"))
        sources = payload.get("sources") or []
        self.render_sources(sources)
        mode = str(payload.get("mode") or "")
        if mode == "llm_error":
            self.statusBar().showMessage("LLM 配置或请求失败", 7000)
        elif mode == "llm":
            self.statusBar().showMessage("LLM 回答完成", 5000)
        else:
            self.statusBar().showMessage("本地检索完成", 5000)

    def clear_sources(self) -> None:
        while self.sources_layout.count() > 1:
            item = self.sources_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def render_sources(self, sources: List[Dict[str, Any]]) -> None:
        self.clear_sources()
        if not sources:
            empty = QLabel("没有引用来源。")
            empty.setObjectName("mutedText")
            self.sources_layout.insertWidget(0, empty)
            return

        for source in sources:
            box = QFrame()
            box.setObjectName("sourceBox")
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(10, 8, 10, 8)

            filename = QLabel(str(source.get("filename") or "未知文件"))
            filename.setObjectName("sourceFilename")
            box_layout.addWidget(filename)

            content = QPlainTextEdit()
            content.setReadOnly(True)
            content.setPlainText(str(source.get("content") or ""))
            content.setMaximumHeight(125)
            content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            box_layout.addWidget(content)
            self.sources_layout.insertWidget(self.sources_layout.count() - 1, box)

    def _task_failed(self, message: str) -> None:
        self._set_busy(False)
        self.answer_output.setPlainText(f"操作失败：{message}")
        self.clear_sources()
        self.statusBar().showMessage("操作失败", 7000)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.ask_button.setDisabled(busy)
        self.add_files_button.setDisabled(busy)
        self.add_folder_button.setDisabled(busy)
        self.delete_button.setDisabled(busy)
        if message:
            self.statusBar().showMessage(message)
        if not busy:
            self.refresh_documents()

    def _release_worker(self, worker: QThread) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:
        active_workers = [worker for worker in self._workers if worker.isRunning()]
        if active_workers:
            answer = QMessageBox.question(
                self,
                "任务仍在运行",
                "当前任务尚未完成，确定退出？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()


APP_STYLE = """
QMainWindow, QWidget {
    background: #f5f7fa;
    color: #18202a;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #d9e0ea;
    background: #ffffff;
}
QTabBar::tab {
    background: #e9eef5;
    border: 1px solid #d9e0ea;
    border-bottom: none;
    padding: 9px 18px;
}
QTabBar::tab:selected {
    background: #ffffff;
    color: #1d4ed8;
}
QLabel#pageTitle {
    font-size: 20px;
    font-weight: 700;
}
QLabel#sectionTitle {
    font-size: 14px;
    font-weight: 600;
}
QLabel#mutedText {
    color: #687383;
}
QLabel#sourceFilename {
    color: #556274;
    font-size: 12px;
    font-weight: 600;
}
QLineEdit, QPlainTextEdit, QTableWidget {
    background: #ffffff;
    border: 1px solid #cfd8e5;
    border-radius: 5px;
    selection-background-color: #bfdbfe;
    padding: 7px;
}
QPushButton {
    background: #2563eb;
    color: #ffffff;
    border: none;
    border-radius: 5px;
    min-height: 32px;
    padding: 0 14px;
    font-weight: 600;
}
QPushButton:hover {
    background: #1d4ed8;
}
QPushButton:disabled {
    background: #aab5c4;
}
QPushButton#dangerButton {
    background: #b42318;
}
QPushButton#dangerButton:hover {
    background: #912018;
}
QFrame#sourceBox {
    background: #ffffff;
    border: 1px solid #d9e0ea;
    border-radius: 6px;
}
QStatusBar {
    background: #ffffff;
    border-top: 1px solid #d9e0ea;
}
"""


def run_desktop(smoke_test: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION)
    window = MainWindow()
    window.show()
    if smoke_test:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(500, app.quit)
    return app.exec()
