"""XML parsing for data that comes from a server or a repository (untrusted).

Python's parser does not fetch external entities, but it does expand internal ones, so a hostile document can
still exhaust memory ("billion laughs"). Polarion's configuration, SOAP and WebDAV answers never need a DTD, so any
document type declaration is refused before parsing.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

ParseError = ET.ParseError
_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
MAX_BYTES = 30 * 1024 * 1024


def fromstring(data: str | bytes) -> ET.Element:
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(raw) > MAX_BYTES:
        raise ET.ParseError("XML document is too large")
    if _DTD.search(raw):
        raise ET.ParseError("XML with a DOCTYPE or ENTITY declaration is not accepted")
    return ET.fromstring(raw)
