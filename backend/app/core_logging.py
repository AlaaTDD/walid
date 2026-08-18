"""
إعداد logging مركزي للمشروع كاملاً -- مؤقت لتشخيص مصدر البطء أثناء رفع
الصور وحساب المقاسات (راجع task memory).

لا يلمس أي منطق حسابي أو هندسي موجود -- وظيفته الوحيدة: قياس الزمن
وطباعته بصيغة موحدة وقابلة للقراءة.

الاستخدام:
    from app.core_logging import get_logger, log_duration

    logger = get_logger(__name__)

    with log_duration(logger, "upload single image", part_id=part_id):
        ...

    أو يدوياً:
        t0 = time.perf_counter()
        ...
        logger.info("upload single image done part_id=%s elapsed_ms=%.1f", part_id, (time.perf_counter() - t0) * 1000)

مستوى الـ log الافتراضي INFO -- يظهر في التيرمينال اللي shغل uvicorn
بدون أي إعداد إضافي مطلوب من المستخدم.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

_CONFIGURED = False


def _configure_root_logger_once() -> None:
    """يضبط شكل الـ log مرة واحدة فقط لكل المشروع، بغض النظر عن عدد مرات
    استدعاء get_logger (يتجنب رسائل مكررة لو uvicorn أعاد تحميل الموديول).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """يرجع logger مضبوط بنفس الاسم (عادة __name__ الملف المستدعي)."""
    _configure_root_logger_once()
    return logging.getLogger(name)


@contextmanager
def log_duration(logger: logging.Logger, label: str, **fields: object) -> Iterator[None]:
    """يقيس الزمن المستغرق لتنفيذ الـ block الداخلي ويطبعه كـ INFO log عند الانتهاء،
    سواء نجح التنفيذ أو فشل بـ exception.

    Args:
        logger: الـ logger المطلوب الكتابة عليه (من get_logger).
        label: وصف قصير للعملية المقاسة (مثلاً "upload single image").
        **fields: أي حقول إضافية مفيدة للسياق (مثلاً part_id="abc123") تظهر في
            السطر النهائي.
    """
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    prefix = f"{label} {extra}".strip()

    t0 = time.perf_counter()
    try:
        yield
    except Exception:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.exception("%s FAILED elapsed_ms=%.1f", prefix, elapsed_ms)
        raise
    else:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info("%s done elapsed_ms=%.1f", prefix, elapsed_ms)
