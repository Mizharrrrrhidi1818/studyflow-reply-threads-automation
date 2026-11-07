"""StudyFlow AI v0.4: narrowly scoped automated Threads webinar replies.

Explicitly gated live mode, baseline-only new comments, SQLite idempotency,
per-run and daily caps, single-host flock via systemd flock command.
Does not write credentials to stdout/logs.
"""
import argparse
import datetime as dt
import logging
import os
import sqlite3
import sys
import re
from zoneinfo import ZoneInfo
from pathlib import Path

import app
import safe_ops

LOG = logging.getLogger('studyflow.auto')
MAX_PER_RUN_HARD = 1
MAX_PER_DAY_HARD = 20
MAX_AGE_HOURS = 24
# Local calendar day for daily reply allowance. Change to Asia/Jakarta if preferred.
DAY_ZONE = ZoneInfo('Europe/Warsaw')

def before_deadline(now=None):
    """Optional opt-in deadline; no deadline is configured by default."""
    cutoff = os.getenv('AUTO_STOP_AT_UTC', '').strip()
    if not cutoff:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    value = dt.datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise SystemExit('AUTO_STOP_AT_UTC must contain timezone offset or Z')
    return now < value

def enforce_deadline():
    if not before_deadline():
        raise SystemExit('STOP: configured AUTO_STOP_AT_UTC has passed')

def positive_int(name, default, maximum):
    value = os.getenv(name, str(default))
    try:
        n = int(value)
    except ValueError:
        raise SystemExit(f'{name} must be an integer')
    if not (1 <= n <= maximum):
        raise SystemExit(f'{name} must be within 1..{maximum}')
    return n

