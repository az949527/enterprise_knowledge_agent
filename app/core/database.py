"""
=== 通用模块（可复用到其他项目）===

说明：
- 创建异步引擎、会话工厂、声明式基类 → 每个项目完全一样
- get_db 依赖注入 → 每个项目完全一样
- init_db 的模型导入部分 → 每次需要换成项目实际的模型

使用时：
1. 第 28-30 行的 import 换成你自己项目的 Model
2. 其他一行都不用改
"""
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """确保 SQLite 数据库父目录存在（测试/开发/打包环境通用，避免目录缺失报错）。"""
    if not database_url.startswith("sqlite"):
        return
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///", "sqlite://"):
        if database_url.startswith(prefix):
            raw = database_url[len(prefix):]
            if raw:
                try:
                    Path(raw).expanduser().resolve().parent.mkdir(
                        parents=True, exist_ok=True
                    )
                except OSError:
                    pass
            return


_ensure_sqlite_parent_dir(settings.DATABASE_URL)

# SQLite WAL 模式 + busy_timeout 解决并发写锁问题
# WAL 模式允许读操作不阻塞写操作
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"timeout": 30, "check_same_thread": False},
    poolclass=NullPool,
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """SQLite 连接后设置 WAL 模式和忙等待超时"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")  # 30 秒
    cursor.close()


async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    async with engine.begin() as conn:
        from app.models.document import Document  # noqa: F401
        from app.models.chunk import Chunk  # noqa: F401
        from app.models.conversation import Conversation  # noqa: F401
        from app.models.message import Message  # noqa: F401
        from app.models.conversation_summary import ConversationSummary  # noqa: F401
        from app.models.retrieval_cache import RetrievalCache  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
