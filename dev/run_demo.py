"""Run the whole connector locally with sample data and no ERP account, then test it end to end.

Starts three local services on 127.0.0.1:
  5055  mock login service (stands in for the ERP login; user admin@demo.test / demo123)
  8820  Data API on the sample data in dev/fixtures
  8810  MCP connector (OAuth + tools)
then runs dev/smoke_test.py and stops everything.

    python dev/run_demo.py           # start, test, stop (exit code 0 = all checks passed)
    python dev/run_demo.py --keep    # start and keep running, e.g. to try it from Claude Code

Development only: the mock login and the sample data are not for production.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEEP = "--keep" in sys.argv
PY = sys.executable
PORTS = {"mock": 5055, "data": 8820, "mcp": 8810}


def wait_ok(url: str, name: str, proc: subprocess.Popen, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"{name} exited early (code {proc.returncode}); see the output above.")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status in (200, 405):
                    return
        except Exception:
            time.sleep(0.5)
    sys.exit(f"{name} did not start within {seconds}s ({url}).")


def main() -> int:
    from cryptography.fernet import Fernet

    tmp = Path(tempfile.mkdtemp(prefix="erp-demo-"))
    instances = tmp / "instances.json"
    instances.write_text('{"instances":{"DEMO-INSTANCE-1":{"name":"zOrder Testing",'
                         '"connection_string":"fixture","tokens":["demo-token"]}}}', encoding="ascii")
    base = f"http://127.0.0.1:{PORTS['mcp']}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DATA_API_", "ERP_MCP_"))}
    env.update({
        # Data API: sample data + dev resolver (both refuse to run without ALLOW_DEV_MODES)
        "DATA_API_ALLOW_DEV_MODES": "true",
        "DATA_API_DATA_SOURCE": "fixture",
        "DATA_API_FIXTURE_DIR": str(ROOT / "dev" / "fixtures"),
        "DATA_API_RESOLVER": "static",
        "DATA_API_INSTANCES_FILE": str(instances),
        # Connector
        "ERP_MCP_PUBLIC_BASE_URL": base,
        "ERP_MCP_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "ERP_MCP_APIFABRIC_URL": f"http://127.0.0.1:{PORTS['mock']}",
        "ERP_MCP_DATA_API_URL": f"http://127.0.0.1:{PORTS['data']}",
        "ERP_MCP_DB_PATH": str(tmp / "connector.sqlite3"),
        "ERP_MCP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ERP_MCP_ALLOW_LOCALHOST_REDIRECTS": "true" if KEEP else "false",
        "ERP_MCP_LOG_LEVEL": "WARNING",
        "PYTHONDONTWRITEBYTECODE": "1",
    })

    def start(name: str, cwd: Path, app: str, port: int) -> subprocess.Popen:
        return subprocess.Popen([PY, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port),
                                 "--log-level", "warning"], cwd=cwd, env=env)

    procs = [
        ("mock login", start("mock", ROOT / "dev", "mock_apifabric:app", PORTS["mock"]),
         f"http://127.0.0.1:{PORTS['mock']}/docs"),
        ("Data API", start("data", ROOT / "data_api", "app.main:app", PORTS["data"]),
         f"http://127.0.0.1:{PORTS['data']}/health"),
        ("MCP connector", start("mcp", ROOT / "mcp_connector", "app.main:app", PORTS["mcp"]),
         f"{base}/health"),
    ]
    try:
        for name, proc, url in procs:
            wait_ok(url, name, proc)
            print(f"started  {name}  ({url.rsplit('/', 1)[0]})")
        if KEEP:
            print(f"\nConnector running at {base}/mcp  (sign in: admin@demo.test / demo123)")
            print(f"Claude Code:  claude mcp add --transport http zorder-erp-demo {base}/mcp")
            print("Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
        print()
        return subprocess.call([PY, str(ROOT / "dev" / "smoke_test.py"), base], env=env)
    except KeyboardInterrupt:
        return 0
    finally:
        for _, proc, _ in procs:
            proc.terminate()
        for _, proc, _ in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
