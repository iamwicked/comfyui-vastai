"""Hypothesis strategies for the network clients and the Instagram_Allowlist (task 15; design §Security,
§A6, Properties 40, 46 and 47).

- loopback_cases(): a ComfyUI or Ollama URL (scheme, host name or literal address, port), the fake
  resolver's answers, proxy variables and the server's answer (Property 46)
- redirect_chains(): 1-4 hops across Hugging Face, Civitai, CDN and unrelated hosts, each hop's answer
  as an injected transport gives it, with the tokens set or not (Property 47)
- influencer_lines() and allowlist_cases(): models.list lines tagged with the Influencer_Ids that
  config_lists() never uses, mixed into config_lists()' lines, a Persona from personas() with
  Character_LoRAs that sometimes sit under nsfw/, and look entries drawn from allowlisted names, near
  misses and NSFW names (Property 40). Each case carries its own oracle, the allowlist the requirements
  define, computed from the generated parts and not from the package.

A module of its own so tests/strategies.py stays untouched; config_lists() comes from there and personas()
from strategies_persona.py.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import sys
from dataclasses import dataclass
from typing import Union

from hypothesis import strategies as st

from helpers.harness import REPO
from strategies import WORDS, config_lists, install_path
from strategies_persona import personas

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import persona as P  # noqa: E402
from influencer.http import Incoming  # noqa: E402

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# ---- Property 46: loopback_cases() ----

LOOPBACK_V4 = ipaddress.IPv4Network("127.0.0.0/8")
LOOPBACK_V6 = ipaddress.IPv6Address("::1")


def loopback(address: str) -> bool:
    """The property's rule, stated on its own: in 127.0.0.0/8, or exactly ::1."""
    ip = ipaddress.ip_address(address)
    return ip in LOOPBACK_V4 if ip.version == 4 else ip == LOOPBACK_V6


# st.ip_addresses() over both families, with loopback answers drawn often enough to matter.
ADDRESSES = st.one_of(
    st.ip_addresses(),
    st.ip_addresses(v=4, network="127.0.0.0/8"),
    st.just(LOOPBACK_V6),
    st.just(ipaddress.IPv6Address("::ffff:127.0.0.1")),  # IPv4-mapped: neither 127.0.0.0/8 nor ::1
    st.sampled_from([ipaddress.ip_address(a) for a in ("0.0.0.0", "::", "10.0.0.1", "169.254.1.1", "fe80::1")]),
)
# Host names the fake resolver answers for; "127.1" and "2130706433" look numeric but are names to a URL.
HOST_NAMES = ("localhost", "comfy", "ollama.local", "gpu-box.example", "127.0.0.1.nip.io", "127.1", "2130706433",
              "my--host")  # fmt: skip
SCHEMES = ("http",) * 6 + ("HTTP", "Http", "https", "ftp", "file", "ws")  # mostly http, so connections happen
PROXY_VARIABLES = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
PROXIES = ("http://10.20.30.40:3128", "http://proxy.invalid:8080", "socks5://127.0.0.1:1080", "http://127.0.0.1:9")
LOOPBACK_ANSWERS = (ipaddress.ip_address("127.0.0.1"), LOOPBACK_V6, ipaddress.ip_address("127.9.8.7"))
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
LOCATIONS = ("http://203.0.113.5/next", "http://127.0.0.1:1/next", "/elsewhere", "https://example.com/")


@dataclass(frozen=True)
class LoopbackCase:
    url: str
    scheme: str  # as written
    host: str  # a name, or an address (an IPv6 one without brackets)
    literal: bool  # host is an address
    port: int | None
    answers: dict[str, tuple[str, ...] | None]  # the fake resolver: lower-case name -> addresses; None: fails
    proxies: dict[str, str]  # proxy variables set while the client runs
    status: int  # what the server answers: 200, or a redirect
    location: str

    @property
    def addresses(self) -> tuple[str, ...] | None:
        """What the host resolves to: itself for an address, the resolver's answer for a name."""
        return (self.host,) if self.literal else self.answers.get(self.host.lower())

    @property
    def reply(self) -> bytes:
        """The server's whole answer, as it arrives on the socket."""
        if self.status == 200:
            return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
        head = f"HTTP/1.1 {self.status} Moved\r\nLocation: {self.location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        return head.encode("ascii")


