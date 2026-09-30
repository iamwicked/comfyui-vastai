"""Tests of the network clients, scripts/influencer/http.py (task 15.1, 15.2, 15.6, 15.7; design §Security;
Req 25.1, 25.2, 25.3, 25.8, 25.9).

- Property 46: the LoopbackClient connects only when every resolved address is loopback, ignores the
  proxy settings and never follows a redirect (a fake resolver and a fake socket; no network)
- Property 47: over redirect chains, HF_TOKEN reaches only Hugging Face hosts and CIVITAI_TOKEN only
  civitai.com, and neither shows in a URL, a message, stdout, stderr or a log (a recording transport)
- unit tests: the start check of COMFYUI_URL and OLLAMA_URL, resolution on every connection, the fake
  ComfyUI behind proxy variables, redirects, timeouts and closed ports, and the ExternalClient's refusals,
  redirect limit, byte limit and error mapping
"""
from __future__ import annotations

import inspect
import io
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from urllib.parse import urlsplit

import pytest
from hypothesis import given, settings

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import http as H  # noqa: E402
from influencer.http import (  # noqa: E402
    BadAnswer,
    ExternalClient,
    HttpError,
    Incoming,
    LoopbackClient,
    Outgoing,
    RedirectRefused,
    Refused,
    Response,
    StatusError,
    TimedOut,
    Unreachable,
    shown_url,
)
from strategies_network import (  # noqa: E402
    CANARIES,
    HF_CANARY,
    PROXY_VARIABLES,
    LoopbackCase,
    RedirectChain,
    loopback,
    loopback_cases,
    own_token,
    redirect_chains,
)

OK_REPLY = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"


class FakeSocket:
    """What a test connector hands the LoopbackClient: it records what is sent and answers with reply."""

    def __init__(self, reply: bytes = OK_REPLY) -> None:
        self.sent = bytearray()
        self.reply = reply
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, mode: str, *args: object, **kwargs: object) -> io.BytesIO:
        return io.BytesIO(self.reply)

    def setsockopt(self, *args: object) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def no_lookup(host: str, port: int) -> list[str]:
    raise AssertionError(f"the resolver was asked for {host}")


def no_connect(address: str, port: int, timeout: object) -> FakeSocket:
    raise AssertionError(f"a connection to {address} port {port} was opened")


# ---- the properties ----


# Feature: ai-influencer-pipeline, Property 46: Loopback confinement
@settings(deadline=None)
@given(case=loopback_cases())
def test_loopback_confinement(case: LoopbackCase) -> None:
    """Validates: Requirements 25.1"""
    sockets: list[FakeSocket] = []
    connections: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> list[str]:
        found = case.answers.get(host)
        if found is None:
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
        return list(found)

    def connector(address: str, port: int, timeout: object) -> FakeSocket:
        connections.append((address, port))
        sockets.append(FakeSocket(case.reply))
        return sockets[-1]

    outcome: object
    with mock.patch.dict(os.environ, case.proxies):
        try:
            client = LoopbackClient(case.url, "ComfyUI", setting="COMFYUI_URL", resolver=resolver, connector=connector)
            outcome = client.request("GET", "/system_stats", timeout=5.0)
        except HttpError as error:
            outcome = error

    addresses = case.addresses
    opens = case.scheme.lower() == "http" and bool(addresses) and all(loopback(a) for a in addresses or ())
    if not opens:
        assert connections == []
        assert isinstance(outcome, (Refused, Unreachable)), outcome
        assert shown_url(case.url).rstrip("/") in str(outcome)
        if case.scheme.lower() == "http" and addresses:
            refused = next(address for address in addresses if not loopback(address))
            assert isinstance(outcome, Refused) and refused in str(outcome)
        return
    # one connection, to the first resolved address and the URL's port: never to a proxy
    assert connections == [(addresses[0], case.port or 80)]
    assert bytes(sockets[0].sent).startswith(b"GET /system_stats HTTP/1.1\r\n")  # origin form, not a proxy's
    assert b"proxy-authorization" not in bytes(sockets[0].sent).lower()
    if case.status == 200:
        assert isinstance(outcome, Response) and outcome.status == 200
    else:
        assert isinstance(outcome, RedirectRefused)  # and no second connection: never followed


class _Recorder(logging.Handler):
    def __init__(self, records: list[str]) -> None:
        super().__init__(logging.DEBUG)
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


