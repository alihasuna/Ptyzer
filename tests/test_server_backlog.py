"""
The server's listen backlog must absorb a page load's burst of parallel connections. Behind
jupyter-server-proxy the connects are non-blocking, so an overflowing backlog fails them outright
(EAGAIN on Linux, surfacing as a 500 / OSError Errno 22 in the proxy; ECONNREFUSED on macOS)
instead of making them wait. socketserver's default backlog of 5 failed most of 30 such connects.
"""
import errno
import os
import shutil
import socket
import tempfile

from ptyzer.ui import server


def test_unix_socket_backlog_absorbs_a_burst_of_nonblocking_connects():
    # Short path: AF_UNIX paths are limited to 104 bytes on macOS (108 on Linux).
    folder = tempfile.mkdtemp(prefix="pz", dir="/tmp")
    path = os.path.join(folder, "ui.sock")
    httpd, _ = server.serve(start_dir=folder, unix_socket=path)   # listening, not yet accepting
    clients, failures = [], []
    try:
        for _ in range(50):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.setblocking(False)
            clients.append(c)
            try:
                c.connect(path)
            except OSError as exc:
                failures.append(errno.errorcode.get(exc.errno, exc.errno))
        assert failures == [], f"{len(failures)} of 50 non-blocking connects failed: {sorted(set(failures))}"
    finally:
        for c in clients:
            c.close()
        httpd.server_close()
        shutil.rmtree(folder, ignore_errors=True)


def test_tcp_server_uses_the_same_backlog():
    httpd, _ = server.serve(port=0, start_dir=tempfile.gettempdir())
    try:
        assert httpd.request_queue_size == server.REQUEST_QUEUE_SIZE >= 64
        assert httpd.daemon_threads
    finally:
        httpd.server_close()
