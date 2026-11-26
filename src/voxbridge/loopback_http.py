"""Deadline-aware loopback HTTP transport shared by speech clients.

The caller owns policy (consent, identity, deadlines, typed errors).
Only the request thread accesses its socket; no cross-thread shutdown.
"""
from contextlib import contextmanager
import http.client
import io
import socket

POLL_SECONDS = .05
CONNECT_SECONDS = 2.0


class _PollingHTTPStream(io.RawIOBase):
    """Retry only socket reads, below http.client's buffered HTTP parser."""

    def __init__(self, sock, check):
        super().__init__()
        self.sock = sock
        self.check = check

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            self.check()
            try:
                count = self.sock.recv_into(buffer)
                self.check()
                return count
            except socket.timeout:
                continue


class _PollingHTTPSocket:
    """Adapter for http.client: one externally owned socket, no hidden waits."""

    def __init__(self, sock, check, poll_seconds):
        self.sock = sock
        self.check = check
        self.sock.settimeout(poll_seconds)

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("HTTP_INVALID_MODE")
        return io.BufferedReader(_PollingHTTPStream(self.sock, self.check))

    def sendall(self, data):
        pending = memoryview(data)
        while pending:
            self.check()
            try:
                count = self.sock.send(pending)
            except socket.timeout:
                continue
            if count <= 0:
                raise OSError("HTTP_SOCKET_CLOSED")
            pending = pending[count:]
        self.check()

    def close(self):
        # HTTPConnection may detach on a Connection: close response while
        # HTTPResponse still reads the body. _request owns the actual socket.
        pass


@contextmanager
def loopback_response(port, method, path, *, headers, check, body=None,
                      connect_timeout=CONNECT_SECONDS,
                      connection_factory=http.client.HTTPConnection, verify_connected=None):
    """Keep standard HTTP framing and close the exact socket on every exit."""
    check()
    conn = connection_factory("127.0.0.1", port, timeout=connect_timeout)
    sock = response = None
    try:
        conn.connect()
        sock = conn.sock
        if verify_connected is not None:
            verify_connected()
        check()
        # Poll below the buffered parser; do not retry partially parsed HTTP.
        conn.sock = _PollingHTTPSocket(sock, check, POLL_SECONDS)
        check()
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        check()
        yield response
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            try:
                conn.close()
            finally:
                if sock is not None:
                    sock.close()
