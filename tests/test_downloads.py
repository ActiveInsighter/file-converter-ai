"""Interrupted transfers must not publish or reuse truncated PDFs."""
import tempfile
import threading
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


class RangedDownloadTests(unittest.TestCase):
    URL = "https://test/big.pdf"
    BODY = b"%PDF-" + bytes(range(256)) * 40

    def probe(self, length=None, **headers):
        length = len(self.BODY) if length is None else length
        headers = {"Content-Length": str(length), "Accept-Ranges": "bytes", **headers}
        return response_context(httpx.Response(
            200, headers=headers, request=httpx.Request("GET", self.URL),
            stream=httpx.ByteStream(b"")))

    def serve(self, body=None, honour=True, failures=()):
        body = self.BODY if body is None else body
        failures = list(failures)
        seen = {"peak": 0, "active": 0, "ranges": []}
        lock = threading.Lock()

        def handler(request):
            wanted = request.headers.get("Range")
            seen["ranges"].append(wanted)
            if failures:
                raise failures.pop(0)
            if not wanted or not honour:
                return httpx.Response(200, content=body)
            start, end = map(int, wanted.removeprefix("bytes=").split("-"))
            return httpx.Response(206, content=body[start:end + 1], headers={
                "Content-Range": f"bytes {start}-{end}/{len(body)}"})

        return httpx.Client(transport=httpx.MockTransport(handler)), seen

    def download(self, client, tmp, probe=None, whole=None):
        destination = Path(tmp) / "source.pdf"
        streams = [probe or self.probe()]
        if whole is not None:
            streams.append(response_context(httpx.Response(
                200, content=whole, request=httpx.Request("GET", self.URL))))
        with patch("downloads.httpx.stream", side_effect=streams) as stream, \
                patch("downloads.range_client", return_value=client), \
                patch("downloads.PARALLEL_MIN_BYTES", 1), \
                patch("downloads.RANGE_BLOCK_BYTES", 1000), \
                patch("downloads.time.sleep"):
            download_pdf(self.URL, destination)
        return destination, stream

    def test_a_big_file_is_fetched_as_ranges_and_reassembled_exactly(self):
        client, seen = self.serve()
        with tempfile.TemporaryDirectory() as tmp:
            destination, stream = self.download(client, tmp)
            self.assertEqual(destination.read_bytes(), self.BODY)
            self.assertFalse(destination.with_suffix(".pdf.part").exists())
        self.assertEqual(stream.call_count, 1, "only the first request; the body is never read whole")
        self.assertEqual(sorted(seen["ranges"]), sorted(
            f"bytes={start}-{min(len(self.BODY), start + 1000) - 1}"
            for start in range(0, len(self.BODY), 1000)))

    def test_a_validator_makes_a_changed_file_visible(self):
        client, seen = self.serve()
        sent = []
        original = client.stream

        def spy(method, url, headers=None, **kwargs):
            sent.append(dict(headers or {}))
            return original(method, url, headers=headers, **kwargs)

        client.stream = spy
        with tempfile.TemporaryDirectory() as tmp:
            self.download(client, tmp, probe=self.probe(ETag='"v1"'))
        self.assertTrue(sent and all(headers["If-Range"] == '"v1"' for headers in sent))

    def test_an_origin_that_ignores_ranges_falls_back_to_one_stream(self):
        client, _ = self.serve(honour=False)
        with tempfile.TemporaryDirectory() as tmp:
            destination, stream = self.download(client, tmp, whole=self.BODY)
            self.assertEqual(destination.read_bytes(), self.BODY)
        self.assertEqual(stream.call_count, 2)

    def test_a_block_that_fails_once_is_retried(self):
        client, seen = self.serve(failures=[httpx.ReadError("reset")])
        with tempfile.TemporaryDirectory() as tmp:
            destination, stream = self.download(client, tmp)
            self.assertEqual(destination.read_bytes(), self.BODY)
        self.assertEqual(stream.call_count, 1)
        self.assertGreater(len(seen["ranges"]), len(range(0, len(self.BODY), 1000)))

    def test_a_block_that_keeps_failing_falls_back_to_one_stream(self):
        client, _ = self.serve(failures=[httpx.ReadError("reset")] * 40)
        with tempfile.TemporaryDirectory() as tmp:
            destination, stream = self.download(client, tmp, whole=self.BODY)
            self.assertEqual(destination.read_bytes(), self.BODY)
        self.assertEqual(stream.call_count, 2)

    def test_small_compressed_or_unranged_responses_are_streamed_whole(self):
        from downloads import range_plan
        request = httpx.Request("GET", self.URL)
        big = 64 * 1024 * 1024
        plan = range_plan(httpx.Response(200, headers={"Content-Length": str(big), "Accept-Ranges": "bytes"},
                                         request=request))
        self.assertEqual(plan, (big, {}))
        for headers in ({"Content-Length": "1000", "Accept-Ranges": "bytes"},
                        {"Content-Length": str(big)},
                        {"Content-Length": str(big), "Accept-Ranges": "none"},
                        {"Content-Length": str(big), "Accept-Ranges": "bytes", "Content-Encoding": "gzip"},
                        {"Accept-Ranges": "bytes"}):
            self.assertIsNone(range_plan(httpx.Response(200, headers=headers, request=request)), headers)
