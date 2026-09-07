import http.client
from http.server import ThreadingHTTPServer
from threading import Thread
import pytest
from deploy_results.local_demo_proxy import make_handler, read_auth


def test_protected_auth_file_required(tmp_path):
    path=tmp_path/'access.txt';path.write_text('Username: demo\nPassword: fixture-secret\n');path.chmod(0o644)
    with pytest.raises(ValueError):read_auth(path)
    path.chmod(0o600)
    assert read_auth(path).startswith('Basic ')


def test_untrusted_host_and_origin_cannot_use_server_credentials():
    server=ThreadingHTTPServer(('127.0.0.1',0), make_handler('Basic fixture',18795,1))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        for headers in ({'Host':'untrusted.test:18795'},{'Host':'127.0.0.1:18795','Origin':'https://untrusted.test'}):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('GET','/pankgraph-vnext/',headers=headers)
            assert conn.getresponse().status==403
            conn.close()
    finally:server.shutdown();server.server_close();thread.join()