# Feature: ai-influencer-pipeline, Property 47: Tokens never leave their own service
@settings(deadline=None)
@given(chain=redirect_chains())
def test_tokens_never_leave_their_own_service(chain: RedirectChain) -> None:
    """Validates: Requirements 25.8, 25.9"""
    sent: list[Outgoing] = []

    def transport(outgoing: Outgoing) -> Incoming:
        sent.append(outgoing)
        return chain.answers[len(sent) - 1]

    client = ExternalClient(extra_hosts=chain.extra_hosts, environ=chain.tokens, transport=transport)
    stdout, stderr, records = io.StringIO(), io.StringIO(), []
    recorder, root = _Recorder(records), logging.getLogger()
    level = root.level
    root.addHandler(recorder)
    root.setLevel(logging.DEBUG)
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                outcome: object = client.get(chain.url)
            except HttpError as error:
                outcome = error
    finally:
        root.removeHandler(recorder)
        root.setLevel(level)

    # requests start only at an allowed host; every later one is a redirect target reached from it
    allowed = H.SOURCE_HOSTS | set(chain.extra_hosts)
    if chain.hosts[0] not in allowed:
        assert sent == [] and isinstance(outcome, Refused)
    else:
        assert [urlsplit(outgoing.url).hostname for outgoing in sent] == list(chain.hosts[: chain.requests])
        if chain.echo is not None:
            assert isinstance(outcome, Refused)  # a Location holding a token value is not followed
        else:
            assert isinstance(outcome, Response) and outcome.status == 200
    # the token of the first host's own service, on the requests that stay on that host, and nowhere else
    variable = own_token(chain.hosts[0])
    for index, outgoing in enumerate(sent):
        stayed = all(host == chain.hosts[0] for host in chain.hosts[: index + 1])
        token = chain.tokens.get(variable) if variable is not None and stayed else None
        assert outgoing.headers.get("Authorization") == (f"Bearer {token}" if token else None)
        host = urlsplit(outgoing.url).hostname or ""
        for name, canary in CANARIES.items():
            assert canary not in outgoing.url
            if canary in repr(dict(outgoing.headers)):
                assert own_token(host) == name
    texts = [stdout.getvalue(), stderr.getvalue(), *records, str(outcome), repr(outcome), repr(client)]
    texts.append(str(getattr(outcome, "url", "")))
    for canary in CANARIES.values():
        assert not any(canary in text for text in texts)


# ---- LoopbackClient: the start check ----


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://192.0.2.1:8188", "192.0.2.1 is not a loopback address"),
        ("http://[2001:db8::1]:8188", "2001:db8::1 is not a loopback address"),
        ("http://0.0.0.0:18188", "0.0.0.0 is not a loopback address"),
        ("http://[::ffff:127.0.0.1]:8188", "is not a loopback address"),
        ("https://127.0.0.1:8188", "the scheme is 'https', not 'http'"),
        ("127.0.0.1:8188", "it has no scheme"),
        ("http://admin:secret@127.0.0.1:8188", "it holds a user name or password"),
        ("http://127.0.0.1:8188/?token=secret", "it holds a query string or fragment"),
        ("http://127.0.0.1:0", "its port is 0"),
        ("http://127.0.0.1:99999", "not a valid URL"),
        ("http://:8188", "it has no host"),
    ],
)
def test_a_url_off_loopback_is_refused_when_the_client_is_built(url: str, expected: str, socket_guard) -> None:
    refused_before = list(socket_guard.refused)
    with pytest.raises(Refused) as caught:
        LoopbackClient(url, "ComfyUI", setting="COMFYUI_URL", resolver=no_lookup, connector=no_connect)
    message = str(caught.value)
    assert message.startswith("ComfyUI at ") and expected in message and "COMFYUI_URL" in message
    assert "secret" not in message  # user info and query strings are never printed
    assert socket_guard.refused == refused_before


