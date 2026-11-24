"""Dooers Threads' file policy as the fake applies it (``file-policy.ts`` and ``inspection.ts``, Dooers Threads 5466bde).

What Dooers Threads does with every upload, person or agent:

- ``inferChatUploadKind``: the type matrix (the SDK's ``infer_chat_upload_kind``);
- ``canonicalMimeType``: the MIME Dooers Threads stores and serves, never the client's raw value;
- ``inspectContent``: the filename rules and the structure of text/plain, PDF, PNG, JPEG and WAV
  (the other accepted types only have size and SHA-256 checked);
- 1 byte to 25 MiB (``ARTIFACT_MAX_BYTES``).

``inspect_content`` raises ``ValueError`` where Dooers Threads throws ``ArtifactStoreError('invalid_content')``.
"""

from __future__ import annotations

import re
import struct
import unicodedata
import zlib

from dooers.agents.server.storage.chat_upload_file_policy import infer_chat_upload_kind

#: ``ARTIFACT_MAX_BYTES`` default of Dooers Threads (25 MiB, the production limit).
MAX_ARTIFACT_BYTES = 26214400

#: ``INSPECTED_EXTENSION_MIME``: the extension decides the stored type (and the inspection).
INSPECTED_EXTENSION_MIME = {
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".wav": "audio/wav",
}

#: ``EXTENSION_MIME`` (copied from ``file-policy.ts``): the stored type when the declared one is generic.
EXTENSION_MIME: dict[str, str] = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".3ds": "image/x-3ds",
    ".3gpp": "audio/3gpp",
    ".3gpp2": "audio/3gpp2",
    ".aac": "audio/x-aac",
    ".adp": "audio/adpcm",
    ".adts": "audio/aac",
    ".aif": "audio/x-aiff",
    ".aifc": "audio/x-aiff",
    ".aiff": "audio/x-aiff",
    ".ass": "audio/aac",
    ".au": "audio/basic",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".btif": "image/prs.btif",
    ".caf": "audio/x-caf",
    ".cgm": "image/cgm",
    ".cmx": "image/x-cmx",
    ".djv": "image/vnd.djvu",
    ".djvu": "image/vnd.djvu",
    ".dra": "audio/vnd.dra",
    ".dts": "audio/vnd.dts",
    ".dtshd": "audio/vnd.dts.hd",
    ".dwg": "image/vnd.dwg",
    ".dxf": "image/vnd.dxf",
    ".ecelp4800": "audio/vnd.nuera.ecelp4800",
    ".ecelp7470": "audio/vnd.nuera.ecelp7470",
    ".ecelp9600": "audio/vnd.nuera.ecelp9600",
    ".eol": "audio/vnd.digital-winds",
    ".fbs": "image/vnd.fastbidsheet",
    ".fh": "image/x-freehand",
    ".fh4": "image/x-freehand",
    ".fh5": "image/x-freehand",
    ".fh7": "image/x-freehand",
    ".fhc": "image/x-freehand",
    ".flac": "audio/x-flac",
    ".fpx": "image/vnd.fpx",
    ".fst": "image/vnd.fst",
    ".g3": "image/g3fax",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".ico": "image/x-icon",
    ".ief": "image/ief",
    ".jp2": "image/jp2",
    ".jpe": "image/jpeg",
    ".jxl": "image/jxl",
    ".kar": "audio/midi",
    ".ktx": "image/ktx",
    ".loas": "audio/aac",
    ".lvp": "audio/vnd.lucent.voice",
    ".m2a": "audio/mpeg",
    ".m3a": "audio/mpeg",
    ".m3u": "audio/x-mpegurl",
    ".m4a": "audio/mp4a-latm",
    ".m4p": "audio/mp4a-latm",
    ".mac": "image/x-macpaint",
    ".mdi": "image/vnd.ms-modi",
    ".mid": "audio/midi",
    ".midi": "audio/midi",
    ".mka": "audio/x-matroska",
    ".mmr": "image/vnd.fujixerox.edmics-mmr",
    ".mp2": "audio/mpeg",
    ".mp2a": "audio/mpeg",
    ".mp3": "audio/mpeg",
    ".mp4a": "audio/mp4",
    ".mpga": "audio/mpeg",
    ".npx": "image/vnd.net-fpx",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".pbm": "image/x-portable-bitmap",
    ".pct": "image/x-pict",
    ".pcx": "image/x-pcx",
    ".pgm": "image/x-portable-graymap",
    ".pic": "image/x-pict",
    ".pict": "image/pict",
    ".pnm": "image/x-portable-anymap",
    ".pnt": "image/x-macpaint",
    ".pntg": "image/x-macpaint",
    ".ppm": "image/x-portable-pixmap",
    ".psd": "image/vnd.adobe.photoshop",
    ".pya": "audio/vnd.ms-playready.media.pya",
    ".qti": "image/x-quicktime",
    ".qtif": "image/x-quicktime",
    ".ra": "audio/x-pn-realaudio",
    ".ram": "audio/x-pn-realaudio",
    ".ras": "image/x-cmu-raster",
    ".rgb": "image/x-rgb",
    ".rip": "audio/vnd.rip",
    ".rlc": "image/vnd.fujixerox.edmics-rlc",
    ".rmi": "audio/midi",
    ".rmp": "audio/x-pn-realaudio-plugin",
    ".s3m": "audio/s3m",
    ".sgi": "image/sgi",
    ".sid": "image/x-mrsid-image",
    ".sil": "audio/silk",
    ".snd": "audio/basic",
    ".spx": "audio/ogg",
    ".svg": "image/svg+xml",
    ".svgz": "image/svg+xml",
    ".tga": "image/x-tga",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".uva": "audio/vnd.dece.audio",
    ".uvg": "image/vnd.dece.graphic",
    ".uvi": "image/vnd.dece.graphic",
    ".uvva": "audio/vnd.dece.audio",
    ".uvvg": "image/vnd.dece.graphic",
    ".uvvi": "image/vnd.dece.graphic",
    ".wax": "audio/x-ms-wax",
    ".wbmp": "image/vnd.wap.wbmp",
    ".wdp": "image/vnd.ms-photo",
    ".weba": "audio/webm",
    ".webp": "image/webp",
    ".wma": "audio/x-ms-wma",
    ".xbm": "image/x-xbitmap",
    ".xif": "image/vnd.xiff",
    ".xm": "audio/xm",
    ".xpm": "image/x-xpixmap",
    ".xwd": "image/x-xwindowdump",
}


