"""PaddleOCR job client: segmenting, polling, parsing, figures and failures."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import fitz
import httpx

from paddle_ocr import (
    SEGMENT_ATTEMPTS,
    PaddleOcr,
    extract_segment,
    link_submit_timeout,
    normalize_markdown,
    parse_tokens,
    plan_segments,
    ramp_segments,
    tidy_inline_math,
)

JOBS = "https://ocr.test/api/v2/ocr/jobs"
RELAY = "https://relay.test/api/file-conversion/relay"


def write_pdf(path: Path, pages: int) -> Path:
    with fitz.open() as document:
        for number in range(1, pages + 1):
            document.new_page().insert_text((72, 72), f"Page {number}")
        document.save(path)
    return path


def page_result(text: str, images: dict | None = None) -> dict:
    return {"markdown": {"text": text, "images": images or {}}, "outputImages": {}}


class FakeService:
    """In-memory AI Studio job API.

    ``fates`` maps the first page text of a segment to the outcomes of its
    successive submissions; anything not listed succeeds.
    """

    def __init__(self, fates=None, accepted_links=(), source: Path | None = None):
        self.fates = {key: list(value) for key, value in (fates or {}).items()}
        self.accepted_links = set(accepted_links)
        self.source = source
        self.jobs = {}
        self.submissions = []
        self.links = []
        self.tokens = []
        #: Relay objects by public link, and the status the relay answers with.
        self.relayed = {}
        self.relay_status = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "PUT" and url == RELAY:
            assert request.headers["Authorization"] == "Bearer relay-secret"
            status = self.relay_status.pop(0) if self.relay_status else 201
            if status != 201:
                return httpx.Response(status, json={"message": "no"})
            link = f"https://relay-files.test/{len(self.relayed) + 1}.pdf"
            self.relayed[link] = request.read()
            return httpx.Response(201, json={"sourceUrl": link})
        if request.method == "POST":
            self.tokens.append(request.headers["Authorization"])
            body = request.read()
            if request.headers["Content-Type"] == "application/json":
                payload = json.loads(body)
                self.links.append((payload["fileUrl"], payload["pageRanges"]))
                relayed = self.relayed.get(payload["fileUrl"])
                if payload["fileUrl"] not in self.accepted_links and relayed is None:
                    return httpx.Response(400, json={"code": 10004, "msg": "unsupported format"})
                first, last = map(int, payload["pageRanges"].split("-"))
                with (fitz.open(stream=relayed, filetype="pdf") if relayed
                      else fitz.open(self.source)) as document:
                    texts = [document[n - 1].get_text().strip() for n in range(first, last + 1)]
            else:
                with fitz.open(stream=body[body.index(b"%PDF"):], filetype="pdf") as document:
                    texts = [page.get_text().strip() for page in document]
            self.submissions.append(texts)
            queue = self.fates.get(texts[0]) or self.fates.get("*") or []
            fate = queue.pop(0) if queue else "ok"
            if fate == "throttle":
                return httpx.Response(429, json={"code": 12002, "msg": "Request frequency too high"})
            if fate == "quota":
                return httpx.Response(403, json={"code": 12001, "msg": "Daily page limit reached"})
            job = f"job-{len(self.submissions)}"
            self.jobs[job] = (fate, texts, 0, request.headers["Authorization"])
            return httpx.Response(200, json={"code": 0, "data": {"jobId": job}})
        if url.startswith(JOBS + "/"):
            job = url.rsplit("/", 1)[1]
            fate, texts, polls, owner = self.jobs[job]
            if request.headers["Authorization"] != owner:
                return httpx.Response(404, json={"code": 11001, "msg": "jobId does not exist"})
            self.jobs[job] = (fate, texts, polls + 1, owner)
            if polls == 0:
                return httpx.Response(200, json={"code": 0, "data": {"state": "running"}})
            if fate == "fail":
                return httpx.Response(200, json={"code": 0, "data": {"state": "failed", "errorMsg": "boom"}})
            return httpx.Response(200, json={"code": 0, "data": {
                "state": "done", "resultUrl": {"jsonUrl": f"https://store.test/{job}.json"}}})
        if url.startswith("https://store.test/job-"):
            job = url.rsplit("/", 1)[1].removesuffix(".json")
            fate, texts, _, _ = self.jobs[job]
            if fate == "short":
                texts = texts[:-1]
            pages = [page_result(
                f'<div style="text-align: center;"><img src="imgs/fig.jpg" alt="Image" /></div>\n\n{text}  $ x $',
                {"imgs/fig.jpg": "https://store.test/fig.jpg"},
            ) for text in texts]
            # Two JSONL lines: results may be split across lines.
            lines = [json.dumps({"result": {"layoutParsingResults": pages[:1]}}),
                     json.dumps({"result": {"layoutParsingResults": pages[1:]}})]
            return httpx.Response(200, text="\n".join(lines) + "\n")
        if url == "https://store.test/fig.jpg":
            return httpx.Response(200, content=b"jpeg-bytes")
        return httpx.Response(404)


class SegmentTests(unittest.TestCase):
    def test_scanned_pages_are_reencoded_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "scan.pdf"
            with fitz.open() as text, fitz.open() as document:
                source = text.new_page()
                for line in range(60):
                    source.insert_text((40, 30 + line * 13), f"Line {line} " * 12, fontsize=9)
                # A 300 DPI scan stored as a high-quality JPEG, like a scanned book.
                scan = source.get_pixmap(dpi=300).tobytes("jpeg", jpg_quality=95)
                for _ in range(2):
                    document.new_page().insert_image(fitz.Rect(0, 0, 595, 842), stream=scan)
                document.save(pdf)
            original = len(fitz.open(pdf).tobytes(garbage=3, deflate=True))
            compact = extract_segment(pdf, 1, 2)
            text_pdf = write_pdf(Path(directory) / "text.pdf", 2)
            text_segment = extract_segment(text_pdf, 1, 2)
        self.assertLess(len(compact), original / 2)
        with fitz.open(stream=compact, filetype="pdf") as document:
            self.assertEqual(document.page_count, 2)
            self.assertEqual(document[0].rect, fitz.Rect(0, 0, 595, 842))
        with fitz.open(stream=text_segment, filetype="pdf") as document:
            self.assertEqual(document[0].get_text().strip(), "Page 1", "vector pages keep their text")


class PaddleOcrTests(unittest.IsolatedAsyncioTestCase):
    async def run_ocr(self, service, pages=5, segment=2, tokens=("t1", "t2"), source_url=None,
                      relay=False, make_pdf=write_pdf, **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        pdf = make_pdf(root / "source.pdf", pages)
        service.source = pdf
        async with httpx.AsyncClient(transport=httpx.MockTransport(service.handler)) as client:
            ocr = PaddleOcr(list(tokens), client=client, work_dir=root / "state", job_url=JOBS,
                            segment_pages=segment, poll_seconds=0, retry_pause=0,
                            source_url=source_url,
                            relay_url=RELAY if relay else None,
                            relay_token="relay-secret" if relay else None, **options)
            task = ocr.start(pdf, 1, pages, 3)
            results = {number: await ocr.page(number) for number in range(1, pages + 1)}
            if task is not None:
                await task
        return ocr, results, root

    async def test_every_page_gets_its_own_draft_and_figure(self):
        service = FakeService()
        ocr, results, root = await self.run_ocr(service)
        self.assertEqual(sorted(len(s) for s in service.submissions), [1, 2, 2])
        for number, page in results.items():
            self.assertEqual(page.markdown, f"![](images/p{number:03d}-1.jpg)\n\nPage {number} $x$")
            figure = page.figures[f"images/p{number:03d}-1.jpg"]
            self.assertEqual(figure.read_bytes(), b"jpeg-bytes")
            self.assertEqual(figure.parent, root / "state" / "figures")
        self.assertEqual(ocr.stats["pages"], 5)
        self.assertEqual(set(service.tokens), {"bearer t1", "bearer t2"}, "tokens rotate")

    async def test_large_segments_are_halved_until_uploads_are_small(self):
        from unittest.mock import patch
        service = FakeService()
        with patch("paddle_ocr.TARGET_UPLOAD_BYTES", 1):
            _, results, _ = await self.run_ocr(service, pages=4, segment=4)
        self.assertEqual(sorted(len(s) for s in service.submissions), [1, 1, 1, 1])
        self.assertEqual([page.markdown.split()[-2] for page in results.values()], ["1", "2", "3", "4"])

    async def test_throttled_and_failed_jobs_are_retried(self):
        service = FakeService({"Page 1": ["throttle", "fail"]})
        _, results, _ = await self.run_ocr(service, pages=2, segment=2)
        self.assertEqual(len(service.submissions), 3)
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_hopeless_segment_falls_back_to_vision_only(self):
        service = FakeService({"Page 1": ["short"] * SEGMENT_ATTEMPTS})
        ocr, results, _ = await self.run_ocr(service, pages=3, segment=2)
        self.assertIsNone(results[1])
        self.assertIsNone(results[2])
        self.assertIsNotNone(results[3])
        self.assertEqual(ocr.stats["failed_pages"], 2)

    async def test_daily_quota_stops_all_further_jobs(self):
        service = FakeService({"*": ["quota"] * 10})
        ocr, results, _ = await self.run_ocr(service, pages=6, segment=2)
        self.assertTrue(all(page is None for page in results.values()))
        self.assertIn("12001", ocr.fatal)
        self.assertLessEqual(len(service.submissions), 3, "no retries once the quota is gone")

    async def test_the_service_fetches_the_link_one_page_range_per_job(self):
        link = "https://files.test/book.pdf"
        service = FakeService(accepted_links={link})
        ocr, results, _ = await self.run_ocr(service, pages=5, segment=2, source_url=link)
        self.assertEqual(sorted(service.links), [(link, "1-2"), (link, "3-4"), (link, "5-5")])
        self.assertEqual([page.markdown.split()[-2] for page in results.values()],
                         ["1", "2", "3", "4", "5"])
        self.assertEqual((ocr.stats["remote_jobs"], ocr.stats["uploaded_jobs"]), (3, 0))

    async def test_a_link_the_service_rejects_falls_back_to_uploads(self):
        link = "https://arxiv.test/pdf/2401.00001"
        service = FakeService()
        ocr, results, _ = await self.run_ocr(service, pages=4, segment=2, source_url=link)
        self.assertEqual(len(service.links), 1, "the link is offered once, then segments upload")
        self.assertEqual((ocr.stats["remote_jobs"], ocr.stats["uploaded_jobs"]), (0, 2))
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_once_the_link_works_segments_are_submitted_concurrently(self):
        link = "https://files.test/book.pdf"
        service = FakeService(accepted_links={link})
        respond, in_flight, peak = service.handler, [0], [0]

        async def handler(request):
            if request.method != "POST":
                return respond(request)
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            await asyncio.sleep(0.05)  # the service downloading the file
            in_flight[0] -= 1
            return respond(request)

        service.handler = handler
        ocr, results, _ = await self.run_ocr(service, pages=7, segment=2, source_url=link)
        self.assertEqual(ocr.stats["remote_jobs"], 4)
        self.assertGreaterEqual(peak[0], 2, "only the probe is serialised")
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_link_submission_that_times_out_switches_to_uploads_at_once(self):
        link = "https://slow.test/manual.pdf"
        service = FakeService(accepted_links={link})
        respond, hangs = service.handler, []

        def handler(request):
            if request.method == "POST" and request.headers["Content-Type"] == "application/json":
                hangs.append(request)
                raise httpx.ReadTimeout("origin download too slow", request=request)
            return respond(request)

        service.handler = handler
        ocr, results, _ = await self.run_ocr(service, pages=6, segment=2, source_url=link)
        self.assertEqual(len(hangs), 1, "one probe, no retries on the link")
        self.assertEqual((ocr.stats["remote_jobs"], ocr.stats["uploaded_jobs"]), (0, 3))
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_link_submissions_get_time_for_the_whole_file(self):
        link = "https://files.test/book.pdf"
        service = FakeService(accepted_links={link})
        timeouts, respond = [], service.handler

        def handler(request):
            if request.method == "POST":
                timeouts.append(request.extensions["timeout"]["read"])
            return respond(request)

        service.handler = handler
        from unittest.mock import patch
        with patch("paddle_ocr.link_submit_timeout", return_value=123.0) as scaled:
            ocr, _, root = await self.run_ocr(service, pages=2, segment=2, source_url=link)
        scaled.assert_called_once_with((root / "source.pdf").stat().st_size)
        self.assertEqual(timeouts, [123.0])
        self.assertEqual(link_submit_timeout(1_000_000), 60.0)
        self.assertEqual(link_submit_timeout(120_000_000), 150.0)
        self.assertEqual(link_submit_timeout(2_000_000_000), 300.0)

    async def test_files_above_the_link_limit_are_uploaded_without_a_probe(self):
        from unittest.mock import patch
        link = "https://files.test/huge.pdf"
        service = FakeService(accepted_links={link})
        with patch("paddle_ocr.REMOTE_MAX_BYTES", 10):
            ocr, results, _ = await self.run_ocr(service, pages=4, segment=2, source_url=link)
        self.assertEqual(service.links, [])
        self.assertEqual((ocr.stats["remote_jobs"], ocr.stats["uploaded_jobs"]), (0, 2))
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_large_sources_go_through_the_relay_segment_by_segment(self):
        from unittest.mock import patch
        link = "https://files.test/book.pdf"
        service = FakeService(accepted_links={link})
        with patch("paddle_ocr.RELAY_PREFERRED_ABOVE_BYTES", 0):
            ocr, results, _ = await self.run_ocr(service, pages=5, segment=2, source_url=link,
                                                 relay=True)
        self.assertNotIn(link, [url for url, _ in service.links], "the big file is never fetched whole")
        self.assertEqual(sorted(ranges for _, ranges in service.links), ["1-1", "1-2", "1-2"])
        self.assertEqual((ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]), (3, 0))
        self.assertEqual([page.markdown.split()[-2] for page in results.values()],
                         ["1", "2", "3", "4", "5"])

    async def test_an_unfetchable_link_falls_back_to_the_relay(self):
        service = FakeService()
        ocr, results, _ = await self.run_ocr(service, pages=4, segment=2,
                                             source_url="https://arxiv.test/pdf/1", relay=True)
        self.assertEqual((ocr.stats["remote_jobs"], ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]),
                         (0, 2, 0))
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_busy_relay_is_retried_and_a_refusing_one_is_dropped(self):
        busy = FakeService()
        busy.relay_status = [503]
        ocr, results, _ = await self.run_ocr(busy, pages=2, segment=2, relay=True)
        self.assertEqual((ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]), (1, 0))
        refusing = FakeService()
        refusing.relay_status = [401, 401]
        ocr, results, _ = await self.run_ocr(refusing, pages=4, segment=2, relay=True)
        self.assertIsNone(ocr.relay_url)
        self.assertEqual((ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]), (0, 2))
        self.assertTrue(all(page is not None for page in results.values()))

    @staticmethod
    def first_page_of(service, url):
        with fitz.open(stream=service.relayed[url], filetype="pdf") as document:
            return document[0].get_text().strip()

    async def test_a_fetch_timeout_is_retried_on_the_link_already_on_the_relay(self):
        # A single HTTP 408 used to drop the relay for the rest of the run and
        # upload every remaining segment at 20-25 KB/s.
        service = FakeService()
        respond, refused = service.handler, []

        def handler(request):
            if (request.method == "POST" and request.headers["Content-Type"] == "application/json"
                    and not refused):
                refused.append(request)
                return httpx.Response(408, json={"code": 408, "msg": "Request Timeout"})
            return respond(request)

        service.handler = handler
        ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, relay=True)
        self.assertEqual(len(service.relayed), 1, "the segment is not uploaded to the relay twice")
        self.assertEqual((ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]), (1, 0))
        self.assertIsNotNone(ocr.relay_url)
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_silent_service_is_retried_on_the_relay_instead_of_dropping_it(self):
        service = FakeService()
        respond, silent = service.handler, []

        def handler(request):
            if (request.method == "POST" and request.headers["Content-Type"] == "application/json"
                    and not silent):
                silent.append(request)
                raise httpx.ReadTimeout("no answer", request=request)
            return respond(request)

        service.handler = handler
        ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, relay=True)
        self.assertEqual((ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]), (1, 0))
        self.assertIsNotNone(ocr.relay_url)
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_segment_the_service_cannot_fetch_is_uploaded_alone(self):
        service = FakeService()
        respond = service.handler

        def handler(request):
            if request.method == "POST" and request.headers["Content-Type"] == "application/json":
                link = json.loads(request.read())["fileUrl"]
                if self.first_page_of(service, link) == "Page 1":
                    return httpx.Response(408, json={"code": 408, "msg": "Request Timeout"})
            return respond(request)

        service.handler = handler
        ocr, results, _ = await self.run_ocr(service, pages=4, segment=2, relay=True)
        self.assertEqual((ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]), (1, 1))
        self.assertIsNotNone(ocr.relay_url, "the relay stays in use for the other segments")
        self.assertEqual([page.markdown.split()[-2] for page in results.values()],
                         ["1", "2", "3", "4"])

    def stalling(self, service, stalls):
        """The first ``stalls`` jobs never finish."""
        respond, submitted = service.handler, []

        def handler(request):
            url = str(request.url)
            if request.method == "POST" and request.headers["Content-Type"] == "application/json":
                response = respond(request)
                submitted.append(response.json()["data"]["jobId"])
                return response
            if url.startswith(JOBS + "/") and url.rsplit("/", 1)[1] in submitted[:stalls]:
                return httpx.Response(200, json={"code": 0, "data": {"state": "running"}})
            return respond(request)

        service.handler = handler

    async def test_a_stalled_relay_job_is_submitted_again_on_the_same_link(self):
        # Run 133: one OCR job did not answer for 5 minutes, the relay was dropped
        # for the whole run and 8 small uploads at ~15 KB/s cost 4 more minutes.
        from unittest.mock import patch
        service = FakeService()
        self.stalling(service, 1)
        with patch("paddle_ocr.RELAY_JOB_TIMEOUT", 0.2):
            ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, relay=True)
        self.assertEqual(len(service.relayed), 1, "the segment is not uploaded to the relay again")
        self.assertEqual((ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]), (2, 0))
        self.assertIsNotNone(ocr.relay_url)
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_failed_relay_job_does_not_drop_the_relay(self):
        service = FakeService({"Page 1": ["fail"]})
        ocr, results, _ = await self.run_ocr(service, pages=4, segment=2, relay=True)
        self.assertIsNotNone(ocr.relay_url)
        self.assertEqual(ocr.stats["uploaded_jobs"], 0)
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_segment_whose_relay_jobs_keep_stalling_is_uploaded_alone(self):
        from unittest.mock import patch
        service = FakeService()
        self.stalling(service, 3)
        with patch("paddle_ocr.RELAY_JOB_TIMEOUT", 0.1):
            ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, relay=True)
        self.assertEqual((ocr.stats["relay_jobs"], ocr.stats["uploaded_jobs"]), (3, 1))
        self.assertIsNotNone(ocr.relay_url, "later segments keep using the relay")
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_the_next_segment_is_on_the_relay_before_the_running_job_finishes(self):
        service = FakeService()
        respond, events = service.handler, []

        def handler(request):
            url = str(request.url)
            if request.method == "PUT":
                events.append("put")
            elif url.startswith(JOBS + "/"):
                response = respond(request)
                if response.json()["data"]["state"] == "done":
                    events.append("done")
                return response
            return respond(request)

        service.handler = handler
        await self.run_ocr(service, pages=3, segment=1, relay=True, parallel_jobs=1)
        self.assertEqual(events.count("put"), 3)
        self.assertLess(events.index("put", events.index("put") + 1), events.index("done"),
                        "segment 2 was staged while job 1 was still running")

    async def test_heavy_scans_are_reencoded_before_they_go_on_the_relay(self):
        def scanned(path, pages):
            with fitz.open() as text, fitz.open() as document:
                source = text.new_page()
                for line in range(60):
                    source.insert_text((40, 30 + line * 13), f"Line {line} " * 12, fontsize=9)
                scan = source.get_pixmap(dpi=300).tobytes("jpeg", jpg_quality=95)
                for _ in range(pages):
                    document.new_page().insert_image(fitz.Rect(0, 0, 595, 842), stream=scan)
                document.save(path)
            return path

        sizes = {}
        for dpi in (150, 0):
            service = FakeService()
            ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, relay=True,
                                                 make_pdf=scanned, relay_compact_dpi=dpi)
            self.assertTrue(all(page is not None for page in results.values()))
            sizes[dpi] = len(next(iter(service.relayed.values())))
        self.assertLess(sizes[150], sizes[0] / 2)

    async def test_reencoding_still_works_when_worker_processes_are_unavailable(self):
        from unittest.mock import patch
        service = FakeService()
        with patch("paddle_ocr.ProcessPoolExecutor", side_effect=OSError("no processes")), \
                patch("paddle_ocr.COMPACT_ABOVE_BYTES_PER_PAGE", 1):
            ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, relay=True)
        self.assertIs(ocr.cutters, False)
        self.assertEqual(ocr.stats["relay_jobs"], 1)
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_full_queue_is_retried_on_the_same_link(self):
        link = "https://files.test/book.pdf"
        service = FakeService(accepted_links={link})
        respond, busy = service.handler, []

        def handler(request):
            if request.method == "POST" and not busy:
                busy.append(request)
                return httpx.Response(200, json={"code": 10010, "msg": "queue full"})
            return respond(request)

        service.handler = handler
        ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, source_url=link)
        self.assertEqual((ocr.stats["remote_jobs"], ocr.stats["uploaded_jobs"]), (1, 0))
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_stalled_link_job_is_replaced_by_an_upload(self):
        from unittest.mock import patch
        link = "https://files.test/book.pdf"
        service = FakeService(accepted_links={link})
        with patch("paddle_ocr.REMOTE_JOB_TIMEOUT", 0):
            ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, source_url=link)
        self.assertEqual((ocr.stats["remote_jobs"], ocr.stats["uploaded_jobs"]), (1, 1))
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_a_failed_link_job_switches_the_run_to_uploads(self):
        link = "https://files.test/book.pdf"
        service = FakeService({"Page 1": ["fail"]}, accepted_links={link})
        ocr, results, _ = await self.run_ocr(service, pages=2, segment=2, source_url=link)
        self.assertEqual((ocr.stats["remote_jobs"], ocr.stats["uploaded_jobs"]), (1, 1))
        self.assertTrue(all(page is not None for page in results.values()))

    async def test_disabled_ocr_resolves_every_page_to_none(self):
        service = FakeService()
        ocr, results, _ = await self.run_ocr(service, pages=2, tokens=())
        self.assertFalse(ocr.enabled)
        self.assertEqual(service.submissions, [])
        self.assertEqual(set(results.values()), {None})

    async def test_cancelling_the_run_releases_waiting_pages(self):
        async def stall(_request):
            await asyncio.sleep(10)

        with tempfile.TemporaryDirectory() as directory:
            pdf = write_pdf(Path(directory) / "s.pdf", 2)
            async with httpx.AsyncClient(transport=httpx.MockTransport(stall)) as client:
                ocr = PaddleOcr(["t"], client=client, work_dir=Path(directory), job_url=JOBS)
                task = ocr.start(pdf, 1, 2, 3)
                await asyncio.sleep(0.05)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertIsNone(await asyncio.wait_for(ocr.page(1), 1))


class MarkdownNormalizationTests(unittest.TestCase):
    def test_html_wrappers_become_plain_markdown(self):
        text = ('<div style="text-align: center;"><img src="imgs/a.jpg" alt="Image" width="36%" /></div>\n\n\n'
                '<div style="text-align: center;">Figure 1: X</div>\n\n $$  A=B $$ \n')
        self.assertEqual(normalize_markdown(text, {"imgs/a.jpg": "images/p001-1.jpg"}),
                         "![](images/p001-1.jpg)\n\nFigure 1: X\n\n$$\nA=B\n$$")

    def test_figures_that_failed_to_download_are_dropped(self):
        self.assertEqual(normalize_markdown('a <img src="imgs/x.jpg" /> b', {}), "a  b")

    def test_tables_are_left_for_the_model(self):
        table = "<table><tr><td>1</td></tr></table>"
        self.assertEqual(normalize_markdown(table, {}), table)

    def test_inline_math_padding_is_removed_without_pairing_across_text(self):
        line = "of $N = 6$ identical, is  $ \\mathrm{L}(x) $ where  $ d_k $, $$ y $$ costs \\$5"
        self.assertEqual(tidy_inline_math(line),
                         "of $N = 6$ identical, is $\\mathrm{L}(x)$ where $d_k$, $$ y $$ costs \\$5")
        self.assertEqual(tidy_inline_math("odd $ count"), "odd $ count")


class HelperTests(unittest.TestCase):
    def test_segments_cover_the_range(self):
        self.assertEqual(plan_segments(3, 9, 3), [(3, 5), (6, 8), (9, 9)])

    def test_the_first_segments_are_small_so_the_model_starts_early(self):
        self.assertEqual(ramp_segments(1, 100, 25),
                         [(1, 5), (6, 15), (16, 30), (31, 55), (56, 80), (81, 100)])
        self.assertEqual(ramp_segments(1, 8, 25), [(1, 5), (6, 8)])
        self.assertEqual(ramp_segments(1, 3, 25), [(1, 3)])
        self.assertEqual(ramp_segments(3, 9, 6), [(3, 7), (8, 9)])
        self.assertEqual(ramp_segments(1, 40, 4), plan_segments(1, 40, 4), "no ramp for small segments")

    def test_tokens_are_split_and_deduplicated(self):
        self.assertEqual(parse_tokens(" a\nb, a ;\n"), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
