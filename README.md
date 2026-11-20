```text
 ________________              _
 \_______________\  __      __(_) _ __    __ _  _ __ ___    __ _  _ __
   \____________\   \ \ /\ / /| || '_ \  / _` || '_ ` _ \  / _` || '_ \
     \_________\     \ V  V / | || | | || (_| || | | | | || (_| || | | |
       \______\       \_/\_/  |_||_| |_| \__, ||_| |_| |_| \__,_||_| |_|
                                         |___/

        your voice · real conversations · you approve every post
```

A terminal copilot for online communities. wingman finds conversations where **you** have something
real to add, drafts replies in **your** voice from your own writing, and posts **only what you
approve**, one reply at a time.

It works with **Bluesky**, **Mastodon**, **GitHub Discussions**, **Reddit** and **Hacker News**. Each platform is a
small adapter, so more can be added.

```
 your blog, pages, CV, repos ──► profile (topics, opinions, voice) ──┐
                                                                     ▼
 platform search / hashtags / repos ──► scan ──► rank by fit ──► review ──► you edit & approve ──► post
                                                                     ▲
                       your own writing, indexed ──► excerpts for each draft
```

wingman is not a bot. Nothing is posted until you've read the final text and confirmed it. Pacing
limits cap how often you post, and drafts never invent experiences for you.

- [Platforms](#platforms)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Platform setup](#platform-setup)
- [Command reference](#command-reference)
- [How it works](#how-it-works)
- [Cost](#cost)
- [Responsible use](#responsible-use)
- [Privacy and your data](#privacy-and-your-data)
- [Contributing](#contributing) · [License](#license)

## Platforms

| | Find posts (`scan`) | Draft | Post | Your stats | Credentials |
|---|---|---|---|---|---|
| **Bluesky** | search queries, hashtags, custom feeds | ✓ (300 chars) | ✓ API | likes | handle + app password |
| **Mastodon** | hashtags, home/local timelines, search | ✓ (500 chars) | ✓ API | favourites | access token from your instance |
| **GitHub Discussions** | repos, discussion searches | ✓ markdown | ✓ comments | upvotes | token, or a logged-in `gh` CLI |
| **Reddit**, `paste` mode (default) | you browse | ✓ | you paste it | replies you log | none |
| **Reddit**, `api` mode | subreddits | ✓ | ✓ API | karma | Reddit API approval |
| **Hacker News** | searches, Ask/Show HN, front page | ✓ replies and posts | you paste it | your comments | none |

## Requirements

- **Python 3.11+**, on macOS, Linux or Windows
- **A Claude API key** from [platform.claude.com](https://platform.claude.com) (or `ant auth login`).
  Claude ranks posts, builds your profile and writes drafts. See [Cost](#cost).
- **Optional:** [Ollama](https://ollama.com) with `nomic-embed-text`, for semantic search over your
  own writing. Without it, wingman uses keyword search only.
- **Optional:** a clipboard tool on Linux (`wl-copy`/`wl-paste` or `xclip`) for Reddit paste mode.

## Install

```bash
uv tool install community-wingman    # or: pipx install community-wingman
```

The package is `community-wingman` on PyPI; the command it installs is `wingman`.
To run the latest unreleased code instead:

```bash
uv tool install git+https://github.com/kamranzafar/wingman
```

Or from a clone, for development:

```bash
git clone https://github.com/kamranzafar/wingman && cd wingman
uv sync
uv run wingman --help
```

## Quick start

```bash
wingman init                # creates wingman.toml and .env here (or --global: ~/.config/wingman)
```

1. **Add your Claude key** to `.env` as `ANTHROPIC_API_KEY`.
2. **Tell wingman about you** in `wingman.toml` under `[profile]`: your `blog_url`, background
   `pages` (an About page) and/or local `files` (a CV or LinkedIn PDF), and your `github` username.
3. **Enable a platform.** Set `enabled = true` under it in `wingman.toml`, add its credentials to
   `.env` (see [Platform setup](#platform-setup)), and edit its `sources`.
4. **Build your profile and index your writing:**
   ```bash
   wingman profile build --notes "anything about you your writing doesn't show"
   wingman profile show        # check it; edit data/profile.json freely
   wingman index               # free: indexes your writing so drafts can draw on it
   ```
5. **Find and answer conversations:**
   ```bash
   wingman check               # can it log in and read?
   wingman scan                # find new posts on your sources, ranked by fit
   wingman review              # draft, edit and post the ones you choose
   ```

## Configuration

wingman keeps its files in one **home** directory, chosen in this order:

1. `$WINGMAN_HOME`, if set
2. the current directory, if it has a `wingman.toml`
3. your user config directory, `~/.config/wingman` (or `$XDG_CONFIG_HOME/wingman`), if it has one
4. otherwise the current directory

| file | what |
|---|---|
| `wingman.toml` | settings. Every option is documented in [`wingman.example.toml`](https://github.com/kamranzafar/wingman/blob/main/wingman.example.toml). Unknown keys are reported as errors, with a "did you mean" hint. |
| `.env` | secrets: API keys, tokens, app passwords. Template: [`.env.example`](https://github.com/kamranzafar/wingman/blob/main/.env.example). |
| `data/` | your profile, database, index and usage log (see [Privacy and your data](#privacy-and-your-data)) |

Main sections of `wingman.toml`:

| section | controls |
|---|---|
| `default_platform` | the platform used when a command gets no `-p`. Empty means the first enabled platform. |
| `[profile]` | what wingman reads about you: blog, pages, files, GitHub, `private_terms`, whether drafts may link your blog |
| `[llm]` | Claude model per task (ranking, profile, extraction, drafting) and reasoning effort |
| `[drafting]` | drafts per thread, excerpts from your writing, length limit, search weighting, ratings |
| `[pacing]` | maximum posts per hour, per day and per source, and the minimum gap between posts |
| `[platforms.<name>]` | `enabled`, `sources`, `keywords`, `max_age_hours`, `per_source_limit`, plus platform-specific options |

Environment variables: `WINGMAN_HOME` (home directory), `OLLAMA_HOST` (where Ollama runs; default
`localhost:11434`), and `WINGMAN_LINE_INPUT=1` (type answers and press Enter instead of single
keypresses).

## Platform setup

### Bluesky

Bluesky's API is open, so wingman runs the whole loop: scan, rank, draft and post.

1. In Bluesky, create an **app password** (Settings → Privacy and security → App passwords). Never
   use your main password.
2. Add `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` to `.env`.
3. In `[platforms.bluesky]`: `enabled = true`, and `sources` as search queries (`"python asyncio"`),
   hashtags (`"#rustlang"`) or custom feed URIs (`at://...`).

```bash
wingman scan -p bluesky
wingman draft https://bsky.app/profile/someone.bsky.social/post/3k...   # one post, from its link
```

Options: `languages` (skip posts tagged with other languages), `service` (your PDS, if you
self-host). Replies are capped at 300 characters, counted the way Bluesky counts them, and links
become clickable.

### Mastodon

Also the full loop, through your own server, so you can reply to posts from any instance.

1. On your server: Preferences → Development → New application, with scopes `read` and
   `write:statuses`. Copy **Your access token**.
2. Add it to `.env` as `MASTODON_ACCESS_TOKEN`.
3. In `[platforms.mastodon]`: `enabled = true`, `instance = "https://your.server"`, and `sources`:
   - `"#hashtag"`: the reliable option
   - `"@home"`: people you follow
   - `"@local"`: your server's local timeline
   - plain words: full-text search, which only covers people who opted in to being searchable

```bash
wingman scan -p mastodon
wingman draft https://any.instance/@someone/113...                      # a post from any server
```

Options: `languages`, `visibility` (`public` or `unlisted`), `max_characters` (if your instance
allows more than 500), `include_bots`. Bots, boosts and replies are skipped. Replies start with the
author's `@mention` so they're notified, and length is counted the way Mastodon does (links count as
23 characters).

### GitHub Discussions

Scan, draft and comment. wingman only adds comments; it never opens new discussions.

1. Set `GITHUB_TOKEN` in `.env` (a classic token with the `public_repo` scope), or just be logged in
   with the GitHub CLI (`gh auth login`). wingman uses that token if `GITHUB_TOKEN` is empty.
2. In `[platforms.github]`: `enabled = true`, and `sources` as repos (`"fastapi/fastapi"`) or
   discussion searches (`"org:apache kafka"`). Many projects don't use Discussions; wingman warns
   you about those.

```bash
wingman scan -p github
wingman draft https://github.com/owner/repo/discussions/42
```

Options: `skip_answered` (default `true`). Closed, locked and your own discussions are always
skipped. Drafts use markdown and code blocks, and see the discussion's category and its top
comments, with the marked answer first. Discussions move slowly, so the example uses a two-week
`max_age_hours`.

### Hacker News

Scan, draft replies and new posts, then submit them yourself: HN has no posting API. When you
press `p`, wingman copies your final text and opens the thread (or, for `wingman post`, HN's submit
form with the title and link filled in); you paste, check and submit. Edit drafts until they're
yours: HN's [guidelines](https://news.ycombinator.com/newsguidelines.html) ask for no generated or
AI-edited text, and the community is quick to spot it.

1. No credentials needed: wingman reads the public [Algolia HN Search API](https://hn.algolia.com/api).
   Optionally set `HN_USERNAME` in `.env` so your own stories are skipped and `stats` lists your comments.
2. In `[platforms.hackernews]`: `enabled = true`, and `sources` as search words (`"postgres"`),
   `"@ask"`, `"@show"`, `"@front"` (front page), or a shortcut with words (`"@ask kafka"`).

```bash
wingman scan -p hackernews
wingman scan -p hackernews -t "@show rust"
wingman draft https://news.ycombinator.com/item?id=12345     # a story, or a comment to reply to
wingman post -p hackernews "Ask HN: How do you retire feature flags?"
wingman post -p hackernews --blog https://example.com/post   # submit your post as a link
```

Options: `min_points` (default `0`). Drafts are plain text (HN has no markdown) and see the story,
its link and the most-discussed top-level comments. For a new post, the topic becomes the title
(the article's title when sharing a blog post; HN allows 80 characters), so phrase it as you want
it to appear. HN hides comment scores, so `stats` shows where and when you comment, not votes.

### Reddit

Reddit's [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
requires approval before you use its API, and Reddit blocks non-browser feed readers. So wingman
has three modes (`mode` in `[platforms.reddit]`):

| | `paste` (default) | `api` | `manual` |
|---|---|---|---|
| Reddit access | none; wingman never contacts Reddit | API approval + script app credentials in `.env` | RSS (currently blocked by Reddit) |
| Finding threads | you browse | `wingman scan` (new and rising) | `wingman scan` |
| Thread content | text you copy from the page | fetched, with subreddit rules | from the feed |
| Posting | copied to your clipboard; you paste and submit | posts after you confirm | you paste and submit |
| `stats` | replies you've logged | full karma breakdown, `--insights` | where and when you post |

**Paste mode, step by step:**

1. Open a thread, select all and copy. Include the sidebar if you want its rules checked.
2. Run `wingman draft <thread url>`. Claude extracts the post, top replies and rules from the copied
   text (treating it as data, not instructions), then shows the drafts.
3. Pick or edit one and press `p`. The reply is copied and the thread opens; paste it and submit.
   Then paste your comment's link back into wingman to log it (optional).

No clipboard? `wingman draft <url> --editor` opens a template to fill in instead.

## Command reference

Every command accepts `--help`. `-p/--platform` defaults to `default_platform`, else the first
enabled platform.

**Setup**

| command | what it does |
|---|---|
| `wingman init [--global]` | create `wingman.toml` and `.env` from the templates; existing files are left alone |
| `wingman check [-p]` | test that wingman can read posts, replies and your history. Prints status only, never tokens. |
| `wingman profile build [--blog URL] [--notes TEXT] [-p] [--no-comments]` | read your writing (and past comments on the platform) and build your profile with Claude |
| `wingman profile show` | show your profile, with `private_terms` masked |
| `wingman index` | index your blog, READMEs, pages and posted replies for drafts. Free, no Claude calls. Re-run after you publish. |

**Find and review**

| command | what it does |
|---|---|
| `wingman scan [-p] [-t TOPIC]… [--rerank] [--show N] [--page-size N]` | find new posts on your sources, or on `--topic`s for this run only, and rank them by fit. Results are paged (`n`/`p`/`q`). `--rerank` scores already-seen posts again. |
| `wingman review [-p] [--limit N] [--min-score 0.5]` | go through the best scanned posts: draft, edit, post or skip |
| `wingman queue [-p]` | approved replies waiting for a pacing slot |
| `wingman flush [-p]` | post queued replies whose time has come |

**Write**

| command | what it does |
|---|---|
| `wingman draft [URL] [-p] [--editor]` | draft a reply to one post you found yourself. The platform comes from the link. |
| `wingman post [TOPIC] [--blog URL] [--notes TEXT] [-p]` | draft a new post (not a reply) about a topic, or sharing one of your blog posts. Bluesky and Mastodon. |

**Insights**

| command | what it does |
|---|---|
| `wingman stats [-p] [--insights]` | your activity by community, hour and length. `--insights` asks Claude what makes your best comments work and feeds that into future drafts. |
| `wingman quality [-p]` | how good the drafts are, from your 1–5 ratings: by model and platform, how much you kept, and common issues |
| `wingman cost` | what wingman has spent on Claude, by task and model |

**Keys while reviewing**

| key | action |
|---|---|
| `1`–`4` | pick a draft |
| `e` | edit the selected draft in `$EDITOR` |
| `r` | rewrite, optionally with an instruction ("shorter", "less formal") |
| `w` | write your own from scratch |
| `o` | open the post in your browser |
| `p` | post (asks for confirmation; queues it if you're over your pace) |
| `s` / `n` / `b` / `q` | skip for good / next, leave for later / back / quit |

After each thread you can rate the drafts 1–5 (5 means posted as-is; Enter skips). `wingman
quality` reports on those ratings.

## How it works

1. **Profile.** `profile build` reads your blog posts, background pages, files, GitHub repos and
   (optionally) past comments, and asks Claude for your topics (with expert, contributor or
   interested levels), your actual opinions, your writing voice and your linkable posts. It's saved
   as `data/profile.json`; edit it freely.
2. **Index.** `index` splits your writing into passages for hybrid search: BM25 keyword search, plus
   Ollama embeddings if available, weighted towards recent work and your own projects.
3. **Scan.** Each platform fetches recent posts from your sources. Posts already seen are skipped.
4. **Rank.** Each post gets:
   - **Fit (0–10):** Claude judges how well *you* specifically could add something useful,
     non-obvious and in line with your profile, and suggests an angle.
   - **Opportunity (0–1):** freshness, traction and how few replies it has yet.
   - **Score:** `0.7 × fit/10 + 0.3 × opportunity`. `review --min-score` filters on it.
5. **Draft.** For each post you review, Claude gets your profile, the most relevant excerpts from
   your writing, the post, its top replies and the community rules. It writes `drafts` drafts in
   your voice (default 2) that don't repeat what others already said, and flags rule concerns and
   any claims you should check before posting.
6. **Approve.** You pick, edit, rewrite or skip. Posting checks length, placeholders,
   `private_terms` and pacing, then asks you to confirm.

## Cost

wingman uses your own Claude API key. Each task uses a model chosen in `[llm]`, with cheaper
models on the high-volume work. Typical costs from real use with the default models:

| task | model | typical cost |
|---|---|---|
| ranking (`scan`), per 20 posts | Haiku | ≈ $0.001 |
| drafting, per thread (2 drafts) | Sonnet | ≈ $0.02 |
| profile build | Sonnet | once; one larger request |

`wingman cost` shows your actual spend by task and model. Lower it with fewer `drafts`, a cheaper
`draft_model`, or fewer, more focused `sources`.

## Responsible use

wingman helps you take part in communities. It doesn't take part for you. Please use it that way.

**Built-in guardrails**

- Nothing is posted until you've seen the final text and confirmed it. There is no auto-post mode.
- Pacing limits (`[pacing]`) cap posts per hour, per day and per community, with a minimum gap
  between posts. Over the limit, approved replies wait in a queue.
- Drafts never invent experiences. Where a personal story would help, they leave a
  `[your … here]` placeholder, and posting is blocked until you fill it in.
- Drafts that are too long, or that contain a `private_terms` word, can't be posted.
- Community rules (Reddit API mode, or copied with the thread) are passed to the drafter, and rule
  concerns are flagged.
- Adapters can mark a community as off-limits for AI drafts (`ai_drafts_allowed=False`). wingman
  then only helps you find threads, and you write the reply yourself.

**Your responsibilities**

- **Follow each platform's terms and each community's rules.** Some communities ban AI-assisted
  posts. Stack Overflow is one, and wingman deliberately doesn't support it. Hacker News's guidelines
  also ask for no generated or AI-edited text; wingman drafts there, but what you post is on you. When in doubt,
  disclose, or write the reply yourself.
- **Only post what you'd stand behind.** Edit drafts until they're true and yours. Don't use
  wingman to promote, astroturf, manipulate votes, or post from multiple accounts.
- **Keep it human-paced.** The default pacing is deliberately conservative.

## Privacy and your data

- **Stays on your machine:** `.env` and everything in `data/` (`profile.json`, `wingman.db` with
  ranked posts, queue and history, `index.json`, `learnings.md`, `usage.jsonl`, and `github.json`, a
  cached copy of your repo READMEs). wingman has no telemetry.
- **Sent to Anthropic's Claude API:**
  - when building your profile: the sources you configured in `[profile]`, as they are
  - when ranking and drafting: your profile, excerpts from your writing, and the posts and replies
    being ranked or answered

  `private_terms` (employer, client or product names) are replaced with a neutral placeholder in
  ranking and drafting prompts. Profile building reads your sources unchanged, so leave anything
  sensitive out of `[profile] files` and `pages`.
- **Sent to each platform:** only the requests needed to read posts and publish what you approve.
  Each token is sent only to its own platform's host. `check` prints status, never tokens.
- Your config and secrets are git-ignored in this repo. Don't commit `wingman.toml`, `.env` or `data/`.

## Contributing

Contributions are welcome, especially new platform adapters. See [CONTRIBUTING.md](https://github.com/kamranzafar/wingman/blob/main/CONTRIBUTING.md)
for setup, ground rules and a step-by-step guide to adding a platform.

```bash
uv sync && uv run pytest -q && uv run ruff check src tests   # no network or credentials needed
```

## License

[Apache License 2.0](https://github.com/kamranzafar/wingman/blob/main/LICENSE)
