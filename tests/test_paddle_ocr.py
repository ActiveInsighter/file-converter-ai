"""PaddleOCR job client: segmenting, polling, parsing, figures and failures."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import fitz
import httpx

from paddle_ocr import SEGMENT_ATTEMPTS, PaddleOcr, normalize_markdown, parse_tokens, plan_segments, tidy_inline_math

JOBS = "https://ocr.test/api/v2/ocr/jobs"


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

    def __init__(self, fates=None):
        self.fates = {key: list(value) for key, value in (fates or {}).items()}
        self.jobs = {}
        self.submissions = []
        self.tokens = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            self.tokens.append(request.headers["Authorization"])
            body = request.read()
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


class PaddleOcrTests(unittest.IsolatedAsyncioTestCase):
    async def run_ocr(self, service, pages=5, segment=2, tokens=("t1", "t2")):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        pdf = write_pdf(root / "source.pdf", pages)
        async with httpx.AsyncClient(transport=httpx.MockTransport(service.handler)) as client:
            ocr = PaddleOcr(list(tokens), client=client, work_dir=root / "state", job_url=JOBS,
                            segment_pages=segment, poll_seconds=0, retry_pause=0)
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

    def test_tokens_are_split_and_deduplicated(self):
        self.assertEqual(parse_tokens(" a\nb, a ;\n"), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
