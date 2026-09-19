from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .debug import DebugLog


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:40] or "run"


@dataclass(slots=True)
class RunContext:
    root: Path
    log: DebugLog

    @classmethod
    def create(cls, runs_dir: Path, label: str, debug: bool = True) -> "RunContext":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root = runs_dir / f"{stamp}_{_slug(label)}"
        root.mkdir(parents=True, exist_ok=False)
        return cls(root=root, log=DebugLog(root / "debug.log", enabled=debug))

    def stage_dir(self, ordinal: int, name: str) -> Path:
        path = self.root / f"{ordinal:02d}_{name}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def dump_json(self, path: Path, data: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        self.log.info("artifact_written", path=str(path))
        return path

    def dump_text(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.log.info("artifact_written", path=str(path))
        return path
