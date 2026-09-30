"""Fake ComfyUI_API and Ollama_API servers for the tests (task 3.3, design Testing Strategy, Req 27.2).

Both are threaded http.server instances on 127.0.0.1:0, driven by a script. The conftest fixtures
comfy_fake and ollama_fake start one per test and point COMFYUI_URL or OLLAMA_URL at it.
"""
from fakes._server import FakeServer, Request, Response, encode_multipart, http_request
from fakes.comfy import ComfyFake, Plan
from fakes.ollama import Answer, OllamaFake

__all__ = [
    "Answer",
    "ComfyFake",
    "FakeServer",
    "OllamaFake",
    "Plan",
    "Request",
    "Response",
    "encode_multipart",
    "http_request",
]