@st.composite
def loopback_cases(draw: st.DrawFn) -> LoopbackCase:
    scheme = draw(st.sampled_from(SCHEMES))
    if draw(st.booleans()):
        ip = draw(ADDRESSES)
        host, literal = str(ip), True
        netloc_host = f"[{host}]" if ip.version == 6 else host
    else:
        host, literal = draw(st.sampled_from(HOST_NAMES)), False
        netloc_host = draw(st.sampled_from((host, host.upper())))
    port = draw(st.none() | st.integers(1, 65535))
    netloc = netloc_host + (f":{port}" if port is not None else "")
    answers: dict[str, tuple[str, ...] | None] = {}
    if not literal:  # any resolver answer: a failure, none, some addresses, or loopback ones only
        found = draw(st.none() | st.lists(ADDRESSES, max_size=3) | st.lists(st.sampled_from(LOOPBACK_ANSWERS), min_size=1, max_size=2))
        answers[host.lower()] = None if found is None else tuple(str(address) for address in found)
    return LoopbackCase(
        url=f"{scheme}://{netloc}{draw(st.sampled_from(('', '/')))}",
        scheme=scheme,
        host=host,
        literal=literal,
        port=port,
        answers=answers,
        proxies=draw(st.dictionaries(st.sampled_from(PROXY_VARIABLES), st.sampled_from(PROXIES), max_size=3)),
        status=draw(st.sampled_from((200, 200, *REDIRECT_STATUSES))),
        location=draw(st.sampled_from(LOCATIONS)),
    )


# ---- Property 47: redirect_chains() ----

HF_CANARY = "hf_CanaryHuggingFace0123456789abcdefXY"
CIVITAI_CANARY = "CanaryCivitai0123456789abcdef4567"
CANARIES = {"HF_TOKEN": HF_CANARY, "CIVITAI_TOKEN": CIVITAI_CANARY}

HUGGING_FACE = ("huggingface.co", "cdn-lfs.huggingface.co", "cas-bridge.xethub.hf.co", "hf.co")
CIVITAI = ("civitai.com",)
CIVITAI_SUBDOMAINS = ("b2.civitai.com", "image.civitai.com")  # Civitai's own, but not civitai.com itself
LIBRARY = ("ollama.com", "registry.ollama.ai")
CDN = ("d2r9dsx7vu0i4o.cloudfront.net", "delivery.r2.cloudflarestorage.com", "objects.githubusercontent.com")
UNRELATED = ("evil.example", "huggingface.co.evil.example", "civitai.com.evil.example", "xhuggingface.co",
             "notcivitai.com")  # fmt: skip
HOSTS = HUGGING_FACE + CIVITAI + CIVITAI_SUBDOMAINS + LIBRARY + CDN + UNRELATED
# What st.sampled_from draws a chain's hosts from: the token services repeated, so a chain often starts at
# one and often stays on it for a hop.
CHAIN_HOSTS = HOSTS + ("huggingface.co", "civitai.com") * 5


def own_token(host: str) -> str | None:
    """The property's rule, stated on its own: which token may reach host."""
    if host in HUGGING_FACE:
        return "HF_TOKEN"
    if host in CIVITAI:
        return "CIVITAI_TOKEN"
    return None


@dataclass(frozen=True)
class RedirectChain:
    hosts: tuple[str, ...]  # the host of each request, in order; hop i + 1 is the redirect target of hop i
    answers: tuple[Incoming, ...]  # what the transport answers to request i
    url: str  # the first request's URL
    extra_hosts: tuple[str, ...]  # the Config_Lists hosts the client is also allowed to start at
    tokens: dict[str, str]  # the environment: the canary tokens that are set
    echo: int | None  # the hop whose redirect puts a token value into its Location, if any

    @property
    def requests(self) -> int:
        """How many requests the chain allows: up to the echoing hop's, whose Location is never followed."""
        return len(self.hosts) if self.echo is None else self.echo + 1


