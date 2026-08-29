"""Market-date helpers for mainland-China financial evidence.

All publication and evidence cut-offs use the Shanghai civil date.  Keeping
this in one module avoids a runner's local timezone changing whether a source
is considered future-dated.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def shanghai_today() -> date:
    return shanghai_now().date()


__all__ = ["SHANGHAI_TZ", "shanghai_now", "shanghai_today"]
