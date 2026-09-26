# Skinstinct LinkedIn drafting assistant

Turns Meera Pillai's raw Telegram notes into LinkedIn drafts in her own
voice, twice a week, for her to review. Nothing is ever posted
automatically - every draft goes through Approve / Revise / Reject in her
private Telegram chat, and Meera always publishes herself.

> **This repository is shared for review only.** No license is granted to
> use, copy, modify, deploy, or redistribute this code, in whole or in
> part - all rights are reserved. It's public so it can be read and
> discussed, not reused. (It also won't run as-is for anyone else: real
> credentials live only in a local, gitignored `.env` that was never
> committed - see "Setup" below.)

## How it works

1. Meera drops notes (text or voice) into her private Telegram channel, as
   she already does. A bot added as channel admin silently stores them.
2. Monday and Thursday at 08:00 IST, the bot transcribes any voice notes,
   cleans and tags every unused note, groups related fragments, and scores
   each 0-10 with a reason (publishability triage). Notes scoring below
   `REJECT_THRESHOLD` (5, in `app/batch.py`) are auto-parked and never reach
   the shortlist - they're still reachable via "Show all", never deleted.
   Meera gets the top 5 of what's left as tap-to-pick buttons (plus
   "Show all" and "Skip this batch").
3. For each note she picks: the app searches Google News RSS on the note's
   tags (last 7 days, up to 3 candidates), OpenAI drafts a 250-450 word
   LinkedIn post in her voice (using `docs/voice_extraction.md` and 2-3 of
   her real LinkedIn posts as examples), a second OpenAI pass checks it
   against the style checklist and revises once, and deterministic checks
   (`app/lint.py`) fix punctuation/spelling/banned-word issues on top.
4. Meera gets the draft with Approve / Revise (up to 3 rounds) / Reject
   buttons. Approve sends a clean copy with `[VERIFY]` tags stripped, ready
   to paste into LinkedIn. Reject parks the note back in the backlog with an
   optional reason - nothing is ever deleted.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # add -r requirements-dev.txt for tests

cp .env.example .env
# fill in .env - see the comments in that file for where each value comes from
```

Required in `.env`:

| Variable | Where to get it |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) - create a bot, then add it as an **admin** of Meera's private notes channel |
| `MEERA_USER_ID` | Message [@userinfobot](https://t.me/userinfobot) as Meera |
| `NOTES_CHANNEL_ID` | Forward a channel message to @userinfobot, or check the logs after the bot receives its first channel post (unrecognised chat ids are logged) |
| `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |

Everything else in `.env.example` has a sensible default.

### Voice file and published posts

- Put the voice analysis at `docs/voice_extraction.md` (already there if you
  used this repo's setup).
- Extract Meera's 15 published pieces into `data/published/` (already done
  here from the seed PDF; see `scripts/split_published.py` if you need to
  redo it from a different source file). Only the 4 LinkedIn posts
  (`linkedin_post_00X.txt`) are ever used as drafting examples - the 11
  newsletters are out of scope for LinkedIn drafting per the voice file's
  own instructions.
- Run `python scripts/build_voice_prompt.py` any time to see exactly what
  gets sent to OpenAI as drafting rules, written to `docs/voice_prompt.txt`.

### Backlog notes

Drop the 60 existing notes into `data/notes/` (one file per note, `.txt`/`.md`
for text, `.ogg`/`.mp3`/`.m4a`/`.wav` for voice), then:

```bash
python scripts/import_notes.py
```

## Running

```bash
python -m app.main
```

This starts long-polling and schedules the Monday/Thursday 08:00
`BATCH_TIMEZONE` batch. **It needs to be running continuously** - a laptop
that's asleep at 8am will miss the batch. For production, run it on a small
always-on box (a cheap VPS, a Raspberry Pi, etc.) with a process supervisor
(systemd, `pm2`, or similar) so it restarts if it crashes.

To test without waiting for the schedule:

```bash
python scripts/run_batch_now.py
```

## Metrics

```bash
python scripts/metrics.py
```

