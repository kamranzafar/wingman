# Contributing to wingman

Thanks for helping. Bug reports, platform adapters, docs and fixes are all welcome.

## Development setup

```bash
git clone https://github.com/kamranzafar/wingman && cd wingman
uv sync                      # Python 3.11+; creates .venv with dev tools
uv run pytest -q             # tests: no network, no Claude calls, no credentials needed
uv run ruff check src tests  # lint (CI runs both on Linux, macOS and Windows)
```

To try your changes for real, run `uv run wingman init` in a scratch directory (or set `WINGMAN_HOME`)
so your own `wingman.toml`, `.env` and `data/` stay separate.

## Ground rules

- **Human in the loop, always.** Nothing may post without the person seeing the final text and
  confirming. No auto-posting modes, no bulk replies, no ways around pacing.
- **Respect platform rules.** Use official APIs and documented limits. If a community bans
  AI-written content, its adapter sets `ai_drafts_allowed=False` (wingman then only finds threads).
- **Secrets stay local.** Tokens come from `.env` (or the platform's own CLI login), are only sent to
  that platform's host, and are never logged or printed.
- **Tests don't touch the network.** Fake the platform client the way `tests/test_mastodon.py` and
  `tests/test_github.py` do.

## Code layout

```
src/wingman/
├── cli.py          # commands only: gather input, call services/core, hand results to views
├── views.py        # rendering reports (tables, panels); ui.py has the shared primitives
├── services.py     # builds config → store, LLM, platform, drafter (composition root)
├── config.py       # typed settings; every default lives here; unknown keys are errors
├── errors.py       # WingmanError → shown as one line, no traceback
├── sources/        # the person's own writing: blog, pages, GitHub, local files
├── profile/        # the interest/voice profile (build, save, load)
├── core/           # drafting, retrieval, review, ranking, store, pacing, privacy, usage, http
└── platforms/      # one package per platform, behind the Platform protocol
```

## Adding a platform

A platform is a package under `src/wingman/platforms/<name>/`. Use `mastodon/` or `github/` as a
template: each is a small client plus an adapter.

1. **`__init__.py`** exposes:
   - `SETTINGS`: a `frozenset` of the `[platforms.<name>]` keys you read beyond the common ones
     (`enabled`, `sources`, `keywords`, `max_age_hours`, `per_source_limit`). Anything else in the
     section is reported to the user as an error.
   - `create(settings) -> Platform`, importing the adapter lazily.
2. **The adapter** implements the `Platform` protocol in `platforms/base.py`:

   | member | what it does |
   |---|---|
   | `name`, `posting` (`"api"` or `"manual"`), `has_scores` | identity; how replies get posted; whether vote counts exist |
   | `constraints` | length limit and how it's counted, markdown or not, `ai_drafts_allowed`, etiquette notes for the drafter |
   | `discover(sources, since, limit)` | candidate posts as `Thread`s: skip replies, your own posts, bots and anything older than `since` |
   | `fetch_context(thread)` | fill in `top_comments` (best first) and `source_rules` |
   | `thread_from_url(url)` | load one post from its web link, or `None` |
   | `submit(reply)` / `publish(text, link)` | post after approval, return the URL; raise `WingmanError` if unsupported |
   | `my_activity(limit)` | the person's own recent posts and scores, for `stats` and the profile |

3. **Register it:** add the name to `PLATFORMS` in `platforms/__init__.py`. If its post links have a
   recognisable shape, teach `platform_for_url` about them so `wingman draft <url>` needs no `-p`.
4. **Document it:** a commented `[platforms.<name>]` section in `wingman.example.toml` (disabled by
   default), any secrets in `.env.example`, and a section in the README's platform guide.
5. **Test it** with a fake client: discovery filters, context ordering, posting payloads, paging,
   URL recognition, and the error messages for missing credentials.

Raise `ConfigError` for anything the user must fix in their config or `.env` (shown as a one-line
warning), and `WingmanError` for other expected failures.

## Pull requests

- Keep changes focused, with tests, and run `uv run ruff check src tests && uv run pytest -q`.
- Describe what changed and why; for a new platform, say how you tested it against the real service.