def test_a_name_must_resolve_to_loopback_addresses_only() -> None:
    both = lambda host, port: ["127.0.0.1", "10.1.2.3"]  # noqa: E731
    with pytest.raises(Refused, match=r"at http://comfy\.example:8188: comfy\.example resolves to 10\.1\.2\.3, which"):
        LoopbackClient("http://comfy.example:8188", "ComfyUI", resolver=both, connector=no_connect)
    with pytest.raises(Unreachable, match="comfy.example resolves to no address"):
        LoopbackClient("http://comfy.example:8188", "ComfyUI", resolver=lambda h, p: [], connector=no_connect)

    def fails(host: str, port: int) -> list[str]:
        raise socket.gaierror(socket.EAI_NONAME, "not known")

    with pytest.raises(Unreachable, match="comfy.example does not resolve"):
        LoopbackClient("http://comfy.example:8188", "ComfyUI", resolver=fails, connector=no_connect)
    assert LoopbackClient("http://LocalHost:1", "Ollama", resolver=lambda h, p: ["::1", "127.0.0.1"]).host == "localhost"


def test_every_connection_resolves_the_name_again() -> None:
    answers = iter([["127.0.0.1"], ["10.1.2.3"]])  # the start check, then the request: a rebinding name
    client = LoopbackClient("http://comfy.example:8188", "ComfyUI", resolver=lambda h, p: next(answers),
                            connector=no_connect)  # fmt: skip
    with pytest.raises(Refused, match="10.1.2.3"):
        client.request("GET", "/system_stats", timeout=1)


def test_the_addresses_are_tried_in_the_resolvers_order() -> None:
    tried = []

    def connector(address: str, port: int, timeout: object) -> FakeSocket:
        tried.append((address, port))
        if address == "::1":
            raise ConnectionRefusedError(61, "Connection refused")
        return FakeSocket()

    client = LoopbackClient("http://localhost:11434", "Ollama", resolver=lambda h, p: ["::1", "127.0.0.1"],
                            connector=connector)  # fmt: skip
    assert client.request("GET", "/api/tags", timeout=1).json() == {}
    assert tried == [("::1", 11434), ("127.0.0.1", 11434)]


def test_the_opener_has_no_proxy_redirect_or_error_handler() -> None:
    # ProxyHandler({}) registers no method, so OpenerDirector keeps only the loopback handler: no handler
    # reads http_proxy, none follows a redirect, none turns a status into an exception
    client = LoopbackClient("http://127.0.0.1:18188", "ComfyUI")
    assert [type(handler) for handler in client.opener.handlers] == [H._LoopbackHandler]
    assert set(client.opener.handle_open) == {"http"}
    assert client.opener.handle_error == {} and client.opener.process_response == {}


# ---- LoopbackClient: requests ----


def test_the_proxy_settings_are_ignored(comfy_fake, monkeypatch, socket_guard) -> None:
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, "http://192.0.2.44:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    client = LoopbackClient(comfy_fake.url, "ComfyUI", setting="COMFYUI_URL")
    assert client.get_json("/system_stats", timeout=5)["system"]["comfyui_version"] == "0.3.60"
    [request] = comfy_fake.calls("GET", "/system_stats")
    assert "proxy-authorization" not in request.headers
    assert not any("192.0.2.44" in where for where in socket_guard.refused)


def test_a_redirect_is_refused_and_never_followed(comfy_fake) -> None:
    comfy_fake.override("GET", "/system_stats", 302, "", "text/plain")
    client = LoopbackClient(comfy_fake.url, "ComfyUI")
    with pytest.raises(RedirectRefused, match=r"/system_stats answered HTTP 302, a redirect; .* never follow"):
        client.request("GET", "/system_stats", timeout=5)
    assert len(comfy_fake.calls()) == 1

    reply = b"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://203.0.113.9/next?sig=secret\r\nContent-Length: 0\r\n\r\n"
    client = LoopbackClient("http://127.0.0.1:18188", "ComfyUI", connector=lambda a, p, t: FakeSocket(reply))
    with pytest.raises(RedirectRefused) as caught:
        client.request("GET", "/queue", timeout=1)
    assert "a redirect to http://203.0.113.9/next;" in str(caught.value) and "secret" not in str(caught.value)


def test_a_held_answer_times_out_and_a_closed_port_is_unreachable(comfy_fake, timing, closed_port) -> None:
    comfy_fake.pause("/queue")
    client = LoopbackClient(comfy_fake.url, "ComfyUI")
    with pytest.raises(TimedOut, match=f"ComfyUI at {comfy_fake.url}/queue: no answer within {timing.request_s:g} s"):
        client.request("GET", "/queue", timeout=timing.request_s)
    closed = LoopbackClient(f"http://127.0.0.1:{closed_port}", "Ollama", setting="OLLAMA_URL")
    with pytest.raises(Unreachable, match=f"Ollama at http://127.0.0.1:{closed_port}/api/tags: no connection"):
        closed.request("GET", "/api/tags", timeout=2)


