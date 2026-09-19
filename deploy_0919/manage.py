"""Manage only a dedicated, marked native database/runtime directory.

Run under its owning unprivileged service account. No shared service manager,
nginx change, global Python installation or pre-existing database is touched.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import time
from urllib.parse import quote

NAME = "pankgraph0919"
PORTS = {"postgres": 15919, "bolt": 17919, "neo4j_http": 17419, "api": 18919}
MARKER = "pankgraph0919-owned-runtime-v1"


def run(args, *, env=None, stdin=None):
    result = subprocess.run([str(a) for a in args], input=stdin, text=True,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        # Child output could contain a credential-bearing DSN; keep it private.
        raise RuntimeError(f"Command failed ({result.returncode}): {Path(str(args[0])).name}")
    return result.stdout


def private_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        json.dump(payload, handle, indent=2)


def free_port(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"Port {port} is occupied; refusing to replace its listener") from exc


def owned_root(root):
    if root.is_symlink() or root.resolve() != root.absolute():
        raise RuntimeError("Runtime root must be a canonical non-symlink path")
    if root.stat().st_uid != os.getuid():
        raise RuntimeError("Runtime root belongs to another account")
    if (root / ".owner").read_text().strip() != MARKER:
        raise RuntimeError("Missing runtime ownership marker")


def credentials(root):
    path = root / "config" / "credentials.json"
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Credential permissions must be private")
    return json.loads(path.read_text())


def neo_env(root):
    return {**os.environ, "NEO4J_HOME": str(root / "neo4j"),
            "NEO4J_CONF": str(root / "neo4j" / "conf")}


def bootstrap(root, pg_bin, neo_archive):
    if root.exists():
        raise RuntimeError("Bootstrap requires an absent directory; existing state is never overwritten")
    for port in PORTS.values():
        free_port(port)
    root.mkdir(mode=0o700)
    (root / ".owner").write_text(MARKER + "\n")
    for name in ("config", "state", "logs", "socket", "sources", "audit", "releases"):
        (root / name).mkdir(mode=0o700)
    secret = {"owner_password": secrets.token_urlsafe(36), "reader_password": secrets.token_urlsafe(36),
              "neo4j_password": secrets.token_urlsafe(36), "api_token": secrets.token_urlsafe(48)}
    private_json(root / "config" / "credentials.json", secret)
    private_json(root / "config" / "runtime.json", {"name": NAME, "ports": PORTS,
                 "pg_bin": str(pg_bin.resolve()), "owner_uid": os.getuid(), "python": sys.executable})
    password_file = root / "config" / "pg-init-password"
    password_file.write_text(secret["owner_password"])
    password_file.chmod(0o600)
    try:
        run([pg_bin / "initdb", "-D", root / "postgres", "-U", "serviceuser",
             "--auth-local=peer", "--auth-host=scram-sha-256", "--encoding=UTF8", "--locale=C",
             "--pwfile", password_file])
    finally:
        password_file.unlink()
    with (root / "postgres" / "postgresql.conf").open("a") as handle:
        handle.write(f"\nlisten_addresses = '127.0.0.1'\nport = {PORTS['postgres']}\n"
                     f"unix_socket_directories = '{root / 'socket'}'\nunix_socket_permissions = 0700\n"
                     "max_connections = 40\nshared_buffers = '256MB'\nlog_statement = 'none'\n"
                     "log_min_error_statement = 'panic'\n")
    # Extract only a clean distribution, never another running instance's data/config.
    with tarfile.open(neo_archive, "r:gz") as archive:
        members = archive.getmembers()
        top = {Path(member.name).parts[0] for member in members}
        if len(top) != 1:
            raise RuntimeError("Expected one distribution root")
        archive.extractall(root, filter="data")
    (root / next(iter(top))).rename(root / "neo4j")
    (root / "neo4j" / "conf" / "neo4j.conf").write_text(
        f"initial.dbms.default_database={NAME}\n"
        "server.default_listen_address=127.0.0.1\nserver.default_advertised_address=127.0.0.1\n"
        f"server.bolt.listen_address=127.0.0.1:{PORTS['bolt']}\n"
        f"server.bolt.advertised_address=127.0.0.1:{PORTS['bolt']}\n"
        "server.bolt.enabled=true\nserver.http.enabled=true\n"
        f"server.http.listen_address=127.0.0.1:{PORTS['neo4j_http']}\n"
        "server.https.enabled=false\ndbms.security.auth_enabled=true\n"
        "server.memory.heap.initial_size=512m\nserver.memory.heap.max_size=2g\n"
        "server.memory.pagecache.size=1g\ndb.tx_log.rotation.retention_policy=1 days\n"
        "dbms.usage_report.enabled=false\n")
    run([root / "neo4j" / "bin" / "neo4j-admin", "dbms", "set-initial-password", secret["neo4j_password"]], env=neo_env(root))
    run([sys.executable, "-m", "venv", root / "venv"])
    start_databases(root)
    run([pg_bin / "createdb", "-h", root / "socket", "-p", str(PORTS["postgres"]), NAME])
    # Random URL-safe credentials contain no quotes. SQL is delivered over stdin, not argv/logs.
    sql = (f"CREATE ROLE {NAME}_reader LOGIN PASSWORD '{secret['reader_password']}' "
           "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;\n"
           f"ALTER ROLE {NAME}_reader SET default_transaction_read_only=on;\n"
           f"ALTER ROLE {NAME}_reader SET statement_timeout='15s';\n"
           f"REVOKE ALL ON DATABASE {NAME} FROM PUBLIC;\n"
           f"GRANT CONNECT ON DATABASE {NAME} TO {NAME}_reader;\n"
           "REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n")
    run([pg_bin / "psql", "-v", "ON_ERROR_STOP=1", "-h", root / "socket", "-p", str(PORTS["postgres"]), "-d", NAME], stdin=sql)
    print(json.dumps({"status": "provisioned", "database": NAME, "ports": PORTS}))


def start_databases(root):
    owned_root(root)
    pg_bin = Path(json.loads((root / "config" / "runtime.json").read_text())["pg_bin"])
    if (root / "postgres" / "postmaster.pid").exists():
        verify_daemon(root / "postgres" / "postmaster.pid", root / "postgres")
    else:
        free_port(PORTS["postgres"])
        run([pg_bin / "pg_ctl", "-D", root / "postgres", "-l", root / "logs" / "postgres.log", "-w", "start"])
    if (root / "neo4j" / "run" / "neo4j.pid").exists():
        verify_daemon(root / "neo4j" / "run" / "neo4j.pid", root / "neo4j")
    else:
        free_port(PORTS["bolt"])
        free_port(PORTS["neo4j_http"])
        run([root / "neo4j" / "bin" / "neo4j", "start"], env=neo_env(root))


def grant_reader(root):
    settings = json.loads((root / "config" / "runtime.json").read_text())
    sql = (f"GRANT USAGE ON SCHEMA cakg_mm TO {NAME}_reader;\n"
           f"GRANT SELECT ON ALL TABLES IN SCHEMA cakg_mm TO {NAME}_reader;\n"
           f"ALTER DEFAULT PRIVILEGES IN SCHEMA cakg_mm GRANT SELECT ON TABLES TO {NAME}_reader;\n")
    run([Path(settings["pg_bin"]) / "psql", "-v", "ON_ERROR_STOP=1", "-h", root / "socket",
         "-p", str(PORTS["postgres"]), "-d", NAME], stdin=sql)


def freeze_graph(root):
    owned_root(root)
    config = root / "neo4j" / "conf" / "neo4j.conf"
    text = config.read_text()
    verify_daemon(root / "neo4j" / "run" / "neo4j.pid", root / "neo4j")
    if "dbms.databases.default_to_read_only=true" not in text:
        with config.open("a") as handle:
            handle.write("dbms.databases.default_to_read_only=true\n")
    if graph_access(root) != "read-only":
        # This is the owned dedicated instance only, verified by canonical root and marker.
        run([root / "neo4j" / "bin" / "neo4j", "restart"], env=neo_env(root))
        for attempt in range(30):
            try:
                if graph_access(root) == "read-only":
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("The dedicated graph did not enter read-only mode")
    print('{"status":"graph_read_only_verified"}')


def graph_access(root):
    from neo4j import GraphDatabase, Query
    secret = credentials(root)
    with GraphDatabase.driver(f"bolt://127.0.0.1:{PORTS['bolt']}", auth=("neo4j", secret["neo4j_password"]),
                              connection_timeout=2, connection_acquisition_timeout=2) as driver:
        with driver.session(database="system") as session:
            rows = session.run(Query("SHOW DATABASES YIELD name,access WHERE name=$name RETURN access", timeout=2), name=NAME).data()
            if len(rows) != 1:
                raise RuntimeError("Dedicated graph database identity unavailable")
            return rows[0]["access"]


def process_identity(pid):
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        return {"uid": proc.stat().st_uid, "start_ticks": stat[19],
                "cwd": str((proc / "cwd").resolve()),
                "cmdline": (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()}
    except FileNotFoundError:
        return None


def verify_daemon(pid_file, expected_directory):
    pid = int(pid_file.read_text().splitlines()[0])
    identity = process_identity(pid)
    if (identity is None or identity["uid"] != os.getuid()
            or str(expected_directory) not in identity["cmdline"]):
        raise RuntimeError("Database PID does not identify the owned instance")
    return identity


def start_api(root, release, snapshot):
    owned_root(root)
    release = release.resolve()
    if not release.is_relative_to(root / "releases"):
        raise RuntimeError("API release must be inside owned releases")
    if graph_access(root) != "read-only":
        raise RuntimeError("API requires the dedicated graph to be verified read-only")
    free_port(PORTS["api"])
    state = root / "state" / "api.json"
    if state.exists():
        old = json.loads(state.read_text())
        if process_identity(old["pid"]) is not None:
            raise RuntimeError("Existing PID is live; use owned stop-api first")
    secret = credentials(root)
    env = {**os.environ,
           "PANK0919_NEO4J_URI": f"bolt://127.0.0.1:{PORTS['bolt']}",
           "PANK0919_NEO4J_USER": "neo4j", "PANK0919_NEO4J_PASSWORD": secret["neo4j_password"],
           "PANK0919_NEO4J_DATABASE": NAME,
           "PANK0919_POSTGRES_READ_DSN": f"postgresql://{NAME}_reader:{quote(secret['reader_password'])}@127.0.0.1:{PORTS['postgres']}/{NAME}",
           "PANK0919_POSTGRES_READ_USER": NAME + "_reader", "PANK0919_API_TOKEN": secret["api_token"],
           "PANK0919_SNAPSHOT_ID": snapshot, "PANK0919_ALLOW_SENSITIVE_RECORDS": "false",
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    with (root / "logs" / "api.log").open("ab") as log:
        process = subprocess.Popen([str(root / "venv" / "bin" / "python"), "-m", "uvicorn",
                                    "pankgraph0919_backend.app:create_app", "--factory", "--host", "127.0.0.1",
                                    "--port", str(PORTS["api"]), "--no-access-log"],
                                   cwd=release, env=env, stdout=log, stderr=log, start_new_session=True)
    time.sleep(1)
    identity = process_identity(process.pid)
    if process.poll() is not None or identity is None:
        raise RuntimeError("API exited during startup; inspect its private log")
    private_json(state, {"pid": process.pid, "identity": identity, "snapshot_id": snapshot})
    print(json.dumps({"status": "started", "pid": process.pid, "port": PORTS["api"]}))


def stop_api(root):
    state = root / "state" / "api.json"
    info = json.loads(state.read_text())
    identity = process_identity(info["pid"])
    if identity is None:
        print('{"status":"already_stopped"}')
        return
    if identity != info["identity"] or identity["uid"] != os.getuid() or "pankgraph0919_backend.app:create_app" not in identity["cmdline"]:
        raise RuntimeError("PID identity mismatch; refusing to signal process")
    os.kill(info["pid"], signal.SIGTERM)
    print('{"status":"stop_requested"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "start-databases", "grant-reader", "freeze-graph", "start-api", "stop-api"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--neo-archive", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--snapshot")
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "bootstrap":
        if not args.pg_bin or not args.neo_archive:
            parser.error("bootstrap requires --pg-bin and --neo-archive")
        bootstrap(args.root, args.pg_bin, args.neo_archive)
        return
    owned_root(args.root)
    with (args.root / "state" / "manager.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "start-databases":
            start_databases(args.root)
        elif args.action == "grant-reader":
            grant_reader(args.root)
        elif args.action == "freeze-graph":
            freeze_graph(args.root)
        elif args.action == "start-api":
            if not args.release or not args.snapshot:
                parser.error("start-api requires --release and --snapshot")
            start_api(args.root, args.release, args.snapshot)
        elif args.action == "stop-api":
            stop_api(args.root)


if __name__ == "__main__":
    main()