Prints posts approved per week, time-to-approve, and the share of approvals
that needed little or no revision - the three things the brief asks the app
to track.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover the pieces that don't need live API calls: auth filtering, the
deterministic lint checks, voice-file section extraction (including that no
newsletter-only rules leak into the LinkedIn prompt), published-post
selection, and the database layer (including that rejecting a note parks it
rather than deleting it). They do not call Telegram or OpenAI.

## Project layout

```
app/
  main.py         entry point: registers handlers, schedules the batch job
  config.py       env var loading, fails fast if anything required is missing
  db.py           SQLite schema and query helpers
  auth.py         only Meera's account + her channel may use the bot
  collector.py     channel_post handler: stores every note
  batch.py        the twice-weekly transcribe -> tag -> score -> shortlist job
  review.py       pick / show all / skip / approve / revise / reject handlers
  drafting.py     OpenAI calls: clean+tag+group, score, draft, self-check, revise
  pipeline.py     ties drafting + news + lint into one persisted draft
  news.py         Google News RSS lookup, last 7 days, up to 3 candidates
  lint.py         deterministic style checks/fixes on top of OpenAI's own check
  voice.py        extracts the drafting-relevant sections of the voice file
  published.py    registry of the 4 LinkedIn posts used as few-shot examples
  llm.py          thin OpenAI client wrapper: retries, JSON schema, logging
  prompts/        prompt templates
scripts/
  import_notes.py     one-off import of the 60 backlog notes
  build_voice_prompt.py  writes docs/voice_prompt.txt for inspection
  run_batch_now.py    manual batch trigger for testing
  metrics.py          prints the three headline metrics
  split_published.py  (setup-time) extracts published posts from the seed PDF
data/
  notes/       backlog note files (input to import_notes.py)
  published/   Meera's 15 published pieces, one file per piece
  audio/       downloaded voice notes
docs/
  voice_extraction.md  the full voice analysis (source of truth)
  voice_prompt.txt      generated: exactly what's sent to OpenAI (gitignored)
tests/
```

## Vercel webhook (alternative deployment)

`api/webhook.py` is a serverless alternative to the long-polling bot
(`app/main.py`): Telegram calls it directly at `POST /api/webhook` instead
of the bot polling `getUpdates`. **This is a separate deployment path, not
a replacement yet** - only note collection (text/voice) and a
button-tap acknowledgement are wired up; the pick -> draft -> Approve/Revise/
Reject flow still lives in `app/review.py` for the long-polling bot. Run
one or the other against the same Telegram bot token, not both at once
(they'd both try to consume the same updates).

### What it hardens

| # | Requirement | Where |
| --- | --- | --- |
| 1 | Reads `message` or `channel_post`, whichever is present | `api/webhook.py:_extract_ids` |
| 2 | Only `ALLOWED_CHAT_IDS` (comma-separated) are processed | same, checked before any work happens |
| 3 | Ignores the bot's own messages (`from.is_bot`) | same |
| 4 | Verifies `X-Telegram-Bot-Api-Secret-Token` against `TELEGRAM_WEBHOOK_SECRET`, 401 on mismatch | `api/webhook.py:webhook()`, before the body is even parsed |
| 5 | Dedupes by `update_id` (`processed_updates` table) | `app/db.py`, checked before any side effect |
| 6 | Every outside call carries an explicit timeout | `app/llm.py` (per-call `timeout`), `app/news.py` (`httpx` timeout), `app/telegram_sync.py` (`httpx` timeout) - all driven by `OUTBOUND_TIMEOUT_SECONDS` |
| 7 | Always 200 for handled/ignored/deduped updates; only a bad secret is non-200 | `api/webhook.py:webhook()` |

### Environment variables (set these in Vercel's dashboard, not `.env`)

| Variable | Notes |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | same bot as the long-polling deployment |
| `TELEGRAM_WEBHOOK_SECRET` | a random string only you and Telegram know. A generated one: `ZtA8sgsnejL2NJ4JqTM5rfbFfqV-8HegJegXlyQMuLg` - generate your own instead of reusing this one, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `ALLOWED_CHAT_IDS` | comma-separated: Meera's user id and her notes channel id, e.g. `6488544401,-1004380334167` |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | same as `.env` |
| `DB_PATH` | defaults to `/tmp/skinstinct.db` - **read the caveat below** |
| `OUTBOUND_TIMEOUT_SECONDS` | defaults to `12` - the per-call budget for OpenAI/News/Telegram; see the duration-budget note below before raising it |

### ⚠️ `/tmp` is not persistent storage

Vercel's filesystem is read-only except `/tmp`, and `/tmp` is **not
guaranteed to survive between invocations** - a serverless function can cold
-start on a fresh instance at any time, wiping it. The dedup table
(`processed_updates`) and the notes/drafts themselves need real persistence
in production. This pass keeps SQLite at `/tmp/skinstinct.db` so the code
runs and the tests pass, but before relying on this in production, swap
`app/db.py`'s connection for a hosted database (Vercel Postgres, Supabase,
or similar) - say the word and I'll do that migration next.