def test_statuses_json_and_the_byte_limit() -> None:
    reply = b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 29\r\n\r\n{\"error\": \"model not found\"}\n"
    client = LoopbackClient("http://127.0.0.1:11434", "Ollama", connector=lambda a, p, t: FakeSocket(reply))
    response = client.request("POST", "/api/show", body=b"{}", content_type="application/json", timeout=1)
    assert (response.status, response.ok, response.url) == (500, False, "http://127.0.0.1:11434/api/show")
    with pytest.raises(StatusError, match=r"Ollama at http://127\.0\.0\.1:11434/api/show answered HTTP 500: model not found$"):
        response.expect()
    assert response.expect(500) is response
    with pytest.raises(BadAnswer, match="did not answer JSON"):
        Response("http://x/y", 200, {}, b"<html>", service="Ollama").json()
    long = b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n0123456789"
    client = LoopbackClient("http://127.0.0.1:18188", "ComfyUI", connector=lambda a, p, t: FakeSocket(long))
    with pytest.raises(BadAnswer, match="longer than 4 bytes"):
        client.request("GET", "/object_info", timeout=1, max_bytes=4)
    assert client.request("GET", "/object_info", timeout=1, max_bytes=10).body == b"0123456789"
    with pytest.raises(ValueError, match="starts with '/'"):
        client.request("GET", "object_info", timeout=1)


# ---- ExternalClient ----


def recording(*answers: Incoming) -> tuple[list[Outgoing], H.Transport]:
    sent: list[Outgoing] = []

    def transport(outgoing: Outgoing) -> Incoming:
        sent.append(outgoing)
        return answers[len(sent) - 1]

    return sent, transport


def test_a_request_starts_only_at_an_allowed_https_host() -> None:
    sent, transport = recording(Incoming(200, {}, b"{}"))
    client = ExternalClient(extra_hosts=["files.example.org"], environ={}, transport=transport)
    for url, expected in (
        ("https://evil.example/x", "evil.example is not a host this command may reach"),
        ("http://huggingface.co/x", "the scheme is 'http'; only https:// URLs are requested"),
        ("https://user:pw@huggingface.co/x", "it holds a user name or password"),
        ("https://huggingface.co.evil.example/x", "is not a host this command may reach"),
    ):
        with pytest.raises(Refused, match=expected):
            client.get(url)
    assert sent == []
    assert client.get("https://FILES.example.org./a?b=c").url == "https://FILES.example.org./a"
    training = ExternalClient(H.TRAINING_HOSTS, environ={}, transport=recording(Incoming(204, {}))[1])
    assert training.head("https://pypi.org/simple/uv/").status == 204
    with pytest.raises(Refused):
        training.get("https://civitai.com/api/v1/models/1")  # Req 25.2 does not list Civitai


def test_a_redirect_changing_host_drops_the_token_for_good() -> None:
    sent, transport = recording(
        Incoming(302, {"location": "https://cdn-lfs.hf.co/repo/x?X-Amz-Signature=abc"}),
        Incoming(307, {"location": "https://huggingface.co/api/again"}),
        Incoming(200, {}, b"{}"),
    )
    client = ExternalClient(environ={"HF_TOKEN": HF_CANARY}, transport=transport)
    response = client.get("https://huggingface.co/amber/maple/resolve/main/x.safetensors")
    assert [outgoing.headers.get("Authorization") for outgoing in sent] == [f"Bearer {HF_CANARY}", None, None]
    assert response.url == "https://huggingface.co/api/again"


def test_a_url_holding_a_token_is_refused_and_never_printed() -> None:
    sent, transport = recording(Incoming(302, {"location": f"/next?t={HF_CANARY}"}))
    client = ExternalClient(environ={"HF_TOKEN": HF_CANARY}, transport=transport)
    with pytest.raises(Refused) as caught:
        client.get("https://huggingface.co/api/models/x")
    assert "holds the value of HF_TOKEN" in str(caught.value) and HF_CANARY not in str(caught.value)
    assert len(sent) == 1
    with pytest.raises(Refused, match="holds the value of HF_TOKEN") as caught:
        client.get(f"https://huggingface.co/{HF_CANARY}/x")
    assert HF_CANARY not in str(caught.value) and HF_CANARY not in str(caught.value.url)


def test_the_tokens_come_only_from_the_environment(monkeypatch) -> None:
    parameters = set(inspect.signature(ExternalClient).parameters)
    assert parameters == {"hosts", "extra_hosts", "environ", "transport", "timing", "max_redirects"}
    monkeypatch.setenv("CIVITAI_TOKEN", "civitai-from-the-environment-0123")
    sent, transport = recording(Incoming(200, {}, b"{}"))
    client = ExternalClient(transport=transport)
    client.get("https://civitai.com/api/v1/model-versions/1")
    assert sent[0].headers["Authorization"] == "Bearer civitai-from-the-environment-0123"
    assert "civitai-from-the-environment" not in repr(client) + repr(sent[0])
    with pytest.raises(ValueError, match="sets no Authorization header"):
        client.get("https://civitai.com/x", headers={"Authorization": "Bearer mine"})


def test_redirect_limits_bytes_and_failures() -> None:
    loop = Incoming(301, {"location": "/again"})
    sent, transport = recording(*[loop] * (H.MAX_REDIRECTS + 1))
    with pytest.raises(BadAnswer, match=f"more than {H.MAX_REDIRECTS} redirects"):
        ExternalClient(environ={}, transport=transport).get("https://ollama.com/library/qwen3")
    assert len(sent) == H.MAX_REDIRECTS + 1
    sent, transport = recording(Incoming(200, {}, b"0123456789"))
    response = ExternalClient(environ={}, transport=transport).get("https://ollama.com/x", max_bytes=4)
    assert (sent[0].max_bytes, response.body, response.truncated) == (4, b"0123", True)
    with pytest.raises(BadAnswer, match="longer than this call reads"):
        response.json()

    def failing(error: BaseException) -> H.Transport:
        def transport(outgoing: Outgoing) -> Incoming:
            raise error

        return transport

    environ = {"HF_TOKEN": HF_CANARY}
    for error, kind in (
        (TimeoutError("timed out"), TimedOut),
        (urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), Unreachable),
        (urllib.error.URLError(f"certificate for {HF_CANARY} failed"), Unreachable),
    ):
        with pytest.raises(kind) as caught:
            ExternalClient(environ=environ, transport=failing(error)).get("https://huggingface.co/x?q=1")
        assert str(caught.value).startswith("https://huggingface.co/x: ") and HF_CANARY not in str(caught.value)
    echo = Incoming(500, {}, f'{{"error": "bad token {HF_CANARY}"}}'.encode())
    with pytest.raises(StatusError, match="answered HTTP 500: bad token <HF_TOKEN>"):
        ExternalClient(environ=environ, transport=recording(echo)[1]).get_json("https://huggingface.co/x")


