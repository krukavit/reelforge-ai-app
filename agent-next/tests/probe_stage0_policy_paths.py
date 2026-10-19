import os
import uuid

import os, uuid


from app.executor import TaskExecutor
from app.policy import authorize


def check(capability, operation=None, validated=False, confirmed=False):
    allowed, reason = authorize(capability, operation, validated=validated, confirmed=confirmed)
    print(capability, operation, validated, confirmed, "=>", allowed, reason)
    return allowed, reason

assert check("run") == (False, "ALWAYS_BLOCKED")
assert check("delete") == (False, "CONFIRM_REQUIRED")
assert check("backup") == (False, "VALIDATION_REQUIRED")
assert check("backup", validated=True) == (True, "VALIDATED")
assert check("health") == (True, "AUTO")

# The real MCP dispatch is intentionally opt-in because it requires the live Bridge/Core.
if os.environ.get("AGENT_NEXT_LIVE") == "1":
    ex = TaskExecutor()
    tid = "stage0-live-" + uuid.uuid4().hex[:8]
    print(ex.execute(tid, "health"))
else:
    print("STAGE0_OFFLINE_OK (set AGENT_NEXT_LIVE=1 for live Bridge/Core dispatch)")
