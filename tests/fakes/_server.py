"""The threaded HTTP server that fakes/comfy.py and fakes/ollama.py build on.

A FakeServer listens on 127.0.0.1:0, records every request, and hands each one to handle(), which a
subclass implements. Its script API, shared by both fakes:

- pause(*paths) / resume(): hold answers (all, or those whose path starts with one of paths) until resumed.
  A held request is recorded but gets no answer; stop() closes it without one, as a silent server would.
- override(method, path, ...): answer one endpoint with a fixed response, such as an HTML page or a 500.
- wait(seconds): an answer delay that stop() cuts short; math.inf waits until stop().

Errors inside handle() become a 500 and are kept in `errors`, which the fixtures check on teardown.

stop() always returns within a few seconds: it releases held and delayed answers, shuts down every open
client connection (so a handler blocked reading a request wakes up), and joins the server and handler
threads with a deadline. A thread still running after that is reported in `errors` instead of waited for.
Every thread is a daemon, so none can keep the test process alive either.
"""
from __future__ import annotations

import http.client
import json
import socket
import socketserver
import threading
import time
import traceback
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class Request:
    """One request as the fake received it."""

    method: str
    path: str  # without the query string
    query: dict[str, list[str]]
    headers: dict[str, str]  # names lowercased
    body: bytes
    received: float  # time.time()

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


@dataclass(frozen=True)
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"

    @classmethod
    def json(cls, obj: object, status: int = 200) -> Response:
        return cls(status, json.dumps(obj).encode("utf-8"), "application/json")


STOP_JOIN_S = 5.0  # how long stop() waits for the server and handler threads, in total


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True  # keeps the list of handler threads, which server_close() joins with a deadline
    fake: FakeServer

    def server_bind(self) -> None:
        # HTTPServer.server_bind would call socket.getfqdn(), a reverse lookup; the fakes need no name.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)

    def server_close(self) -> None:
        # ThreadingMixIn.server_close() joins every handler thread without a timeout; a stuck handler would
        # hang the test run. Close the socket, then join until the deadline and report what is left.
        socketserver.TCPServer.server_close(self)
        deadline = time.monotonic() + STOP_JOIN_S
        threads = vars(self).get("_threads")  # a list once a request was served; before that, none
        for thread in list(threads) if isinstance(threads, list) else []:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self.fake._error(f"{self.fake.name}: handler thread {thread.name} still running after stop()")


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "FakeServer/1"
    timeout = 5  # a client that connects and never sends its request cannot hold a handler for long

    def setup(self) -> None:
        super().setup()
        self.server.fake._opened(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.fake._closed(self.connection)

    def do_GET(self) -> None:
        self._dispatch()

    do_POST = do_PUT = do_DELETE = do_GET

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        parts = urlsplit(self.path)
        request = Request(
            method=self.command,
            path=parts.path,
            query=parse_qs(parts.query, keep_blank_values=True),
            headers={name.lower(): value for name, value in self.headers.items()},
            body=body,
            received=time.time(),
        )
        response = self.server.fake._serve(request)
        if response is None:  # stopped while held: close without an answer
            self.close_connection = True
            return
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)
        except OSError:  # the client gave up waiting (EPIPE, ECONNRESET, or EPROTOTYPE on macOS)
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server's signature
        pass


