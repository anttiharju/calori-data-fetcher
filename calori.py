from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import networkx as nx
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

DASHBOARD_URL = "https://www.calori.fi/en/dashboard"
API_URL = "https://qnlcyahsjuladxeiehcq.supabase.co"
PROJECT_ID = "qnlcyahsjuladxeiehcq"
SESSION_PATH = Path(__file__).resolve().parent / ".calori" / "session.json"
SELECTION_FIELDS = (
    "day_of_week,meal_time,week_id,"
    "meals!sub_meal_selections_meal_id_fkey("
    "id,calories,protein,carbs,fat,"
    "meal_templates!meals_generic_id_fkey(name,image_url))"
)
DAY_OFFSETS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MEAL_ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2, "snack": 3}
MENU_PAGE_SIZE = 500


class ExportedMeal(TypedDict):
    date: str
    day_of_week: str
    meal_time: str
    name: str | None
    image_url: str | None
    macros: dict[str, float | None]


class MealOpportunity(TypedDict):
    name: str
    opportunities: int
    spots: list[str]


class PlannedSelection(TypedDict):
    slot: tuple[str, str]
    id: str
    previous_meal_id: str
    meal_id: str
    name: str


class SelectionPlan(TypedDict):
    monday: dt.date
    week_id: str
    subscription_id: str
    meals: list[PlannedSelection]


class CaloriError(Exception):
    pass


class APIError(CaloriError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Calori returned HTTP {status}.")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(path, api_key, access_token=None, params=None, body=None, *, method=None):
    url = API_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"apikey": api_key, "Accept": "application/json"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if method == "PATCH":
        headers["Prefer"] = "return=representation"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirects).open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        raise APIError(error.code) from None
    except urllib.error.URLError, OSError:
        raise CaloriError("Cannot connect to Calori. Review your network connection.") from None
    except ValueError, UnicodeError:
        raise CaloriError("Calori did not return valid JSON.") from None


class ScriptSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        source = dict(attrs).get("src")
        if tag == "script" and source:
            url = urllib.parse.urljoin(DASHBOARD_URL, source)
            parsed = urllib.parse.urlparse(url)
            if (
                parsed.scheme == "https"
                and parsed.netloc == "www.calori.fi"
                and parsed.path.startswith("/_next/static/chunks/")
            ):
                self.sources.append(url)


