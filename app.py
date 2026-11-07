"""StudyFlow AI — safe, small Threads webinar reply MVP.

Dry-run is the default. A live reply requires BOTH LIVE_MODE=true and --live.
"""
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WEBINAR_URL = 'https://bangkit-edu.myr.id/webinar/sharing-session-3rd-study-abroad-self-funded-in-poland'
API_ROOT = 'https://graph.threads.net/v1.0'
DB_PATH = Path(os.getenv('DB_PATH', 'data/studyflow.db'))
LOG = logging.getLogger('studyflow')


def load_dotenv(path='.env'):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def answer(comment):
    """Deterministic responses: avoid guessing prices, visas or academic rules."""
    s = comment.lower()
    intent = 'registration'
    if any(k in s for k in ('visa', 'permit', 'izin tinggal')):
        intent = 'visa'
    elif any(k in s for k in ('biaya', 'harga', 'price', 'cost', 'tuition', 'fee', 'berapa')):
        intent = 'cost'
    elif any(k in s for k in ('kerja', 'job', 'work', 'part-time', 'part time')):
        intent = 'work'
    elif any(k in s for k in ('beasiswa', 'scholarship')):
        intent = 'scholarship'
    english = any(k in s for k in ('how', 'where', 'what', 'please', 'price', 'cost', 'tuition', 'register', 'scholarship')) and not any(k in s for k in ('berapa', 'gimana', 'cara', 'biaya', 'daftar'))
    if english:
        answers = {
            'registration': 'Thanks for your interest! 🇵🇱 Registration for our paid Study Abroad Self-Funded in Poland webinar is here:',
            'cost': 'We will discuss tuition and living costs in Poland during the webinar. Registration details:',
            'visa': 'We will discuss the application and visa process in the webinar. Official requirements should be verified with the relevant authorities. Register:',
            'work': 'The webinar covers arrival and finding work in Poland. Work eligibility depends on your situation. Register:',
            'scholarship': 'This session focuses on the self-funded route to studying in Poland (not a scholarship guarantee). Register:',
        }
    else:
        answers = {
            'registration': 'Terima kasih sudah tertarik! 🇵🇱 Pendaftaran webinar berbayar Study Abroad Self-Funded in Poland ada di sini:',
            'cost': 'Di webinar kita bahas biaya kuliah dan hidup di Polandia. Detail dan pendaftaran:',
            'visa': 'Di webinar kita bahas proses aplikasi dan visa. Syarat resmi tetap perlu dicek ke otoritas terkait. Daftar di sini:',
            'work': 'Kita akan bahas kedatangan dan mencari kerja di Polandia. Hak bekerja bergantung pada status masing-masing. Daftar:',
            'scholarship': 'Webinar ini fokus pada jalur kuliah mandiri (self-funded), bukan jaminan beasiswa. Daftar:',
        }
    return intent, f'{answers[intent]} {WEBINAR_URL}'


def relevant(comment):
    s = comment.lower()
    keywords = ('poland', 'polandia', 'webinar', 'study', 'kuliah', 'daftar', 'register',
                'biaya', 'harga', 'visa', 'kerja', 'beasiswa', 'scholarship', 'info', 'link')
    return any(k in s for k in keywords)


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS replies (
        comment_id TEXT PRIMARY KEY,
        username TEXT,
        comment_text TEXT,
        intent TEXT,
        reply_text TEXT,
        status TEXT NOT NULL,
        published_id TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    existing = {row[1] for row in conn.execute('PRAGMA table_info(replies)')}
    if 'container_id' not in existing:
        conn.execute('ALTER TABLE replies ADD COLUMN container_id TEXT')
    if 'last_error' not in existing:
        conn.execute('ALTER TABLE replies ADD COLUMN last_error TEXT')
    conn.commit()
    return conn


def api_request(method, path, token, params=None):
    url = API_ROOT + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method=method,
        headers={'Authorization': f'Bearer {token}', 'User-Agent': 'StudyFlowAI/0.1'})
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # Only print allowlisted Meta diagnostic fields. Never log the request URL/token.
        details = {}
        try:
            details = json.loads(e.read(8192)).get('error', {})
        except (ValueError, UnicodeDecodeError):
            pass
        safe = {k: details.get(k) for k in ('message', 'type', 'code', 'error_subcode', 'fbtrace_id') if details.get(k) is not None}
        raise RuntimeError(f'Threads HTTP {e.code}: {json.dumps(safe, ensure_ascii=False)}') from None
    except urllib.error.URLError as e:
        raise RuntimeError(f'Threads API connection failed: {e.reason}') from None


def fetch_comments(token, post_id, max_pages=3):
    after = None
    for _ in range(max_pages):
        params = {'fields': 'id,text,username,is_reply_owned_by_me,timestamp', 'limit': '50'}
        if after:
            params['after'] = after
        obj = api_request('GET', '/' + urllib.parse.quote(post_id, safe='') + '/replies', token, params)
        for comment in obj.get('data', []):
            yield comment
        new_after = obj.get('paging', {}).get('cursors', {}).get('after')
        if not obj.get('data') or not new_after or new_after == after:
            break
        after = new_after


