"""Minimal valid files: they pass Maestro's content inspection (``maestro_file_policy.inspect_content``)."""

from __future__ import annotations

import struct
import zlib


def _png_chunk(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def png_bytes(*, width: int = 1, height: int = 1) -> bytes:
    """A real ``width`` x ``height`` 8-bit grayscale PNG (IHDR, one IDAT, IEND, valid CRCs)."""
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(raw)) + _png_chunk(b"IEND")


def pdf_bytes() -> bytes:
    """A minimal one-object PDF 1.4: header, ``obj``/``endobj``, ``trailer``, ``startxref`` and ``%%EOF``."""
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\nxref\n0 1\n0000000000 65535 f \n"
        b"trailer\n<< /Root 1 0 R /Size 2 >>\nstartxref\n9\n%%EOF\n"
    )


def wav_bytes(*, frames: int = 8) -> bytes:
    """A mono 8 kHz 16-bit PCM WAV of silence."""
    data = b"\x00\x00" * frames
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def jpeg_bytes() -> bytes:
    """A minimal baseline JPEG skeleton: SOI, SOF0 (1x1), SOS, EOI (what the inspection checks)."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, 1, 1, 1) + b"\x01\x11\x00"
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00" + b"\x00"
    return b"\xff\xd8" + sof + sos + b"\xff\xd9"