@st.composite
def redirect_chains(draw: st.DrawFn) -> RedirectChain:
    hosts = tuple(draw(st.lists(st.sampled_from(CHAIN_HOSTS), min_size=1, max_size=4)))
    tokens = draw(st.sampled_from((CANARIES, {"HF_TOKEN": HF_CANARY}, {"CIVITAI_TOKEN": CIVITAI_CANARY}, {})))
    echo = draw(st.none() | st.integers(0, len(hosts) - 2)) if len(hosts) > 1 else None
    answers = []
    for index in range(len(hosts) - 1):
        target = f"/hop{index + 1}/file.bin?X-Signature=s{index}"
        if hosts[index + 1] != hosts[index] or draw(st.booleans()):
            target = f"https://{hosts[index + 1]}{target}"
        if index == echo and tokens:
            value = draw(st.sampled_from(sorted(tokens.values())))
            target += draw(st.sampled_from((f"&t={value}", f"#{value}")))
        answers.append(Incoming(draw(st.sampled_from(REDIRECT_STATUSES)), {"location": target}))
    answers.append(Incoming(200, {"content-type": "application/json"}, b'{"size": 1}'))
    return RedirectChain(
        hosts=hosts,
        answers=tuple(answers),
        url=f"https://{hosts[0]}/api/models/amber/tree/main?recursive=1",
        extra_hosts=tuple(draw(st.lists(st.sampled_from(HOSTS), max_size=2))),
        tokens=dict(tokens),
        echo=echo if tokens else None,
    )


# ---- Property 40: influencer_lines() and allowlist_cases() ----

INSTAGRAM_IDS = ("influencer", "influencer-video")
OTHER_IDS = ("influencer-nsfw", "character-design", "amber", "krea2")
LORA_PREFIX = "models/loras/"


def _word() -> st.SearchStrategy[str]:
    return st.sampled_from(WORDS).flatmap(lambda w: st.sampled_from((w, w.upper(), w.capitalize())))


@st.composite
def _file(draw: st.DrawFn) -> str:
    parts = draw(st.lists(_word(), min_size=1, max_size=2))
    return draw(st.sampled_from(("-", "_"))).join(parts) + ".safetensors"


@dataclass(frozen=True)
class InfluencerLine:
    text: str  # the models.list line, active or commented out
    name: str | None  # the oracle: its path below models/loras/ when it joins the allowlist


@st.composite
def influencer_lines(draw: st.DrawFn) -> InfluencerLine:
    """A models.list line whose tag lists an Influencer_Id, or influencer-nsfw (an NSFW adapter under
    loras/nsfw), sometimes commented out."""
    nsfw = draw(st.integers(0, 4)) == 0
    filename = draw(_file())
    if nsfw:
        kind, subdir, ids = "lora", "loras/nsfw", ["influencer-nsfw"]
    else:
        kind = draw(st.sampled_from(("lora", "lora", "lora", "checkpoint", "vae", "diffusion")))
        folder = draw(st.lists(_word(), min_size=1, max_size=2).map("/".join))
        subdir = draw(st.sampled_from(("", f"loras/{folder}", folder)))
        ids = draw(st.lists(st.sampled_from(INSTAGRAM_IDS + OTHER_IDS[1:]), min_size=1, max_size=3, unique=True))
    tag = "wf:" + draw(st.sampled_from((",", ", "))).join(ids)
    active = draw(st.integers(0, 4)) != 0
    text = f"{kind}|{filename}|hf|amber/maple::{filename}|{subdir}|{tag}"
    if not active:
        text = draw(st.sampled_from(("#", "# "))) + text
    path = install_path(kind, filename, subdir)
    joins = active and any(ident in INSTAGRAM_IDS for ident in ids) and path.startswith(LORA_PREFIX)
    return InfluencerLine(text, path[len(LORA_PREFIX) :] if joins else None)


