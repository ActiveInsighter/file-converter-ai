"""Interrupted transfers must not publish or reuse truncated PDFs."""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from downloads import download_pdf


class FailingStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"%PDF-partial"
        raise httpx.RemoteProtocolError("connection interrupted")


@contextmanager
def response_context(response):
    yield response


class DownloadTests(unittest.TestCase):
    def test_interrupted_body_is_retried_and_atomically_replaced(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "source.pdf"
            destination.write_bytes(b"previous complete PDF")
            @contextmanager
            def stream(*args, **kwargs):
                self.assertEqual(destination.read_bytes(), b"previous complete PDF")
                calls.append(1)
                yield httpx.Response(200, request=httpx.Request("GET", "https://test/doc.pdf"),
                                     stream=FailingStream() if len(calls) == 1 else httpx.ByteStream(b"%PDF-complete"))
            with patch("downloads.httpx.stream", stream), patch("downloads.time.sleep"):
                download_pdf("https://test/doc.pdf", destination)
            self.assertEqual(len(calls), 2)
            self.assertEqual(destination.read_bytes(), b"%PDF-complete")
            self.assertFalse(destination.with_suffix(".pdf.part").exists())

    def test_unshared_or_missing_file_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = httpx.Response(403, request=httpx.Request("GET", "https://test/doc.pdf"))
            with patch("downloads.httpx.stream", return_value=response_context(response)) as request, self.assertRaises(httpx.HTTPStatusError):
                download_pdf("https://test/doc.pdf", Path(tmp) / "source.pdf")
            self.assertEqual(request.call_count, 1)

    def test_non_pdf_does_not_replace_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "source.pdf"
            destination.write_bytes(b"old")
            response = httpx.Response(200, content=b"<html>sign in</html>", request=httpx.Request("GET", "https://test/doc.pdf"))
            with patch("downloads.httpx.stream", return_value=response_context(response)), self.assertRaises(ValueError):
                download_pdf("https://test/doc.pdf", destination)
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(destination.with_suffix(".pdf.part").exists())
