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
    assert "shared MCP/Core/Bridge/TNNL stack remains external to this package" in installer


def test_public_mcp_template_is_safe():
    path = ROOT / "config" / "mcp.example.txt"
    assert path.is_file(), f"missing public MCP template: {path}"

    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    assert {
        "MCP_URL",
        "MCP_PROTOCOL",
        "MCP_TOKEN_FILE",
        "MCP_SESSION_REQUIRED",
        "MCP_ACCEPT",
        "MCP_TIMEOUT",
    } <= values.keys()
    assert "Bearer " not in path.read_text(encoding="utf-8")
    assert "api_key=" not in path.read_text(encoding="utf-8").lower()


def test_installer_preserves_existing_mcp_config():
    installer = (ROOT / "INSTALL.sh").read_text(encoding="utf-8")
    assert 'cp -p "$TARGET/config/mcp.txt" "$PRESERVED_MCP_CONFIG"' in installer
    assert 'cp -p "$PRESERVED_MCP_CONFIG" "$TARGET/config/mcp.txt"' in installer
    assert 'cp -p "$TARGET/config/mcp.example.txt" "$TARGET/config/mcp.txt"' in installer
    assert "cleanup_preserved_mcp_config()" in installer
    assert "trap cleanup_preserved_mcp_config EXIT" in installer
    assert 'rm -f -- "$PRESERVED_MCP_CONFIG"' in installer
