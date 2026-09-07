"""Loopback browser access through an existing SSH forward; no secrets in URLs.

Start the SSH forward separately, then pass the protected access.txt file.
This does not activate the public nginx route or manage any remote process.
"""
import argparse
import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def read_auth(path):
    if path.stat().st_mode & 0o077:
        raise ValueError('access file must be readable only by its owner')
    values = {}
    for line in path.read_text().splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            values[key.strip().lower()] = value.strip()
    if not values.get('username') or not values.get('password'):
        raise ValueError('access file requires Username and Password')
    return 'Basic ' + base64.b64encode((values['username']+':'+values['password']).encode()).decode()


def make_handler(auth, port, upstream_port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def handle_request(self):
            host = self.headers.get('Host', '')
            if host not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                self.send_error(403); return
            if self.headers.get('Origin') not in (None, 'http://'+host):
                self.send_error(403); return
            if not self.path.startswith('/pankgraph-vnext/'):
                self.send_error(404); return
            try: length = int(self.headers.get('Content-Length', 0))
            except ValueError: self.send_error(400); return
            if not 0 <= length <= 2_000_000 or self.headers.get('Transfer-Encoding'):
                self.send_error(413); return
            body = self.rfile.read(length) if length else None
            headers = {k:v for k,v in self.headers.items() if k.lower() not in ('authorization','connection','transfer-encoding')}
            headers['Authorization'] = auth
            conn = http.client.HTTPConnection('127.0.0.1', upstream_port, timeout=130)
            started = False
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                self.send_response(response.status)
                for key,value in response.getheaders():
                    if key.lower() not in ('connection','transfer-encoding'): self.send_header(key,value)
                self.send_header('Connection','close'); self.end_headers(); started=True
                while chunk := response.read1(65536):
                    self.wfile.write(chunk); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError): pass
            except (OSError, http.client.HTTPException):
                if not started: self.send_error(502, 'Demo tunnel unavailable')
            finally:
                conn.close(); self.close_connection=True
        do_GET = handle_request
        do_POST = handle_request
    return Handler


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--access-file',type=Path,required=True)
    parser.add_argument('--port',type=int,default=18795)
    parser.add_argument('--upstream-port',type=int,default=18796)
    args=parser.parse_args()
    for port in (args.port,args.upstream_port):
        if not 1024 <= port <= 65535: parser.error('port must be 1024–65535')
    ThreadingHTTPServer(('127.0.0.1',args.port),make_handler(read_auth(args.access_file),args.port,args.upstream_port)).serve_forever()

if __name__=='__main__': main()
