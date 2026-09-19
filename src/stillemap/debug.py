from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DebugLog:
    def __init__(self, path: Path | None = None, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, level: str, message: str, **data: Any) -> None:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
            **data,
        }
        line = json.dumps(event, default=str, ensure_ascii=False)
        print(line, file=sys.stderr if level in {"ERROR", "WARN"} else sys.stdout, flush=True)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def info(self, message: str, **data: Any) -> None:
        if self.enabled:
            self._write("INFO", message, **data)

    def warn(self, message: str, **data: Any) -> None:
        self._write("WARN", message, **data)

    def error(self, message: str, exc: BaseException | None = None, **data: Any) -> None:
        if exc is not None:
            data["exception"] = repr(exc)
            data["traceback"] = "".join(traceback.format_exception(exc))
        self._write("ERROR", message, **data)
