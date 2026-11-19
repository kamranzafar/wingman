# wingman

A terminal copilot for online communities. It finds threads where you can add something real,
drafts replies in your voice, and posts **only what you approve**. It works with Reddit, Bluesky,
Mastodon and GitHub Discussions; each platform is an adapter in `src/wingman/platforms/`, so more can be added.

## Setup

```bash
uv sync
uv run wingman init          # creates wingman.toml and .env from the examples
```

1. Put your Claude API key in `.env` (`ANTHROPIC_API_KEY`), or run `ant auth login`.
2. In `wingman.toml`, set `blog_url` (and, for `api` mode, the subreddits under `[platforms.reddit] sources`).

`.env`, `wingman.toml` and `data/` are git-ignored.

### Reddit modes

Reddit's [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
requires approval before using its API, and Reddit blocks non-browser feed readers. So wingman has
these modes (`mode` in `[platforms.reddit]`):

| | `paste` (default) | `api` |
|---|---|---|
| Needs Reddit access | none: wingman never contacts Reddit | API approval + script app credentials in `.env` |
| Finding threads | you browse; `wingman draft <url>` | `wingman scan` ranks new/rising threads |
| Thread content | text you copy from the page | fetched |
| Posting | copies the draft and opens the thread; you paste and submit | posts after you confirm |
| `stats` | replies you've logged through wingman | full karma breakdown + `--insights` |

(`manual` mode reads RSS feeds; Reddit currently blocks it for non-browser clients.)

## Daily use

Once, in either mode (and again whenever your blog grows):

```bash
uv run wingman profile build --notes "anything about you the blog doesn't show"
uv run wingman index     # free: indexes your blog, READMEs, pages and posted replies for drafts
```

`wingman index` gives each draft the most relevant passages from your own writing (hybrid search:
BM25 keyword search always, plus local Ollama embeddings if Ollama is running with
`nomic-embed-text` pulled). Re-run it after you publish new posts or post replies.

### Paste mode

1. Open a thread in your browser, select all (Cmd+A) and copy (Cmd+C). Include the sidebar if you
   want the community rules checked.
2. `uv run wingman draft <thread url>`. Wingman reads the copied text (Claude pulls out the post,
   top replies and rules), then shows 2-3 drafts.
3. Pick or edit one and press `p`: the reply is copied to your clipboard and the thread opens.
   Paste, submit, then paste your comment's link back into wingman (optional) so it's logged.

No clipboard? `wingman draft <url> --editor` opens a template to paste into instead.

### Bluesky

Bluesky's API is open, so wingman does the whole loop there: scan, rank, draft, and post after you
approve. Add your handle and an **app password** (Settings → Privacy and security → App passwords;
never your main password) to `.env` as `BLUESKY_HANDLE` / `BLUESKY_APP_PASSWORD`, then:

```bash
uv run wingman check -p bluesky                          # can it log in and read?
uv run wingman scan -p bluesky                           # search your topics, rank posts (Haiku)
uv run wingman review -p bluesky                         # draft, edit, post what you approve
uv run wingman draft https://bsky.app/profile/x/post/y   # one post: no copy-paste needed
```

Sources in `[platforms.bluesky]` are search queries or custom feed URIs. Replies are capped at
Bluesky's 300 characters, links become clickable automatically, and pacing limits apply as usual.

### Mastodon

Also the full loop. Set `instance` in `[platforms.mastodon]` to your server, then create a token
there (Preferences → Development → New application, scopes `read` and `write:statuses`) and add it
to `.env` as `MASTODON_ACCESS_TOKEN`:

```bash
uv run wingman check -p mastodon                         # can it read your instance?
uv run wingman scan -p mastodon                          # hashtags / home timeline, ranked
uv run wingman review -p mastodon                        # draft, edit, post what you approve
uv run wingman draft https://fosstodon.org/@x/123        # a post from any instance
```

Sources are hashtags (`#java`, the reliable option), `@home` (people you follow), `@local` (your
instance), or plain words (full-text search, which only covers people who opted in to it). Bots,
boosts and replies are skipped. Replies start with the author's `@mention` so they're notified, and
count length the way Mastodon does (links are 23 characters).