def sent_today(conn, now):
    """Count all attempted reservations to fail closed on uncertain publishing.

    SQLite timestamps from CURRENT_TIMESTAMP are UTC. A needs_review entry may
    have been published despite the HTTP failure, so it counts toward capacity.
    """
    local_day = now.astimezone(DAY_ZONE).date()
    start_local = dt.datetime.combine(local_day, dt.time.min, tzinfo=DAY_ZONE)
    end_local = dt.datetime.combine(local_day + dt.timedelta(days=1), dt.time.min, tzinfo=DAY_ZONE)
    begin = start_local.astimezone(dt.timezone.utc)
    end = end_local.astimezone(dt.timezone.utc)
    return conn.execute(
        "SELECT COUNT(*) FROM replies WHERE status IN ('sent','pending','needs_review') AND updated_at >= ? AND updated_at < ?",
        (begin.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()[0]

# Only engage with webinar-related comments or genuine questions under the webinar post.
# This deliberately rejects short non-questions such as "gilaa".
TOPIC_WORDS = (
    'webinar', 'poland', 'polandia', 'kuliah', 'study abroad', 'study in',
    'self funded', 'self-funded', 'study overseas', 'universitas', 'university',
    'kampus', 'pendaftaran', 'daftar', 'register', 'registration',
    'tuition', 'biaya', 'harga', 'living cost', 'biaya hidup',
    'visa', 'student visa', 'student', 'beasiswa', 'scholarship',
    'syarat', 'persyaratan', 'admission', 'part time', 'part-time',
    'konsultasi', 'link pendaftaran', 'link webinar',
)
QUESTION_LEADS = (
    'apa', 'apakah', 'bagaimana', 'gimana', 'gmn', 'berapa',
    'kapan', 'dimana', 'di mana', 'boleh', 'bisa', 'dapatkah',
    'mau tanya', 'ingin tanya', 'ada info', 'info dong', 'linknya',
    'how', 'what', 'when', 'where', 'why', 'can i', 'could i',
    'do i', 'is it', 'are there', 'may i', 'please',
)

def webinar_relevant(comment):
    s = re.sub(r'\s+', ' ', (comment or '').casefold()).strip()
    if len(s) < 5:
        return False
    topic = any(word in s for word in TOPIC_WORDS)
    question = s.endswith('?') or s.startswith(QUESTION_LEADS)
    # An explicit topic is useful, as is a substantive question under this
    # webinar post. Generic compliments/interjections are intentionally ignored.
    return topic or (question and len(s) >= 9)

def eligible_for_auto(conn, comment, now, activated_at=None):
    # safe_ops.candidates applies baseline, owned-comment, relevance, timestamp and idempotency checks.
    username = (comment.get('username') or '').strip()
    # Do not globally block a username just because it was replied to on
    # an older post. SQLite comment_id uniqueness remains the replay guard.
    if not username:
        return False
    when = safe_ops.utcparse(comment.get('timestamp'))
    if when is None or activated_at is None or when <= activated_at:
        return False
    age = (now - when).total_seconds()
    return 0 <= age <= MAX_AGE_HOURS * 3600

def activate():
    enforce_deadline()
    token, post_id = safe_ops.load()
    app.DB_PATH = Path(os.getenv('DB_PATH', 'data/studyflow.db'))
    conn = safe_ops.db()
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key='baseline_post_id'").fetchone()
        if not row or row[0] != post_id:
            raise SystemExit('Missing baseline for current post; run safe_ops.py baseline first')
        old = conn.execute("SELECT value FROM app_meta WHERE key='auto_activation_time'").fetchone()
        if old:
            print('Activation already saved:', old[0])
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with conn:
            conn.execute("INSERT INTO app_meta (key,value) VALUES ('auto_activation_time',?)", (now,))
        print('Automation activation time saved:', now)
        print('Only comments posted AFTER this time are eligible for automatic replies.')
    finally:
        conn.close()

def run(send=False):
    if not before_deadline():
        print('STOP: configured cutoff reached. No replies published.')
        return
    token, post_id = safe_ops.load()
    if not post_id.isdigit():
        raise SystemExit('Invalid THREADS_POST_ID')
    db_path = os.getenv('DB_PATH', 'data/studyflow.db')
    app.DB_PATH = Path(db_path)
    if send and (os.getenv('AUTO_REPLY_ENABLED', 'false').lower() != 'true' or os.getenv('LIVE_MODE', 'false').lower() != 'true'):
        raise SystemExit('Publishing refused: AUTO_REPLY_ENABLED=true and LIVE_MODE=true required')
    conn = safe_ops.db()
    try:
        activation = conn.execute("SELECT value FROM app_meta WHERE key='auto_activation_time'").fetchone()
        if not activation:
            raise SystemExit('Run auto_reply.py --activate first; no replies permitted')
        activated_at = safe_ops.utcparse(activation[0])
        # Only run when a complete baseline exists. Candidates refuses otherwise.
        now = dt.datetime.now(dt.timezone.utc)
        # Use the same relevance policy for filtering AND the final app.process
        # guard, restoring it afterwards to avoid changing the existing app.
        original_relevant = app.relevant
        app.relevant = webinar_relevant
        try:
            candidates = list(safe_ops.candidates(conn, token, post_id))
        finally:
            app.relevant = original_relevant
        if not before_deadline():
            print('STOP: configured cutoff reached during scanning. No replies published.')
            return
        per_run = positive_int('AUTO_MAX_PER_RUN', 1, MAX_PER_RUN_HARD)
        per_day = positive_int('AUTO_MAX_PER_DAY', 20, MAX_PER_DAY_HARD)
        filtered = [c for c in candidates if eligible_for_auto(conn, c, now, activated_at)]
        filtered.sort(key=lambda c: c.get('timestamp', ''))
        already = sent_today(conn, now)
        cap = max(0, min(per_run, per_day - already))
        print(f'New eligible replies: {len(filtered)}; reserved/sent today: {already}/{per_day}; run cap: {cap}')
        # Refuse to perform any network publish when daily allowance is exhausted.
        if cap == 0:
            print('DAILY LIMIT: no publishing capacity left.')
        for c in filtered[:cap]:
            if not before_deadline():
                print('STOP: configured cutoff reached before publishing next reply.')
                return
            print(f"Candidate comment_id={c['id']} username={c['username']}")
            if not send:
                intent, reply = app.answer(c['text'])
                print(f'  PREVIEW [{intent}] {reply}')
                continue
            # Double gate: command flag and env are both required.
            if os.getenv('AUTO_REPLY_ENABLED', 'false').lower() != 'true' or os.getenv('LIVE_MODE', 'false').lower() != 'true':
                raise SystemExit('Publishing refused: AUTO_REPLY_ENABLED=true and LIVE_MODE=true required')
            intent, reply = app.answer(c['text'])
            if len(reply) > safe_ops.MAX_TEXT_LENGTH:
                print(f"Skipped comment_id={c['id']}: reply exceeds limit")
                continue
            # Guard actual HTTP POST as well, including after container readiness polling.
            # This prevents a publish that started near the cutoff from finishing after it.
            original_api_request = app.api_request
            def guarded_api_request(method, path, api_token, params=None):
                if method.upper() == 'POST' and not before_deadline():
                    raise RuntimeError('Publishing blocked: configured cutoff reached')
                return original_api_request(method, path, api_token, params)
            try:
                app.api_request = guarded_api_request
                app.relevant = webinar_relevant
                reviewed, published = app.process(conn, [c], token=token, live=True, max_replies=1)
            finally:
                app.api_request = original_api_request
                app.relevant = original_relevant
            state = conn.execute('SELECT status FROM replies WHERE comment_id=?', (str(c['id']),)).fetchone()
            print(f"Result comment_id={c['id']} status={state[0] if state else 'missing'} published={published}")
            # Stop for human review on any uncertain/failure state.
            if not state or state[0] != 'sent' or published != 1:
                raise SystemExit('STOP: uncertain publishing result; review database and Threads, do not retry')
        if not send:
            print('DRY RUN: nothing published.')
    finally:
        conn.close()

def main():
    parser = argparse.ArgumentParser(description='Safe Threads auto-reply: preview by default')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--send', action='store_true', help='Publish only when AUTO_REPLY_ENABLED and LIVE_MODE are both true')
    mode.add_argument('--activate', action='store_true', help='Set one-time activation timestamp; does not publish')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.activate:
        safe_ops.load()
        activate()
    else:
        run(send=args.send)

if __name__ == '__main__':
    main()
