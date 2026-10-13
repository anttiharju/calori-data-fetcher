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
        self.week_closed = False
        self.selections = None
        self.patches = []
        self.meals = []
        self.templates = [{"id": "test-template", "name": "Test meal"}]
        self.availability = [
            {
                "day_of_week": row["day_of_week"],
                "meal_time": row["meal_time"],
                "generic_id": "test-template",
                "is_default": False,
            }
            for row in meal_rows()
        ]

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/auth/v1/user":
            return {"id": "test-user"}
        if path == "/rest/v1/subscriptions":
            return self.subscriptions
        if path == "/rest/v1/weeks":
            assert params is not None
            monday = params["starting_date"][3:]
            return (
                [{"id": monday, "starting_date": monday, "is_closed": self.week_closed}]
                if self.weeks
                else []
            )
        if path == "/rest/v1/sub_meal_selections":
            return meal_rows() if self.selections is None else copy.deepcopy(self.selections)
        if path in (
            "/rest/v1/meal_templates",
            "/rest/v1/consumption_slots_mixed",
            "/rest/v1/meals",
        ):
            assert params is not None
            rows = {
                "/rest/v1/meal_templates": self.templates,
                "/rest/v1/consumption_slots_mixed": self.availability,
                "/rest/v1/meals": self.meals,
            }[path]
            offset = int(params["offset"])
            return rows[offset : offset + int(params["limit"])]
        raise AssertionError("Unexpected request: " + path)

    def patch(self, path, body, params):
        assert self.selections is not None
        assert path == "/rest/v1/sub_meal_selections"
        assert params["sub_id"] == "eq.test-subscription"
        self.patches.append((path, copy.deepcopy(body), dict(params)))
        for row in self.selections:
            if all(
                params[field] == "eq." + row[field]
                for field in ("id", "week_id", "day_of_week", "meal_time", "meal_id")
            ):
                row.update(body)
                template = next(
                    template
                    for template in self.templates
                    if body["meal_id"] in (template.get("small_id"), template.get("large_id"))
                )
                variant = next(meal for meal in self.meals if meal["id"] == body["meal_id"])
                row["meals"] = {**variant, "meal_templates": template}
                return [{"id": row["id"], "meal_id": row["meal_id"]}]
        return []


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


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.monday = dt.date(2026, 10, 5)

    def test_counts_unique_spots_and_sorts_rarest_then_name_without_mutation(self):
        templates = [
            {"id": "apple", "name": "Apple Delight"},
            {"id": "apple-variant", "name": "Apple Delight"},
            {"id": "beef", "name": "Beef & Potatoes"},
            {"id": "zucchini", "name": "Zucchini Pasta"},
            {"id": "unused", "name": "Not on this menu"},
        ]
        rows = [
            {"day_of_week": day, "meal_time": meal_time, "generic_id": template_id}
            for day, meal_time, template_id in [
                ("tue", "lunch", "beef"),
                ("mon", "snack", "apple"),
                ("thu", "lunch", "zucchini"),
                ("mon", "breakfast", "apple"),
                ("mon", "lunch", "beef"),
                ("mon", "breakfast", "apple"),
                ("mon", "breakfast", "apple-variant"),
            ]
        ]
        original = copy.deepcopy((rows, templates))
        self.assertEqual(
            calori.rank_meal_opportunities(rows, templates),
            [
                {"name": "Zucchini Pasta", "opportunities": 1, "spots": ["thu/lunch"]},
                {
                    "name": "Apple Delight",
                    "opportunities": 2,
                    "spots": ["mon/breakfast", "mon/snack"],
                },
                {
                    "name": "Beef & Potatoes",
                    "opportunities": 2,
                    "spots": ["mon/lunch", "tue/lunch"],
                },
            ],
        )
        self.assertEqual((rows, templates), original)

    def test_empty_menu_has_no_opportunities(self):
        self.assertEqual(calori.rank_meal_opportunities([], []), [])

    def test_counts_defaults_and_alternatives_from_the_api_menu(self):
        templates = [
            {"id": "raspberry", "name": "Raspberry & Quark"},
            {"id": "banana", "name": "Banana Quark"},
        ]
        rows = [
            {
                "day_of_week": day,
                "meal_time": meal_time,
                "generic_id": "raspberry",
                "is_default": is_default,
            }
            for day, meal_time, is_default in [
                ("mon", "breakfast", False),
                ("tue", "breakfast", True),
                ("tue", "snack", True),
            ]
        ]
        self.assertEqual(
            calori.rank_meal_opportunities(rows, templates),
            [
                {
                    "name": "Raspberry & Quark",
                    "opportunities": 3,
                    "spots": ["mon/breakfast", "tue/breakfast", "tue/snack"],
                },
            ],
        )

    def test_rejects_unknown_slots_and_missing_template_names(self):
        row = {"day_of_week": "mon", "meal_time": "breakfast", "generic_id": "apple"}
        template = {"id": "apple", "name": "Apple Delight"}
        for field in ["day_of_week", "meal_time"]:
            with self.subTest(field=field), self.assertRaisesRegex(calori.CaloriError, "unknown"):
                calori.rank_meal_opportunities([dict(row, **{field: "unknown"})], [template])
        for templates in [[], [{"id": "apple"}], [{"id": "apple", "name": " "}]]:
            with (
                self.subTest(templates=templates),
                self.assertRaisesRegex(calori.CaloriError, "meal template"),
            ):
                calori.rank_meal_opportunities([row], templates)

    def test_fetches_all_pages_for_each_requested_week(self):
        with mock.patch("calori.MENU_PAGE_SIZE", 2):
            result = calori.export_meal_opportunities(self.client, self.monday, 2)
        expected = calori.rank_meal_opportunities(self.client.availability, self.client.templates)
        self.assertEqual(
            result, {self.monday: expected, self.monday + dt.timedelta(weeks=1): expected}
        )
        self.assertEqual(result[self.monday][0]["opportunities"], 28)
        queries = [
            params for path, params in self.client.calls if path.endswith("consumption_slots_mixed")
        ]
        self.assertEqual(
            [params["week_id"] for params in queries],
            ["eq.2026-10-05"] * 15 + ["eq.2026-10-12"] * 15,
        )
        self.assertEqual(
            [params["offset"] for params in queries[:15]], list(map(str, range(0, 29, 2)))
        )
        self.assertTrue(all(params["order"] == "id" for params in queries))
        self.assertTrue(all("is_default" not in params for params in queries))

    def test_keeps_opportunities_separate_between_weeks(self):
        next_monday = self.monday + dt.timedelta(weeks=1)
        get = self.client.get

        def response(path, params=None):
            assert params is not None
            if path.endswith("consumption_slots_mixed") and params["week_id"] == "eq.2026-10-12":
                return []
            return get(path, params)

        with mock.patch.object(self.client, "get", side_effect=response):
            result = calori.export_meal_opportunities(self.client, self.monday, 2)
        self.assertEqual(result[self.monday][0]["opportunities"], 28)
        self.assertEqual(result[next_monday], [])

    def test_fetch_keeps_defaults_without_using_current_selections(self):
        self.client.templates.append({"id": "raspberry", "name": "Raspberry & Quark"})
        self.client.availability = [
            {
                "day_of_week": "tue",
                "meal_time": meal_time,
                "generic_id": "raspberry",
                "is_default": is_default,
            }
            for meal_time, is_default in [("breakfast", True), ("snack", False)]
        ]
        result = calori.export_meal_opportunities(self.client, self.monday, 1)[self.monday]
        self.assertEqual(
            result,
            [
                {
                    "name": "Raspberry & Quark",
                    "opportunities": 2,
                    "spots": ["tue/breakfast", "tue/snack"],
                }
            ],
        )
        self.assertFalse(
            any(path.endswith("sub_meal_selections") for path, _params in self.client.calls)
        )

    def test_rejects_invalid_ranges_missing_weeks_and_invalid_responses(self):
        for day, count in [(dt.date(2026, 10, 6), 1), (self.monday, 0)]:
            with self.subTest(day=day, count=count), self.assertRaises(calori.CaloriError):
                calori.export_meal_opportunities(self.client, day, count)
        self.client.weeks = False
        with self.assertRaisesRegex(calori.CaloriError, "No unique menu"):
            calori.export_meal_opportunities(self.client, self.monday, 1)
        with (
            mock.patch.object(self.client, "get", return_value={}),
            self.assertRaisesRegex(calori.CaloriError, "list of menu rows"),
        ):
            calori.export_meal_opportunities(self.client, self.monday, 1)