def test_the_default_transport_has_no_proxy_no_redirects_and_no_plain_http() -> None:
    opener = H.external_opener()  # ProxyHandler({}) registers nothing, as above
    assert [type(handler) for handler in opener.handlers] == [urllib.request.HTTPSHandler]
    assert set(opener.handle_open) == {"https"}
    assert opener.handle_error == {} and opener.process_response == {}
    assert H.read_limited(io.BytesIO(b"abcdef"), 4) == (b"abcd", True)
    assert H.read_limited(io.BytesIO(b"abcd"), 4) == (b"abcd", False)
    assert H.read_limited(io.BytesIO(b""), 0) == (b"", False)


def test_token_hosts_and_shown_urls() -> None:
    assert [H.token_variable(host) for host in ("huggingface.co", "cdn-lfs.hf.co", "HF.CO.", "civitai.com",
                                                "b2.civitai.com", "xhuggingface.co", "civitai.com.evil.example")] == [
        "HF_TOKEN", "HF_TOKEN", "HF_TOKEN", "CIVITAI_TOKEN", None, None, None,
    ]  # fmt: skip
    assert shown_url("https://u:p@h.example:8443/a/b?token=1#f") == "https://h.example:8443/a/b"
    assert [H.is_loopback_address(a) for a in ("127.255.0.1", "::1", "::1%lo0", "::ffff:127.0.0.1", "0.0.0.0", "x")] == [
        True, True, True, False, False, False,
    ]  # fmt: skip
