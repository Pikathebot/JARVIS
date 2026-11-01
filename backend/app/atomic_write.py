"""Write a file so a crash mid-write leaves the old contents, never half of the new (PLAN 4.29 A3)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Union


def write_text_atomic(path: Union[str, Path], text: str, encoding: str = "utf-8") -> None:
    """Temp file beside *path*, flushed to disk, then swapped in with ``os.replace`` (one step
    on Windows and POSIX). Creates the parent folder. On failure the temp file is removed and
    the original is untouched."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