def character_qualifies(path: str) -> bool:
    """Req 21.7's rule for a Character_LoRA, stated on its own: relative, no `..` segment, and not under
    nsfw/ (first segment in any letter case)."""
    segments = path.split("/")
    real = [segment for segment in segments if segment not in ("", ".")]
    under_nsfw = len(real) > 1 and real[0].casefold() == "nsfw"
    return bool(path) and not path.startswith("/") and ".." not in segments and not under_nsfw


def near_misses(name: str) -> list[str]:
    """Names one step from name: another letter case, a `..` segment, an nsfw/ prefix, an extra slash."""
    first, _, rest = name.partition("/")
    return [
        name.swapcase(),
        name.upper(),
        name.capitalize(),
        f"../{name}",
        f"{first}/../{name}",
        f"nsfw/{name}",
        f"NSFW/{name}",
        f"/{name}",
        f"{name}/",
        f"{first}//{rest}" if rest else f"./{name}",
    ]


@dataclass(frozen=True)
class AllowlistCase:
    models_text: str
    persona: P.Persona  # personas(), with Character_LoRAs sometimes under nsfw/
    expected: frozenset[str]  # the oracle's allowlist
    entries: tuple[str | P.LoraRef, ...]  # look entries: allowlisted names, near misses and NSFW names
    split: int  # for the Persona check: entries[:split] go to looks.stills.instagram, the rest to video
    platform: str | None  # the Post's platform: an Instagram Post either way


def entry_name(entry: str | P.LoraRef) -> str:
    return entry.lora if isinstance(entry, P.LoraRef) else entry


@st.composite
def allowlist_cases(draw: st.DrawFn) -> AllowlistCase:
    config = draw(config_lists(max_lines=6))
    extra = draw(st.lists(influencer_lines(), min_size=1, max_size=8))
    models = list(draw(st.permutations(list(config.models) + [line.text for line in extra])))
    persona = draw(personas())
    characters = st.none() | st.builds(
        P.LoraRef,
        st.one_of(
            _file().map(lambda f: f"characters/{f}"),
            _file().map(lambda f: f"nsfw/{f}"),
            _file().map(lambda f: f"NSFW/{f}"),
            st.just(persona.character_loras.stills.lora if persona.character_loras.stills else "characters/me.safetensors"),
        ),
    )
    persona = dataclasses.replace(persona, character_loras=P.CharacterLoras(draw(characters), draw(characters)))
    refs = (persona.character_loras.stills, persona.character_loras.video)
    expected = frozenset({line.name for line in extra if line.name is not None}) | frozenset(
        ref.lora for ref in refs if ref is not None and character_qualifies(ref.lora)
    )
    known = sorted(expected) or ["looks/amber.safetensors"]
    allowed = st.sampled_from(sorted(expected)) if expected else st.nothing()
    near_miss = st.sampled_from(known).flatmap(lambda name: st.sampled_from(near_misses(name)))
    nsfw_names = [line.text.split("|")[1] for line in extra if "influencer-nsfw" in line.text]
    nsfw = st.sampled_from([f"nsfw/{name}" for name in nsfw_names] or ["nsfw/amber.safetensors"])
    strengths = st.floats(-P.LOOK_STRENGTH_LIMIT, P.LOOK_STRENGTH_LIMIT)

    def entry(names: st.SearchStrategy[str]) -> st.SearchStrategy[str | P.LoraRef]:
        return st.one_of(names, st.builds(P.LoraRef, names, strengths))  # a plain name, or any strength

    # mostly the design's mix; sometimes allowlisted names only, so lists that pass are common too
    mixed = st.lists(entry(st.one_of(allowed, near_miss, nsfw)), max_size=10)
    entries = tuple(draw(st.one_of(mixed, st.lists(entry(allowed), min_size=1, max_size=4))))
    return AllowlistCase(
        models_text="".join(f"{line}\n" for line in models),
        persona=persona,
        expected=expected,
        entries=entries,
        split=draw(st.integers(0, len(entries))),
        platform=draw(st.sampled_from((None, "instagram"))),
    )
