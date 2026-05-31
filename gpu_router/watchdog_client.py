#!/usr/bin/env python3
"""Client-side watchdog for the GPU tunnel + router (modeled on watchdog/watchdog.py).

Cron-driven (every minute via flock). Applies a two-strike rule per component:

  * tunnel : the supervisor PROCESS must be alive (it self-reconnects on drops,
             so we never kill a live supervisor — we only restart a dead one).
  * router : the router process must be alive AND :8010/health must return 200.

On the 2nd consecutive failure it restarts the dead component via the run_*.sh
launcher, and emails a throttled alert. Restarting the tunnel re-triggers a Duo
push (approve on your phone).

Crontab (this box; cron is available here):
  * * * * * /usr/bin/flock -n /db/usr/rickyhan/PanKLLM_implementation/gpu_router/watchdog.lock \
      /path/to/python3 /db/usr/rickyhan/PanKLLM_implementation/gpu_router/watchdog_client.py \
      >> /db/usr/rickyhan/PanKLLM_implementation/gpu_router/logs/watchdog.log 2>&1
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import subprocess
import sys
import time
import urllib.request
from email.message import EmailMessage

import gpu_config as cfg

FAIL_THRESHOLD = int(os.environ.get("WATCHDOG_FAIL_THRESHOLD", "2"))
EMAIL_TO = os.environ.get("WATCHDOG_EMAIL_TO", "rickyhan@umich.edu")
EMAIL_THROTTLE_S = int(os.environ.get("WATCHDOG_EMAIL_THROTTLE", "3600"))

log = logging.getLogger("wd-client")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def _load_state() -> dict:
    try:
        return json.loads(cfg.WATCHDOG_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        cfg.WATCHDOG_STATE_FILE.write_text(json.dumps(state))
    except Exception as exc:
        log.warning("could not persist state: %s", exc)


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
def _pid_alive(pid_file) -> bool:
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _http_ok(url: str, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _tunnel_ok() -> bool:
    return _pid_alive(cfg.TUNNEL_PID_FILE)


def _router_ok() -> bool:
    return _pid_alive(cfg.ROUTER_PID_FILE) and _http_ok(
        f"http://{cfg.ROUTER_HOST}:{cfg.ROUTER_PORT}/health"
    )


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def _restart(component: str) -> None:
    script = cfg.BASE_DIR / (f"run_{component}.sh")
    log.warning("restarting %s via %s", component, script)
    try:
        subprocess.run(["bash", str(script)], cwd=str(cfg.BASE_DIR),
                       check=False, timeout=60)
    except Exception as exc:
        log.error("restart of %s failed: %s", component, exc)


def _maybe_email(state: dict, component: str, detail: str) -> None:
    now = time.time()
    key = f"last_email_{component}"
    if now - state.get(key, 0) < EMAIL_THROTTLE_S:
        return
    msg = EmailMessage()
    msg["Subject"] = f"[gpu_router] {component} restarted"
    msg["From"] = EMAIL_TO
    msg["To"] = EMAIL_TO
    msg.set_content(f"{component} failed {FAIL_THRESHOLD}x and was restarted.\n\n{detail}\n")
    try:
        with smtplib.SMTP("localhost", timeout=10) as s:
            s.send_message(msg)
        state[key] = now
        log.info("alert email sent for %s", component)
    except Exception as exc:
        log.warning("could not send email (%s); continuing", exc)


def _check(state: dict, component: str, ok: bool) -> None:
    fkey = f"fails_{component}"
    if ok:
        if state.get(fkey, 0):
            log.info("%s healthy again", component)
        state[fkey] = 0
        return
    state[fkey] = state.get(fkey, 0) + 1
    log.warning("%s check FAILED (%d/%d)", component, state[fkey], FAIL_THRESHOLD)
    if state[fkey] >= FAIL_THRESHOLD:
        _restart(component)
        _maybe_email(state, component, f"{component} down at {time.ctime()}")
        state[fkey] = 0  # reset; give the restart a cycle to take effect


def main() -> None:
    state = _load_state()
    _check(state, "tunnel", _tunnel_ok())
    _check(state, "router", _router_ok())
    _save_state(state)


if __name__ == "__main__":
    main()
