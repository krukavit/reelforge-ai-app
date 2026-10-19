import hashlib
import json
import os
import tempfile
from pathlib import Path


class CapabilityInventory:
    VERSION = 1
    SOURCE = "live_tools_list"

    def build_snapshot(self, tools, proven_correct=None):
        if not isinstance(tools, list):
            raise ValueError("live tools must be a list")

        seen = set()
        normalized = {}
        proven = set(proven_correct or set())

        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("live tool must be an object")

            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("every live tool must have a name")

            if name in seen:
                raise ValueError(f"duplicate live tool: {name}")
            seen.add(name)

            normalized_tool = dict(tool)
            status = "PROVEN_CORRECT" if name in proven else "AVAILABLE"

            normalized[name] = {
                "status": status,
                "proven_correct": name in proven,
                "definition": normalized_tool,
            }

        canonical = {
            "version": self.VERSION,
            "source": self.SOURCE,
            "tools": normalized,
        }

        payload = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        fingerprint = hashlib.sha256(payload).hexdigest()

        return {
            **canonical,
            "tools_count": len(normalized),
            "fingerprint": fingerprint,
        }

    def reconcile(self, client, proven_correct=None):
        response = client.tools_list()

        if not isinstance(response, dict):
            raise ValueError("invalid tools/list response")

        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("invalid tools/list response: result must be an object")

        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ValueError(
                "invalid tools/list response: result.tools must be a list"
            )

        return self.build_snapshot(
            tools,
            proven_correct=proven_correct,
        )

    def save_atomic(self, path, snapshot):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