def _extension(filename: str) -> str:
    """``splitExt``: the last extension; a name of only dots before it has none."""
    separator, dot = filename.rfind("/"), filename.rfind(".")
    if dot <= separator or all(char == "." for char in filename[separator + 1 : dot]):
        return ""
    return filename[dot:]


def _base_mime_type(mime_type: str) -> str:
    return (mime_type or "").split(";", 1)[0].strip().lower()


def canonical_mime_type(mime_type: str, filename: str) -> str:
    """``canonicalMimeType``: the MIME Dooers Threads stores and serves for the upload."""
    extension = _extension(filename or "").lower()
    if extension in INSPECTED_EXTENSION_MIME:
        return INSPECTED_EXTENSION_MIME[extension]
    base = _base_mime_type(mime_type)
    if base in ("", "application/octet-stream") or "*" in base:
        return EXTENSION_MIME.get(extension, base)
    return base


def _invalid() -> ValueError:
    return ValueError("invalid_content")


_FORBIDDEN_NAME_CHARS = re.compile("[/\\\u202a-\u202e\u2066-\u2069]")
_PNG_OPTIONAL_CHUNKS = {"PLTE", "tEXt", "pHYs", "sRGB", "gAMA", "cHRM", "tRNS"}


def inspect_content(data: bytes, declared_mime_type: str, filename: str) -> None:
    """``inspectContent``: the filename rules and the structure of the canonical type."""
    try:
        filename.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid() from None
    if (
        not filename
        or filename != unicodedata.normalize("NFC", filename)
        or len(filename.encode("utf-8")) > 255
        or filename.strip() != filename
        or _FORBIDDEN_NAME_CHARS.search(filename)
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        or filename in (".", "..")
        or not data
        or infer_chat_upload_kind(declared_mime_type, filename) is None
    ):
        raise _invalid()
    mime_type = canonical_mime_type(declared_mime_type, filename)
    checks = {
        "text/plain": _inspect_text,
        "application/pdf": _inspect_pdf,
        "audio/wav": _inspect_wav,
        "image/png": _inspect_png,
        "image/jpeg": _inspect_jpeg,
    }
    if mime_type in checks:
        checks[mime_type](data)