### GitHub Discussions

Full loop too: scan, rank, draft, comment after you approve. wingman uses `GITHUB_TOKEN` from `.env`
(a classic token with the `public_repo` scope), or the token of a logged-in GitHub CLI (`gh auth login`):

```bash
uv run wingman check -p github                                      # can it search and read?
uv run wingman scan -p github                                       # open discussions, ranked
uv run wingman review -p github                                     # draft, edit, comment
uv run wingman draft https://github.com/owner/repo/discussions/42   # one discussion
```

Sources in `[platforms.github]` are repos (`spring-projects/spring-boot`) or discussion searches
(`pgvector`, `org:apache kafka`). Answered, closed and locked discussions are skipped. Drafts use
markdown and code blocks; wingman only comments, it never opens new discussions.

### API mode

```bash
uv run wingman scan              # fetch new/rising threads, rank them against your profile
uv run wingman review            # go through the best ones; pick, edit, post or skip
uv run wingman stats             # your activity (api mode: karma breakdown; add --insights)
uv run wingman queue / flush     # api mode only: replies waiting for a pacing slot
```

### Review keys

| key | action |
|---|---|
| `1`-`3` | pick a draft |
| `e` | edit the selected draft in `$EDITOR` |
| `w` | write your own from scratch |
| `r` | regenerate, optionally with an instruction ("shorter", "less formal") |
| `o` | open the thread in your browser |
| `p` | post (asks for confirmation; respects pacing) |
| `s` / `n` / `b` / `q` | skip for good / next (leave for later) / back to the previous thread / quit |

## How it scores threads

- **Relevance (0-10):** Claude judges how well *you* specifically could contribute, from your profile.
- **Opportunity (0-1):** freshness, upvote velocity, and how few comments there are yet.
- **Combined:** `0.7 × relevance/10 + 0.3 × opportunity`. `review --min-score` filters on it.

## Guardrails

- Nothing is ever posted without you pressing `p` and confirming.
- Drafts never invent experiences; they leave `[your … here]` placeholders, and posting is blocked
  until you fill them in.
- Subreddit rules are fetched and shown alongside drafts; rule concerns are flagged.
- Pacing limits (`[pacing]` in `wingman.toml`) cap posts per hour, per day and per community.

## Files

| path | what |
|---|---|
| `data/profile.json` | your interest/voice profile — edit it freely |
| `data/learnings.md` | output of `stats --insights`, used when drafting |
| `data/wingman.db` | SQLite: ranked threads, post queue, comment history |

## Adding a platform

Each platform is its own package:

```
src/wingman/platforms/
├── base.py            # the Platform protocol every adapter implements
├── __init__.py        # registry: PLATFORMS = ["reddit", ...]
└── reddit/
    ├── __init__.py    # create(settings) -> picks the adapter for `mode`
    ├── api.py         # PRAW adapter (mode = "api")
    └── rss.py         # feed reader + manual posting (mode = "manual")
```

To add one, create `platforms/<name>/` with an `__init__.py` exposing `create(settings)` that returns
an object implementing the `Platform` protocol (`discover`, `fetch_context`, `submit`, `my_activity`,
plus `constraints`, `posting`, `has_scores`), add `<name>` to `PLATFORMS`, and add a
`[platforms.<name>]` section to `wingman.toml`. Set `ai_drafts_allowed=False` for communities that
ban AI-written content (e.g. Stack Overflow): wingman will then find threads but leave the writing to you.

## Development

```bash
uv run pytest            # tests (no network, no Claude calls)
uv run ruff check src tests
```

```
src/wingman/
├── cli.py          # commands only: gather input, call services/core, print
├── services.py     # builds config → store, LLM, platform, drafter (composition root)
├── config.py       # typed settings; every default lives here; unknown keys are errors
├── errors.py       # WingmanError → shown as one line, no traceback
├── sources/        # your writing: blog, pages, GitHub (cached), local files
├── profile/        # the interest/voice profile (build, save, load)
├── core/           # drafting, retrieval, review, ranking, store, pacing, privacy, usage, http
└── platforms/      # one package per platform, behind the Platform protocol
```
