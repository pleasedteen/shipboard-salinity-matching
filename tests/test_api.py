"""HTTP interface tests, exercised over a real socket."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import FIXTURES, FIXTURE_FILES, ServiceTestCase

from app.api import make_server


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

    def test_http_import_rejects_path(self) -> None:
        # An HTTP caller may only send the material body; a server-side path
        # would import bytes the caller never transmitted.
        status, body = self.call("POST", "/api/materials", {
            "voyage": "KX-26-03",
            "materials": [{"name": "lab.txt", "kind": "lab",
                           "path": str(FIXTURES / "lab_results_1.txt")}],
        })
        self.assertEqual(status, 400)
        self.assertIn("path", body["error"])
        status, listed = self.call("GET", "/api/materials?voyage=KX-26-03")
        self.assertEqual(listed["items"], [])

    def test_http_body_import_attests_its_own_bytes(self) -> None:
        status, body = self.call("POST", "/api/materials", {
            "voyage": "KX-26-03",
            "materials": [{"name": "lab.txt", "kind": "lab",
                           "content": (FIXTURES / "lab_results_1.txt").read_text("utf-8")}],
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["items"][0]["trust"], "trusted")
        self.assertFalse(body["items"][0]["verified"])


if __name__ == "__main__":
    unittest.main()