def read_public_text(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError, OSError, UnicodeError:
        raise CaloriError("Cannot read the public Calori client configuration.") from None


def find_public_key(source):
    for candidate in re.findall(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", source):
        try:
            payload = candidate.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except ValueError, UnicodeError:
            continue
        if (
            isinstance(claims, dict)
            and claims.get("role") == "anon"
            and claims.get("ref") == PROJECT_ID
        ):
            return candidate
    return None


def discover_public_key():
    parser = ScriptSources()
    parser.feed(read_public_text("https://www.calori.fi/en/signin"))
    for source in dict.fromkeys(parser.sources):
        api_key = find_public_key(read_public_text(source))
        if api_key:
            return api_key
    raise CaloriError("The Calori client configuration changed. No public API key was found.")


def write_text(path: Path | str, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def dump_yaml(document) -> str:
    serializer = YAML()
    serializer.allow_unicode = True
    serializer.default_flow_style = False
    with StringIO() as output:
        serializer.dump(document, output)
        return output.getvalue()


def dump_week(days, monday: dt.date) -> str:
    document = CommentedMap(days)
    for day, offset in DAY_OFFSETS.items():
        date = (monday + dt.timedelta(days=offset)).isoformat()
        document.yaml_add_eol_comment(date, key=day, column=5)
    return dump_yaml(document)


def write_weekly_meals(
    meals: list[ExportedMeal],
    starting_date: dt.date,
    week_count: int,
    output_dir: Path,
    opportunities_by_week: dict[dt.date, list[MealOpportunity]] | None = None,
) -> dict[Path, int]:
    weeks: dict[dt.date, list[ExportedMeal]] = {
        starting_date + dt.timedelta(weeks=offset): [] for offset in range(week_count)
    }
    for meal in meals:
        day = dt.date.fromisoformat(meal["date"])
        monday = day - dt.timedelta(days=day.weekday())
        if monday not in weeks:
            raise CaloriError("A meal falls outside the requested weeks.")
        weeks[monday].append(meal)

    exports = []
    for monday, week_meals in weeks.items():
        iso_week = monday.isocalendar()
        path = output_dir / str(iso_week.year) / f"week{iso_week.week:02d}.yaml"
        days = {day: [] for day in DAY_OFFSETS}
        for meal in week_meals:
            days[meal["day_of_week"]].append(
                {
                    "meal": meal["name"],
                    "time": meal["meal_time"],
                    "image": meal["image_url"],
                    "macros": meal["macros"],
                }
            )
        text = dump_week(days, monday)
        exports.append((path, len(week_meals), text))
        if opportunities_by_week is not None:
            opportunities = opportunities_by_week[monday]
            menu_path = path.with_stem(path.stem + "-meals")
            exports.append((menu_path, len(opportunities), dump_yaml({"meals": opportunities})))

    for path, _count, text in exports:
        write_text(path, text)
    return {path: count for path, count, _text in exports}


def exported_weeks(output_dir: Path) -> list[tuple[dt.date, Path]]:
    weeks = []
    for path in output_dir.glob("[0-9][0-9][0-9][0-9]/week[0-9][0-9].yaml"):
        try:
            monday = dt.date.fromisocalendar(int(path.parent.name), int(path.stem[4:]), 1)
        except ValueError:
            raise CaloriError(f"Invalid ISO week in {path}.") from None
        weeks.append((monday, path))
    return sorted(weeks)


def count_selected_meals(weeks: list[tuple[dt.date, Path]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    reader = YAML(typ="safe")
    for _monday, path in weeks:
        days = reader.load(path.read_text(encoding="utf-8"))
        if not isinstance(days, dict) or set(days) != set(DAY_OFFSETS):
            raise CaloriError(f"Invalid weekly export: {path}.")
        for day_meals in days.values():
            if not isinstance(day_meals, list):
                raise CaloriError(f"Invalid meal list in {path}.")
            for meal in day_meals:
                name = meal.get("meal") if isinstance(meal, dict) else None
                if not isinstance(name, str) or not name.strip():
                    raise CaloriError(f"Invalid meal name in {path}.")
                counts[name] += 1
    return counts


def write_meal_stats(output_dir: Path) -> dict[Path, int]:
    counts = count_selected_meals(exported_weeks(output_dir)[:-1])
    meals = [
        {"name": name, "eaten": counts[name]}
        for name in sorted(counts, key=lambda name: (name.casefold(), name))
    ]
    path = output_dir / "stats.yaml"
    write_text(path, dump_yaml([{"meals": meals}]))
    return {path: len(meals)}


def save_session(path, api_key, tokens):
    if not isinstance(tokens, dict) or not all(
        isinstance(tokens.get(field), str) and tokens[field]
        for field in ("access_token", "refresh_token")
    ):
        raise CaloriError(
            "Calori did not return a usable session. Sign in through the dashboard to review your account."
        )
    expires_at = tokens.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        expires_in = tokens.get("expires_in", 3600)
        if not isinstance(expires_in, (int, float)):
            raise CaloriError("Calori returned an invalid session expiry.")
        expires_at = time.time() + expires_in
    session = {
        "api_key": api_key,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": expires_at,
    }
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    write_json(path, session)
    return session


class CaloriClient:
    def __init__(self, session_path=SESSION_PATH):
        self.session_path = Path(session_path)
        try:
            self.session = json.loads(self.session_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            raise CaloriError(
                "No usable local session exists. Run: uv run calori.py login"
            ) from None
        if not isinstance(self.session, dict) or not all(
            isinstance(self.session.get(field), str) and self.session[field]
            for field in ("api_key", "access_token", "refresh_token")
        ):
            raise CaloriError("The local session is invalid. Run: uv run calori.py login")
        if not isinstance(self.session.get("expires_at"), (int, float)):
            raise CaloriError("The local session expiry is invalid. Run: uv run calori.py login")

    def refresh(self):
        try:
            tokens = request_json(
                "/auth/v1/token",
                self.session["api_key"],
                params={"grant_type": "refresh_token"},
                body={"refresh_token": self.session["refresh_token"]},
            )
        except APIError as error:
            if error.status in (400, 401, 403):
                raise CaloriError(
                    "The session expired or was revoked. Run: uv run calori.py login"
                ) from None
            raise
        self.session = save_session(self.session_path, self.session["api_key"], tokens)

    def request(self, path, params=None, *, body=None, method=None):
        if self.session["expires_at"] <= time.time() + 60:
            self.refresh()
        try:
            return request_json(
                path,
                self.session["api_key"],
                self.session["access_token"],
                params,
                body=body,
                method=method,
            )
        except APIError as error:
            if error.status != 401:
                raise
        self.refresh()
        return request_json(
            path,
            self.session["api_key"],
            self.session["access_token"],
            params,
            body=body,
            method=method,
        )

    def get(self, path, params=None):
        return self.request(path, params)

    def patch(self, path, body, params):
        return self.request(path, params, body=body, method="PATCH")


def single_relation(value, label):
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict):
        raise CaloriError(f"The API returned a missing or ambiguous {label}.")
    return value


def normalize_meals(rows, starting_date: dt.date) -> list[ExportedMeal]:
    result: list[ExportedMeal] = []
    seen = set()
    for row in rows:
        day = row.get("day_of_week")
        meal_time = row.get("meal_time")
        if day not in DAY_OFFSETS or meal_time not in MEAL_ORDER:
            raise CaloriError("The API returned an unknown day or meal slot.")
        slot = (day, meal_time)
        if slot in seen:
            raise CaloriError("The API returned duplicate meal slots.")
        seen.add(slot)
        meal = single_relation(row.get("meals"), "meal")
        template = single_relation(meal.get("meal_templates"), "meal template")
        result.append(
            {
                "date": (starting_date + dt.timedelta(days=DAY_OFFSETS[day])).isoformat(),
                "day_of_week": day,
                "meal_time": meal_time,
                "name": template.get("name"),
                "image_url": template.get("image_url"),
                "macros": {
                    field: meal.get(field) for field in ("calories", "protein", "carbs", "fat")
                },
            }
        )
    return sorted(result, key=lambda meal: (meal["date"], MEAL_ORDER[meal["meal_time"]]))


def rank_meal_opportunities(rows, templates) -> list[MealOpportunity]:
    names = {template["id"]: template.get("name") for template in templates}
    spots_by_name: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        day = row.get("day_of_week")
        meal_time = row.get("meal_time")
        if day not in DAY_OFFSETS or meal_time not in MEAL_ORDER:
            raise CaloriError("The API returned an unknown day or meal slot.")
        name = names.get(row.get("generic_id"))
        if not isinstance(name, str) or not name.strip():
            raise CaloriError("The API returned a missing or unnamed meal template.")
        spots_by_name.setdefault(name, set()).add((day, meal_time))

    result: list[MealOpportunity] = []
    for name, spots in spots_by_name.items():
        ordered_spots = sorted(spots, key=lambda spot: (DAY_OFFSETS[spot[0]], MEAL_ORDER[spot[1]]))
        result.append(
            {
                "name": name,
                "opportunities": len(spots),
                "spots": [f"{day}/{meal_time}" for day, meal_time in ordered_spots],
            }
        )
    return sorted(
        result, key=lambda meal: (meal["opportunities"], meal["name"].casefold(), meal["name"])
    )


def plan_unique_meals(
    opportunities: list[MealOpportunity],
    eaten: set[str],
    slots: list[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    if not slots or len(slots) != len(set(slots)):
        raise CaloriError("The week has missing or duplicate meal slots.")
    if any(day not in DAY_OFFSETS or meal_time not in MEAL_ORDER for day, meal_time in slots):
        raise CaloriError("The API returned an unknown day or meal slot.")
    ordered_slots = sorted(slots, key=lambda slot: (DAY_OFFSETS[slot[0]], MEAL_ORDER[slot[1]]))
    available = {meal["name"]: meal for meal in opportunities if meal["name"] not in eaten}
    names = sorted(
        available,
        key=lambda name: (available[name]["opportunities"], name.casefold(), name),
    )
    slot_nodes = {slot: index for index, slot in enumerate(ordered_slots)}
    graph = nx.Graph()
    graph.add_nodes_from(slot_nodes.values(), bipartite=0)
    for index, name in enumerate(names, start=len(ordered_slots)):
        for spot in available[name]["spots"]:
            day, _separator, meal_time = spot.partition("/")
            slot = (day, meal_time)
            if slot in slot_nodes:
                graph.add_edge(slot_nodes[slot], index)
    matches = nx.algorithms.bipartite.maximum_matching(graph, top_nodes=slot_nodes.values())
    plan = {
        slot: names[matches[node] - len(ordered_slots)]
        for slot, node in slot_nodes.items()
        if node in matches
    }
    if len(plan) != len(slots):
        raise CaloriError(
            "No complete plan exists with uneaten, unique meals. "
            f"Only {len(plan)} of {len(slots)} slots can be filled."
        )
    return plan


def get_week_id(client, monday, *, require_open=False):
    records = client.get(
        "/rest/v1/weeks",
        {"select": "id,starting_date,is_closed", "starting_date": "eq." + monday.isoformat()},
    )
    if not isinstance(records, list) or len(records) != 1:
        raise CaloriError(f"No unique menu exists for the week of {monday}.")
    if require_open and records[0].get("is_closed") is not False:
        raise CaloriError(f"The week of {monday} is closed for selections.")
    return records[0]["id"]


def get_menu_rows(client, path, params):
    rows = []
    while True:
        page = client.get(
            path,
            {
                **params,
                "order": "id",
                "limit": str(MENU_PAGE_SIZE),
                "offset": str(len(rows)),
            },
        )
        if not isinstance(page, list):
            raise CaloriError("The API did not return a list of menu rows.")
        rows.extend(page)
        if len(page) < MENU_PAGE_SIZE:
            return rows


def export_meal_opportunities(client, starting_date, week_count):
    if starting_date.weekday() != 0 or week_count < 1:
        raise CaloriError("Use a Monday and at least one week.")
    templates = get_menu_rows(client, "/rest/v1/meal_templates", {"select": "id,name"})
    result = {}
    for offset in range(week_count):
        monday = starting_date + dt.timedelta(weeks=offset)
        week_id = get_week_id(client, monday)
        rows = get_menu_rows(
            client,
            "/rest/v1/consumption_slots_mixed",
            {"select": "day_of_week,meal_time,generic_id", "week_id": "eq." + week_id},
        )
        result[monday] = rank_meal_opportunities(rows, templates)
    return result


def get_subscription_id(client):
    user = client.get("/auth/v1/user")
    if not isinstance(user, dict) or not user.get("id"):
        raise CaloriError("The API did not return an authenticated user.")
    subscriptions = client.get(
        "/rest/v1/subscriptions",
        {
            "select": "id",
            "user_id": "eq." + user["id"],
            "status": "in.(trialing,active,past_due)",
            "order": "created.desc",
            "limit": "1",
        },
    )
    if not subscriptions:
        raise CaloriError("No active subscription exists for this account.")
    return subscriptions[0]["id"]


def get_selections(client, subscription_id, week_id, fields=SELECTION_FIELDS):
    rows = client.get(
        "/rest/v1/sub_meal_selections",
        {"select": fields, "sub_id": "eq." + subscription_id, "week_id": "eq." + week_id},
    )
    if not isinstance(rows, list):
        raise CaloriError("The API did not return a list of meal selections.")
    return rows


def export_meals(client, starting_date, week_count):
    if starting_date.weekday() != 0 or week_count < 1:
        raise CaloriError("Use a Monday and at least one week.")
    subscription_id = get_subscription_id(client)
    meals = []
    for offset in range(week_count):
        week_start = starting_date + dt.timedelta(weeks=offset)
        week_id = get_week_id(client, week_start)
        rows = get_selections(client, subscription_id, week_id)
        meals.extend(normalize_meals(rows, week_start))
    return meals


def selection_state(rows) -> dict[tuple[str, str], tuple[str, str]]:
    state = {}
    for row in rows:
        slot = (row.get("day_of_week"), row.get("meal_time"))
        if (
            slot[0] not in DAY_OFFSETS
            or slot[1] not in MEAL_ORDER
            or not all(
                isinstance(row.get(field), str) and row[field] for field in ("id", "meal_id")
            )
        ):
            raise CaloriError("The API returned an invalid meal selection.")
        if slot in state:
            raise CaloriError("The API returned duplicate meal slots.")
        state[slot] = (row["id"], row["meal_id"])
    if not state:
        raise CaloriError(
            "The latest week has no selected meal slots. Run the fetch command again."
        )
    return state


def prepare_selection_plan(client, output_dir: Path) -> SelectionPlan:
    weeks = exported_weeks(output_dir)
    if not weeks:
        raise CaloriError("No exported weeks exist. Run: uv run calori.py fetch")
    monday = weeks[-1][0]
    if monday < current_monday():
        raise CaloriError("The latest exported week is in the past. Run the fetch command again.")
    eaten = set(count_selected_meals(weeks[:-1]))
    week_id = get_week_id(client, monday, require_open=True)
    subscription_id = get_subscription_id(client)
    selections = get_selections(
        client, subscription_id, week_id, "id,meal_id,day_of_week,meal_time"
    )
    state = selection_state(selections)
    templates = get_menu_rows(
        client, "/rest/v1/meal_templates", {"select": "id,name,small_id,large_id"}
    )
    by_id = {template["id"]: template for template in templates}
    variants = {}
    for field in ("small_id", "large_id"):
        for template in templates:
            if template.get(field):
                variants[template[field]] = field
    if any(meal_id not in variants for _selection_id, meal_id in state.values()):
        raise CaloriError("Cannot determine the size of a current meal.")
    rows = get_menu_rows(
        client,
        "/rest/v1/consumption_slots_mixed",
        {"select": "day_of_week,meal_time,generic_id", "week_id": "eq." + week_id},
    )
    eligible = []
    candidates: dict[tuple[tuple[str, str], str], str] = {}
    for row in rows:
        slot = (row.get("day_of_week"), row.get("meal_time"))
        if slot not in state:
            continue
        template = by_id.get(row.get("generic_id"))
        if template is None:
            raise CaloriError("The API returned a missing meal template.")
        name = template.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CaloriError("The API returned a missing or unnamed meal template.")
        meal_id = template.get(variants[state[slot][1]])
        if isinstance(meal_id, str) and meal_id:
            eligible.append(row)
            candidates.setdefault((slot, name), meal_id)
    names = plan_unique_meals(rank_meal_opportunities(eligible, templates), eaten, list(state))
    return {
        "monday": monday,
        "week_id": week_id,
        "subscription_id": subscription_id,
        "meals": [
            {
                "slot": slot,
                "id": state[slot][0],
                "previous_meal_id": state[slot][1],
                "meal_id": candidates[slot, name],
                "name": name,
            }
            for slot, name in names.items()
        ],
    }


def apply_selection_plan(client, plan: SelectionPlan) -> int:
    if plan["monday"] < current_monday():
        raise CaloriError("The planned week is in the past. Run the fetch command again.")
    if get_week_id(client, plan["monday"], require_open=True) != plan["week_id"]:
        raise CaloriError("The menu week changed. Run the fetch command again.")
    fields = "id,meal_id,day_of_week,meal_time"
    current = selection_state(
        get_selections(client, plan["subscription_id"], plan["week_id"], fields)
    )
    expected = {meal["slot"]: (meal["id"], meal["previous_meal_id"]) for meal in plan["meals"]}
    if current != expected:
        raise CaloriError("Your selections changed during planning. Run the select command again.")

    updated = 0
    try:
        for meal in plan["meals"]:
            if meal["meal_id"] == meal["previous_meal_id"]:
                continue
            day, meal_time = meal["slot"]
            saved = client.patch(
                "/rest/v1/sub_meal_selections",
                {
                    "meal_id": meal["meal_id"],
                    "is_initial": False,
                    "created_at": dt.datetime.now(dt.UTC).isoformat(),
                },
                {
                    "select": "id,meal_id",
                    "id": "eq." + meal["id"],
                    "sub_id": "eq." + plan["subscription_id"],
                    "week_id": "eq." + plan["week_id"],
                    "day_of_week": "eq." + day,
                    "meal_time": "eq." + meal_time,
                    "meal_id": "eq." + meal["previous_meal_id"],
                },
            )
            if saved != [{"id": meal["id"], "meal_id": meal["meal_id"]}]:
                raise CaloriError(f"Calori did not confirm the update for {day}/{meal_time}.")
            updated += 1
        saved_state = selection_state(
            get_selections(client, plan["subscription_id"], plan["week_id"], fields)
        )
        if saved_state != {meal["slot"]: (meal["id"], meal["meal_id"]) for meal in plan["meals"]}:
            raise CaloriError("The saved selections do not match the plan.")
    except CaloriError as error:
        raise CaloriError(
            f"Selection stopped after {updated} confirmed updates. "
            "Changes can already be saved. Run fetch before retrying. " + str(error)
        ) from None
    return updated


def current_monday(now=None):
    try:
        today = (now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo("Europe/Helsinki")).date()
    except ZoneInfoNotFoundError:
        raise CaloriError(
            "The Helsinki time zone is unavailable. Supply the Monday with --week YYYY-MM-DD."
        ) from None
    return today - dt.timedelta(days=today.weekday())


def parse_monday(value):
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a date in YYYY-MM-DD format.") from None
    if day.weekday() != 0:
        raise argparse.ArgumentTypeError("The date must be a Monday.")
    return day


def positive_integer(value):
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a positive integer.") from None
    if count < 1:
        raise argparse.ArgumentTypeError("Use a positive integer.")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export Calori meals or select unique, uneaten meals."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Sign in and store a private local session.")
    login.add_argument(
        "--email", help="Your Calori email address. The command prompts for it by default."
    )
    fetch = commands.add_parser("fetch", help="Export the current week and next week by default.")
    fetch.add_argument(
        "--week",
        type=parse_monday,
        help="First Monday, in YYYY-MM-DD format. Default: current week in Helsinki.",
    )
    fetch.add_argument(
        "--weeks", type=positive_integer, default=2, help="Number of weeks. Default: 2."
    )
    fetch.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data"),
        help="Directory for weekly YAML files and stats.yaml. Default: data.",
    )
    select = commands.add_parser(
        "select", help="Preview unique, uneaten meals for the latest exported week."
    )
    select.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data"),
        help="Directory with weekly exports and meal history. Default: data.",
    )
    select.add_argument(
        "--apply", action="store_true", help="Save the plan to Calori. Default: preview only."
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "login":
            if not sys.stdin.isatty():
                raise CaloriError(
                    "Run login in an interactive terminal. Do not pipe a password into this command."
                )
            api_key = discover_public_key()
            email = args.email or input("Email: ").strip()
            password = getpass.getpass("Password: ")
            if not email or not password:
                raise CaloriError("The email address and password are required.")
            tokens = request_json(
                "/auth/v1/token",
                api_key,
                params={"grant_type": "password"},
                body={"email": email, "password": password},
            )
            save_session(SESSION_PATH, api_key, tokens)
            print(
                "Signed in. The local session is private. The password was not saved.",
                file=sys.stderr,
            )
        else:
            client = CaloriClient()
            if args.command == "select":
                plan = prepare_selection_plan(client, args.output_dir)
                print(
                    dump_yaml(
                        {
                            "week": plan["monday"].isoformat(),
                            "meals": [
                                {"spot": "/".join(meal["slot"]), "name": meal["name"]}
                                for meal in plan["meals"]
                            ],
                        }
                    ),
                    end="",
                )
                if not args.apply:
                    print(
                        "Preview only. No selections changed. Add --apply to save.", file=sys.stderr
                    )
                    return 0
                updated = apply_selection_plan(client, plan)
                starting_date = plan["monday"]
                week_count = 1
                print(f"Saved {updated} meal changes for {starting_date}.", file=sys.stderr)
            else:
                starting_date = args.week or current_monday()
                week_count = args.weeks
            meals = export_meals(client, starting_date, week_count)
            opportunities = export_meal_opportunities(client, starting_date, week_count)
            exports = write_weekly_meals(
                meals, starting_date, week_count, args.output_dir, opportunities
            )
            exports.update(write_meal_stats(args.output_dir))
            for path, count in exports.items():
                print(f"Exported {count} meals to {path}.", file=sys.stderr)
        return 0
    except (CaloriError, OSError, YAMLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except EOFError, KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
