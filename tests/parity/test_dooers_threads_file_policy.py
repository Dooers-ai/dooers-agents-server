"""The fake's copy of Dooers Threads' file policy (``file-policy.ts`` and ``inspection.ts``, Dooers Threads 5466bde)."""

from __future__ import annotations

import pytest

from tests.parity import samples
from tests.parity.dooers_threads_file_policy import canonical_mime_type, dooers_threads_accepts_file, inspect_content


@pytest.mark.parametrize(
    "mime_type,filename,expected",
    [
        # Inspected extensions win over the declared type.
        ("text/html", "relatorio.pdf", "application/pdf"),
        ("image/jpg", "foto.JPEG", "image/jpeg"),
        ("application/octet-stream", "nota.txt", "text/plain"),
        # Generic declared type: the type of the extension.
        ("application/octet-stream", "dados.csv", "text/csv"),
        ("", "resposta.ogg", "audio/ogg"),
        ("audio/*", "recado.mp3", "audio/mpeg"),
        # Otherwise the declared type, base and lower case.
        ("Audio/OGG; codecs=opus", "resposta.ogg", "audio/ogg"),
        ("application/octet-stream", "sem-extensao", "application/octet-stream"),
    ],
)
def test_canonical_mime_type(mime_type, filename, expected):
    assert canonical_mime_type(mime_type, filename) == expected


@pytest.mark.parametrize(
    "mime_type,filename,data,expected",
    [
        ("image/png", "a.png", samples.png_bytes(), True),
        ("application/pdf", "a.pdf", samples.pdf_bytes(), True),
        ("audio/wav", "a.wav", samples.wav_bytes(), True),
        ("image/jpeg", "a.jpg", samples.jpeg_bytes(), True),
        ("text/plain", "a.txt", "olá".encode(), True),
        ("audio/ogg", "a.ogg", b"OggS\x00any", True),  # types without a structure check
        ("application/x-msdownload", "a.exe", b"MZ", False),
        ("image/png", "a.png", b"", False),
    ],
)
def test_accepts_file(mime_type, filename, data, expected):
    assert dooers_threads_accepts_file(mime_type, filename, data) is expected


@pytest.mark.parametrize(
    "filename,data",
    [
        ("a.png", b"\x89PNG\r\n\x1a\nparity-chart"),  # no IHDR, no IEND
        ("a.png", samples.png_bytes()[:-12]),  # no IEND
        ("a.png", samples.png_bytes() + b"x"),  # trailing bytes
        ("a.png", samples.png_bytes()[:30] + b"\x00" + samples.png_bytes()[31:]),  # bad CRC
        ("a.pdf", b"%PDF-1.4 parity-report"),
        ("a.pdf", samples.pdf_bytes().replace(b"trailer", b"trail")),
        ("a.pdf", samples.pdf_bytes().replace(b"endobj", b"endobj /JavaScript")),
        ("a.wav", samples.wav_bytes()[:-2]),
        ("a.jpg", samples.jpeg_bytes()[:-2]),
        ("a.txt", b"\xff\xfe"),
        ("a.txt", b"<html></html>"),
        ("a.txt", b"%PDF-1.4"),
        ("a.txt", b"nul\x00byte"),
        ("a/b.txt", b"ok"),
        (" a.txt", b"ok"),
        ("a" * 256 + ".txt", b"ok"),
    ],
)
def test_content_inspection_refuses(filename, data):
    # The declared type is generic: only the canonical type (from the extension) decides.
    assert dooers_threads_accepts_file("application/octet-stream", filename, data) is False
    with pytest.raises(ValueError):
        inspect_content(data, "application/octet-stream", filename)


def test_size_limits():
    assert dooers_threads_accepts_file("text/plain", "a.txt", b"x") is True
    assert dooers_threads_accepts_file("text/plain", "a.txt", b"x" * 26214401) is False
