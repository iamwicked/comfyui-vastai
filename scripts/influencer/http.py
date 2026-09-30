"""The network clients (design §Security, §C6; Req 25.1, 25.2, 25.3, 25.8, 25.9).

LoopbackClient: the only way the Batch_Generator, the Export_Tool and the Training_Kit's helpers reach the
ComfyUI_API and the Ollama_API (Req 25.1).
- It checks its URL when it is built, which is when a command starts, before any request: an http:// URL
  with a host and a port or none, and no user name, password, query string or fragment, whose host is a
  loopback address or a name that resolves only to loopback addresses (127.0.0.0/8 or ::1). Anything else
  raises Refused (or Unreachable for a name that does not resolve), naming the URL and the refused address.
- Every connection resolves the host again and opens a socket only when every address it gets is
  loopback, and then only to those addresses, so a name whose answer changes after the start check still
  cannot lead anywhere else.
- It builds its opener from ProxyHandler({}) and its own HTTP handler only. ProxyHandler({}) sets no proxy
  (it registers no method at all), so http_proxy, HTTPS_PROXY and the other proxy settings are never read;
  and with no redirect handler and no error processor a 3xx answer comes back as it is and is refused
  (RedirectRefused), never followed.

ExternalClient: the Disk_Estimator's, the Source_Checker's and the Training_Kit helpers' requests to the
internet (Req 25.2, 25.3, 25.8, 25.9).
- It requests https:// URLs only, on the hosts it is built with (SOURCE_HOSTS for Req 25.8, TRAINING_HOSTS
  for Req 25.2, plus the hosts named in the URLs of the Config_Lists lines being processed), and follows
  redirects from there itself: to any https:// host, at most MAX_REDIRECTS times.
- HF_TOKEN goes only to a Hugging Face host and CIVITAI_TOKEN only to civitai.com, as the Authorization
  header of the first request. A redirect that changes the scheme, host or port drops the header for the
  rest of the chain (urllib's own redirect handler would forward it).
- The tokens are read only from the environment variables of those names: there is no option, prompt or
  configuration entry for either. A URL that holds a token value is refused, messages print URLs without
  their query string, and any token value in a message is replaced by its variable name, so no token
  reaches stdout, stderr or a file (Req 25.9).
- It reads at most max_bytes of an answer, and nothing of a redirect's body.
- Each single exchange goes through a Transport: by default urllib with ProxyHandler({}) and HTTPSHandler
  only, so no proxy either. The tests hand in a recording one, and never reach the network.

Every failure is an HttpError, which is an EnvironmentFailure (exit status 2): Refused (RedirectRefused),
Unreachable, TimedOut and BadAnswer (StatusError for an HTTP status the call does not take).

Standard library only: the Disk_Estimator runs without PyYAML, Pillow or NumPy (§C5).
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from influencer.errors import EnvironmentFailure
from influencer.timing import DEFAULT_TIMING, Timing

USER_AGENT = "comfyui-vastai-influencer/1"
READ_CHUNK = 64 * 1024

# ---- errors ----


class HttpError(EnvironmentFailure):
    """A request that was refused, got no answer, or got an answer the call cannot use. The message names
    the URL without its query string, and url holds that same text (None when there is no URL yet)."""

    def __init__(self, message: str, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class Refused(HttpError):
    """The client would not connect or go on: a URL, an address or a host outside what it may reach."""


class RedirectRefused(Refused):
    """A LoopbackClient got a 3xx answer, which it never follows."""


class Unreachable(HttpError):
    """No connection: the name did not resolve, or the connection was refused, reset or closed."""


class TimedOut(HttpError):
    """No answer within the call's time limit."""


class BadAnswer(HttpError):
    """An answer the call cannot use: not JSON, larger than the call reads, or not the shape it needs."""


class StatusError(BadAnswer):
    """An answer with an HTTP status the call does not take. status and body (as read) are kept."""

    def __init__(self, message: str, url: str | None, status: int, body: bytes = b"") -> None:
        super().__init__(message, url)
        self.status = status
        self.body = body


# ---- URLs, addresses and answers ----


