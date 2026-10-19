import json
from pathlib import Path
from typing import Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CapabilityRegistry:
    def __init__(self, path=None):
        raw_path = path if path is not None else PROJECT_ROOT / "state" / "capability_registry.json"
        self.path = Path(raw_path).expanduser()
        if not self.path.is_absolute():
            self.path = PROJECT_ROOT / self.path
        self._data = None

    def load(self) -> dict:
        if not self.path.exists():
            raise FileNotFoundError(f"Capability registry not found: {self.path}")

        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        tools = data.get("result", {}).get("tools")
        if not isinstance(tools, list):
            raise ValueError("Invalid capability registry: result.tools must be a list")
        names = [item.get("name") for item in tools if isinstance(item, dict)]
        if any(not name for name in names):
            raise ValueError("Invalid capability registry: every tool must have a name")
        if len(names) != len(set(names)):
            raise ValueError("Invalid capability registry: duplicate tool names")

        self._data = data
        return data

    def names(self) -> List[str]:
        data = self._data if self._data is not None else self.load()
        names = {
            item.get("name")
            for item in data.get("result", {}).get("tools", [])
            if isinstance(item, dict) and item.get("name")
        }
        return sorted(names)

    def has(self, capability: str) -> bool:
        return capability in self.names()

    def get(self, capability: str) -> Optional[dict]:
        data = self._data if self._data is not None else self.load()
        for item in data.get("result", {}).get("tools", []):
            if isinstance(item, dict) and item.get("name") == capability:
                return item
        return None

    def validate(self) -> Dict[str, object]:
        data = self._data if self._data is not None else self.load()
        tools = data.get("result", {}).get("tools", [])
        names = self.names()
        global_blocked = {"run", "session_run"}

        return {
            "tools_count": len(tools),
            "unique_tools_count": len(names),
            "has_run": "run" in names,
            "has_session_run": "session_run" in names,
            "global_blocked": sorted(global_blocked),
            "status": "OK" if names else "CHECK",
        }
