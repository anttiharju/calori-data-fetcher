import base64
import contextlib
import copy
import datetime as dt
import io
import json
import stat
import tempfile
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

import yaml

import calori


def meal_rows():
    return [
        {
            "day_of_week": day,
            "meal_time": meal_time,
            "week_id": "test-week",
            "meals": {
                "id": "test-meal",
                "calories": 489,
                "protein": 27,
                "carbs": 69,
                "fat": 8,
                "meal_templates": {
                    "id": "test-template",
                    "large_id": "test-large-meal",
                    "small_id": "test-small-meal",
                    "name": "Test meal",
                    "labels": "test-label",
                    "is_new": True,
                    "expiry": 5,
                    "popular": True,
                    "created_at": "2026-08-18T09:12:49+00:00",
                    "description": "Test description",
                    "in_rotation": True,
                    "lactose_num": 0,
                    "large_macros": {"calories": 700},
                    "limited_time": True,
                    "small_macros": {"calories": 350},
                    "thumbnail_url": "https://example.invalid/thumbnail.webp",
                    "updated_recipe": True,
                    "cover_image_url": "https://example.invalid/cover.webp",
                    "image_url": "https://example.invalid/meal.jpg",
                    "nutrition": {"kcal": 157.8575},
                    "ingredients": {
                        "en": "Oats, milk",
                        "fi": "Finnish ingredients",
                        "sw": "Swedish ingredients",
                    },
                    "allergens": ["test-allergen"],
                },
            },
        }
        for day in reversed(calori.DAY_OFFSETS)
        for meal_time in reversed(calori.MEAL_ORDER)
    ]


class FakeClient:
    def __init__(self):
        self.calls = []
        self.subscriptions = [{"id": "test-subscription"}]
        self.weeks = True

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/auth/v1/user":
            return {"id": "test-user"}
        if path == "/rest/v1/subscriptions":
            return self.subscriptions
        if path == "/rest/v1/weeks":
            assert params is not None
            return [{"id": params["starting_date"][3:]}] if self.weeks else []
        if path == "/rest/v1/sub_meal_selections":
            return meal_rows()
        raise AssertionError("Unexpected request: " + path)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.monday = dt.date(2026, 10, 5)

    def test_two_weeks_have_56_sorted_meals_and_dates(self):
        result = calori.export_meals(self.client, self.monday, 2)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 56)
        expected = calori.normalize_meals(meal_rows(), self.monday)
        expected += calori.normalize_meals(meal_rows(), self.monday + dt.timedelta(weeks=1))
        self.assertEqual(result, expected)
        first = result[0]
        last = result[-1]
        self.assertEqual((first["date"], first["meal_time"]), ("2026-10-05", "breakfast"))
        self.assertEqual((last["date"], last["meal_time"]), ("2026-10-18", "snack"))
        self.assertEqual(yaml.safe_load(yaml.safe_dump(result)), result)

    def test_queries_only_the_authenticated_subscription_and_requested_weeks(self):
        calori.export_meals(self.client, self.monday, 2)
        queries = dict(self.client.calls)
        self.assertEqual(queries["/rest/v1/subscriptions"]["user_id"], "eq.test-user")
        self.assertEqual(
            queries["/rest/v1/subscriptions"]["status"], "in.(trialing,active,past_due)"
        )
        selections = [
            params for path, params in self.client.calls if path.endswith("sub_meal_selections")
        ]
        self.assertEqual([params["sub_id"] for params in selections], ["eq.test-subscription"] * 2)
        self.assertEqual(
            [params["week_id"] for params in selections], ["eq.2026-10-05", "eq.2026-10-12"]
        )

    def test_preserves_selected_macros(self):
        meal = calori.normalize_meals(meal_rows(), self.monday)[0]
        self.assertEqual(meal["macros"], {"calories": 489, "protein": 27, "carbs": 69, "fat": 8})
        self.assertNotIn("sub_id", meal)

    def test_omits_template_and_promotes_image_without_mutating_the_response(self):
        rows = meal_rows()
        original = copy.deepcopy(rows)
        meals = calori.normalize_meals(rows, self.monday)
        for meal in meals:
            self.assertEqual(
                set(meal), {"date", "day_of_week", "meal_time", "name", "image_url", "macros"}
            )
            self.assertEqual(meal["name"], "Test meal")
            self.assertEqual(meal["image_url"], "https://example.invalid/meal.jpg")
        self.assertEqual(rows, original)

    def test_missing_image_url_remains_null(self):
        for template in [{"name": "Test meal"}, {"name": "Test meal", "image_url": None}]:
            with self.subTest(template=template):
                row = meal_rows()[0]
                row["meals"]["meal_templates"] = template
                meal = calori.normalize_meals([row], self.monday)[0]
                self.assertIsNone(meal["image_url"])

    def test_accepts_single_item_relations_without_mutating_the_response(self):
        row = meal_rows()[0]
        row["meals"]["meal_templates"] = [row["meals"]["meal_templates"]]
        row["meals"] = [row["meals"]]
        original = copy.deepcopy(row)
        meal = calori.normalize_meals([row], self.monday)[0]
        self.assertEqual(meal["name"], "Test meal")
        self.assertEqual(meal["image_url"], "https://example.invalid/meal.jpg")
        self.assertEqual(row, original)

    def test_rejects_duplicate_unknown_and_missing_slots(self):
        row = meal_rows()[0]
        with self.assertRaisesRegex(calori.CaloriError, "duplicate"):
            calori.normalize_meals([row, row], self.monday)
        row["day_of_week"] = "unknown"
        with self.assertRaisesRegex(calori.CaloriError, "unknown"):
            calori.normalize_meals([row], self.monday)
        row["day_of_week"] = "mon"
        row["meals"] = None
        with self.assertRaisesRegex(calori.CaloriError, "missing"):
            calori.normalize_meals([row], self.monday)

    def test_rejects_missing_subscription_or_week(self):
        self.client.subscriptions = []
        with self.assertRaisesRegex(calori.CaloriError, "No active subscription"):
            calori.export_meals(self.client, self.monday, 2)
        self.client.subscriptions = [{"id": "test-subscription"}]
        self.client.weeks = False
        with self.assertRaisesRegex(calori.CaloriError, "No unique menu"):
            calori.export_meals(self.client, self.monday, 2)

    def test_rejects_invalid_range_and_preserves_empty_selections(self):
        for day, count in [(dt.date(2026, 10, 6), 1), (self.monday, 0)]:
            with self.assertRaises(calori.CaloriError):
                calori.export_meals(self.client, day, count)
        self.assertEqual(calori.normalize_meals([], self.monday), [])
        with mock.patch(__name__ + ".meal_rows", return_value=[]):
            self.assertEqual(calori.export_meals(self.client, self.monday, 2), [])


class WeeklyExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output_dir = Path(self.directory.name) / "data"
        self.monday = dt.date(2026, 10, 5)
        self.meals = calori.export_meals(FakeClient(), self.monday, 2)

    def test_groups_meals_by_weekday_without_repeated_dates_or_mutation(self):
        for index, meal in enumerate(self.meals):
            meal["name"] = f"Meal {index}"
        original = copy.deepcopy(self.meals)
        exports = calori.write_weekly_meals(self.meals, self.monday, 2, self.output_dir)
        self.assertEqual(
            exports,
            {
                self.output_dir / "2026/week41.yaml": 28,
                self.output_dir / "2026/week42.yaml": 28,
            },
        )
        for offset, path in enumerate(exports):
            text = path.read_text()
            days = yaml.safe_load(text)
            self.assertEqual(list(days), list(calori.DAY_OFFSETS))
            for day, day_offset in calori.DAY_OFFSETS.items():
                date = self.monday + dt.timedelta(weeks=offset, days=day_offset)
                self.assertIn(f"{day}: # {date.isoformat()}\n", text)
            week_meals = self.meals[offset * 28 : (offset + 1) * 28]
            for day, meals in days.items():
                expected = [
                    {
                        "meal": meal["name"],
                        "time": meal["meal_time"],
                        "image": meal["image_url"],
                        "macros": meal["macros"],
                    }
                    for meal in week_meals
                    if meal["day_of_week"] == day
                ]
                self.assertEqual(meals, expected)
                self.assertEqual([meal["time"] for meal in meals], list(calori.MEAL_ORDER))
                for meal in meals:
                    self.assertEqual(list(meal), ["meal", "time", "image", "macros"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.meals, original)

    def test_december_monday_uses_the_next_iso_year(self):
        monday = dt.date(2019, 12, 30)
        meals = calori.export_meals(FakeClient(), monday, 1)
        meals[0]["name"] = "December Monday meal"
        exports = calori.write_weekly_meals(meals, monday, 1, self.output_dir)
        path = self.output_dir / "2020/week01.yaml"
        self.assertEqual(exports, {path: 28})
        self.assertIn("mon: # 2019-12-30\n", path.read_text())
        self.assertIn("sun: # 2020-01-05\n", path.read_text())
        self.assertEqual(yaml.safe_load(path.read_text())["mon"][0]["meal"], "December Monday meal")

    def test_week_53_and_week_01_use_separate_year_directories(self):
        monday = dt.date(2020, 12, 28)
        meals = calori.export_meals(FakeClient(), monday, 2)
        meals[27]["name"] = "January Sunday meal"
        meals[28]["name"] = "January Monday meal"
        exports = calori.write_weekly_meals(meals, monday, 2, self.output_dir)
        self.assertEqual(
            exports,
            {
                self.output_dir / "2020/week53.yaml": 28,
                self.output_dir / "2021/week01.yaml": 28,
            },
        )
        first_week = yaml.safe_load((self.output_dir / "2020/week53.yaml").read_text())
        next_week = yaml.safe_load((self.output_dir / "2021/week01.yaml").read_text())
        self.assertIn("sun: # 2021-01-03\n", (self.output_dir / "2020/week53.yaml").read_text())
        self.assertIn("mon: # 2021-01-04\n", (self.output_dir / "2021/week01.yaml").read_text())
        self.assertEqual(first_week["sun"][-1]["meal"], "January Sunday meal")
        self.assertEqual(next_week["mon"][0]["meal"], "January Monday meal")

    def test_empty_weeks_still_get_yaml_files(self):
        exports = calori.write_weekly_meals([], self.monday, 2, self.output_dir)
        self.assertEqual(list(exports.values()), [0, 0])
        for offset, path in enumerate(exports):
            text = path.read_text()
            days = yaml.safe_load(text)
            self.assertEqual(list(days), list(calori.DAY_OFFSETS))
            self.assertEqual(days, {day: [] for day in calori.DAY_OFFSETS})
            for day, day_offset in calori.DAY_OFFSETS.items():
                date = self.monday + dt.timedelta(weeks=offset, days=day_offset)
                self.assertIn(f"{day}: [] # {date.isoformat()}\n", text)

    def test_days_without_meals_have_empty_lists(self):
        exports = calori.write_weekly_meals(self.meals[:1], self.monday, 1, self.output_dir)
        path = self.output_dir / "2026/week41.yaml"
        self.assertEqual(exports, {path: 1})
        days = yaml.safe_load(path.read_text())
        self.assertEqual(len(days["mon"]), 1)
        for day in ["tue", "wed", "thu", "fri", "sat", "sun"]:
            self.assertEqual(days[day], [])

    def test_yaml_preserves_unicode_and_null_values(self):
        self.meals[0]["name"] = "Cr\u00e8me: oats & fruit"
        self.meals[0]["image_url"] = None
        calori.write_weekly_meals(self.meals, self.monday, 2, self.output_dir)
        text = (self.output_dir / "2026/week41.yaml").read_text()
        self.assertIn("Cr\u00e8me", text)
        self.assertEqual(
            yaml.safe_load(text)["mon"][0],
            {
                "meal": "Cr\u00e8me: oats & fruit",
                "time": "breakfast",
                "image": None,
                "macros": self.meals[0]["macros"],
            },
        )

    def test_repeated_export_replaces_only_requested_weeks(self):
        history = self.output_dir / "2026/week40.yaml"
        calori.write_text(history, "[]\n")
        calori.write_weekly_meals(self.meals, self.monday, 2, self.output_dir)
        self.meals[0]["name"] = "Updated meal"
        calori.write_weekly_meals(self.meals, self.monday, 2, self.output_dir)
        updated = yaml.safe_load((self.output_dir / "2026/week41.yaml").read_text())
        self.assertEqual(updated["mon"][0]["meal"], "Updated meal")
        self.assertEqual(history.read_text(), "[]\n")
        self.assertEqual(len(list(self.output_dir.rglob("*.yaml"))), 3)

    def test_serialization_failure_preserves_all_existing_exports(self):
        exports = calori.write_weekly_meals(self.meals, self.monday, 2, self.output_dir)
        originals = {path: path.read_bytes() for path in exports}
        with (
            mock.patch("calori.dump_week", side_effect=["[]\n", calori.YAMLError("Invalid YAML")]),
            self.assertRaises(calori.YAMLError),
        ):
            calori.write_weekly_meals(self.meals, self.monday, 2, self.output_dir)
        self.assertEqual({path: path.read_bytes() for path in exports}, originals)

    def test_failed_file_replacement_preserves_previous_export(self):
        path = self.output_dir / "2026/week41.yaml"
        calori.write_text(path, "[]\n")
        with (
            mock.patch("calori.os.replace", side_effect=OSError("Cannot replace file")),
            self.assertRaises(OSError),
        ):
            calori.write_text(path, "- name: Changed\n")
        self.assertEqual(path.read_text(), "[]\n")
        self.assertEqual(list(path.parent.iterdir()), [path])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "session.json"
        self.tokens = {
            "access_token": "test-access",
            "refresh_token": "test-refresh",
            "expires_at": time.time() + 3600,
            "user": {"email": "not-exported@example.invalid"},
            "password": "not-saved",
        }

    def save(self):
        return calori.save_session(self.path, "test-public-key", self.tokens)

    def test_stores_only_tokens_and_config_with_private_permissions(self):
        self.save()
        self.path.chmod(0o644)
        self.save()
        saved = json.loads(self.path.read_text())
        self.assertEqual(set(saved), {"api_key", "access_token", "refresh_token", "expires_at"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_missing_or_invalid_session_has_a_login_instruction(self):
        for value in [None, "not JSON", "[]", "{}"]:
            if value is not None:
                self.path.parent.mkdir(exist_ok=True)
                self.path.write_text(value)
            with self.assertRaisesRegex(calori.CaloriError, "login"):
                calori.CaloriClient(self.path)

    def test_refreshes_expired_session_and_persists_rotated_tokens(self):
        self.tokens["expires_at"] = 0
        self.save()
        client = calori.CaloriClient(self.path)
        fresh = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
        with mock.patch("calori.request_json", side_effect=[fresh, {"id": "test-user"}]) as request:
            self.assertEqual(client.get("/auth/v1/user"), {"id": "test-user"})
        self.assertEqual(
            request.call_args_list[0].kwargs["body"], {"refresh_token": "test-refresh"}
        )
        self.assertEqual(request.call_args_list[1].args[2], "new-access")
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "new-refresh")

    def test_retries_unauthorized_get_once(self):
        self.save()
        client = calori.CaloriClient(self.path)
        with mock.patch(
            "calori.request_json",
            side_effect=[calori.APIError(401), self.tokens, {"id": "test-user"}],
        ) as request:
            self.assertEqual(client.get("/auth/v1/user"), {"id": "test-user"})
            self.assertEqual(request.call_count, 3)
        with mock.patch(
            "calori.request_json",
            side_effect=[calori.APIError(401), self.tokens, calori.APIError(401)],
        ) as request:
            with self.assertRaises(calori.APIError):
                client.get("/auth/v1/user")
            self.assertEqual(request.call_count, 3)

    def test_revoked_refresh_requires_login_and_preserves_existing_file(self):
        self.tokens["expires_at"] = 0
        self.save()
        original = self.path.read_bytes()
        with (
            mock.patch("calori.request_json", side_effect=calori.APIError(400)),
            self.assertRaisesRegex(calori.CaloriError, "login"),
        ):
            calori.CaloriClient(self.path).get("/auth/v1/user")
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_refresh_response_does_not_replace_the_session(self):
        self.save()
        original = self.path.read_bytes()
        with self.assertRaises(calori.CaloriError):
            calori.save_session(self.path, "test-public-key", {"access_token": "partial"})
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_json_write_preserves_the_previous_export(self):
        calori.write_json(self.path, {"previous": True})
        with self.assertRaises(TypeError):
            calori.write_json(self.path, {"invalid": object()})
        self.assertEqual(json.loads(self.path.read_text()), {"previous": True})
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class ConfigurationTests(unittest.TestCase):
    def public_key(self, role="anon", project=calori.PROJECT_ID):
        payload = (
            base64.urlsafe_b64encode(json.dumps({"role": role, "ref": project}).encode())
            .decode()
            .rstrip("=")
        )
        return "eyJhbGciOiJIUzI1NiJ9." + payload + ".test-signature"

    def test_discovers_only_the_public_key_for_this_project(self):
        expected = self.public_key()
        candidates = [
            self.public_key(role="authenticated"),
            self.public_key(project="other"),
            expected,
        ]
        self.assertEqual(calori.find_public_key(" ".join(candidates)), expected)
        self.assertIsNone(calori.find_public_key("eyJabc.bad.bad"))

    def test_discovers_key_from_same_origin_client_scripts(self):
        html = '<script src="https://unrelated.example/script.js"></script><script src="/_next/static/chunks/app.js"></script>'
        expected = self.public_key()
        with mock.patch("calori.read_public_text", side_effect=[html, expected]) as read:
            self.assertEqual(calori.discover_public_key(), expected)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args.args[0], "https://www.calori.fi/_next/static/chunks/app.js")

    def test_request_uses_authorization_and_encoded_query_without_redirects(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'[{"id":"test-meal"}]')
        with mock.patch("calori.urllib.request.build_opener", return_value=opener) as build:
            result = calori.request_json(
                "/rest/v1/meals", "test-key", "test-token", {"select": "id,name"}
            )
        self.assertEqual(result, [{"id": "test-meal"}])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertIn("select=id%2Cname", request.full_url)
        build.assert_called_once_with(calori.NoRedirects)

    def test_http_errors_do_not_disclose_response_contents(self):
        body = io.BytesIO(b"private-token")
        error = urllib.error.HTTPError(calori.API_URL, 401, "private-response", Message(), body)
        opener = mock.Mock()
        opener.open.side_effect = error
        with (
            mock.patch("calori.urllib.request.build_opener", return_value=opener),
            self.assertRaisesRegex(calori.APIError, "HTTP 401") as caught,
        ):
            calori.request_json("/auth/v1/user", "test-key")
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(body.closed)

    def test_current_week_uses_helsinki_not_the_utc_date(self):
        sunday_utc = dt.datetime(2026, 10, 11, 22, tzinfo=dt.UTC)
        self.assertEqual(calori.current_monday(sunday_utc), dt.date(2026, 10, 12))


class CommandTests(unittest.TestCase):
    def test_login_prompts_locally_and_never_saves_or_prints_the_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "session.json"
            tokens = {
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
            }
            output = io.StringIO()
            errors = io.StringIO()
            with (
                mock.patch("calori.SESSION_PATH", path),
                mock.patch("calori.sys.stdin.isatty", return_value=True),
                mock.patch("calori.discover_public_key", return_value="test-key"),
                mock.patch("builtins.input", return_value="example@example.invalid"),
                mock.patch("calori.getpass.getpass", return_value="test-password"),
                mock.patch("calori.request_json", return_value=tokens) as request,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                status = calori.main(["login"])
            self.assertEqual(status, 0)
            self.assertEqual(request.call_args.args, ("/auth/v1/token", "test-key"))
            self.assertEqual(request.call_args.kwargs["params"], {"grant_type": "password"})
            self.assertEqual(
                request.call_args.kwargs["body"],
                {"email": "example@example.invalid", "password": "test-password"},
            )
            self.assertNotIn(
                "test-password", path.read_text() + output.getvalue() + errors.getvalue()
            )

    def test_fetch_defaults_to_current_and_next_week_yaml_files(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.chdir(directory),
            mock.patch("calori.CaloriClient", return_value=FakeClient()),
            mock.patch("calori.current_monday", return_value=dt.date(2026, 10, 5)),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            status = calori.main(["fetch"])
            self.assertEqual(status, 0)
            for filename in ["data/2026/week41.yaml", "data/2026/week42.yaml"]:
                days = yaml.safe_load(Path(filename).read_text())
                self.assertEqual(list(days), list(calori.DAY_OFFSETS))
                self.assertEqual(sum(len(meals) for meals in days.values()), 28)
                self.assertIn(filename, errors.getvalue())
            self.assertEqual(output.getvalue(), "")

    def test_custom_output_directory_contains_only_requested_week(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "exports"
            output = io.StringIO()
            errors = io.StringIO()
            with (
                mock.patch("calori.CaloriClient", return_value=FakeClient()),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                status = calori.main(
                    [
                        "fetch",
                        "--week",
                        "2026-10-05",
                        "--weeks",
                        "1",
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(status, 0)
            path = output_dir / "2026/week41.yaml"
            days = yaml.safe_load(path.read_text())
            self.assertEqual(list(days), list(calori.DAY_OFFSETS))
            self.assertEqual(sum(len(meals) for meals in days.values()), 28)
            self.assertEqual(list(output_dir.rglob("*.yaml")), [path])
            self.assertEqual(output.getvalue(), "")
            self.assertIn("Exported 28 meals", errors.getvalue())

    def test_missing_login_writes_no_files(self):
        output = io.StringIO()
        errors = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.chdir(directory),
            mock.patch("calori.CaloriClient", side_effect=calori.CaloriError("Run login.")),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            status = calori.main(["fetch"])
            self.assertFalse(Path("data").exists())
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("login", errors.getvalue())

    def test_invalid_arguments_fail_before_network_access(self):
        arguments = [
            ["fetch", "--weeks", "0"],
            ["fetch", "--week", "2026-10-06"],
            ["fetch", "--week", "bad"],
        ]
        for args in arguments:
            with self.subTest(args=args), mock.patch("calori.CaloriClient") as client:
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as caught,
                ):
                    calori.main(args)
                self.assertEqual(caught.exception.code, 2)
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
