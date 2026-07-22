"""Encoding-safe console output for Windows desktop entry points."""

from __future__ import annotations

import sys
from typing import TextIO


def _encodable_text(message: str, stream: TextIO) -> str:
    """Escape only characters that the attached legacy stream cannot encode."""

    encoding = getattr(stream, "encoding", None)
    if not isinstance(encoding, str) or not encoding:
        return message
    try:
        message.encode(encoding, errors="strict")
    except LookupError:
        return message.encode("ascii", errors="backslashreplace").decode("ascii")
    except UnicodeEncodeError:
        return message.encode(encoding, errors="backslashreplace").decode(encoding)
    return message


def write_console_message(message: str, *, error: bool = False) -> None:
    """Write one line when stdio exists, including on strict cp1252/GBK pipes."""

    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(_encodable_text(str(message), stream), file=stream)
