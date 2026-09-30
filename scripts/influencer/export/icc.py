"""The fixed ICC v2 sRGB display profile every exported JPEG embeds (design §C8; Req 13.7, 14.4).

build() writes, in pure Python: the 128-byte header (version 2.1, class `mntr`, RGB data, XYZ PCS, the D50
illuminant), then the tags `desc`, `cprt`, `wtpt` (D50), the Bradford-adapted sRGB primaries `rXYZ`,
`gXYZ` and `bXYZ`, and one 1024-entry `curv` of the sRGB transfer function that `rTRC`, `gTRC` and `bTRC`
share. It takes no argument and writes no creation date and no profile ID, so its bytes never change and a
re-export stays byte-identical. SRGB_V2 holds them.

Standard library only; nothing here reads or writes a file.
"""
from __future__ import annotations

import struct

DESCRIPTION = "sRGB IEC61966-2.1 (influencer)"
COPYRIGHT = "No copyright, use freely"
CURVE_ENTRIES = 1024

D50 = (0.9642, 1.0, 0.8249)  # the PCS illuminant, as ICC.1 gives it
# The sRGB primaries (IEC 61966-2-1) adapted from D65 to D50 with the Bradford transform: the columns of
# the RGB to XYZ(D50) matrix.
RED_XYZ = (0.4360747, 0.2225045, 0.0139322)
GREEN_XYZ = (0.3850649, 0.7168786, 0.0971045)
BLUE_XYZ = (0.1430804, 0.0606169, 0.7141733)


def _s15f16(value: float) -> bytes:
    return struct.pack(">i", round(value * 65536))


def _xyz(xyz: tuple[float, float, float]) -> bytes:
    return b"XYZ " + bytes(4) + b"".join(_s15f16(v) for v in xyz)


def _desc(text: str) -> bytes:
    """A v2 textDescriptionType: the ASCII text with its NUL, no Unicode and no ScriptCode text."""
    ascii_text = text.encode("ascii") + b"\0"
    return (
        b"desc" + bytes(4) + struct.pack(">I", len(ascii_text)) + ascii_text
        + struct.pack(">II", 0, 0)  # Unicode language code and count
        + struct.pack(">HB", 0, 0) + bytes(67)  # ScriptCode code, count and its 67-byte field
    )  # fmt: skip


def _text(text: str) -> bytes:
    return b"text" + bytes(4) + text.encode("ascii") + b"\0"


def _srgb_to_linear(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _curve() -> bytes:
    values = [round(_srgb_to_linear(i / (CURVE_ENTRIES - 1)) * 65535) for i in range(CURVE_ENTRIES)]
    return b"curv" + bytes(4) + struct.pack(">I", CURVE_ENTRIES) + struct.pack(f">{CURVE_ENTRIES}H", *values)


def _pad(data: bytes) -> bytes:
    return data + bytes(-len(data) % 4)


def build() -> bytes:
    """The profile's bytes; the same on every call."""
    curve = _curve()
    elements: list[tuple[bytes, bytes]] = [
        (b"desc", _desc(DESCRIPTION)),
        (b"cprt", _text(COPYRIGHT)),
        (b"wtpt", _xyz(D50)),
        (b"rXYZ", _xyz(RED_XYZ)),
        (b"gXYZ", _xyz(GREEN_XYZ)),
        (b"bXYZ", _xyz(BLUE_XYZ)),
        (b"rTRC", curve),
        (b"gTRC", curve),  # shares rTRC's data
        (b"bTRC", curve),
    ]
    table_size = 4 + 12 * len(elements)
    offset = 128 + table_size
    entries, blobs, placed = [], [], {}
    for signature, data in elements:
        if data not in placed:
            placed[data] = offset
            blobs.append(_pad(data))
            offset += len(blobs[-1])
        entries.append(struct.pack(">4sII", signature, placed[data], len(data)))
    size = offset
    header = (
        struct.pack(">I", size)
        + bytes(4)  # preferred CMM: none
        + struct.pack(">I", 0x02100000)  # version 2.1.0
        + b"mntr" + b"RGB " + b"XYZ "
        + bytes(12)  # creation date: none, so the bytes never change
        + b"acsp"
        + bytes(4)  # primary platform
        + bytes(4)  # flags
        + bytes(4) + bytes(4)  # device manufacturer and model
        + bytes(8)  # device attributes
        + struct.pack(">I", 0)  # rendering intent: perceptual
        + b"".join(_s15f16(v) for v in D50)  # the PCS illuminant
        + bytes(4)  # creator
        + bytes(16)  # profile ID: none
        + bytes(28)  # reserved
    )  # fmt: skip
    assert len(header) == 128
    profile = header + struct.pack(">I", len(elements)) + b"".join(entries) + b"".join(blobs)
    assert len(profile) == size
    return profile


SRGB_V2 = build()