def shown_url(url: str) -> str:
    """url as every message prints it: scheme, host, port and path, without the user name, password, query
    string or fragment."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<a URL that does not parse>"
    netloc = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """text as an IP address, an IPv6 zone such as %lo0 left out; None when it is no address."""
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None


LOOPBACK_V4 = ipaddress.IPv4Network("127.0.0.0/8")
LOOPBACK_V6 = ipaddress.IPv6Address("::1")


def is_loopback_address(address: str) -> bool:
    """True for an address in 127.0.0.0/8, or ::1 (with or without a zone). An IPv4-mapped IPv6 address
    such as ::ffff:127.0.0.1 is neither, so it is not loopback here, whatever the Python version's
    ipaddress says; a host name is no address at all."""
    ip = _ip(address)
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in LOOPBACK_V4
    return ip == LOOPBACK_V6


def answer_excerpt(body: bytes, limit: int = 200) -> str:
    """A short, single-line view of an answer body for a message: the `error` text of a JSON answer that
    has one (Ollama's {"error": "..."} or ComfyUI's {"error": {"message": "..."}}), else the text itself."""
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str):
            text = error
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def read_limited(stream: Any, max_bytes: int) -> tuple[bytes, bool]:
    """At most max_bytes of stream, and whether it held more. It reads max_bytes + 1 bytes at most, in
    chunks, so an answer is never read much past what the caller takes."""
    if max_bytes < 0:
        raise ValueError(f"max_bytes must be 0 or more, not {max_bytes}")
    chunks: list[bytes] = []
    size = 0
    while size <= max_bytes:
        chunk = stream.read(min(READ_CHUNK, max_bytes + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    data = b"".join(chunks)
    return data[:max_bytes], size > max_bytes


@dataclass(frozen=True)
class Response:
    """One answer, fully read (or read up to the call's max_bytes when truncated is True)."""

    url: str  # the answering URL as messages print it: no query string
    status: int
    headers: Mapping[str, str]  # names in lower case
    body: bytes = b""
    truncated: bool = False  # the ExternalClient stopped reading at max_bytes
    service: str = ""  # "ComfyUI", "Ollama": names the service in messages
    redact: Callable[[str], str] | None = field(default=None, repr=False, compare=False)  # applied to messages

    @property
    def where(self) -> str:
        return f"{self.service} at {self.url}" if self.service else self.url

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def _message(self, text: str) -> str:
        return self.redact(text) if self.redact is not None else text

    def expect(self, *statuses: int) -> Response:
        """self when its status is one of statuses (default: any 2xx), else a StatusError naming the URL,
        the status and an excerpt of the answer."""
        if (self.status in statuses) if statuses else self.ok:
            return self
        detail = answer_excerpt(self.body)
        message = f"{self.where} answered HTTP {self.status}" + (f": {detail}" if detail else "")
        raise StatusError(self._message(message), self.url, self.status, self.body)

    def json(self) -> Any:
        """The body as JSON, or a BadAnswer naming the URL."""
        if self.truncated:
            raise BadAnswer(f"{self.where}: the answer is longer than this call reads", self.url)
        try:
            return json.loads(self.body)
        except ValueError as error:
            excerpt = answer_excerpt(self.body, 80)
            detail = f"; it starts {excerpt!r}" if excerpt else "; it is empty"
            message = f"{self.where} did not answer JSON ({type(error).__name__}){detail}"
            raise BadAnswer(self._message(message), self.url) from None


def _no_answer(reason: object, where: str, url: str, timeout: float | None) -> HttpError:
    """The HttpError for an exchange that failed below HTTP: a timeout, or no connection."""
    if isinstance(reason, TimeoutError):  # socket.timeout is TimeoutError from Python 3.10
        limit = f" within {timeout:g} s" if isinstance(timeout, (int, float)) else ""
        return TimedOut(f"{where}: no answer{limit}", url)
    if isinstance(reason, OSError) and reason.strerror:
        detail = reason.strerror
    else:
        detail = str(reason) or type(reason).__name__
    return Unreachable(f"{where}: no connection ({detail})", url)


# ---- LoopbackClient (Req 25.1) ----

Resolver = Callable[[str, int], Sequence[str]]  # (host name, port) -> its addresses
Connector = Callable[[str, int, Any], socket.socket]  # (address, port, timeout) -> a connected socket

LOOPBACK_SCHEME = "http"
DEFAULT_HTTP_PORT = 80
MAX_LOOPBACK_BYTES = 64 * 1024 * 1024  # /object_info of a ComfyUI with many node packs stays well below


def system_resolver(host: str, port: int) -> list[str]:
    """The addresses getaddrinfo gives for host, in its order, each once."""
    found: list[str] = []
    for *_, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = str(sockaddr[0])
        if address not in found:
            found.append(address)
    return found


def system_connector(address: str, port: int, timeout: Any) -> socket.socket:
    """A TCP connection to one literal address; getaddrinfo on a literal sends no DNS query."""
    return socket.create_connection((address, port), timeout=timeout)


class _LoopbackConnection(http.client.HTTPConnection):
    """An HTTPConnection that gets its socket from LoopbackClient.open_socket, never from a host name."""

    def __init__(self, host: str, *, client: LoopbackClient, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._client = client

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        self.sock = self._client.open_socket(self.timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:  # a system without TCP_NODELAY
            pass


class _LoopbackHandler(urllib.request.HTTPHandler):
    """urllib's HTTP handler with _LoopbackConnection in place of http.client.HTTPConnection."""

    def __init__(self, client: LoopbackClient) -> None:
        super().__init__()
        self._client = client

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(self._connection, req)

    def _connection(self, host: str, **kwargs: Any) -> _LoopbackConnection:
        return _LoopbackConnection(host, client=self._client, **kwargs)


class LoopbackClient:
    """HTTP to one service at a loopback address (see the module docstring).

    url is the service URL (COMFYUI_URL, OLLAMA_URL), service its name in messages ("ComfyUI", "Ollama")
    and setting the environment variable that sets it, for the hint in a refusal. resolver and connector
    replace getaddrinfo and socket.create_connection, for the tests. Building the client checks the URL and
    resolves its host, so build it when the command starts: nothing is sent before request().
    """

    def __init__(self, url: str, service: str, *, setting: str | None = None, resolver: Resolver | None = None,
                 connector: Connector | None = None) -> None:  # fmt: skip
        self.service = service
        self.setting = setting
        self._resolver = resolver or system_resolver
        self._connector = connector or system_connector
        self.url, self.host, self.port = self._parse(url)
        self.opener = urllib.request.OpenerDirector()
        self.opener.add_handler(urllib.request.ProxyHandler({}))  # no proxy, whatever http_proxy says
        self.opener.add_handler(_LoopbackHandler(self))  # and no redirect handler, no error processor
        self.addresses()  # the start check (Req 25.1)

    @property
    def where(self) -> str:
        return f"{self.service} at {shown_url(self.url)}"

    def _hint(self) -> str:
        name = self.setting or f"the {self.service} URL"
        return f"set {name} to an http:// URL at 127.0.0.1 or [::1], such as http://127.0.0.1:<port>"

    def _parse(self, url: str) -> tuple[str, str, int]:
        """(the URL without a trailing slash, its host, its port), or Refused naming what is wrong."""
        shown = shown_url(url)
        where = f"{self.service} at {shown}"
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError as error:
            raise Refused(f"{where}: not a valid URL ({error}); {self._hint()}", shown) from None
        faults = []
        if not parts.scheme:
            faults.append("it has no scheme")
        elif parts.scheme != LOOPBACK_SCHEME:
            faults.append(f"the scheme is {parts.scheme!r}, not 'http'")
        if parts.username is not None or parts.password is not None:
            faults.append("it holds a user name or password")
        if parts.query or parts.fragment:
            faults.append("it holds a query string or fragment")
        if not parts.hostname:
            faults.append("it has no host")
        if port == 0:
            faults.append("its port is 0")
        if faults:
            raise Refused(f"{where}: {'; '.join(faults)}; {self._hint()}", shown)
        base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
        return base, str(parts.hostname), port or DEFAULT_HTTP_PORT

    def addresses(self) -> list[str]:
        """The host's addresses, each of them loopback: the host itself when it is an address, else what the
        resolver answers. A resolver failure or an empty answer is Unreachable; any address outside
        127.0.0.0/8 and ::1 is Refused, naming the URL and that address."""
        host, url = self.host, shown_url(self.url)
        if _ip(host) is not None:
            found = [host]
        else:
            try:
                found = [str(address) for address in self._resolver(host, self.port)]
            except OSError as error:  # socket.gaierror and the like
                raise Unreachable(f"{self.where}: {host} does not resolve ({error})", url) from None
            if not found:
                raise Unreachable(f"{self.where}: {host} resolves to no address", url)
        for address in found:
            if not is_loopback_address(address):
                said = f"{host} is" if address == host else f"{host} resolves to {address}, which is"
                raise Refused(
                    f"{self.where}: {said} not a loopback address; the influencer tools reach {self.service} "
                    f"only at 127.0.0.0/8 or ::1 (Req 25.1): {self._hint()}",
                    url,
                )
        return found

    def open_socket(self, timeout: Any) -> socket.socket:
        """A socket connected to the service, opened only once every address of the host is loopback, and
        only to those addresses, in the resolver's order; the last connection error when none answers."""
        failure: OSError | None = None
        for address in self.addresses():
            try:
                return self._connector(address, self.port, timeout)
            except OSError as error:
                failure = error
        assert failure is not None  # addresses() never returns an empty list
        raise failure

    def request(self, method: str, path: str, *, body: bytes | None = None, content_type: str | None = None,
                timeout: float, max_bytes: int = MAX_LOOPBACK_BYTES) -> Response:  # fmt: skip
        """One request to <url><path>, with the given timeout for the connection and each read. Any status but
        a 3xx comes back as a Response (check it with Response.expect); a 3xx is RedirectRefused, a body
        longer than max_bytes a BadAnswer, no answer TimedOut or Unreachable."""
        if not path.startswith("/"):
            raise ValueError(f"a LoopbackClient path starts with '/', not {path!r}")
        url = self.url + path
        shown = shown_url(url)
        where = f"{self.service} at {shown}"
        headers = {"User-Agent": USER_AGENT}
        if content_type is not None:
            headers["Content-Type"] = content_type
        outgoing = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(outgoing, timeout=timeout) as answer:
                status = int(answer.status)
                received = {name.lower(): value for name, value in answer.headers.items()}
                if 300 <= status < 400:
                    target = received.get("location")
                    to = f" to {shown_url(urllib.parse.urljoin(url, target))}" if target else ""
                    raise RedirectRefused(
                        f"{where} answered HTTP {status}, a redirect{to}; the influencer tools never follow a "
                        f"redirect from {self.service}, so they cannot be led away from loopback (Req 25.1)",
                        shown,
                    )
                data, more = read_limited(answer, max_bytes)
        except HttpError:
            raise
        except urllib.error.URLError as error:  # a failure while connecting or sending
            raise _no_answer(error.reason, where, shown, timeout) from None
        except (OSError, http.client.HTTPException) as error:  # a failure while reading the answer
            raise _no_answer(error, where, shown, timeout) from None
        if more:
            raise BadAnswer(f"{where}: the answer is longer than {max_bytes} bytes", shown)
        return Response(shown, status, received, data, service=self.service)

    def get_json(self, path: str, *, timeout: float, max_bytes: int = MAX_LOOPBACK_BYTES) -> Any:
        """GET path; the JSON of a 2xx answer, else a StatusError or BadAnswer naming the URL."""
        return self.request("GET", path, timeout=timeout, max_bytes=max_bytes).expect().json()

    def post_json(self, path: str, data: object, *, timeout: float) -> Response:
        """POST data as JSON to path; the Response, whatever its status."""
        body = json.dumps(data).encode("utf-8")
        return self.request("POST", path, body=body, content_type="application/json", timeout=timeout)


# ---- ExternalClient (Req 25.2, 25.3, 25.8, 25.9) ----

HF_TOKEN, CIVITAI_TOKEN = "HF_TOKEN", "CIVITAI_TOKEN"  # environment variables, the only source of the tokens
TOKEN_VARIABLES = (HF_TOKEN, CIVITAI_TOKEN)

HUGGING_FACE_HOST = "huggingface.co"
HUGGING_FACE_DOMAINS = ("huggingface.co", "hf.co")  # these and their subdomains are Hugging Face hosts
CIVITAI_HOST = "civitai.com"  # the one host CIVITAI_TOKEN goes to
OLLAMA_LIBRARY_HOSTS = ("ollama.com", "registry.ollama.ai")

# Req 25.8: what the Disk_Estimator and Source_Checker start requests at, besides the hosts named in the
# Config_Lists URLs they process (ExternalClient's extra_hosts). The Ollama_API is the LoopbackClient's.
SOURCE_HOSTS = frozenset({HUGGING_FACE_HOST, CIVITAI_HOST, *OLLAMA_LIBRARY_HOSTS})
# Req 25.2: the Training_Kit's download hosts. The Ollama and ComfyUI APIs are the LoopbackClient's.
TRAINING_HOSTS = frozenset({
    "github.com", "api.github.com", "raw.githubusercontent.com",  # GitHub
    "pypi.org", "files.pythonhosted.org",  # PyPI
    "download.pytorch.org",  # the PyTorch package index
    "registry.npmjs.org",  # the npm registry
    HUGGING_FACE_HOST,
    "binaries.prisma.sh",  # Prisma's engine host, while the ai-toolkit UI builds
    "fonts.googleapis.com", "fonts.gstatic.com",  # Google Fonts, fetched by Next.js at build time
})  # fmt: skip

EXTERNAL_SCHEME = "https"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 1024 * 1024  # a metadata answer: model API JSON, a file listing
METHODS = ("GET", "HEAD", "POST")  # POST: the Disk_Estimator's Hugging Face paths-info listing (§C10)
_CALLER_HEADERS_REFUSED = ("authorization", "proxy-authorization", "cookie")


def normal_host(host: str) -> str:
    """A host name as the ExternalClient compares it: lower case, without one trailing dot."""
    host = host.lower()
    return host[:-1] if host.endswith(".") else host


def is_hugging_face_host(host: str) -> bool:
    """huggingface.co, hf.co, or a subdomain of either."""
    host = normal_host(host)
    return any(host == domain or host.endswith("." + domain) for domain in HUGGING_FACE_DOMAINS)


def token_variable(host: str) -> str | None:
    """The variable whose token host may receive: HF_TOKEN for a Hugging Face host, CIVITAI_TOKEN for
    civitai.com itself (none of its subdomains), else None."""
    if is_hugging_face_host(host):
        return HF_TOKEN
    if normal_host(host) == CIVITAI_HOST:
        return CIVITAI_TOKEN
    return None


@dataclass(frozen=True)
class Outgoing:
    """One exchange an ExternalClient hands its Transport, which follows no redirect itself."""

    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)  # may hold a token: kept out of repr()
    timeout: float
    max_bytes: int  # read at most this much of the body
    body: bytes = field(default=b"", repr=False)  # a POST's request body; empty for GET and HEAD


@dataclass(frozen=True)
class Incoming:
    """A Transport's answer to one exchange."""

    status: int
    headers: Mapping[str, str]  # names in lower case
    body: bytes = b""
    truncated: bool = False  # the body held more than max_bytes


Transport = Callable[[Outgoing], Incoming]


def external_opener() -> urllib.request.OpenerDirector:
    """ProxyHandler({}) and HTTPSHandler with certificate checks, nothing else: no proxy, no plain http,
    no redirect handler and no error processor, so every status comes back as it is."""
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.ProxyHandler({}))
    opener.add_handler(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener


def urllib_transport(request: Outgoing) -> Incoming:
    """The default Transport: one exchange through external_opener(). It reads at most max_bytes of the
    body, and nothing of a HEAD answer or a redirect."""
    data = request.body if request.method == "POST" else None
    outgoing = urllib.request.Request(request.url, data=data, headers=dict(request.headers), method=request.method)
    answer = external_opener().open(outgoing, timeout=request.timeout)
    if answer is None:  # a scheme external_opener() does not handle
        raise urllib.error.URLError(f"no handler for {urllib.parse.urlsplit(request.url).scheme!r}")
    with answer:
        status = int(answer.status)
        headers = {name.lower(): value for name, value in answer.headers.items()}
        if request.method == "HEAD" or status in REDIRECT_STATUSES:
            return Incoming(status, headers)
        body, truncated = read_limited(answer, request.max_bytes)
    return Incoming(status, headers, body, truncated)


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin(parts: urllib.parse.SplitResult) -> tuple[str, str, int | None]:
    """(scheme, host, port) with the scheme's default port filled in: a redirect that changes any of the
    three changes host for the Authorization rule."""
    return (parts.scheme, normal_host(parts.hostname or ""), parts.port or _DEFAULT_PORTS.get(parts.scheme))


class ExternalClient:
    """Metadata requests to internet hosts (see the module docstring).

    hosts are where a request may start: SOURCE_HOSTS (the default) or TRAINING_HOSTS, and extra_hosts the
    hosts named in the URLs of the Config_Lists lines being processed. environ is where HF_TOKEN and
    CIVITAI_TOKEN are read, once, when the client is built (default: os.environ). transport makes each
    single exchange (default: urllib_transport); timing gives the default time limit per exchange.
    """

    def __init__(self, hosts: Iterable[str] = SOURCE_HOSTS, *, extra_hosts: Iterable[str] = (),
                 environ: Mapping[str, str] | None = None, transport: Transport | None = None,
                 timing: Timing | None = None, max_redirects: int = MAX_REDIRECTS) -> None:  # fmt: skip
        self.hosts = frozenset(normal_host(host) for host in (*hosts, *extra_hosts))
        environ = os.environ if environ is None else environ
        self._tokens = {name: environ.get(name) or None for name in TOKEN_VARIABLES}
        self._transport = transport or urllib_transport
        self._timing = timing or DEFAULT_TIMING
        self._max_redirects = max_redirects

    def __repr__(self) -> str:  # never the tokens
        return f"ExternalClient(hosts={sorted(self.hosts)!r})"

    def redact(self, text: str) -> str:
        """text with each token value replaced by <HF_TOKEN> or <CIVITAI_TOKEN>."""
        for name, value in self._tokens.items():
            if value:
                text = text.replace(value, f"<{name}>")
        return text

    def _refuse(self, cls: type[HttpError], message: str, url: str) -> HttpError:
        return cls(self.redact(message), self.redact(shown_url(url)))

    def _redacted(self, error: HttpError) -> HttpError:
        """error again, with the token values out of its message and URL."""
        url = error.url and self.redact(error.url)
        if isinstance(error, StatusError):
            return StatusError(self.redact(str(error)), url, error.status, error.body)
        return type(error)(self.redact(str(error)), url)

    def _checked(self, url: str, *, redirected_from: str | None = None) -> urllib.parse.SplitResult:
        """url's parts when the client may request it: https, a host, no user name or password, and no
        token value anywhere in it. Otherwise Refused, naming the URL (and the redirecting one)."""
        shown = shown_url(url)
        via = f" (a redirect from {shown_url(redirected_from)})" if redirected_from else ""
        try:
            parts = urllib.parse.urlsplit(url)
            parts.port  # noqa: B018 - raises ValueError for a port that is not a number in range
        except ValueError as error:
            raise self._refuse(Refused, f"{shown}{via}: not a valid URL ({error})", url) from None
        faults = []
        if parts.scheme != EXTERNAL_SCHEME:
            scheme = f"the scheme is {parts.scheme!r}" if parts.scheme else "it has no scheme"
            faults.append(f"{scheme}; only https:// URLs are requested")
        if not parts.hostname:
            faults.append("it has no host")
        if parts.username is not None or parts.password is not None:
            faults.append("it holds a user name or password")
        for name, value in self._tokens.items():
            if value and value in url:
                faults.append(f"it holds the value of {name}, which goes only into a header to its own service")
        if faults:
            raise self._refuse(Refused, f"{shown}{via}: {'; '.join(faults)}", url)
        return parts

    def _exchange(self, outgoing: Outgoing) -> Incoming:
        where = shown_url(outgoing.url)
        try:
            return self._transport(outgoing)
        except HttpError as error:
            failure = error
        except urllib.error.URLError as error:  # before OSError, which it derives from
            failure = _no_answer(error.reason, where, where, outgoing.timeout)
        except (OSError, http.client.HTTPException) as error:
            failure = _no_answer(error, where, where, outgoing.timeout)
        raise self._redacted(failure) from None

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                max_bytes: int = DEFAULT_MAX_BYTES, timeout: float | None = None,
                body: bytes = b"") -> Response:  # fmt: skip
        """GET, HEAD or POST url, following up to MAX_REDIRECTS redirects, and the final answer, whatever its
        status. Reads at most max_bytes of its body (Response.truncated tells when there was more). A POST
        sends body; a 301, 302 or 303 answer to it continues as a GET without the body, as urllib does,
        and a 307 or 308 repeats the POST."""
        if method not in METHODS:
            raise ValueError(f"the ExternalClient sends {', '.join(METHODS)} only, not {method!r}")
        if body and method != "POST":
            raise ValueError(f"the ExternalClient sends a request body with POST only, not with {method}")
        refused = [name for name in (headers or {}) if name.lower() in _CALLER_HEADERS_REFUSED]
        if refused:
            raise ValueError(f"the ExternalClient sets no {', '.join(refused)} header for a caller")
        timeout = self._timing.external_request_s if timeout is None else timeout
        parts = self._checked(url)
        host = normal_host(parts.hostname or "")
        if host not in self.hosts:
            allowed = ", ".join(sorted(self.hosts)) or "none"
            message = (
                f"{shown_url(url)}: {host} is not a host this command may reach (Req 25.2, 25.8); it reaches "
                f"{allowed}, and the hosts those redirect to"
            )
            raise self._refuse(Refused, message, url)
        variable = token_variable(host)
        token = self._tokens.get(variable) if variable else None
        authorization = f"Bearer {token}" if token else None
        sent_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        for _ in range(self._max_redirects + 1):
            sent = dict(sent_headers)
            if authorization is not None:
                sent["Authorization"] = authorization
            answer = self._exchange(Outgoing(method, url, sent, timeout, max_bytes, body))
            location = answer.headers.get("location")
            if answer.status not in REDIRECT_STATUSES or not location:
                body, truncated = answer.body, answer.truncated
                if len(body) > max_bytes:  # a transport that read more than it was asked to
                    body, truncated = body[:max_bytes], True
                return Response(self.redact(shown_url(url)), answer.status, dict(answer.headers), body, truncated,
                                redact=self.redact)  # fmt: skip
            target = urllib.parse.urljoin(url, location)
            target_parts = self._checked(target, redirected_from=url)
            if _origin(target_parts) != _origin(parts):
                authorization = None  # dropped for the rest of the chain, which urllib would forward
            if method == "POST" and answer.status in (301, 302, 303):
                method, body = "GET", b""
                sent_headers = {name: value for name, value in sent_headers.items() if name.lower() != "content-type"}
            url, parts = target, target_parts
        raise self._refuse(BadAnswer, f"{shown_url(url)}: more than {self._max_redirects} redirects", url)

    def get(self, url: str, **options: Any) -> Response:
        return self.request("GET", url, **options)

    def head(self, url: str, **options: Any) -> Response:
        return self.request("HEAD", url, **options)

    def post(self, url: str, body: bytes, content_type: str, **options: Any) -> Response:
        headers = {**options.pop("headers", {}), "Content-Type": content_type}
        return self.request("POST", url, body=body, headers=headers, **options)

    def get_json(self, url: str, **options: Any) -> Any:
        """GET url; the JSON of a 2xx answer, else a StatusError or BadAnswer naming the URL."""
        return self.get(url, **options).expect().json()