def _inspect_text(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid() from None
    if any((ord(char) < 32 and ord(char) not in (9, 10, 13)) or ord(char) == 127 for char in text):
        raise _invalid()
    # Active content does not get text/plain by the client's declaration.
    if re.match(r"\s*(?:<|%PDF-|\{\\rtf)", text, re.IGNORECASE):
        raise _invalid()


def _inspect_pdf(data: bytes) -> None:
    text = data.decode("latin-1")
    if (
        not re.match(r"%PDF-1\.[0-7][\r\n]", text)
        or not re.search(r"%%EOF\s*\Z", text)
        or not re.search(r"\b\d+\s+\d+\s+obj\b", text)
        or not re.search(r"\bendobj\b", text)
        or not re.search(r"\btrailer\b", text)
        or not re.search(r"startxref\s+\d+", text)
        or re.search(r"/(?:JavaScript|JS|Launch|OpenAction|AA|EmbeddedFile|RichMedia|Encrypt)\b", text)
    ):
        raise _invalid()


def _inspect_wav(data: bytes) -> None:
    """RIFF/WAVE PCM integer, mono or stereo, 8 or 16 bits; bounded structural inspection."""
    if len(data) < 44 or data[:4] != b"RIFF" or struct.unpack_from("<I", data, 4)[0] != len(data) - 8 or data[8:12] != b"WAVE":
        raise _invalid()
    offset, chunks, block_align, have_format, have_data = 12, 0, 0, False, False
    while offset + 8 <= len(data) and chunks < 128:
        chunks += 1
        tag = data[offset : offset + 4]
        (length,) = struct.unpack_from("<I", data, offset + 4)
        offset += 8
        if length > len(data) - offset or offset + length + (length & 1) > len(data):
            raise _invalid()
        if tag == b"fmt ":
            if have_format or have_data or length != 16:
                raise _invalid()
            codec, channels, sample_rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", data, offset)
            if (
                codec != 1
                or channels not in (1, 2)
                or not 8000 <= sample_rate <= 192000
                or bits not in (8, 16)
                or align != channels * (bits // 8)
                or byte_rate != sample_rate * align
            ):
                raise _invalid()
            block_align, have_format = align, True
        elif tag == b"data":
            if not have_format or have_data or not length or length % block_align:
                raise _invalid()
            have_data = True
        elif tag != b"JUNK":
            raise _invalid()
        # Only zero-valued alignment padding, never a hidden payload.
        if length & 1 and data[offset + length] != 0:
            raise _invalid()
        offset += length + (length & 1)
    if offset != len(data) or not have_format or not have_data:
        raise _invalid()


def _inspect_png(data: bytes) -> None:
    if data[:8] != bytes.fromhex("89504e470d0a1a0a"):
        raise _invalid()
    offset, chunks, has_data, ended = 8, 0, False, False
    while offset + 12 <= len(data) and chunks < 4096:
        chunks += 1
        (length,) = struct.unpack_from(">I", data, offset)
        if length > len(data) - offset - 12:
            raise _invalid()
        kind = data[offset + 4 : offset + 8].decode("ascii", "replace")
        payload = data[offset + 8 : offset + 8 + length]
        if zlib.crc32(data[offset + 4 : offset + 8 + length]) != struct.unpack_from(">I", data, offset + 8 + length)[0]:
            raise _invalid()
        if chunks == 1 and kind != "IHDR":
            raise _invalid()
        if kind == "IHDR":
            width, height = struct.unpack_from(">II", payload, 0) if length == 13 else (0, 0)
            if (
                chunks != 1
                or length != 13
                or not width
                or not height
                or width * height > 16777216
                or payload[10] != 0
                or payload[11] != 0
                or payload[12] > 1
            ):
                raise _invalid()
        elif kind == "IDAT":
            has_data = True
        elif kind == "IEND":
            if length or not has_data:
                raise _invalid()
            ended = True
        elif kind not in _PNG_OPTIONAL_CHUNKS:
            raise _invalid()
        offset += length + 12
        if ended:
            break
    if not ended or offset != len(data):
        raise _invalid()


def _inspect_jpeg(data: bytes) -> None:
    if len(data) < 4 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        raise _invalid()
    offset, frame, scan, segments = 2, False, False, 0
    while offset < len(data) - 2 and segments < 4096:
        segments += 1
        if data[offset] != 0xFF:
            raise _invalid()
        marker = data[offset + 1]
        offset += 2
        if offset + 2 > len(data):
            raise _invalid()
        (length,) = struct.unpack_from(">H", data, offset)
        if length < 2 or offset + length > len(data) - 2:
            raise _invalid()
        if marker in (0xC0, 0xC2):
            if length < 8:
                raise _invalid()
            height, width = struct.unpack_from(">HH", data, offset + 3)
            if not width or not height or width * height > 16777216:
                raise _invalid()
            frame = True
        if marker == 0xDA:
            scan = True
            break
        if marker not in (0xC0, 0xC2, 0xC4, 0xDB, 0xDD) and not 0xE0 <= marker <= 0xEF:
            raise _invalid()
        offset += length
    if not frame or not scan:
        raise _invalid()


def dooers_threads_accepts_file(mime_type: str, filename: str, data: bytes) -> bool:
    """Dooers Threads' file policy: the type matrix, 1 byte to 25 MiB and the content inspection."""
    if not 0 < len(data) <= MAX_ARTIFACT_BYTES:
        return False
    try:
        inspect_content(data, mime_type, filename)
    except ValueError:
        return False
    return True
