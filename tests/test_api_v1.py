"""Contract tests for the mobile API surface (api_v1.py).

Run: python -m unittest discover -s tests

These cover the promises an installed binary depends on, which is exactly the
set that can't be fixed by redeploying:

  * the v1 routes exist and answer,
  * the store-compliance flag actually withholds listing URLs,
  * withholding them for mobile does NOT damage the same search for the web UI
    (it did, in the first draft — the rows are shared objects),
  * the client-version gate returns 426 and exempts the probes,
  * CORS stays closed unless an origin is configured.

Stdlib unittest on purpose: the app has no test dependency today and this
shouldn't be the change that adds one.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the fact store at a throwaway file before app import — importing app.py
# starts the stat engine, and a test run must never touch a real gun_scout.db.
_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("GUN_SCOUT_DB", os.path.join(_TMP.name, "test.db"))

import api_v1          # noqa: E402
import app as app_mod  # noqa: E402
import store           # noqa: E402
from models import Listing  # noqa: E402


class ApiV1TestCase(unittest.TestCase):
    def setUp(self):
        self.client = app_mod.app.test_client()
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        api_v1.MIN_CLIENT_VERSION = "0.0.0"

    # ---- shape -----------------------------------------------------------

    def test_meta_describes_the_server(self):
        body = self.client.get("/api/v1/meta").get_json()
        self.assertEqual(body["api_version"], 1)
        self.assertIn("listing_links", body["features"])
        self.assertTrue(any(v["id"] == "guns" for v in body["verticals"]))

    def test_probes(self):
        self.assertEqual(self.client.get("/api/v1/healthz").status_code, 200)
        # The engine is started by importing app.py, so readiness holds here.
        self.assertEqual(self.client.get("/api/v1/readyz").status_code, 200)

    def test_schema_and_clients_per_vertical(self):
        for vertical in ("guns", "parts", "ammo"):
            schema = self.client.get(f"/api/v1/{vertical}/schema").get_json()
            self.assertEqual(schema["id"], vertical)
            self.assertTrue(schema["inputs"])
            self.assertEqual(
                self.client.get(f"/api/v1/{vertical}/clients").status_code, 200)

    def test_unknown_vertical_is_404_not_a_default(self):
        # verticals.get() falls back to guns for unknown ids; the API must not
        # inherit that, or a typo would silently search the wrong engine.
        self.assertEqual(self.client.get("/api/v1/rockets/schema").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/rockets/stats").status_code, 404)

    def test_missing_search_reports_expired(self):
        response = self.client.get("/api/v1/search/999999")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.get_json()["expired"])

    # ---- store-compliance flag ------------------------------------------

    def _search_with_one_listing(self):
        search_id = store.create_search({"vertical": "guns"}, ["gunscom"])
        store.record_listing(
            search_id,
            Listing(site="gunscom", url="https://www.guns.com/listing/1",
                    title="Glock 19 Gen5 9mm", price=499.0),
            False)
        return search_id

    def test_listing_urls_are_withheld_by_default(self):
        search_id = self._search_with_one_listing()
        row = self.client.get(f"/api/v1/search/{search_id}").get_json()["listings"][0]
        self.assertEqual(row["url"], "")
        # Everything else still comes through: this withholds the link, not
        # the market information the app exists to show.
        self.assertEqual(row["price"], 499.0)
        self.assertTrue(row["title"])

    def test_listing_urls_appear_when_the_flag_is_on(self):
        search_id = self._search_with_one_listing()
        os.environ["GS_MOBILE_LISTING_LINKS"] = "1"
        row = self.client.get(f"/api/v1/search/{search_id}").get_json()["listings"][0]
        self.assertEqual(row["url"], "https://www.guns.com/listing/1")

    def test_withholding_does_not_damage_the_web_view(self):
        """Regression: store.get_search_state hands back the live row dicts,
        so blanking the URL in place erased it for every other reader of the
        same search — including the web UI polling it concurrently."""
        search_id = self._search_with_one_listing()
        self.client.get(f"/api/v1/search/{search_id}")          # mobile poll
        legacy = self.client.get(f"/api/search/{search_id}").get_json()
        self.assertEqual(legacy["listings"][0]["url"],
                         "https://www.guns.com/listing/1")

    # ---- client version gate --------------------------------------------

    def test_old_clients_are_told_to_upgrade(self):
        api_v1.MIN_CLIENT_VERSION = "2.0.0"
        response = self.client.get("/api/v1/meta",
                                   headers={"X-Client-Version": "1.9.9"})
        self.assertEqual(response.status_code, 426)
        self.assertEqual(response.get_json()["min_client_version"], "2.0.0")

    def test_current_clients_pass_and_probes_are_exempt(self):
        api_v1.MIN_CLIENT_VERSION = "2.0.0"
        headers = {"X-Client-Version": "2.0.1"}
        self.assertEqual(self.client.get("/api/v1/meta", headers=headers).status_code, 200)
        # A retired build must still be able to reach the probes, and the
        # platform's own health checks send no version header at all.
        self.assertEqual(
            self.client.get("/api/v1/healthz",
                            headers={"X-Client-Version": "0.1"}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/meta").status_code, 200)

    # ---- CORS ------------------------------------------------------------

    def test_cors_is_closed_unless_configured(self):
        response = self.client.get("/api/v1/meta",
                                   headers={"Origin": "https://example.com"})
        self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

    def test_cors_allows_only_listed_origins(self):
        os.environ["GS_CORS_ORIGINS"] = "http://localhost:8081"
        allowed = self.client.get("/api/v1/meta",
                                  headers={"Origin": "http://localhost:8081"})
        self.assertEqual(allowed.headers["Access-Control-Allow-Origin"],
                         "http://localhost:8081")
        other = self.client.get("/api/v1/meta",
                                headers={"Origin": "https://evil.example"})
        self.assertIsNone(other.headers.get("Access-Control-Allow-Origin"))

    # ---- the worker protocol is not part of this surface -----------------

    def test_worker_routes_are_not_versioned(self):
        # The worker link is operator-to-operator and deploys with the server;
        # it deliberately has no place in the public client contract.
        self.assertEqual(self.client.get("/api/v1/worker/status").status_code, 404)


if __name__ == "__main__":
    unittest.main()