### Vercel function duration limits

As of writing, Vercel's `maxDuration` ceiling depends on your plan (check
your dashboard - these numbers do change): **Hobby** defaults to 10s,
configurable up to **60s**; **Pro** defaults to 15s, configurable up to
**300s** (higher with Fluid Compute enabled); **Enterprise** up to 900s.
`vercel.json` in this repo sets `api/webhook.py` to the Hobby ceiling (60s):

```json
{ "functions": { "api/webhook.py": { "maxDuration": 60 } } }
```

**This matters a lot once the draft flow is wired in.** The OpenAI pipeline
(draft + self-check, each with up to 5 retries and backoff up to ~40s on
overload - see `app/llm.py`) can take minutes in the worst case, which
no plan's webhook duration covers. When that flow moves into the webhook,
either give it a much shorter retry budget specific to the synchronous
path, or - better - have the webhook return 200 immediately and hand the
actual drafting off to a background job (Vercel's Background Functions,
or a queue like Upstash QStash) that messages Meera when it's done. Flagging
this now so it's a deliberate decision when the news step lands, not a
surprise when a pick silently times out.

### Registering the webhook with Telegram

Once deployed, run this once (replace `<VERCEL_URL>` with your deployment's
domain and `<SECRET>` with the `TELEGRAM_WEBHOOK_SECRET` value you set):

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://<VERCEL_URL>/api/webhook",
    "secret_token": "<SECRET>",
    "allowed_updates": ["message", "channel_post"]
  }'
```

As the exact `setWebhook` GET URL you asked for (less safe to paste around
since it puts the secret in a URL that can end up in shell history/logs -
prefer the `curl -X POST` above where possible):

```
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https%3A%2F%2F<VERCEL_URL>%2Fapi%2Fwebhook&secret_token=<SECRET>&allowed_updates=%5B%22message%22%2C%22channel_post%22%5D
```

Note `allowed_updates` is exactly `["message", "channel_post"]` as asked -
that deliberately **excludes `callback_query`**, so button taps won't reach
the webhook at all yet. That's consistent with this pass's scope (button
acknowledgement in `app/webhook_handlers.py` is effectively unreachable
until you add `"callback_query"` to this list once that flow is wired up).

Confirm it took effect:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

To switch back to long-polling (`python -m app.main`) later, first remove
the webhook - the two modes can't run against the same bot at once:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/deleteWebhook"
```

## Safeguards

- **No invented facts.** OpenAI may only reference a headline that the news
  feed actually returned (checked, not just trusted - see
  `drafting._verify_news_reference_or_drop`), and every scientific claim is
  wrapped in `[VERIFY]` so Meera can check it before publishing.
- **Nothing is deleted.** "Show all" pages through every unused note,
  including auto-rejected and manually rejected ones; both are parked with
  a reason and return in future batches for re-scoring.
- **Low scores auto-park, but never hide for good.** Notes scoring below
  `REJECT_THRESHOLD` (0-10 scale, `app/batch.py`) are filtered out of the
  shortlist automatically - a real change from "the AI only ranks" - but
  they're always still reachable through "Show all", and a note that gets
  more material added can score higher and resurface on its own.
- **Only Meera can use the bot.** Every handler checks her Telegram user id
  and the configured channel id; anything else is silently ignored.
- **Secrets stay in the environment.** All API keys and ids come from `.env`
  (gitignored), never hard-coded.
