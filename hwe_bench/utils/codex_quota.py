"""Query Codex rate limits via the app-server JSON-RPC interface.

Lazily starts a ``codex app-server`` subprocess on first call and keeps it
alive for the duration of the process.  Each ``get_quota()`` call opens a
short-lived WebSocket connection, sends ``initialize`` + ``account/rateLimits/read``,
and returns the parsed result.
"""

from __future__ import annotations

import atexit
import json
import logging
import socket
import subprocess
import time
from typing import Any

_server_proc: subprocess.Popen | None = None
_server_port: int | None = None
_logger = logging.getLogger("codex_quota")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_server() -> int:
    global _server_proc, _server_port
    if _server_proc is not None and _server_proc.poll() is None:
        return _server_port  # type: ignore[return-value]

    port = _find_free_port()
    _server_proc = subprocess.Popen(
        ["codex", "app-server", "--listen", f"ws://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for server to be ready
    for _ in range(20):
        time.sleep(0.3)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
    _server_port = port
    _logger.info("codex app-server started on port %d (pid %d)", port, _server_proc.pid)
    return port


def _cleanup() -> None:
    global _server_proc
    if _server_proc is not None:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
        _server_proc = None


atexit.register(_cleanup)

# Thresholds: secondary (7d) is checked first with stricter remaining requirement
_PRIMARY_THRESHOLD = 85    # 5h window: pause if used >= 85%
_SECONDARY_THRESHOLD = 95  # 7d window: pause if used >= 95% (remaining < 5%)


def wait_if_quota_exceeded(limit_id: str = "codex", _max_rechecks: int = 10) -> None:
    """Check quota and sleep until reset if over threshold.

    Secondary (7d) window is checked first with higher priority.
    Re-checks after each wait to ensure both windows are clear.
    """
    for _ in range(_max_rechecks):
        quota = get_quota(limit_id)
        if quota is None:
            return

        blocked = False
        # Check secondary (7d) first — higher priority
        for window, label, threshold in [
            ("secondary", "7d window", _SECONDARY_THRESHOLD),
            ("primary", "5h window", _PRIMARY_THRESHOLD),
        ]:
            used = quota.get(f"{window}_used", 0)
            resets_at = quota.get(f"{window}_resets", 0)
            if not isinstance(used, (int, float)) or not isinstance(resets_at, (int, float)):
                continue
            if used >= threshold:
                wait_seconds = resets_at - time.time() + 5
                if wait_seconds > 0:
                    reset_str = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(resets_at)
                    )
                    print(
                        f"\n[QUOTA] {label} at {used}% used (threshold {threshold}%). "
                        f"Waiting {wait_seconds:.0f}s until reset at {reset_str}..."
                    )
                    time.sleep(wait_seconds)
                blocked = True
                break  # re-check from the top after waiting

        if not blocked:
            return

    print("[QUOTA] WARNING: exhausted recheck limit, proceeding anyway.")


def get_quota(limit_id: str = "codex") -> dict[str, Any] | None:
    """Return rate-limit snapshot for a specific Codex limit bucket.

    Args:
        limit_id: Which quota bucket to read. "codex" for the main models
                  (gpt-5.4 etc.), "codex_bengalfox" for GPT-5.3-Codex-Spark.

    Returns dict with keys: primary_used, primary_resets,
    secondary_used, secondary_resets, plan_type.  None on failure.
    """
    try:
        import websockets.sync.client
    except ImportError:
        _logger.warning("websockets not installed; quota check unavailable")
        return None

    try:
        port = _ensure_server()
        with websockets.sync.client.connect(
            f"ws://127.0.0.1:{port}", close_timeout=5
        ) as ws:
            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"clientInfo": {"name": "quota-probe", "version": "0.1"}},
            }))
            ws.recv()

            ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "account/rateLimits/read", "params": {},
            }))
            resp = json.loads(ws.recv())

        result = resp.get("result", {})
        by_id = result.get("rateLimitsByLimitId") or {}
        rl = by_id.get(limit_id) or result.get("rateLimits") or {}
        primary = rl.get("primary") or {}
        secondary = rl.get("secondary") or {}
        return {
            "primary_used": primary.get("usedPercent", 0),
            "primary_resets": primary.get("resetsAt", 0),
            "secondary_used": secondary.get("usedPercent", 0),
            "secondary_resets": secondary.get("resetsAt", 0),
            "plan_type": rl.get("planType"),
        }
    except Exception as e:
        _logger.warning("quota query failed: %s", e)
        return None
