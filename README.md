# Calori Optimizer

Export your selected Calori meals as weekly YAML files.
The statistics include every meal name from the Calori catalog, with counts from all stored weeks.
The `select` command plans meals near a daily calorie target for the latest exported week.
The command uses the same API as the web dashboard.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.13.0 or later
- A Calori account with an active web subscription

## Setup

From this repository directory, run:

```sh
uv sync --locked
```

uv installs Python 3.14 from [.python-version](.python-version) and creates a local virtual environment.
The project requires Python 3.14. PuLP 4 and its HiGHS solver have prebuilt macOS wheels for this version.
ruamel.yaml writes the YAML files and their date comments.
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

On October 13, 2026, `tree --dirsfirst data` shows:

```text
data
├── 2026
│   ├── week41.yaml
│   ├── week42.yaml
│   └── week43.yaml
└── stats.yaml

2 directories, 4 files
```

The directory includes selected-meal exports from earlier weeks.

The default week starts on Monday in the Europe/Helsinki time zone.
Each filename uses the Monday's ISO week number and ISO year. Week numbers have two digits.
For example, Monday December 30, 2019 belongs to ISO week 01 of 2020.
A complete four-meal, seven-day plan contains 28 selected meals per week.

For one specific week, run:

```sh
uv run calori.py fetch --week 2026-10-19 --weeks 1
```

For a different output directory, run:

```sh
uv run calori.py fetch --output-dir exports
```

The command sends errors and file summaries to stderr. It does not write meal data to stdout.
It fetches and serializes all requested weeks before it writes weekly files.
It then rebuilds the meal statistics from the stored weekly files.
Each file replacement is atomic. Files for other weeks remain unchanged.
A file-system error can leave some weeks updated and others unchanged. A failed command returns a nonzero exit code.

## Select Meals

To preview a plan near 2,500 calories per day for the latest exported week, run:

```sh
uv run calori.py select
```

The preview shows meal names, slots, calories, and historical eaten counts as YAML on stdout.
It also shows the calorie total for each day.
It does not change your selections or meal exports.

For a different daily calorie target, run:

```sh
uv run calori.py select --calories 2400
```

To prefer uneaten meals with at least 2,100 calories every day, run:

```sh
uv run calori.py select --kcal-floor 2100
```

The floor is a hard minimum for each day, not a weekly average.
This mode prioritizes meal variety before calorie closeness. The preview includes the requested `kcal_floor`.

To save the plan to Calori, run:

```sh
uv run calori.py select --apply
```

To save a plan with a daily floor, run:

```sh
uv run calori.py select --kcal-floor 2100 --apply
```

To require a specific meal in the plan, run:

```sh
uv run calori.py select --kcal-floor 2164 --include "Peanut Butter Delight"
```

To save that plan, run:

```sh
uv run calori.py select --kcal-floor 2164 --include "Peanut Butter Delight" --apply
```

The `--include` option requires an exact meal name. Each included meal must appear once in the week, even if you previously ate it.
Repeated flags can require multiple meals. Repeated requests for the same name still require only one occurrence.

The preview lists included names under `include`. The planner keeps the active mode's optimization priorities for the other meals.
The option also works without `--kcal-floor`.

The `--apply` option changes your selections on Calori. Each run builds a fresh plan from the live API menu.
The planner reads its preference history directly from earlier weekly exports.
It excludes the target week that it replaces, although the statistics include that week.
Previously eaten meals remain eligible.

The planner uses PuLP 4 and HiGHS to optimize the whole week together.
Each meal name can appear only once in the week, even across different portion sizes.
Without `--kcal-floor`, the planner applies these priorities in order:

1. Smallest sum of absolute daily differences from the calorie target.
2. Smallest difference for the day furthest from the target.
3. Lowest total historical eaten count for the week.

With `--kcal-floor`, every day must meet the floor. The planner then applies these priorities in order:

1. Most meals absent from the preference history.
2. Lowest total historical eaten count for the week.
3. Smallest sum of absolute daily differences from the calorie target.
4. Smallest difference for the day furthest from the target.

The `--calories` option remains a soft target in floor mode. It is not an upper limit.
Meal names and variant IDs define stable weights for remaining ties.
The command preserves each slot's meal size and does not add slots.
Calories come from the actual meal variants, not template nutrition. Exact daily totals are not always possible.
Defaults remain eligible, regardless of the website chooser filters.

If the week has no full plan with unique meals, the command stops without changes.
Unavailable included meals and conflicting inclusion requests also stop the command without changes.
An impossible calorie floor also stops the command without changes. The planner never lowers the floor automatically.
Each optimization stage has a 30-second limit. The command stops if the solver cannot prove an optimal result within that limit.
Missing or invalid calorie values also cause an error.
Closed weeks and weeks in the past cause an error.

Before the first update, the command compares your live selections with the original plan data.
Each update also requires the previous meal to remain unchanged in that slot.
After a successful save, the command refreshes that week's selected meals and the meal statistics.
Calori saves one slot per request. A failed request can leave some changes saved.

If the command reports a partial failure, run the fetch command before you try again.

For a different export directory, run:

```sh
uv run calori.py select --output-dir exports
```

## YAML Formats

### Selected Meals

Each `weekNN.yaml` file maps `mon` through `sun` to lists of selected meals, in weekday and meal-slot order.
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

### Meal Statistics

Each fetch rebuilds `stats.yaml` in the output directory from all stored `YEAR/weekNN.yaml` files.
The statistics include every stored week, including the newest week, across all year directories.
The command includes every distinct name from the Calori meal catalog. Meals without stored selections have a count of zero.
Historical meal names remain present even if the catalog no longer includes them.

Example structure:

```yaml
"Apple Delight": 2
"Uneaten meal": 0
```

Each top-level key is a double-quoted meal name. Its integer value counts each selected occurrence, including repeats on the same day.
These counts come from selected meals, not a consumption log.

Counts use descending order. Equal counts use alphabetical names without case differences.
Each fetch replaces the previous counts, so repeat runs do not increase them.

With no stored selections, every catalog meal has a count of zero. An empty history and empty catalog produce `{}`.
Catalog membership does not guarantee availability for a particular week.

If a historical export is invalid, the command leaves the previous statistics unchanged and returns an error.
The weekly files can already contain updated data at that point.

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
Both checkers target Python 3.14.