class FakeServer:
    """Base of the fakes: lifecycle, request log, pauses and overrides (see the module docstring)."""

    name = "fake"

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._requests: list[Request] = []
        self._paused: tuple[str, ...] | None = None  # None: answering; (): all held; else held prefixes
        self._overrides: dict[tuple[str, str], Response] = {}
        self._stopping = False
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()  # client connections a handler is serving
        self.errors: list[str] = []  # tracebacks of handle() failures, and threads that outlived stop()

    # ---- lifecycle ----

    def start(self) -> FakeServer:
        if self._server is not None:
            raise RuntimeError(f"{self.name} is already running")
        self._stopping = False
        server = _Server(("127.0.0.1", 0), _Handler)
        server.fake = self
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, name=f"{self.name}-server", daemon=True
        )
        thread.start()
        self._server, self._thread = server, thread
        return self

    def stop(self) -> None:
        """Stops serving within a few seconds, whatever the clients and scripted answers are doing."""
        server, thread = self._server, self._thread
        if server is None or thread is None:
            return
        with self._cond:
            self._stopping = True
            self._cond.notify_all()  # release held requests and delayed answers
            connections = list(self._connections)
        if thread.is_alive():  # shutdown() would wait forever for a serve loop that is not running
            server.shutdown()  # the loop polls every 0.05 s
        for connection in connections:  # wake handlers blocked reading a request or writing an answer
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already closed by the client or the handler
        server.server_close()  # closes the listening socket, joins the handler threads with a deadline
        thread.join(STOP_JOIN_S)
        if thread.is_alive():
            self._error(f"{self.name}: server thread {thread.name} still running after stop()")
        self._server = self._thread = None

    def __enter__(self):  # noqa: ANN204 - returns the subclass
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError(f"{self.name} is not running")
        return self._server.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---- script API ----

    def pause(self, *paths: str) -> None:
        """Hold every answer, or those whose path starts with one of paths, until resume() or stop()."""
        with self._cond:
            self._paused = tuple(paths)

    def resume(self) -> None:
        with self._cond:
            self._paused = None
            self._cond.notify_all()

    def override(
        self, method: str, path: str, status: int = 200, body: bytes | str = b"", content_type: str = "application/json"
    ) -> None:
        """Answer `method path` with this response from now on, instead of the fake's own answer."""
        data = body.encode("utf-8") if isinstance(body, str) else body
        with self._cond:
            self._overrides[(method.upper(), path)] = Response(status, data, content_type)

    def clear_overrides(self) -> None:
        with self._cond:
            self._overrides.clear()

    def wait(self, seconds: float) -> bool:
        """Sleep up to seconds (math.inf: until stop()); False when stop() cut the wait short."""
        deadline = time.monotonic() + seconds
        with self._cond:
            while not self._stopping:
                left = deadline - time.monotonic()
                if left <= 0:
                    return True
                self._cond.wait(min(left, 1.0))  # finite slices: Condition.wait rejects math.inf
            return False

    # ---- request log ----

    @property
    def requests(self) -> list[Request]:
        with self._cond:
            return list(self._requests)

    def calls(self, method: str | None = None, path: str | None = None) -> list[Request]:
        """Recorded requests, optionally only one method and one path; a path ending in `*` is a prefix."""
        found = []
        for request in self.requests:
            if method is not None and request.method != method.upper():
                continue
            if path is not None:
                if path.endswith("*"):
                    if not request.path.startswith(path[:-1]):
                        continue
                elif request.path != path:
                    continue
            found.append(request)
        return found

    # ---- serving ----

    def handle(self, request: Request) -> Response:
        raise NotImplementedError

    def _held(self, path: str) -> bool:
        return self._paused is not None and (not self._paused or path.startswith(self._paused))

    def _serve(self, request: Request) -> Response | None:
        with self._cond:
            self._requests.append(request)
            while self._held(request.path) and not self._stopping:
                self._cond.wait(1.0)  # woken by resume() or stop(); the timeout only re-checks
            if self._stopping:
                return None
            override = self._overrides.get((request.method, request.path))
        if override is not None:
            return override
        try:
            return self.handle(request)
        except Exception:  # a bug in the fake: answer 500 and keep the traceback for the fixture
            trace = traceback.format_exc()
            self._error(trace)
            return Response(500, trace.encode("utf-8"), "text/plain")

    def _error(self, text: str) -> None:
        with self._cond:
            self.errors.append(text)

    def _opened(self, connection: socket.socket) -> None:
        with self._cond:
            self._connections.add(connection)

    def _closed(self, connection: socket.socket) -> None:
        with self._cond:
            self._connections.discard(connection)


# ---- client helpers for the tests ----


def http_request(
    url: str,
    method: str = "GET",
    *,
    json_body: object = None,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, str], bytes]:
    """One request with http.client (no proxy settings, no redirects); returns status, headers, body."""
    parts = urlsplit(url)
    all_headers = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=timeout)
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.request(method, target, body=body, headers=all_headers)
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


def encode_multipart(
    fields: Mapping[str, str], files: Mapping[str, tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    """A multipart/form-data body: fields name -> value, files name -> (filename, data, content type).
    Returns the body and its Content-Type header."""
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value.encode() + b"\r\n"
        )
    for name, (filename, data, content_type) in files.items():
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        )
        chunks.append(head.encode() + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    """Parses a multipart/form-data body into name -> (filename or None, data). Raises ValueError."""
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("no multipart boundary")
    boundary = content_type.split(marker, 1)[1].split(";", 1)[0].strip().strip('"').encode()
    parts = body.split(b"--" + boundary)
    fields: dict[str, tuple[str | None, bytes]] = {}
    for part in parts[1:]:
        if part.startswith(b"--"):
            break
        part = part[2:] if part.startswith(b"\r\n") else part
        head, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            raise ValueError("a multipart part has no header end")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        params: dict[str, str] = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-disposition":
                for item in value.split(";")[1:]:
                    key, _, raw = item.strip().partition("=")
                    params[key.lower()] = raw.strip().strip('"')
        if "name" not in params:
            raise ValueError("a multipart part has no name")
        fields[params["name"]] = (params.get("filename"), data)
    return fields
