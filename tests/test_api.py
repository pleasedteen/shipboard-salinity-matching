"""HTTP interface tests, exercised over a real socket."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import FIXTURES, FIXTURE_FILES, ServiceTestCase

from app.api import make_server

PAGED = FIXTURES / "paged_deck"


class ApiTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = make_server("127.0.0.1", 0, self.service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def call(self, method: str, path: str, body=None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def post_fixtures(self) -> tuple[int, dict]:
        materials = [
            {"name": name, "kind": kind, "content": (FIXTURES / name).read_text("utf-8")}
            for name, kind in FIXTURE_FILES
        ]
        return self.call("POST", "/api/materials", {"voyage": "KX-26-03",
                                                    "materials": materials})

    def test_full_review_cycle_over_http(self) -> None:
        status, body = self.post_fixtures()
        self.assertEqual(status, 201)
        self.assertTrue(all(item["created"] for item in body["items"]))

        status, batches = self.call("GET", "/api/batches?voyage=KX-26-03")
        self.assertEqual(status, 200)
        by_code = {b["batch_code"]: b for b in batches["items"]}
        self.assertEqual(by_code["KX-2603-A"]["status"], "published")
        self.assertEqual(by_code["KX-2603-E"]["status"], "held")

        status, samples = self.call("GET", "/api/samples?voyage=KX-26-03")
        self.assertEqual(status, 200)
        self.assertGreater(samples["total"], 0)
        self.assertTrue(all(item["published"] for item in samples["items"]))

        status, queue = self.call("GET", "/api/review?status=open&voyage=KX-26-03")
        self.assertEqual(status, 200)
        item = next(i for i in queue["items"] if i["kind"] == "ambiguous_assignment")
        status, detail = self.call("GET", f"/api/review/{item['id']}")
        self.assertEqual(status, 200)
        self.assertIn("KX-2603-G", detail["source"]["content"])

        chosen = item["candidates"][0]["result"]
        status, decided = self.call(
            "POST", f"/api/review/{item['id']}/decide",
            {"action": "confirm", "reviewer": "张敏", "value": {"result": chosen}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(decided["status"], "resolved")

        status, batch = self.call("GET", "/api/batches/KX-2603-G")
        self.assertEqual(batch["status"], "published")
        status, samples = self.call("GET", "/api/samples?batch=KX-2603-G")
        self.assertEqual(samples["total"], 4)

    def test_reimport_is_idempotent_over_http(self) -> None:
        self.post_fixtures()
        status, body = self.post_fixtures()
        self.assertEqual(status, 201)
        self.assertTrue(all(item["unchanged"] for item in body["items"]))
        status, materials = self.call("GET", "/api/materials?voyage=KX-26-03")
        self.assertEqual(len(materials["items"]), 5)

    def test_revise_and_withdraw_over_http(self) -> None:
        self.post_fixtures()
        content = (FIXTURES / "lab_results_1.txt").read_text("utf-8")
        status, report = self.call(
            "POST", "/api/materials/lab_results_1/revise?voyage=KX-26-03",
            {"content": content.replace("33.842", "33.842")},
        )
        self.assertEqual(status, 200)
        self.assertTrue(report["unchanged"])

        status, report = self.call(
            "POST", "/api/materials/lab_results_2/withdraw?voyage=KX-26-03", {})
        self.assertEqual(status, 200)
        self.assertTrue(report["withdrawn"])
        status, batch = self.call("GET", "/api/batches/KX-2603-F")
        self.assertEqual(batch["status"], "held")

    def test_page_upload_over_http(self) -> None:
        voyage = "PG-26-01"
        status, body = self.call("POST", "/api/materials", {"voyage": voyage, "materials": [
            {"name": "handover.html", "kind": "handover",
             "content": (PAGED / "handover.html").read_text("utf-8")},
            {"name": "lab_results.txt", "kind": "lab",
             "content": (PAGED / "lab_results.txt").read_text("utf-8")},
        ]})
        self.assertEqual(status, 201)

        def send(*numbers):
            return self.call("POST", "/api/materials/pages", {
                "voyage": voyage, "slug": "deck", "kind": "deck",
                "version": 1, "expected_pages": 4,
                "pages": [{"page": n, "name": f"deck_p{n}.txt",
                           "content": (PAGED / f"deck_p{n}.txt").read_text("utf-8")}
                          for n in numbers],
            })

        status, body = send(4, 2)
        self.assertEqual(status, 201)
        self.assertEqual(body["items"][0]["status"], "collecting")
        self.assertEqual(body["items"][0]["missing_pages"], [1, 3])
        status, sets = self.call("GET", f"/api/page-sets/deck?voyage={voyage}")
        self.assertEqual(status, 200)
        self.assertEqual(sets["received_pages"], [2, 4])
        self.assertFalse(sets["active"])

        status, body = send(2)
        self.assertEqual(body["items"][0]["unchanged_pages"], [2])

        status, body = send(1, 3)
        self.assertEqual(body["items"][0]["status"], "activated")
        status, batches = self.call("GET", f"/api/batches?voyage={voyage}")
        self.assertEqual({b["batch_code"]: b["status"] for b in batches["items"]},
                         {"PG-2601-A": "published", "PG-2601-B": "published"})
        status, samples = self.call("GET", f"/api/samples?voyage={voyage}&batch=PG-2601-B")
        self.assertEqual(samples["total"], 5)
        locator = samples["items"][0]["provenance"]["deck"][0]
        self.assertIn(locator["file"], ("deck_p3.txt", "deck_p4.txt"))
        status, sets = self.call("GET", f"/api/page-sets/deck?voyage={voyage}")
        self.assertTrue(sets["active"])
        self.assertEqual(sets["missing_pages"], [])

    def test_errors_are_json(self) -> None:
        status, body = self.call("GET", "/api/batches/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")
        status, body = self.call("POST", "/api/materials", {"materials": []})
        self.assertEqual(status, 400)
        status, body = self.call("GET", "/api/samples?min_salinity=abc")
        self.assertEqual(status, 400)

    def test_docs_describe_inputs(self) -> None:
        status, body = self.call("GET", "/api/docs")
        self.assertEqual(status, 200)
        self.assertIn("POST /api/materials", body["docs"])
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("endpoints", body)


if __name__ == "__main__":
    unittest.main()