def publish_reply(token, comment_id, reply, on_container=None):
    """Two-step publish: store container ID, check readiness, publish at most once.

    Never blindly retry a failed or ambiguous publish. The caller persists
    needs_review and must reconcile that state manually.
    """
    container = api_request('POST', '/me/threads', token, {
        'media_type': 'TEXT', 'text': reply, 'reply_to_id': comment_id
    })
    container_id = str(container['id'])
    if on_container:
        on_container(container_id)
    for attempt in range(5):
        obj = api_request('GET', '/' + urllib.parse.quote(container_id, safe=''), token, {
            'fields': 'status,error_message'
        })
        status = obj.get('status')
        print(f'Container status: {status}')
        if status == 'FINISHED':
            break
        if status in ('ERROR', 'EXPIRED', 'PUBLISHED'):
            raise RuntimeError(f'Container status {status}: {obj.get("error_message", "")!s}')
        if attempt < 4:
            time.sleep(10)
    else:
        raise RuntimeError('Container not FINISHED after readiness checks; review manually')
    # Exactly one publish request. On any failure mark needs_review, not retry.
    result = api_request('POST', '/me/threads_publish', token, {'creation_id': container_id})
    return result['id']


def process(conn, items, token=None, live=False, max_replies=5):
    reviewed = 0
    published = 0
    for c in items:
        comment_id = str(c.get('id', ''))
        message = str(c.get('text', '') or '').strip()
        if not comment_id or not message or c.get('is_reply_owned_by_me') is True:
            continue
        if not relevant(message):
            continue
        if conn.execute('SELECT 1 FROM replies WHERE comment_id=?', (comment_id,)).fetchone():
            continue
        intent, reply = answer(message)
        username = str(c.get('username', ''))
        if not live:
            print(json.dumps({'comment_id': comment_id, 'username': username,
                              'intent': intent, 'suggested_reply': reply}, ensure_ascii=False))
            reviewed += 1
            continue
        if published >= max_replies:
            LOG.warning('Live reply cap reached; run a later scan to process more')
            break
        # Reserve before network operation. Never blindly retry uncertain publish outcomes.
        conn.execute('''INSERT INTO replies
            (comment_id,username,comment_text,intent,reply_text,status)
            VALUES (?,?,?,?,?,?)''', (comment_id, username, message, intent, reply, 'pending'))
        conn.commit()
        try:
            def save_container(cid):
                conn.execute('UPDATE replies SET container_id=?, updated_at=CURRENT_TIMESTAMP WHERE comment_id=?', (cid, comment_id))
                conn.commit()
            published_id = publish_reply(token, comment_id, reply, on_container=save_container)
        except (RuntimeError, KeyError) as exc:
            conn.execute('UPDATE replies SET status=?, last_error=?, updated_at=CURRENT_TIMESTAMP WHERE comment_id=?',
                         ('needs_review', str(exc)[:1000], comment_id))
            conn.commit()
            LOG.exception('Publish uncertain or failed; manual review needed for comment %s', comment_id)
            continue
        conn.execute('''UPDATE replies SET status=?, published_id=?, updated_at=CURRENT_TIMESTAMP
                        WHERE comment_id=?''', ('sent', published_id, comment_id))
        conn.commit()
        published += 1
    return reviewed, published


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description='StudyFlow AI Threads reply MVP')
    parser.add_argument('command', choices=['demo', 'scan', 'status'])
    parser.add_argument('--live', action='store_true', help='Actually publish replies (also requires LIVE_MODE=true)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    conn = connect_db()
    if args.command == 'status':
        results = conn.execute('SELECT status, COUNT(*) FROM replies GROUP BY status').fetchall()
        print('Reply status:', dict(results))
        return
    if args.command == 'demo':
        sample = [
            {'id': 'demo1', 'username': 'guest_a', 'text': 'Info webinar Poland dong'},
            {'id': 'demo2', 'username': 'guest_b', 'text': 'Berapa biaya kuliah di Polandia?'},
            {'id': 'demo3', 'username': 'guest_c', 'text': 'How to register?'}
        ]
        process(conn, sample)
        return
    token = os.getenv('THREADS_ACCESS_TOKEN', '')
    post_id = os.getenv('THREADS_POST_ID', '')
    if not token or not post_id:
        parser.error('Set THREADS_ACCESS_TOKEN and THREADS_POST_ID in .env for scan')
    live = args.live and os.getenv('LIVE_MODE', 'false').lower() == 'true'
    if args.live and not live:
        parser.error('Live publish also requires LIVE_MODE=true in .env')
    max_replies = max(1, min(10, int(os.getenv('MAX_REPLIES_PER_RUN', '3'))))
    reviewed, published = process(conn, fetch_comments(token, post_id), token, live, max_replies)
    print(f'Completed. Previewed={reviewed}, published={published}, live={live}')

if __name__ == '__main__':
    main()