class CaloriePlanningTests(unittest.TestCase):
    def setUp(self):
        self.options: dict[tuple[str, str], list[calori.MealChoice]] = {
            ("mon", "breakfast"): [
                {"name": "Small breakfast", "meal_id": "small", "calories": 600},
                {"name": "Big breakfast", "meal_id": "big", "calories": 800},
            ],
            ("mon", "lunch"): [
                {"name": "Light lunch", "meal_id": "light", "calories": 1500},
                {"name": "Hearty lunch", "meal_id": "hearty", "calories": 1900},
            ],
        }

    def test_finds_the_closest_total_before_preferring_less_eaten_meals(self):
        original = copy.deepcopy(self.options)
        plan = calori.plan_calorie_meals(
            self.options, calori.Counter({"Small breakfast": 5, "Hearty lunch": 3})
        )
        self.assertEqual(
            [meal["name"] for meal in plan.values()], ["Small breakfast", "Hearty lunch"]
        )
        self.assertEqual(sum(meal["calories"] for meal in plan.values()), 2500)
        self.assertEqual(self.options, original)

    def test_equal_calorie_totals_prefer_lower_historical_counts(self):
        self.options["mon", "lunch"][0]["calories"] = 1700
        plan = calori.plan_calorie_meals(
            self.options, calori.Counter({"Small breakfast": 5, "Light lunch": 1})
        )
        self.assertEqual([meal["name"] for meal in plan.values()], ["Big breakfast", "Light lunch"])

    def test_calorie_deviation_uses_both_sides_of_the_target_and_decimal_values(self):
        options: dict[tuple[str, str], list[calori.MealChoice]] = {
            ("mon", "lunch"): [
                {"name": "Above", "meal_id": "above", "calories": 2500.2},
                {"name": "Below", "meal_id": "below", "calories": 2499.6},
            ]
        }
        plan = calori.plan_calorie_meals(options, calori.Counter())
        self.assertEqual(plan["mon", "lunch"]["name"], "Above")

    def test_same_name_cannot_repeat_within_a_day_even_for_different_variants(self):
        self.options["mon", "breakfast"][0]["name"] = "Same meal"
        self.options["mon", "lunch"][1]["name"] = "Same meal"
        plan = calori.plan_calorie_meals(self.options, calori.Counter())
        self.assertEqual(len({meal["name"] for meal in plan.values()}), 2)
        self.assertNotEqual(sum(meal["calories"] for meal in plan.values()), 2500)

    def test_meals_cannot_repeat_across_days(self):
        first: calori.MealChoice = {"name": "First", "meal_id": "first", "calories": 2500}
        second: calori.MealChoice = {"name": "Second", "meal_id": "second", "calories": 2500}
        options = {("mon", "lunch"): [first], ("tue", "lunch"): [first, second]}
        plan = calori.plan_calorie_meals(options, calori.Counter())
        self.assertEqual([meal["name"] for meal in plan.values()], ["First", "Second"])
        options["tue", "lunch"] = [first]
        with self.assertRaisesRegex(calori.CaloriError, "No complete plan"):
            calori.plan_calorie_meals(options, calori.Counter())

    def test_reserves_a_meal_that_is_the_only_choice_for_a_later_day(self):
        shared: calori.MealChoice = {"name": "Shared", "meal_id": "shared", "calories": 2500}
        alternative: calori.MealChoice = {
            "name": "Alternative",
            "meal_id": "other",
            "calories": 2400,
        }
        options = {("mon", "lunch"): [shared, alternative], ("tue", "lunch"): [shared]}
        plan = calori.plan_calorie_meals(options, calori.Counter())
        self.assertEqual([meal["name"] for meal in plan.values()], ["Alternative", "Shared"])

    def test_optimizes_calorie_deviations_across_the_whole_week(self):
        shared: calori.MealChoice = {"name": "Shared", "meal_id": "shared", "calories": 2500}
        monday: calori.MealChoice = {"name": "Monday", "meal_id": "monday", "calories": 2400}
        tuesday: calori.MealChoice = {"name": "Tuesday", "meal_id": "tuesday", "calories": 2000}
        options = {("mon", "lunch"): [shared, monday], ("tue", "lunch"): [shared, tuesday]}
        plan = calori.plan_calorie_meals(options, calori.Counter())
        self.assertEqual([meal["name"] for meal in plan.values()], ["Monday", "Shared"])

    def test_equal_total_deviation_prefers_a_smaller_worst_day(self):
        options: dict[tuple[str, str], list[calori.MealChoice]] = {
            ("mon", "lunch"): [
                {"name": "First", "meal_id": "first-mon", "calories": 2500},
                {"name": "Second", "meal_id": "second-mon", "calories": 2300},
            ],
            ("tue", "lunch"): [
                {"name": "First", "meal_id": "first-tue", "calories": 2200},
                {"name": "Second", "meal_id": "second-tue", "calories": 2000},
            ],
        }
        plan = calori.plan_calorie_meals(options, calori.Counter())
        self.assertEqual([meal["name"] for meal in plan.values()], ["Second", "First"])
        self.assertEqual(sum(abs(meal["calories"] - 2500) for meal in plan.values()), 500)
        self.assertEqual(max(abs(meal["calories"] - 2500) for meal in plan.values()), 300)

    def test_unproven_solver_results_are_rejected(self):
        result = mock.Mock(
            status=calori.pulp.constants.LpSolveStatus.NotSolved,
            has_solution=True,
            objective=0,
        )
        with (
            mock.patch.object(calori.pulp.LpProblem, "solve", return_value=result),
            self.assertRaisesRegex(calori.CaloriError, "proven optimal"),
        ):
            calori.plan_calorie_meals(self.options, calori.Counter())

    def test_solver_failure_reports_a_clear_error(self):
        with (
            mock.patch.object(
                calori.pulp.LpProblem, "solve", side_effect=calori.pulp.PulpSolverError("Failed")
            ),
            self.assertRaisesRegex(calori.CaloriError, "Cannot run the weekly meal optimizer"),
        ):
            calori.plan_calorie_meals(self.options, calori.Counter())

    def test_custom_target_and_input_order_do_not_change_the_objective(self):
        expected = calori.plan_calorie_meals(self.options, calori.Counter(), 2100)
        self.assertEqual(sum(meal["calories"] for meal in expected.values()), 2100)
        reversed_options = {
            slot: list(reversed(choices)) for slot, choices in reversed(self.options.items())
        }
        self.assertEqual(
            calori.plan_calorie_meals(reversed_options, calori.Counter(), 2100), expected
        )

    def test_incomplete_or_invalid_options_fail_without_relaxing_uniqueness(self):
        self.options["mon", "breakfast"] = []
        with self.assertRaisesRegex(calori.CaloriError, "No complete plan"):
            calori.plan_calorie_meals(self.options, calori.Counter())
        for options, target in [({}, 2500), (self.options, 0), ({("bad", "lunch"): []}, 2500)]:
            with (
                self.subTest(options=options, target=target),
                self.assertRaises(calori.CaloriError),
            ):
                calori.plan_calorie_meals(options, calori.Counter(), target)

    def test_nonfinite_nonpositive_or_boolean_calories_are_rejected(self):
        for calories in [float("nan"), float("inf"), 0, -1, True]:
            with (
                self.subTest(calories=calories),
                self.assertRaisesRegex(calori.CaloriError, "calories"),
            ):
                self.options["mon", "breakfast"][0]["calories"] = calories
                calori.plan_calorie_meals(self.options, calori.Counter())


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output_dir = Path(self.directory.name) / "data"
        self.monday = dt.date(2026, 10, 19)
        clock = mock.patch("calori.current_monday", return_value=dt.date(2026, 10, 12))
        clock.start()
        self.addCleanup(clock.stop)
        self.client = FakeClient()
        self.client.templates = [
            {"id": name, "name": name, "small_id": name + "-small", "large_id": name + "-large"}
            for name in ["Apple Delight", "Banana Quark", "Previous meal", "Current meal"]
        ]
        self.client.meals = [
            {"id": name + "-" + size, "calories": calories}
            for name, sizes in {
                "Apple Delight": (600, 1400),
                "Banana Quark": (1100, 1100),
                "Previous meal": (900, 1900),
                "Current meal": (700, 1300),
            }.items()
            for size, calories in zip(("small", "large"), sizes, strict=True)
        ]
        self.client.availability = [
            {"day_of_week": "mon", "meal_time": meal_time, "generic_id": name}
            for name, meal_time in [
                ("Apple Delight", "breakfast"),
                ("Apple Delight", "snack"),
                ("Banana Quark", "breakfast"),
                ("Previous meal", "snack"),
            ]
        ]
        self.selections = [
            {
                "id": "selection-" + meal_time,
                "meal_id": "Current meal-" + size,
                "day_of_week": "mon",
                "meal_time": meal_time,
                "week_id": "2026-10-19",
                "meals": {"id": "Current meal-" + size, "meal_templates": {"name": "Current meal"}},
            }
            for meal_time, size in [("breakfast", "small"), ("snack", "large")]
        ]
        self.client.selections = self.selections
        history = calori.normalize_meals(meal_rows()[:1], self.monday - dt.timedelta(weeks=1))
        history[0]["name"] = "Previous meal"
        calori.write_weekly_meals(history, self.monday - dt.timedelta(weeks=1), 2, self.output_dir)

    def test_prepares_latest_week_from_live_menu_and_history_without_writes(self):
        calori.write_text(self.output_dir / "stats.yaml", "not the source of history\n")
        originals = {path: path.read_bytes() for path in self.output_dir.rglob("*.yaml")}
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        self.assertEqual(plan["monday"], self.monday)
        self.assertEqual(plan["week_id"], "2026-10-19")
        self.assertEqual(plan["subscription_id"], "test-subscription")
        self.assertEqual(plan["target_calories"], 2500)
        self.assertEqual(plan["daily_calories"], {"mon": 2500})
        self.assertEqual(
            [(meal["slot"], meal["name"], meal["meal_id"]) for meal in plan["meals"]],
            [
                (("mon", "breakfast"), "Banana Quark", "Banana Quark-small"),
                (("mon", "snack"), "Apple Delight", "Apple Delight-large"),
            ],
        )
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_prepares_and_applies_unique_meals_across_multiple_days(self):
        self.selections.append(dict(self.selections[0], id="selection-tuesday", day_of_week="tue"))
        self.client.availability.append(
            {"day_of_week": "tue", "meal_time": "breakfast", "generic_id": "Banana Quark"}
        )
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        self.assertEqual(
            [meal["name"] for meal in plan["meals"]],
            ["Apple Delight", "Previous meal", "Banana Quark"],
        )
        self.assertEqual(len({meal["name"] for meal in plan["meals"]}), 3)
        self.assertEqual(calori.apply_selection_plan(self.client, plan), 3)

    def test_duplicate_planned_names_prevent_all_writes(self):
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        plan["meals"][1]["name"] = plan["meals"][0]["name"]
        with self.assertRaisesRegex(calori.CaloriError, "duplicate meal names"):
            calori.apply_selection_plan(self.client, plan)
        self.assertEqual(self.client.patches, [])

    def test_excludes_unavailable_sizes_but_allows_previously_eaten_meals(self):
        del self.client.templates[1]["small_id"]
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        self.assertEqual(
            [meal["name"] for meal in plan["meals"]], ["Apple Delight", "Previous meal"]
        )
        self.assertEqual([meal["eaten"] for meal in plan["meals"]], [0, 1])
        self.assertEqual(plan["daily_calories"], {"mon": 2500})

    def test_missing_calories_fail_before_saving_selections(self):
        self.client.meals = []
        with self.assertRaisesRegex(calori.CaloriError, "calories"):
            calori.prepare_selection_plan(self.client, self.output_dir)
        self.assertEqual(self.client.patches, [])

    def test_rejects_closed_weeks_and_unknown_current_meal_sizes(self):
        self.client.week_closed = True
        with self.assertRaisesRegex(calori.CaloriError, "closed"):
            calori.prepare_selection_plan(self.client, self.output_dir)
        self.client.week_closed = False
        self.selections[0]["meal_id"] = "unknown"
        with self.assertRaisesRegex(calori.CaloriError, "size"):
            calori.prepare_selection_plan(self.client, self.output_dir)

    def test_rejects_missing_or_past_exports_before_network_access(self):
        with self.assertRaisesRegex(calori.CaloriError, "No exported weeks"):
            calori.prepare_selection_plan(self.client, self.output_dir / "missing")
        with (
            mock.patch("calori.current_monday", return_value=dt.date(2026, 10, 26)),
            self.assertRaisesRegex(calori.CaloriError, "in the past"),
        ):
            calori.prepare_selection_plan(self.client, self.output_dir)
        self.assertEqual(self.client.calls, [])

    def test_rejects_missing_duplicate_or_invalid_selection_rows(self):
        row = self.selections[0]
        for rows in [[], [row, row], [dict(row, id=None)]]:
            with self.subTest(rows=rows), self.assertRaises(calori.CaloriError):
                calori.selection_state(rows)

    def test_shared_variant_ids_follow_the_dashboard_large_convention(self):
        self.client.templates[-1]["small_id"] = "Current meal-large"
        for row in self.selections:
            row["meal_id"] = "Current meal-large"
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        self.assertTrue(all(meal["meal_id"].endswith("-large") for meal in plan["meals"]))

    def test_applies_a_scoped_plan_checks_saved_rows_and_skips_unchanged_meals(self):
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        self.assertEqual(calori.apply_selection_plan(self.client, plan), 2)
        self.assertEqual(len(self.client.patches), 2)
        for meal, (path, body, params) in zip(plan["meals"], self.client.patches, strict=True):
            self.assertEqual(path, "/rest/v1/sub_meal_selections")
            self.assertEqual(body["meal_id"], meal["meal_id"])
            self.assertFalse(body["is_initial"])
            self.assertEqual(
                dt.datetime.fromisoformat(body["created_at"]).utcoffset(), dt.timedelta()
            )
            self.assertEqual(
                params,
                {
                    "select": "id,meal_id",
                    "id": "eq." + meal["id"],
                    "sub_id": "eq.test-subscription",
                    "week_id": "eq.2026-10-19",
                    "day_of_week": "eq.mon",
                    "meal_time": "eq." + meal["slot"][1],
                    "meal_id": "eq." + meal["previous_meal_id"],
                },
            )
        second_plan = calori.prepare_selection_plan(self.client, self.output_dir)
        self.assertEqual(calori.apply_selection_plan(self.client, second_plan), 0)
        self.assertEqual(len(self.client.patches), 2)

    def test_changes_to_live_selections_or_week_status_prevent_all_writes(self):
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        original = self.selections[0]["meal_id"]
        self.selections[0]["meal_id"] = "a-new-selection"
        with self.assertRaisesRegex(calori.CaloriError, "selections changed"):
            calori.apply_selection_plan(self.client, plan)
        self.selections[0]["meal_id"] = original
        self.client.week_closed = True
        with self.assertRaisesRegex(calori.CaloriError, "closed"):
            calori.apply_selection_plan(self.client, plan)
        self.assertEqual(self.client.patches, [])

    def test_write_failure_stops_the_plan_and_reports_partial_progress(self):
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        patch = self.client.patch

        def fail_second_update(path, body, params):
            if self.client.patches:
                raise calori.APIError(409)
            return patch(path, body, params)

        with (
            mock.patch.object(self.client, "patch", side_effect=fail_second_update),
            self.assertRaisesRegex(calori.CaloriError, "after 1 confirmed updates"),
        ):
            calori.apply_selection_plan(self.client, plan)
        self.assertEqual(len(self.client.patches), 1)

    def test_unconfirmed_update_is_not_reported_as_successful(self):
        plan = calori.prepare_selection_plan(self.client, self.output_dir)
        with (
            mock.patch.object(self.client, "patch", return_value=[]),
            self.assertRaisesRegex(calori.CaloriError, "after 0 confirmed updates"),
        ):
            calori.apply_selection_plan(self.client, plan)

    def test_select_command_previews_then_applies_and_refreshes_exports(self):
        originals = {path: path.read_bytes() for path in self.output_dir.rglob("*.yaml")}
        arguments = ["select", "--output-dir", str(self.output_dir)]
        with (
            mock.patch("calori.CaloriClient", return_value=self.client),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(calori.main(arguments), 0)
        preview = yaml.safe_load(output.getvalue())
        self.assertEqual(preview["week"], "2026-10-19")
        self.assertEqual(preview["target_calories"], 2500)
        self.assertEqual(preview["daily_calories"], {"mon": 2500})
        self.assertEqual(len(preview["meals"]), 2)
        self.assertEqual(sum(meal["calories"] for meal in preview["meals"]), 2500)
        self.assertNotIn("test-subscription", output.getvalue())
        self.assertIn("Preview only", errors.getvalue())
        self.assertEqual(self.client.patches, [])
        self.assertEqual(
            {path: path.read_bytes() for path in self.output_dir.rglob("*.yaml")}, originals
        )
        with (
            mock.patch("calori.CaloriClient", return_value=self.client),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(calori.main([*arguments, "--apply"]), 0)
        self.assertIn("Saved 2 meal changes", errors.getvalue())
        saved = yaml.safe_load((self.output_dir / "2026/week43.yaml").read_text())
        self.assertEqual([meal["meal"] for meal in saved["mon"]], ["Banana Quark", "Apple Delight"])
        self.assertEqual(sum(meal["macros"]["calories"] for meal in saved["mon"]), 2500)
        self.assertEqual(
            yaml.safe_load((self.output_dir / "stats.yaml").read_text()),
            [{"meals": [{"name": "Previous meal", "eaten": 1}]}],
        )

    def test_custom_calorie_target_is_used_in_the_command_preview(self):
        with (
            mock.patch("calori.CaloriClient", return_value=self.client),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                calori.main(["select", "--calories", "3000", "--output-dir", str(self.output_dir)]),
                0,
            )
        preview = yaml.safe_load(output.getvalue())
        self.assertEqual(preview["target_calories"], 3000)
        self.assertEqual(preview["daily_calories"], {"mon": 3000})
        self.assertEqual(self.client.patches, [])

    def test_invalid_calorie_targets_fail_before_network_access(self):
        for target in ["0", "-1", "invalid"]:
            with (
                self.subTest(target=target),
                mock.patch("calori.CaloriClient") as client,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                calori.main(["select", "--calories", target])
            client.assert_not_called()

    def test_impossible_plan_fails_before_changes_to_calori_or_local_files(self):
        self.client.availability = self.client.availability[:2]
        originals = {path: path.read_bytes() for path in self.output_dir.rglob("*.yaml")}
        with (
            mock.patch("calori.CaloriClient", return_value=self.client),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(
                calori.main(["select", "--apply", "--output-dir", str(self.output_dir)]), 1
            )
        self.assertIn("No complete plan", errors.getvalue())
        self.assertEqual(self.client.patches, [])
        self.assertEqual(
            {path: path.read_bytes() for path in self.output_dir.rglob("*.yaml")}, originals
        )


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

    def test_companion_files_preserve_selected_exports_and_use_the_requested_schema(self):
        exports = calori.write_weekly_meals(self.meals, self.monday, 2, self.output_dir)
        originals = {path: path.read_bytes() for path in exports}
        opportunities = calori.export_meal_opportunities(FakeClient(), self.monday, 2)
        opportunities[self.monday][0]["name"] = "Cr\u00e8me: oats & fruit"
        opportunities[self.monday + dt.timedelta(weeks=1)] = []
        exports = calori.write_weekly_meals(
            self.meals, self.monday, 2, self.output_dir, opportunities
        )
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        for offset, week in enumerate([41, 42]):
            path = self.output_dir / "2026" / f"week{week}-meals.yaml"
            monday = self.monday + dt.timedelta(weeks=offset)
            document = yaml.safe_load(path.read_text())
            self.assertEqual(document, {"meals": opportunities[monday]})
            self.assertEqual(exports[path], len(opportunities[monday]))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            for meal in document["meals"]:
                self.assertEqual(list(meal), ["name", "opportunities", "spots"])
                self.assertEqual(meal["opportunities"], len(meal["spots"]))

    def test_companion_files_use_iso_years_at_the_year_boundary(self):
        monday = dt.date(2020, 12, 28)
        opportunities = calori.export_meal_opportunities(FakeClient(), monday, 2)
        exports = calori.write_weekly_meals([], monday, 2, self.output_dir, opportunities)
        self.assertEqual(
            set(exports),
            {
                self.output_dir / "2020/week53.yaml",
                self.output_dir / "2020/week53-meals.yaml",
                self.output_dir / "2021/week01.yaml",
                self.output_dir / "2021/week01-meals.yaml",
            },
        )

    def test_companion_serialization_failure_preserves_all_existing_exports(self):
        opportunities = calori.export_meal_opportunities(FakeClient(), self.monday, 2)
        exports = calori.write_weekly_meals(
            self.meals, self.monday, 2, self.output_dir, opportunities
        )
        originals = {path: path.read_bytes() for path in exports}
        with (
            mock.patch(
                "calori.dump_yaml",
                side_effect=[
                    "mon: []\n",
                    "meals: []\n",
                    "mon: []\n",
                    calori.YAMLError("Invalid YAML"),
                ],
            ),
            self.assertRaises(calori.YAMLError),
        ):
            calori.write_weekly_meals(self.meals, self.monday, 2, self.output_dir, opportunities)
        self.assertEqual({path: path.read_bytes() for path in exports}, originals)


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output_dir = Path(self.directory.name) / "data"
        self.path = self.output_dir / "stats.yaml"

    def write_week(self, monday, names):
        iso_week = monday.isocalendar()
        path = self.output_dir / str(iso_week.year) / f"week{iso_week.week:02d}.yaml"
        days = {day: [] for day in calori.DAY_OFFSETS}
        days["mon"] = [{"meal": name} for name in names]
        calori.write_text(path, calori.dump_week(days, monday))
        return path

    def test_counts_stored_selections_and_excludes_newest_week_and_availability(self):
        paths = [
            self.write_week(
                dt.date(2026, 10, 5), ["Banana Quark", "Apple Delight", "Apple Delight"]
            ),
            self.write_week(
                dt.date(2026, 10, 12), ["Banana Quark", "Banana Quark", "Cr\u00e8me: oats"]
            ),
            self.write_week(dt.date(2026, 10, 19), ["Apple Delight", "Future meal"]),
        ]
        originals = {path: path.read_bytes() for path in paths}
        calori.write_text(self.output_dir / "2026/week44-meals.yaml", "meals: []\n")
        self.assertEqual(calori.write_meal_stats(self.output_dir), {self.path: 3})
        self.assertEqual(
            yaml.safe_load(self.path.read_text()),
            [
                {
                    "meals": [
                        {"name": "Apple Delight", "eaten": 2},
                        {"name": "Banana Quark", "eaten": 3},
                        {"name": "Cr\u00e8me: oats", "eaten": 1},
                    ]
                }
            ],
        )
        self.assertEqual({path: path.read_bytes() for path in paths}, originals)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_empty_history_and_single_week_produce_empty_stats(self):
        for names in [None, ["Apple Delight"]]:
            with self.subTest(names=names):
                if names is not None:
                    self.write_week(dt.date(2026, 10, 19), names)
                self.assertEqual(calori.write_meal_stats(self.output_dir), {self.path: 0})
                self.assertEqual(yaml.safe_load(self.path.read_text()), [{"meals": []}])

    def test_newest_week_uses_the_iso_year_not_only_the_week_number(self):
        self.write_week(dt.date(2020, 12, 28), ["Apple Delight", "Apple Delight"])
        self.write_week(dt.date(2021, 1, 4), ["Future meal"])
        calori.write_meal_stats(self.output_dir)
        self.assertEqual(
            yaml.safe_load(self.path.read_text()),
            [{"meals": [{"name": "Apple Delight", "eaten": 2}]}],
        )

    def test_rebuilds_counts_without_double_counting_and_advances_the_excluded_week(self):
        self.write_week(dt.date(2026, 10, 5), ["Apple Delight"])
        self.write_week(dt.date(2026, 10, 12), ["Banana Quark"])
        calori.write_meal_stats(self.output_dir)
        original = self.path.read_bytes()
        calori.write_meal_stats(self.output_dir)
        self.assertEqual(self.path.read_bytes(), original)
        self.write_week(dt.date(2026, 10, 5), ["Apple Delight", "Apple Delight"])
        self.write_week(dt.date(2026, 10, 19), ["Future meal"])
        calori.write_meal_stats(self.output_dir)
        self.assertEqual(
            yaml.safe_load(self.path.read_text()),
            [
                {
                    "meals": [
                        {"name": "Apple Delight", "eaten": 2},
                        {"name": "Banana Quark", "eaten": 1},
                    ]
                }
            ],
        )

    def test_invalid_history_preserves_existing_stats(self):
        history = self.write_week(dt.date(2026, 10, 5), ["Apple Delight"])
        self.write_week(dt.date(2026, 10, 19), ["Future meal"])
        calori.write_meal_stats(self.output_dir)
        original = self.path.read_bytes()
        invalid_meal = {day: [] for day in calori.DAY_OFFSETS}
        invalid_meal["mon"] = [{"meal": None}]
        for document in [[], {}, dict.fromkeys(calori.DAY_OFFSETS), invalid_meal]:
            with self.subTest(document=document):
                calori.write_text(history, calori.dump_yaml(document))
                with self.assertRaisesRegex(calori.CaloriError, "Invalid"):
                    calori.write_meal_stats(self.output_dir)
                self.assertEqual(self.path.read_bytes(), original)


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

    def test_patch_refreshes_authorization_and_retries_the_same_update_once(self):
        self.save()
        client = calori.CaloriClient(self.path)
        body = {"meal_id": "new-meal", "is_initial": False}
        params = {"id": "eq.selection"}
        saved = [{"id": "selection", "meal_id": "new-meal"}]
        with mock.patch(
            "calori.request_json", side_effect=[calori.APIError(401), self.tokens, saved]
        ) as request:
            self.assertEqual(client.patch("/rest/v1/sub_meal_selections", body, params), saved)
        for call in [request.call_args_list[0], request.call_args_list[2]]:
            self.assertEqual(call.args[3], params)
            self.assertEqual(call.kwargs, {"body": body, "method": "PATCH"})
        with mock.patch("calori.request_json", side_effect=calori.APIError(403)) as request:
            with self.assertRaises(calori.APIError):
                client.patch("/rest/v1/sub_meal_selections", body, params)
            self.assertEqual(request.call_count, 1)

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

    def test_patch_requests_the_saved_row_and_sends_json(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'[{"id":"selection","meal_id":"new-meal"}]')
        body = {"meal_id": "new-meal", "is_initial": False}
        with mock.patch("calori.urllib.request.build_opener", return_value=opener):
            result = calori.request_json(
                "/rest/v1/sub_meal_selections",
                "test-key",
                "test-token",
                {"id": "eq.selection"},
                body,
                method="PATCH",
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.get_header("Prefer"), "return=representation")
        self.assertEqual(json.loads(request.data), body)
        self.assertEqual(result, [{"id": "selection", "meal_id": "new-meal"}])

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
                menu_path = Path(filename).with_stem(Path(filename).stem + "-meals")
                menu = yaml.safe_load(menu_path.read_text())
                self.assertEqual(menu["meals"][0]["opportunities"], 28)
                self.assertIn(str(menu_path), errors.getvalue())
            self.assertEqual(
                yaml.safe_load(Path("data/stats.yaml").read_text()),
                [{"meals": [{"name": "Test meal", "eaten": 28}]}],
            )
            self.assertIn("data/stats.yaml", errors.getvalue())
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
            menu_path = output_dir / "2026/week41-meals.yaml"
            stats_path = output_dir / "stats.yaml"
            self.assertEqual(set(output_dir.rglob("*.yaml")), {path, menu_path, stats_path})
            self.assertEqual(yaml.safe_load(menu_path.read_text())["meals"][0]["opportunities"], 28)
            self.assertEqual(yaml.safe_load(stats_path.read_text()), [{"meals": []}])
            self.assertEqual(output.getvalue(), "")
            self.assertIn("Exported 28 meals", errors.getvalue())

    def test_invalid_menu_writes_no_files(self):
        client = FakeClient()
        client.templates = []
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.chdir(directory),
            mock.patch("calori.CaloriClient", return_value=client),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(calori.main(["fetch"]), 1)
            self.assertFalse(Path("data").exists())
        self.assertIn("meal template", errors.getvalue())

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
