"""Verify a source bundle index and load the dedicated database pair.

Only local private configuration is read; no credential is passed in argv.
"""
import argparse
import hashlib
import json
from pathlib import Path

from neo4j import GraphDatabase
import psycopg

from cakg.multimodal import load_postgres_files, load_neo4j
from .manage import NAME, PORTS, credentials, owned_root, private_json


def indexed_paths(index_path, index):
    base = index_path.parent.resolve()
    paths = []
    for item in index["bundles"]:
        path = (base / item["path"]).resolve()
        if not path.is_relative_to(base):
            raise ValueError("Bundle path escapes its indexed source directory")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if sha != item["sha256"]:
            raise ValueError("Source bundle checksum mismatch")
        paths.append(path)
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("Bundle index must contain unique paths")
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--graph-only", action="store_true")
    args = parser.parse_args()
    owned_root(args.root)
    index = json.loads(args.index.read_text())
    paths = indexed_paths(args.index, index)
    snapshot = index["snapshot_id"]
    secret = credentials(args.root)
    with psycopg.connect(host=str(args.root / "socket"), port=PORTS["postgres"],
                         dbname=NAME, user="serviceuser", autocommit=True) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == NAME
        with GraphDatabase.driver(f"bolt://127.0.0.1:{PORTS['bolt']}", auth=("neo4j", secret["neo4j_password"])) as graph:
            with graph.session(database=NAME) as session:
                if session.run("MATCH (n) RETURN count(n) AS n").single()["n"]:
                    raise ValueError("New graph must be empty; no destructive retry is provided")
            if not args.graph_only:
                result = load_postgres_files(paths, conn, snapshot, index["manifest"])
                private_json(args.root / "audit" / "postgres-load.json", result)
                print(json.dumps(result), flush=True)
            result = load_neo4j(conn, graph, NAME, snapshot)
            result["bundle_index_sha256"] = hashlib.sha256(args.index.read_bytes()).hexdigest()
            private_json(args.root / "audit" / "graph-load.json", result)
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
