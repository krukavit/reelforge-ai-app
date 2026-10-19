from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent.parent


def test_bootstrap_registry_exists_and_is_valid():
    path = ROOT / "state" / "capability_registry.json"
    assert path.is_file(), f"missing bootstrap registry: {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    tools = data["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names
    assert len(names) == len(set(names))
    assert "session_run" in names


def test_worker_package_does_not_mutate_sys_path():
    paths = list((ROOT / "app").glob("*.py"))
    offenders = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        if "sys.path.insert" in source or "sys.path.append" in source:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"runtime path mutation remains: {offenders}"


def test_installer_does_not_package_or_create_shared_mcp_stack():
    installer = (ROOT / "INSTALL.sh").read_text(encoding="utf-8")
    assert 'cp -a "$SCRIPT_DIR/ops"' not in installer
    assert 'mkdir -p "$TARGET/ops"' not in installer
    assert "shared MCP/Core/Bridge/TNNL stack is external" in installer
