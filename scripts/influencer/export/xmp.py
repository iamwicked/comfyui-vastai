"""The XMP packet that marks every exported image as AI-generated, and its insertion (design §C8; Req 13.8,
13.9).

insert(jpeg_bytes, packet) adds one APP1 segment, `http://ns.adobe.com/xap/1.0/\\0` followed by the
packet, directly after the JFIF APP0 segment, so the tool depends on no Pillow version's `xmp=` support.
DIGITAL_SOURCE_TYPE_PACKET, a module constant, sets Iptc4xmpExt:DigitalSourceType to the IPTC URI for
trainedAlgorithmicMedia, so every exported image carries the identical bytes.

Standard library only; nothing here reads or writes a file.
"""
from __future__ import annotations

import struct

XMP_NAMESPACE = b"http://ns.adobe.com/xap/1.0/\x00"
MAX_PAYLOAD = 65533  # a segment's length field is 2 bytes and counts itself
SOI = b"\xff\xd8"
APP0 = b"\xff\xe0"
APP1 = b"\xff\xe1"

TRAINED_ALGORITHMIC_MEDIA = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"

DIGITAL_SOURCE_TYPE_PACKET = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about=""\n'
    '    xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"\n'
    f'    Iptc4xmpExt:DigitalSourceType="{TRAINED_ALGORITHMIC_MEDIA}"/>\n'
    " </rdf:RDF>\n"
    "</x:xmpmeta>\n"
    '<?xpacket end="r"?>'
).encode("utf-8")


def segment(packet: bytes) -> bytes:
    """The APP1 segment of packet. A payload beyond MAX_PAYLOAD bytes raises ValueError."""
    payload = XMP_NAMESPACE + packet
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"the XMP payload is {len(payload)} bytes; one APP1 segment holds at most {MAX_PAYLOAD}")
    return APP1 + struct.pack(">H", len(payload) + 2) + payload


def insert(jpeg_bytes: bytes, packet: bytes = DIGITAL_SOURCE_TYPE_PACKET) -> bytes:
    """jpeg_bytes with segment(packet) directly after its APP0 segment. Bytes that do not start with SOI
    followed by a whole APP0 segment raise ValueError instead of being written."""
    data = bytes(jpeg_bytes)
    if data[:2] != SOI or data[2:4] != APP0 or len(data) < 6:
        raise ValueError("not a JFIF JPEG: the bytes do not start with SOI followed by an APP0 segment")
    (length,) = struct.unpack(">H", data[4:6])
    end = 4 + length
    if length < 2 or end > len(data):
        raise ValueError(f"the APP0 segment's length {length} runs past the end of the {len(data)} bytes")
    return data[:end] + segment(packet) + data[end:]
