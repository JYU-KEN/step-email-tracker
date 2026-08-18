import os
import tempfile
import unittest


class TrackingAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        os.environ.pop("DATABASE_URL", None)
        os.environ["DB_FILE"] = os.path.join(cls.tmpdir.name, "tracking.db")
        os.environ["ADMIN_TOKEN"] = "test-admin-token"

        global app_module
        import app as app_module

        cls.app_module = app_module
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def setUp(self):
        conn = self.app_module.get_db()
        conn.execute("DELETE FROM email_opens")
        conn.execute("DELETE FROM link_clicks")
        conn.execute("DELETE FROM subscribers")
        conn.commit()
        conn.close()

    def test_register_subscriber_requires_valid_email(self):
        res = self.client.post("/api/subscriber/register", json={"email": ""})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"], "valid email is required")

    def test_register_subscriber_deduplicates_email(self):
        first = self.client.post("/api/subscriber/register", json={"email": "User@Example.com"})
        second = self.client.post("/api/subscriber/register", json={"email": "user@example.com"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn("tracking_token", first.get_json())
        self.assertTrue(second.get_json()["duplicate"])
        self.assertEqual(first.get_json()["tracking_token"], second.get_json()["tracking_token"])
        self.assertEqual(self.client.get("/api/tracking").get_json()["total_subscribers"], 1)

    def test_token_tracking_counts_subscribers_separately_on_same_ip(self):
        first = self.client.post("/api/subscriber/register", json={"email": "first@example.com"}).get_json()
        second = self.client.post("/api/subscriber/register", json={"email": "second@example.com"}).get_json()

        self.client.get(f"/track/open/1?sid={first['tracking_token']}")
        self.client.get(f"/track/open/1?sid={second['tracking_token']}")
        self.client.get(f"/track/open/1?sid={first['tracking_token']}")

        day1 = next(row for row in self.client.get("/api/tracking").get_json()["tracking"] if row["day"] == 1)
        self.assertEqual(day1["opens"], 2)

    def test_sent_count_uses_japan_time_for_sqlite(self):
        conn = self.app_module.get_db()
        conn.execute(
            "INSERT INTO subscribers (email, registered_at) VALUES (?, datetime('now', '+9 hours', '-25 hours'))",
            ("old@example.com",),
        )
        conn.commit()
        conn.close()

        tracking = self.client.get("/api/tracking").get_json()["tracking"]
        day1 = next(row for row in tracking if row["day"] == 1)
        self.assertEqual(day1["sent"], 1)

    def test_invalid_tracking_day_is_not_counted(self):
        self.client.get("/track/open/99")
        self.client.get("/track/click/99?url=/thanks")

        data = self.client.get("/api/tracking").get_json()["tracking"]
        self.assertTrue(all(row["opens"] == 0 and row["clicks"] == 0 for row in data))

    def test_click_redirect_rejects_unsafe_schemes(self):
        res = self.client.get("/track/click/1?url=javascript:alert(1)", follow_redirects=False)

        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["Location"], "/")

    def test_click_redirect_respects_allowed_hosts(self):
        os.environ["ALLOWED_CLICK_HOSTS"] = "allowed.example.com"
        self.addCleanup(lambda: os.environ.pop("ALLOWED_CLICK_HOSTS", None))

        blocked = self.client.get("/track/click/1?url=https://blocked.example.com/page", follow_redirects=False)
        allowed = self.client.get("/track/click/1?url=https://allowed.example.com/page", follow_redirects=False)

        self.assertEqual(blocked.headers["Location"], "/")
        self.assertEqual(allowed.headers["Location"], "https://allowed.example.com/page")

    def test_reset_requires_admin_token(self):
        self.client.get("/track/click/1?url=/thanks")

        denied = self.client.post("/api/tracking/reset_opens")
        allowed = self.client.post("/api/tracking/reset_opens", headers={"X-Admin-Token": "test-admin-token"})

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        day1 = next(row for row in self.client.get("/api/tracking").get_json()["tracking"] if row["day"] == 1)
        self.assertEqual(day1["clicks"], 0)


if __name__ == "__main__":
    unittest.main()
