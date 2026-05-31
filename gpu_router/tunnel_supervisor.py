#!/usr/bin/env python3
"""Persistent, auto-reconnecting SSH tunnel to the ARC vLLM GPU fleet.

Opens ONE ssh connection through ``lighthouse.arc-ts.umich.edu`` holding all the
``-L`` forwards (so a single Duo approval brings up every tunnel), authenticates
with the password from ``.env`` + a Duo push, then blocks until the connection
dies and reconnects with backoff.

Auth flow (keyboard-interactive):
    1. "password:"            -> send SSH_PASSWORD
    2. "Passcode or option:"  -> send DUO_OPTION ("1" = push) -> approve on phone
    3. ssh -N goes quiet      -> we confirm the local ports answer /health

Because we use password+Duo (no SSH keys), every *reconnect* needs a fresh phone
tap. Keepalives (ServerAliveInterval/CountMax) keep the single connection alive
for long stretches, so taps are rare in practice.

Run via run_tunnel.sh (nohup) or directly:  python3 tunnel_supervisor.py
Stop with SIGTERM/SIGINT (run_tunnel.sh writes tunnel.pid for this).
"""
from __future__ import annotations

import logging
import os
import re
import signal
import socket
import sys
import time

import gpu_config as cfg

try:
    import pexpect
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: pexpect is not installed. Install with:\n"
        "    pip install pexpect\n"
    )
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
log = logging.getLogger("tunnel")


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(cfg.LOG_DIR / "tunnel.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]


_PASSWORD_RE = re.compile(rb"(?i)password:")
_DUO_RE = re.compile(rb"(?i)(passcode or option|duo two-factor|enter a passcode|second factor)")
_DENIED_RE = re.compile(rb"(?i)(permission denied|authentication failed)")


def _build_ssh_command() -> list[str]:
    cmd = ["ssh", *cfg.SSH_OPTIONS]
    for fwd in cfg.TUNNELS:
        cmd += fwd.ssh_flag()
    cmd.append(f"{cfg.SSH_USER}@{cfg.LIGHTHOUSE_HOST}")
    return cmd


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _all_local_ports_up() -> bool:
    return all(_port_open(f.local_port) for f in cfg.TUNNELS)


def _local_ports_status() -> str:
    return " ".join(
        f"{f.local_port}={'up' if _port_open(f.local_port) else 'down'}"
        for f in cfg.TUNNELS
    )


class TunnelSupervisor:
    def __init__(self) -> None:
        self.child: "pexpect.spawn | None" = None
        self._stop = False

    # -- lifecycle ------------------------------------------------------- #
    def request_stop(self, *_a) -> None:
        log.info("stop requested; shutting down tunnel")
        self._stop = True
        self._kill_child()

    def _kill_child(self) -> None:
        if self.child is not None and self.child.isalive():
            try:
                self.child.terminate(force=True)
            except Exception:
                pass

    # -- one connection attempt ----------------------------------------- #
    def _connect_once(self) -> bool:
        """Spawn ssh, authenticate. Returns True if the tunnel came up."""
        cmd = _build_ssh_command()
        log.info("connecting: ssh %s ... %s@%s (%d forwards)",
                 "-N", cfg.SSH_USER, cfg.LIGHTHOUSE_HOST, len(cfg.TUNNELS))
        self.child = pexpect.spawn(cmd[0], cmd[1:], timeout=60, encoding=None)

        # Step 1: password
        idx = self.child.expect([_PASSWORD_RE, _DENIED_RE, pexpect.EOF, pexpect.TIMEOUT])
        if idx == 0:
            if not cfg.SSH_PASSWORD:
                log.error("SSH_PASSWORD is empty in .env; cannot authenticate")
                self._kill_child()
                return False
            self.child.sendline(cfg.SSH_PASSWORD)
        elif idx == 1:
            log.error("authentication failed at password stage")
            return False
        else:
            log.error("did not get a password prompt (idx=%s); ssh output: %r",
                      idx, self._tail())
            return False

        # Step 2: Duo
        idx = self.child.expect([_DUO_RE, _DENIED_RE, pexpect.EOF, pexpect.TIMEOUT])
        if idx == 0:
            log.info("Duo prompt -> sending option %s (approve the push on your phone)",
                     cfg.DUO_OPTION)
            self.child.sendline(cfg.DUO_OPTION)
        elif idx == 1:
            log.error("authentication failed (wrong password?)")
            return False
        elif idx == 2:
            log.error("connection closed before Duo prompt; ssh output: %r", self._tail())
            return False
        else:
            # No Duo prompt within timeout. Some configs auto-push; fall through
            # and let the port-poll below decide.
            log.warning("no explicit Duo prompt seen; will poll for tunnel anyway")

        # Step 3: ssh -N goes silent once connected. Poll the local ports.
        deadline = time.time() + cfg.TUNNEL_CONNECT_GRACE
        while time.time() < deadline:
            if not self.child.isalive():
                log.error("ssh exited during connect; output: %r", self._tail())
                return False
            if _all_local_ports_up():
                log.info("tunnel up: %s", _local_ports_status())
                return True
            time.sleep(2)

        log.error("tunnel did not come up within %ds (status: %s)",
                  cfg.TUNNEL_CONNECT_GRACE, _local_ports_status())
        self._kill_child()
        return False

    def _tail(self, n: int = 400) -> str:
        try:
            buf = (self.child.before or b"") + (self.child.after or b"") \
                if self.child else b""
            if isinstance(buf, bytes):
                buf = buf.decode("utf-8", "replace")
            return buf[-n:]
        except Exception:
            return "<unavailable>"

    # -- monitor a live connection -------------------------------------- #
    def _monitor(self) -> None:
        """Block while the connection is healthy; return when it drops."""
        assert self.child is not None
        while not self._stop and self.child.isalive():
            if not _all_local_ports_up():
                log.warning("a forwarded port went down (%s); recycling connection",
                            _local_ports_status())
                self._kill_child()
                return
            # Drain any ssh chatter so the buffer doesn't fill; tolerate silence.
            try:
                self.child.expect([pexpect.TIMEOUT, pexpect.EOF], timeout=15)
            except Exception:
                pass
        log.info("connection ended (alive=%s, stop=%s)",
                 self.child.isalive() if self.child else False, self._stop)

    # -- main loop ------------------------------------------------------- #
    def run(self) -> None:
        backoff = cfg.TUNNEL_BACKOFF_BASE
        while not self._stop:
            ok = self._connect_once()
            if ok:
                backoff = cfg.TUNNEL_BACKOFF_BASE  # reset on success
                self._monitor()
                if self._stop:
                    break
                log.info("reconnecting after drop ...")
                continue
            if self._stop:
                break
            log.info("retrying in %ds", backoff)
            slept = 0
            while slept < backoff and not self._stop:
                time.sleep(1)
                slept += 1
            backoff = min(cfg.TUNNEL_BACKOFF_MAX, backoff * 2)
        log.info("tunnel supervisor exiting")


def main() -> None:
    _setup_logging()
    # Record our own PID so the watchdog / stop.sh can find us.
    try:
        cfg.TUNNEL_PID_FILE.write_text(str(os.getpid()))
    except Exception as exc:
        log.warning("could not write pid file: %s", exc)

    sup = TunnelSupervisor()
    signal.signal(signal.SIGTERM, sup.request_stop)
    signal.signal(signal.SIGINT, sup.request_stop)
    try:
        sup.run()
    finally:
        try:
            if cfg.TUNNEL_PID_FILE.exists():
                cfg.TUNNEL_PID_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
