"""Authenticated local query client; the bearer token never appears in argv or logs.

Pass a JSON request file or '-' for stdin. The API retains its normal redaction
and read-only boundaries. Run as the owner of the isolated runtime directory.
"""
import argparse
import json
from pathlib import Path
import sys

import httpx
from .manage import PORTS, credentials, owned_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--request", required=True)
    parser.add_argument("--route", choices=["/query", "/genes/search", "/records/search"], default="/query")
    args = parser.parse_args()
    owned_root(args.root)
    raw = sys.stdin.read(64001) if args.request == "-" else Path(args.request).read_text()
    if len(raw.encode()) > 64000:
        raise ValueError("Request exceeds API body limit")
    payload = json.loads(raw)
    token = credentials(args.root)["api_token"]
    with httpx.Client(trust_env=False, timeout=60) as client:
        response = client.post(f"http://127.0.0.1:{PORTS['api']}{args.route}", json=payload,
                               headers={"Authorization": "Bearer " + token})
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    if response.status_code != 200:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
