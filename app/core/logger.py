"""
=== 通用模块（可复用到其他项目）===

说明：
- 日志配置是纯基础设施，跟业务没有任何关系
- 复制到新项目直接能用，一行都不用改

使用时：
    在其他文件中 from app.core.logger import logger
    logger.info("...") / logger.error("...") 直接调用
"""
import logging

from app.core.config import settings


def setup_logger(name: str = __name__) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


logger = setup_logger("enterprise_knowledge_agent")
