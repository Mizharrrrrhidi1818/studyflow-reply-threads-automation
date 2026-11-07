# StudyFlow AI — Threads-first webinar automation (MVP v0.1)

A lightweight Python proof-of-concept for responding to relevant comments on **your own** Threads webinar promotional post. It directs interested users to the existing **paid** webinar registration page:

https://bangkit-edu.myr.id/webinar/sharing-session-3rd-study-abroad-self-funded-in-poland

## What works
- Read top-level replies from an authorized Threads post (up to 3 pages of 50 per scan).
- Detect simple Indonesian/English webinar questions using conservative keyword rules.
- Preview tailored public replies without sending anything (**default**).
- Store successful live replies in SQLite to reduce duplicates.
- Cap each live scan to a small number of replies.
- Reserve an ID before publishing; failed/uncertain replies require manual review rather than blind retries.
- Zero third-party Python packages; no local LLM or Docker required.

## What this is NOT
- No Threads DM automation. Threads DM access is not provided by the public API used here.
- No Instagram integration yet; add as phase 2 via the supported Instagram private-reply API.
- No AI-model integration yet; phase 1 is deterministic templates, safer during launch week.
- No payment webhook or paid registration data access; checkout is handled by the existing provider.
- No guarantee of working live until Meta app permissions and production account configuration are approved.

## Run locally or on Ubuntu VPS

```bash
sudo apt update
sudo apt install -y python3 python3-venv
mkdir -p ~/apps
# unzip studyflow-ai.zip in ~/apps; cd into ~/apps/studyflow-ai
cd ~/apps/studyflow-ai
python3 -m unittest discover -s tests -v
python3 app.py demo
cp .env.example .env
chmod 600 .env
```

After you create a Meta Developer app with the Threads use case, complete official OAuth authorization for your own Threads account. Request scopes such as `threads_basic`, `threads_read_replies`, `threads_content_publish`, `threads_manage_replies` as appropriate. Do NOT share the token in chat or GitHub. Edit `.env` with:

- `THREADS_ACCESS_TOKEN`: authorized Threads user access token
- `THREADS_POST_ID`: numerical media ID of your own promo post
- `LIVE_MODE=false` (keep this while testing)

Then run:

```bash
python3 app.py scan           # Preview only; calls the API but does not publish
python3 app.py status         # Local send count
```

**Live publishing: explicitly opt in only after verifying previews, permissions and Meta's automation terms.** Change `.env` to `LIVE_MODE=true`, then run `python3 app.py scan --live`. Each live scan publishes at most `MAX_REPLIES_PER_RUN` (default 3, maximum 10). Do not schedule automatic publishing before a human-reviewed pilot.

## Meta API behavior

- `GET /{thread-id}/replies` reads top-level replies.
- `POST /me/threads` with `media_type=TEXT`, `text`, `reply_to_id` creates a reply media container.
- `POST /me/threads_publish` with `creation_id` publishes it.
- Auth via Threads OAuth access token; obtain through Meta's official developer dashboard.

The Meta API may change, permissions may need app review, and rate limits apply. Check the latest official docs before live usage:
- https://developers.facebook.com/docs/threads/
- https://www.postman.com/meta/threads/collection/dht3nzz/threads-api

## Important deployment notes

- Keep `.env` outside GitHub, protect with `chmod 600`.
- Avoid sending promotional replies to unrelated conversations. This MVP targets a single specified own post and keyword-matched comments only.
- `needs_review` means the API request may have partially succeeded; inspect the comment manually before any retry.
- SQLite is local and stores commenter usernames and texts; use restricted permissions and a retention policy.
- This MVP uses polling **only when invoked**, not a background daemon.
- No Docker image needed for the first iteration; 2 GB RAM/20 GB free disk is sufficient to try this lightweight program.
- For fuller coverage, add careful pagination/checkpoints, webhook processing, token refresh, testing, and monitoring in subsequent phases.

## Next phases
1. OAuth setup, real read-only API test, and approved live pilot.
2. Better classification (optional hosted AI, grounded in verified webinar FAQ).
3. Instagram professional account private replies through official API.
4. Docker, CI/CD, health checks, monitoring, retention and dashboard.
