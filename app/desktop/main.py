from __future__ import annotations

import asyncio
import os
import sys
import time
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
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
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

from app.lite.desktop_query import query_desktop_index
from app.lite.indexer import (
    IndexCancelledError,
    IndexFormatError,
    SUPPORTED_EXTENSIONS,
    delete_index_document,
    list_index_documents,
    sync_index_paths,
)
from app.lite.index_diagnostics import diagnose_index
from app.lite.query_planner import plan_query
from app.lite.remote_retrieval import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RETRIEVAL_BASE_URL,
    set_remote_access,
)
from app.security import (
    DEFAULT_SERVICE,
    get_backend_name,
    get_secret,
    set_secret,
)
from app.documents import (
    CSV_ENCODING_CANDIDATES,
    CsvEncodingError,
)


APP_NAME = "Local Knowledge Tool"
ORGANIZATION = "EnterpriseKnowledgeAgent"
SETTINGS_APP_NAME = "Local Knowledge Tool Desktop 1.0"
SETTINGS_SCHEMA_VERSION = 3
DESKTOP_FILE_FILTER = (
    "Knowledge files ("
    + " ".join(f"*{extension}" for extension in sorted(SUPPORTED_EXTENSIONS))
    + ")"
)


def desktop_index_dir() -> Path:
    override = os.getenv("DESKTOP_INDEX_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return Path("data/lite_index").resolve()
    app_data = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    return Path(app_data).resolve() / "lite_index"


def desktop_temp_dir() -> Path:
    override = os.getenv("DESKTOP_TEMP_DIR")
    if override:
        return Path(override).expanduser().resolve()
    temp_root = QStandardPaths.writableLocation(QStandardPaths.TempLocation)
    return Path(temp_root).resolve() / "EnterpriseKnowledgeAgent"


def cleanup_stale_temp_files() -> None:
    """删除超过 7 天的应用临时残留文件。PDF 页面图片不会落盘。"""
    root = desktop_temp_dir()
    if not root.exists():
        return
    cutoff = time.time() - 7 * 24 * 3600
    for path in root.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def cleanup_temp_files() -> None:
    """正常退出时清理本进程创建的应用临时文件。"""
    root = desktop_temp_dir()
    if not root.exists():
        return
    try:
        for path in root.iterdir():
            try:
                path.unlink()
            except OSError:
                continue
        root.rmdir()
    except OSError:
        pass


class IndexWorker(QThread):
    completed = Signal(dict)
    progress_changed = Signal(dict)
    encoding_required = Signal(str, str)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
        self,
        paths: List[Path],
        index_dir: Path,
        csv_encodings: Dict[str, str] | None = None,
        source_root: Path | None = None,
        force_reparse: bool = False,
        replace_all: bool = False,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.index_dir = index_dir
        self.csv_encodings = dict(csv_encodings or {})
        self.source_root = source_root
        self.force_reparse = force_reparse
        self.replace_all = replace_all

    def run(self) -> None:
        try:
            stats = sync_index_paths(
                self.paths,
                self.index_dir,
                source_root=self.source_root,
                source_label=(
                    self.source_root.as_posix()
                    if self.source_root is not None
                    else "desktop_upload"
                ),
                remove_missing=self.replace_all,
                force_reparse=self.force_reparse,
                csv_encodings=self.csv_encodings,
                progress=self.progress_changed.emit,
                should_cancel=self.isInterruptionRequested,
            )
            self.completed.emit(stats.__dict__)
        except CsvEncodingError as exc:
            failed_path = next(
                (
                    path
                    for path in self.paths
                    if path.name == Path(exc.source_path).name
                ),
                Path(exc.source_path),
            )
            self.encoding_required.emit(str(failed_path.resolve()), str(exc))
        except IndexCancelledError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class DiagnosticWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, index_dir: Path) -> None:
        super().__init__()
        self.index_dir = index_dir

    def run(self) -> None:
        try:
            self.completed.emit(diagnose_index(self.index_dir))
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
        llm_api_key: str,
        llm_base_url: str,
        llm_model: str,
        use_embedding: bool,
        use_reranker: bool,
        retrieval_api_key: str,
        retrieval_base_url: str,
        embedding_model: str,
        reranker_model: str,
        offline: bool = False,
    ) -> None:
        super().__init__()
        self.query = query
        self.index_dir = index_dir
        self.use_llm = use_llm
        self.llm_api_key = llm_api_key
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.use_embedding = use_embedding
        self.use_reranker = use_reranker
        self.retrieval_api_key = retrieval_api_key
        self.retrieval_base_url = retrieval_base_url
        self.embedding_model = embedding_model
        self.reranker_model = reranker_model
        self.offline = offline

    def run(self) -> None:
        try:
            result = asyncio.run(
                query_desktop_index(
                    self.query,
                    self.index_dir,
                    top_k=5,
                    use_llm=self.use_llm,
                    llm_api_key=self.llm_api_key,
                    llm_base_url=self.llm_base_url,
                    llm_model=self.llm_model,
                    use_embedding=self.use_embedding,
                    use_reranker=self.use_reranker,
                    retrieval_api_key=self.retrieval_api_key,
                    retrieval_base_url=self.retrieval_base_url,
                    embedding_model=self.embedding_model,
                    reranker_model=self.reranker_model,
                    offline=self.offline,
                )
            )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.index_dir = desktop_index_dir()
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(ORGANIZATION, SETTINGS_APP_NAME)
        self.settings.setFallbacksEnabled(False)
        cleanup_stale_temp_files()
        self._initialize_release_settings()
        self._workers: List[QThread] = []
        self._active_index_worker: IndexWorker | None = None
        self._settings_loading = False
        self._network_active = False

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
        if current_version < 1:
            self.settings.remove("llm/api_key")
        if current_version < 2:
            self.settings.remove("retrieval/api_key")
        if current_version < 3:
            # 迁移旧明文 API Key：读入系统凭据库后从设置中删除。
            legacy_llm = self.settings.value("llm/api_key", "", str)
            legacy_retrieval = self.settings.value("retrieval/api_key", "", str)
            if legacy_llm:
                set_secret(DEFAULT_SERVICE, "llm_api_key", legacy_llm)
            if legacy_retrieval:
                set_secret(DEFAULT_SERVICE, "retrieval_api_key", legacy_retrieval)
            self.settings.remove("llm/api_key")
            self.settings.remove("retrieval/api_key")
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
        self.use_embedding_checkbox = QCheckBox("使用远程 Embedding")
        actions.addWidget(self.use_embedding_checkbox)
        self.use_reranker_checkbox = QCheckBox("使用远程 Reranker")
        actions.addWidget(self.use_reranker_checkbox)
        actions.addStretch()

        self.ask_button = QPushButton("查询")
        self.ask_button.setIcon(self.style().standardIcon(QStyle.SP_ArrowForward))
        self.ask_button.clicked.connect(self.ask_question)
        actions.addWidget(self.ask_button)
        layout.addLayout(actions)

        self.network_status_label = QLabel("正在连接远程服务…")
        self.network_status_label.setObjectName("networkStatus")
        self.network_status_label.setVisible(False)
        layout.addWidget(self.network_status_label)

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

        self.rebuild_button = QPushButton("从文件夹重建")
        self.rebuild_button.setIcon(
            self.style().standardIcon(QStyle.SP_BrowserReload)
        )
        self.rebuild_button.clicked.connect(self.choose_rebuild_folder)
        actions.addWidget(self.rebuild_button)

        self.diagnose_button = QPushButton("诊断")
        self.diagnose_button.setIcon(
            self.style().standardIcon(QStyle.SP_MessageBoxInformation)
        )
        self.diagnose_button.clicked.connect(self.start_index_diagnosis)
        actions.addWidget(self.diagnose_button)

        actions.addStretch()

        self.delete_button = QPushButton("删除所选")
        self.delete_button.setObjectName("dangerButton")
        self.delete_button.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        self.delete_button.clicked.connect(self.delete_selected_document)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)

        progress_row = QHBoxLayout()
        self.index_progress_label = QLabel("索引任务")
        self.index_progress_label.setObjectName("mutedText")
        self.index_progress_label.setVisible(False)
        progress_row.addWidget(self.index_progress_label)
        self.index_progress = QProgressBar()
        self.index_progress.setRange(0, 100)
        self.index_progress.setValue(0)
        self.index_progress.setTextVisible(True)
        self.index_progress.setVisible(False)
        progress_row.addWidget(self.index_progress, 1)
        self.cancel_index_button = QPushButton("取消")
        self.cancel_index_button.setIcon(
            self.style().standardIcon(QStyle.SP_BrowserStop)
        )
        self.cancel_index_button.setDisabled(True)
        self.cancel_index_button.clicked.connect(self.cancel_indexing)
        progress_row.addWidget(self.cancel_index_button)
        layout.addLayout(progress_row)

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

        heading = QLabel("模型设置")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        llm_heading = QLabel("LLM")
        llm_heading.setObjectName("sectionTitle")
        layout.addWidget(llm_heading)

        llm_form = QFormLayout()
        llm_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("填写你的 LLM API Key")
        llm_form.addRow("API Key", self.api_key_input)

        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("https://api.deepseek.com")
        llm_form.addRow("Base URL", self.base_url_input)

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("deepseek-v4-flash")
        llm_form.addRow("Model", self.model_input)
        layout.addLayout(llm_form)

        retrieval_heading = QLabel("远程检索")
        retrieval_heading.setObjectName("sectionTitle")
        layout.addWidget(retrieval_heading)

        retrieval_form = QFormLayout()
        retrieval_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.retrieval_api_key_input = QLineEdit()
        self.retrieval_api_key_input.setEchoMode(QLineEdit.Password)
        self.retrieval_api_key_input.setPlaceholderText("填写 Embedding / Reranker API Key")
        retrieval_form.addRow("API Key", self.retrieval_api_key_input)

        self.retrieval_base_url_input = QLineEdit()
        self.retrieval_base_url_input.setPlaceholderText(DEFAULT_RETRIEVAL_BASE_URL)
        retrieval_form.addRow("Base URL", self.retrieval_base_url_input)

        self.embedding_model_input = QLineEdit()
        self.embedding_model_input.setPlaceholderText(DEFAULT_EMBEDDING_MODEL)
        retrieval_form.addRow("Embedding Model", self.embedding_model_input)

        self.reranker_model_input = QLineEdit()
        self.reranker_model_input.setPlaceholderText(DEFAULT_RERANKER_MODEL)
        retrieval_form.addRow("Reranker Model", self.reranker_model_input)
        layout.addLayout(retrieval_form)

        actions = QHBoxLayout()
        self.save_feedback = QLabel("已加载保存的设置")
        self.save_feedback.setObjectName("mutedText")
        actions.addWidget(self.save_feedback)
        actions.addStretch()
        self.save_button = QPushButton("设置已保存")
        self.save_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.save_button.setDisabled(True)
        self.save_button.clicked.connect(self.save_settings)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

        privacy_heading = QLabel("隐私与安全")
        privacy_heading.setObjectName("sectionTitle")
        layout.addWidget(privacy_heading)

        self.offline_checkbox = QCheckBox("完全离线模式（禁止一切远程请求）")
        self.offline_checkbox.setChecked(True)
        layout.addWidget(self.offline_checkbox)

        self.offline_note = QLabel(
            "离线模式下不会发起任何远程请求，检索与回答完全在本地完成。"
        )
        self.offline_note.setWordWrap(True)
        self.offline_note.setObjectName("mutedText")
        layout.addWidget(self.offline_note)

        self.credential_backend_label = QLabel(
            f"API Key 安全存储：{get_backend_name()}"
        )
        self.credential_backend_label.setWordWrap(True)
        self.credential_backend_label.setObjectName("mutedText")
        layout.addWidget(self.credential_backend_label)

        note = QLabel(
            "启用远程检索时，只会发送明确展示的问题和检索到的文档片段到配置的服务地址；"
            "API Key 保存在系统凭据库，不会写入明文设置文件。"
        )
        note.setWordWrap(True)
        note.setObjectName("mutedText")
        layout.addWidget(note)
        layout.addStretch()

        for line_edit in (
            self.api_key_input,
            self.base_url_input,
            self.model_input,
            self.retrieval_api_key_input,
            self.retrieval_base_url_input,
            self.embedding_model_input,
            self.reranker_model_input,
        ):
            line_edit.textChanged.connect(self._mark_settings_dirty)
        for checkbox in (
            self.use_llm_checkbox,
            self.use_embedding_checkbox,
            self.use_reranker_checkbox,
            self.offline_checkbox,
        ):
            checkbox.stateChanged.connect(self._mark_settings_dirty)
        self.offline_checkbox.stateChanged.connect(
            lambda *_args: self._apply_offline_state()
        )
        return page

    def _load_settings(self) -> None:
        self._settings_loading = True
        try:
            self.api_key_input.setText(get_secret(DEFAULT_SERVICE, "llm_api_key"))
            self.base_url_input.setText(
                self.settings.value("llm/base_url", "https://api.deepseek.com", str)
            )
            self.model_input.setText(
                self.settings.value("llm/model", "deepseek-v4-flash", str)
            )
            self.retrieval_api_key_input.setText(
                get_secret(DEFAULT_SERVICE, "retrieval_api_key")
            )
            self.retrieval_base_url_input.setText(
                self.settings.value(
                    "retrieval/base_url",
                    DEFAULT_RETRIEVAL_BASE_URL,
                    str,
                )
            )
            self.embedding_model_input.setText(
                self.settings.value(
                    "retrieval/embedding_model",
                    DEFAULT_EMBEDDING_MODEL,
                    str,
                )
            )
            self.reranker_model_input.setText(
                self.settings.value(
                    "retrieval/reranker_model",
                    DEFAULT_RERANKER_MODEL,
                    str,
                )
            )
            self.use_llm_checkbox.setChecked(
                self.settings.value("features/use_llm", True, bool)
            )
            self.use_embedding_checkbox.setChecked(
                self.settings.value("features/use_embedding", False, bool)
            )
            self.use_reranker_checkbox.setChecked(
                self.settings.value("features/use_reranker", False, bool)
            )
            self.offline_checkbox.setChecked(
                self.settings.value("privacy/offline_mode", True, bool)
            )
        finally:
            self._settings_loading = False
        self._apply_offline_state()
        self._set_settings_saved_state("已加载保存的设置")

    def save_settings(self) -> None:
        credential_errors = []
        try:
            set_secret(DEFAULT_SERVICE, "llm_api_key", self.api_key_input.text().strip())
            set_secret(
                DEFAULT_SERVICE,
                "retrieval_api_key",
                self.retrieval_api_key_input.text().strip(),
            )
        except Exception as exc:
            credential_errors.append(str(exc))
        self.settings.setValue("llm/base_url", self.base_url_input.text().strip())
        self.settings.setValue("llm/model", self.model_input.text().strip())
        self.settings.setValue(
            "retrieval/base_url",
            self.retrieval_base_url_input.text().strip(),
        )
        self.settings.setValue(
            "retrieval/embedding_model",
            self.embedding_model_input.text().strip(),
        )
        self.settings.setValue(
            "retrieval/reranker_model",
            self.reranker_model_input.text().strip(),
        )
        self.settings.setValue("features/use_llm", self.use_llm_checkbox.isChecked())
        self.settings.setValue(
            "features/use_embedding",
            self.use_embedding_checkbox.isChecked(),
        )
        self.settings.setValue(
            "features/use_reranker",
            self.use_reranker_checkbox.isChecked(),
        )
        self.settings.setValue("privacy/offline_mode", self.offline_checkbox.isChecked())
        self.settings.sync()
        self._apply_offline_state()
        if credential_errors:
            QMessageBox.warning(
                self,
                "凭据保存失败",
                "API Key 未能写入系统凭据库，请检查系统凭据服务后重试：\n"
                + "\n".join(credential_errors),
            )
        self._set_settings_saved_state("设置已保存到当前系统用户")
        self.statusBar().showMessage("模型设置已保存", 4000)

    def _mark_settings_dirty(self, *_args) -> None:
        if self._settings_loading:
            return
        self.save_button.setText("保存设置")
        self.save_button.setEnabled(True)
        self.save_feedback.setText("有未保存的更改")
        self.save_feedback.setObjectName("warningText")
        self.save_feedback.style().unpolish(self.save_feedback)
        self.save_feedback.style().polish(self.save_feedback)

    def _set_settings_saved_state(self, message: str) -> None:
        self.save_button.setText("设置已保存")
        self.save_button.setDisabled(True)
        self.save_feedback.setText(message)
        self.save_feedback.setObjectName("savedText")
        self.save_feedback.style().unpolish(self.save_feedback)
        self.save_feedback.style().polish(self.save_feedback)

    def _apply_offline_state(self) -> None:
        offline = self.offline_checkbox.isChecked()
        set_remote_access(not offline)
        for checkbox in (
            self.use_llm_checkbox,
            self.use_embedding_checkbox,
            self.use_reranker_checkbox,
        ):
            checkbox.setEnabled(not offline)
        self.offline_note.setVisible(offline)
        if self._settings_loading:
            return
        if offline:
            self.network_status_label.setVisible(False)
            self._network_active = False

    def _set_network_indicator(self, active: bool) -> None:
        self._network_active = bool(active)
        self.network_status_label.setVisible(active)

    def _consented_endpoints(self) -> set[str]:
        raw = str(self.settings.value("privacy/remote_consent", "", str) or "")
        return {value for value in raw.split(",") if value}

    def _confirm_remote_consent(
        self,
        query: str,
        use_llm: bool,
        use_embedding: bool,
        use_reranker: bool,
    ) -> bool:
        endpoints: list[tuple[str, str]] = []
        llm_base = self.base_url_input.text().strip()
        if use_llm and llm_base:
            endpoints.append(("LLM", llm_base))
        retrieval_base = self.retrieval_base_url_input.text().strip()
        if (use_embedding or use_reranker) and retrieval_base:
            endpoints.append(("Embedding / Reranker", retrieval_base))
        if not endpoints:
            return True

        consented = self._consented_endpoints()
        pending = [(name, url) for name, url in endpoints if url not in consented]
        if not pending:
            return True

        endpoint_text = "\n".join(f"• {name}：{url}" for name, url in pending)
        message = (
            "即将发起远程调用，将把以下数据发送到配置的服务：\n\n"
            f"问题内容：{query[:200]}\n\n"
            "发送范围：问题文本，以及本地检索到的文档片段、文件名和 "
            "Sheet/页面定位元数据。\n\n"
            "目标服务：\n"
            f"{endpoint_text}\n\n"
            "请确认允许向上述服务发送这些数据。"
        )
        box = QMessageBox(self)
        box.setWindowTitle("确认远程调用")
        box.setIcon(QMessageBox.Warning)
        box.setText(message)
        remember = box.addButton("同意并记住", QMessageBox.AcceptRole)
        once = box.addButton("仅本次同意", QMessageBox.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel:
            return False
        if clicked is remember:
            merged = consented | {url for _, url in pending}
            self.settings.setValue(
                "privacy/remote_consent",
                ",".join(sorted(merged)),
            )
            self.settings.sync()
        return True

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择知识文档",
            "",
            DESKTOP_FILE_FILTER,
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
            QMessageBox.information(
                self,
                "没有文档",
                "所选文件夹中没有 PDF、TXT、MD、CSV 或 XLSX 文件。",
            )
            return
        self.start_indexing(paths, source_root=Path(folder))

    def choose_rebuild_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择完整知识库文件夹",
        )
        if not folder:
            return
        root = Path(folder)
        paths = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not paths:
            QMessageBox.information(
                self,
                "没有文档",
                "所选文件夹中没有可重建的知识文档。",
            )
            return
        answer = QMessageBox.question(
            self,
            "确认重建",
            "重建成功后，当前知识库将完全替换为所选文件夹中的文档。继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.start_indexing(
                paths,
                source_root=root,
                force_reparse=True,
                replace_all=True,
            )

    def start_indexing(
        self,
        paths: List[Path],
        csv_encodings: Dict[str, str] | None = None,
        *,
        source_root: Path | None = None,
        force_reparse: bool = False,
        replace_all: bool = False,
    ) -> None:
        self._set_busy(True, "正在读取并构建索引...")
        self._set_indexing_state(True)
        resolved_encodings = dict(csv_encodings or {})
        worker = IndexWorker(
            paths,
            self.index_dir,
            resolved_encodings,
            source_root=source_root,
            force_reparse=force_reparse,
            replace_all=replace_all,
        )
        self._active_index_worker = worker
        worker.completed.connect(self._indexing_completed)
        worker.progress_changed.connect(self._indexing_progress_changed)
        worker.encoding_required.connect(
            lambda failed_path, message: self._choose_csv_encoding(
                paths,
                resolved_encodings,
                failed_path,
                message,
                source_root=source_root,
                force_reparse=force_reparse,
                replace_all=replace_all,
            )
        )
        worker.cancelled.connect(self._indexing_cancelled)
        worker.failed.connect(self._indexing_failed)
        worker.finished.connect(lambda: self._release_worker(worker))
        self._workers.append(worker)
        worker.start()

    def _choose_csv_encoding(
        self,
        paths: List[Path],
        csv_encodings: Dict[str, str],
        failed_path: str,
        message: str,
        *,
        source_root: Path | None = None,
        force_reparse: bool = False,
        replace_all: bool = False,
    ) -> None:
        self._set_busy(False)
        self._set_indexing_state(False)
        choices = list(CSV_ENCODING_CANDIDATES) + ["latin-1"]
        encoding, accepted = QInputDialog.getItem(
            self,
            "选择 CSV 编码",
            f"{Path(failed_path).name} 无法自动确认编码。\n{message}\n请选择编码后重试：",
            choices,
            0,
            True,
        )
        if not accepted or not encoding.strip():
            self.statusBar().showMessage("已取消 CSV 编码选择", 7000)
            return
        retry_encodings = dict(csv_encodings)
        retry_encodings[str(Path(failed_path).resolve())] = encoding.strip()
        self.start_indexing(
            paths,
            retry_encodings,
            source_root=source_root,
            force_reparse=force_reparse,
            replace_all=replace_all,
        )

    def _indexing_completed(self, payload: Dict[str, Any]) -> None:
        self._set_indexing_state(False)
        self._set_busy(False)
        self.refresh_documents()
        added = int(payload.get("added_count") or 0)
        updated = int(payload.get("updated_count") or 0)
        removed = int(payload.get("removed_count") or 0)
        skipped = payload.get("skipped_files") or []
        failed = payload.get("failed_files") or []
        parts = [f"新增 {added}", f"更新 {updated}", f"删除 {removed}"]
        if skipped:
            parts.append(f"未变化 {len(skipped)}")
        if failed:
            parts.append(f"失败 {len(failed)}")
            details = "\n".join(
                f"{item.get('filename')}: {item.get('error')}"
                for item in failed
            )
            QMessageBox.warning(
                self,
                "部分文档未更新",
                "其他文档已提交，以下文档保留旧版本或未加入：\n" + details,
            )
        message = "，".join(parts)
        self.statusBar().showMessage(message, 7000)

    def _indexing_progress_changed(self, payload: Dict[str, Any]) -> None:
        current = int(payload.get("current") or 0)
        total = max(int(payload.get("total") or 0), 1)
        phase = str(payload.get("phase") or "")
        filename = str(payload.get("filename") or "")
        labels = {
            "fingerprint": "检查",
            "parsing": "解析",
            "parsed": "已解析",
            "skipped": "未变化",
            "failed": "失败",
            "committing": "提交",
            "completed": "完成",
        }
        self.index_progress.setValue(min(int(current * 100 / total), 100))
        text = labels.get(phase, "索引")
        self.index_progress_label.setText(
            f"{text}：{filename}" if filename else text
        )

    def cancel_indexing(self) -> None:
        worker = self._active_index_worker
        if worker is None or not worker.isRunning():
            return
        worker.requestInterruption()
        self.cancel_index_button.setDisabled(True)
        self.index_progress_label.setText("正在取消...")
        self.statusBar().showMessage("正在取消索引任务")

    def _indexing_cancelled(self) -> None:
        self._set_indexing_state(False)
        self._set_busy(False)
        self.statusBar().showMessage("索引任务已取消，原索引保持不变", 7000)

    def _indexing_failed(self, message: str) -> None:
        self._set_indexing_state(False)
        self._set_busy(False)
        QMessageBox.critical(self, "索引失败", message)
        self.statusBar().showMessage("索引失败，原索引保持不变", 7000)

    def start_index_diagnosis(self) -> None:
        self._set_busy(True, "正在诊断索引...")
        worker = DiagnosticWorker(self.index_dir)
        worker.completed.connect(self._diagnosis_completed)
        worker.failed.connect(self._task_failed)
        worker.finished.connect(lambda: self._release_worker(worker))
        self._workers.append(worker)
        worker.start()

    def _diagnosis_completed(self, payload: Dict[str, Any]) -> None:
        self._set_busy(False)
        counts = payload.get("counts") or {}
        issues = payload.get("issues") or []
        warnings = payload.get("warnings") or []
        lines = [
            f"状态：{payload.get('status')}",
            (
                "文档 {documents}，节点 {nodes}，父节点 {parents}，Chunk {chunks}"
            ).format(**counts),
        ]
        if payload.get("recovered_transaction"):
            lines.append("已自动恢复上次未完成的索引事务。")
        lines.extend(f"错误：{item.get('message')}" for item in issues)
        lines.extend(f"警告：{item.get('message')}" for item in warnings)
        QMessageBox.information(self, "索引诊断", "\n".join(lines))

    def refresh_documents(self) -> None:
        index_error = ""
        try:
            documents = list_index_documents(self.index_dir)
        except IndexFormatError as exc:
            documents = []
            index_error = str(exc)
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
        label = f"索引位置：{self.index_dir}"
        if index_error:
            label += f"\n索引需要重建：{index_error}"
            self.statusBar().showMessage("旧索引不兼容，请重建索引")
        self.index_path_label.setText(label)
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

        offline = self.offline_checkbox.isChecked()
        use_llm = self.use_llm_checkbox.isChecked()
        use_embedding = self.use_embedding_checkbox.isChecked()
        use_reranker = self.use_reranker_checkbox.isChecked()
        try:
            planning_documents = list_index_documents(self.index_dir)
        except IndexFormatError:
            planning_documents = []
        query_plan = plan_query(query, planning_documents)
        if query_plan.is_structured_inventory:
            remote_use_llm = False
            remote_use_embedding = False
            remote_use_reranker = False
        elif query_plan.is_summary:
            remote_use_llm = use_llm
            remote_use_embedding = False
            remote_use_reranker = False
        else:
            remote_use_llm = use_llm
            remote_use_embedding = use_embedding
            remote_use_reranker = use_reranker
        remote_requested = (
            remote_use_llm
            or remote_use_embedding
            or remote_use_reranker
        ) and not offline
        if remote_requested and not self._confirm_remote_consent(
            query,
            remote_use_llm,
            remote_use_embedding,
            remote_use_reranker,
        ):
            return

        self.save_settings()
        self._set_busy(True, "正在检索并生成答案...")
        self.answer_output.setPlainText("正在查询...")
        self.clear_sources()
        self._set_network_indicator(remote_requested)

        worker = QueryWorker(
            query=query,
            index_dir=self.index_dir,
            use_llm=use_llm,
            llm_api_key=self.api_key_input.text().strip(),
            llm_base_url=self.base_url_input.text().strip(),
            llm_model=self.model_input.text().strip(),
            use_embedding=use_embedding,
            use_reranker=use_reranker,
            retrieval_api_key=self.retrieval_api_key_input.text().strip(),
            retrieval_base_url=self.retrieval_base_url_input.text().strip(),
            embedding_model=self.embedding_model_input.text().strip(),
            reranker_model=self.reranker_model_input.text().strip(),
            offline=offline,
        )
        worker.completed.connect(self._query_completed)
        worker.failed.connect(self._task_failed)
        worker.finished.connect(lambda: self._release_worker(worker))
        self._workers.append(worker)
        worker.start()

    def _query_completed(self, payload: Dict[str, Any]) -> None:
        self._set_busy(False)
        self._set_network_indicator(False)
        self.answer_output.setPlainText(str(payload.get("answer") or "没有返回答案。"))
        sources = payload.get("sources") or []
        self.render_sources(sources)
        mode = str(payload.get("mode") or "")
        retrieval = payload.get("retrieval") or {}
        if retrieval.get("offline"):
            self.statusBar().showMessage("完全离线模式，本地回答完成", 7000)
        elif mode == "llm_error":
            self.statusBar().showMessage("LLM 配置或请求失败", 7000)
        elif mode == "embedding_error":
            self.statusBar().showMessage("Embedding 配置或请求失败", 7000)
        elif mode == "reranker_error":
            self.statusBar().showMessage("Reranker 配置或请求失败", 7000)
        elif mode == "llm":
            self.statusBar().showMessage("LLM 回答完成", 5000)
        elif retrieval.get("remote"):
            self.statusBar().showMessage("远程检索完成", 5000)
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
            # 表格优先展示延迟生成的展示文本（Markdown 表格），否则用检索片段。
            content.setPlainText(
                str(source.get("display_content") or source.get("content") or "")
            )
            content.setMaximumHeight(125)
            content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            box_layout.addWidget(content)
            self.sources_layout.insertWidget(self.sources_layout.count() - 1, box)

    def _task_failed(self, message: str) -> None:
        self._set_busy(False)
        self._set_network_indicator(False)
        self.answer_output.setPlainText(f"操作失败：{message}")
        self.clear_sources()
        self.statusBar().showMessage("操作失败", 7000)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.ask_button.setDisabled(busy)
        self.add_files_button.setDisabled(busy)
        self.add_folder_button.setDisabled(busy)
        self.rebuild_button.setDisabled(busy)
        self.diagnose_button.setDisabled(busy)
        self.delete_button.setDisabled(busy)
        if message:
            self.statusBar().showMessage(message)
        if not busy:
            self.refresh_documents()

    def _set_indexing_state(self, active: bool) -> None:
        self.index_progress_label.setVisible(active)
        self.index_progress.setVisible(active)
        self.cancel_index_button.setEnabled(active)
        if active:
            self.index_progress.setValue(0)
            self.index_progress_label.setText("准备索引...")
        else:
            self.cancel_index_button.setDisabled(True)

    def _release_worker(self, worker: QThread) -> None:
        if worker is self._active_index_worker:
            self._active_index_worker = None
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
        cleanup_temp_files()


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
QLabel#savedText {
    color: #067647;
}
QLabel#warningText {
    color: #a15c00;
}
QLabel#sourceFilename {
    color: #556274;
    font-size: 12px;
    font-weight: 600;
}
QLabel#networkStatus {
    color: #b45309;
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
