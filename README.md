# Calori Data Fetcher

Export your selected Calori meals as weekly YAML files. The command uses the same API as the web dashboard.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.13.0 or later
- A Calori account with an active web subscription

## Setup

From this repository directory, run:

```sh
uv sync --locked
```

uv installs Python 3.15.0 from [.python-version](.python-version) and creates a local virtual environment.
The project requires Python 3.15 or newer. ruamel.yaml writes the YAML files and their date comments.
Ruff, ty, PyYAML, and the PyYAML type stubs are development dependencies. [uv.lock](uv.lock) pins the dependency versions.

## Sign In

From this repository directory, run:

```sh
uv run calori.py login
```

Enter your email address. At the password prompt, enter your password directly in the terminal.

This login creates a separate session from your browser. The command stores tokens locally, but it does not save your password.
The command refreshes the session automatically.

If Calori expires or revokes the session, run the login command again.

## Export Meals

To export the current week and next week, run:

```sh
uv run calori.py fetch
```

On October 10, 2026, this command writes:

```text
data/2026/week41.yaml
data/2026/week42.yaml
```

The default week starts on Monday in the Europe/Helsinki time zone.
Each filename uses the Monday's ISO week number and ISO year. Week numbers have two digits.
For example, Monday December 30, 2019 belongs to ISO week 01 of 2020.
A complete four-meal, seven-day plan contains 28 meals per file.

For one specific week, run:

```sh
uv run calori.py fetch --week 2026-10-05 --weeks 1
```

For a different output directory, run:

```sh
uv run calori.py fetch --output-dir exports
```

The command sends errors and file summaries to stderr. It does not write meal data to stdout.
It fetches and serializes all requested weeks before it writes files.
Each file replacement is atomic. Files for other weeks remain unchanged.
A file-system error can leave some weeks updated and others unchanged. A failed command returns a nonzero exit code.

## YAML Format

Each file maps `mon` through `sun` to lists of selected meals, in weekday and meal-slot order.
Each weekday key has its actual date as an inline comment, for example `mon: # 2026-10-05`.
Empty days also have a date comment, for example `mon: [] # 2026-10-05`.
Meal entries omit `date` and `day_of_week`.
Macros remain numbers.

Each meal contains these keys in order:

- `meal`, with the meal name
- `time`, with the meal slot
- `image`, with the main meal image URL or `null` if no URL exists
- `macros`, with the selected meal's `calories`, `protein`, `carbs`, and `fat`

The exporter uses only the meal name and main image URL from the template.
It omits the `template` section, including ingredients and template nutrition, and all ID fields.
The selected-meal macros remain unchanged.

The command selects the newest subscription with an active, trialing, or past-due status, as the dashboard does.
Days without selected meals contain empty lists. All seven weekday keys remain present, including in empty weeks.
Missing menu weeks cause an error.

## Local Data

The command keeps its session in `.calori/session.json`, next to [calori.py](calori.py). The session format remains JSON.
The session directory uses permission `0700`. The session file uses permission `0600`.
The tokens are not encrypted on disk.

Do not share the session file. Its tokens grant account access.

[.gitignore](.gitignore) excludes the local session directory and the `data` export directory.
The fetch command does not change your meals or subscription.

Calori announces a future shutdown of the web service, with no fixed date. Changes to the web API can break this command.

## Development

Run the checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run python -m unittest -v
```

For safe lint fixes and formatting, run:

```sh
uv run ruff check --fix .
uv run ruff format .
```

[pyproject.toml](pyproject.toml) contains the project, Ruff, and ty configuration.
Both checkers target Python 3.15.
