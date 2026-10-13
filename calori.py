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
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


class ExportedMeal(TypedDict):
    date: str
    day_of_week: str
    meal_time: str
    name: str | None
    image_url: str | None
    macros: dict[str, float | None]


class CaloriError(Exception):
    pass


class APIError(CaloriError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Calori returned HTTP {status}.")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(path, api_key, access_token=None, params=None, body=None):
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
    request = urllib.request.Request(url, data=data, headers=headers)
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


def dump_week(days, monday: dt.date) -> str:
    document = CommentedMap(days)
    for day, offset in DAY_OFFSETS.items():
        date = (monday + dt.timedelta(days=offset)).isoformat()
        document.yaml_add_eol_comment(date, key=day, column=5)
    serializer = YAML()
    serializer.allow_unicode = True
    serializer.default_flow_style = False
    with StringIO() as output:
        serializer.dump(document, output)
        return output.getvalue()


def write_weekly_meals(
    meals: list[ExportedMeal],
    starting_date: dt.date,
    week_count: int,
    output_dir: Path,
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

    for path, _count, text in exports:
        write_text(path, text)
    return {path: count for path, count, _text in exports}


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

    def get(self, path, params=None):
        if self.session["expires_at"] <= time.time() + 60:
            self.refresh()
        try:
            return request_json(path, self.session["api_key"], self.session["access_token"], params)
        except APIError as error:
            if error.status != 401:
                raise
        self.refresh()
        return request_json(path, self.session["api_key"], self.session["access_token"], params)


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


def export_meals(client, starting_date, week_count):
    if starting_date.weekday() != 0 or week_count < 1:
        raise CaloriError("Use a Monday and at least one week.")
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
    subscription_id = subscriptions[0]["id"]
    meals = []
    for offset in range(week_count):
        week_start = starting_date + dt.timedelta(weeks=offset)
        records = client.get(
            "/rest/v1/weeks",
            {
                "select": "id,starting_date",
                "starting_date": "eq." + week_start.isoformat(),
            },
        )
        if not isinstance(records, list) or len(records) != 1:
            raise CaloriError(f"No unique menu exists for the week of {week_start}.")
        week_id = records[0]["id"]
        rows = client.get(
            "/rest/v1/sub_meal_selections",
            {
                "select": SELECTION_FIELDS,
                "sub_id": "eq." + subscription_id,
                "week_id": "eq." + week_id,
            },
        )
        if not isinstance(rows, list):
            raise CaloriError("The API did not return a list of meal selections.")
        meals.extend(normalize_meals(rows, week_start))
    return meals


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
    parser = argparse.ArgumentParser(description="Export your Calori meals as weekly YAML files.")
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
        help="Directory for YEAR/weekNN.yaml files. Default: data.",
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
            starting_date = args.week or current_monday()
            meals = export_meals(CaloriClient(), starting_date, args.weeks)
            exports = write_weekly_meals(meals, starting_date, args.weeks, args.output_dir)
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
