"""Safety gate for StudyFlow AI v0.2: baseline + bounded preview + manual one-reply publish.

Do NOT schedule the send command. Existing app.py remains unchanged.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path

import app

MAX_PAGES = 30
MAX_TEXT_LENGTH = 500

def utcparse(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)

def load():
    app.load_dotenv()
    token = os.getenv('THREADS_ACCESS_TOKEN', '')
    post_id = os.getenv('THREADS_POST_ID', '')
    if not token or not post_id or not post_id.isdigit():
        sys.exit('Set valid THREADS_ACCESS_TOKEN and numeric THREADS_POST_ID in .env')
    return token, post_id

def db():
    conn = app.connect_db()
    conn.execute('CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.commit()
    return conn

def fetch_all(token, post_id):
    comments=[]
    seen=set()
    after=None
    for page in range(MAX_PAGES):
        params={'fields':'id,text,username,is_reply_owned_by_me,timestamp', 'limit':'50'}
        if after:
            params['after']=after
        data=app.api_request('GET', '/' + urllib.parse.quote(post_id, safe='') + '/replies',token,params)
        batch=data.get('data', [])
        for item in batch:
            if item.get('id') and str(item['id']) not in seen:
                comments.append(item)
                seen.add(str(item['id']))
        new_after=data.get('paging',{}).get('cursors',{}).get('after')
        if not batch or not new_after or new_after == after:
            return comments
        after=new_after
    raise RuntimeError(f'Pagination not exhausted after {MAX_PAGES} pages; refusing baseline/preview to avoid missing older replies')

def baseline(conn, token, post_id):
    existing=conn.execute("SELECT value FROM app_meta WHERE key='baseline_post_id'").fetchone()
    if existing:
        sys.exit('Baseline already exists. Refusing to reset automatically.')
    start=dt.datetime.now(dt.timezone.utc)
    comments=fetch_all(token, post_id)
    print(f'Existing replies found: {len(comments)}')
    print('No replies will be published.')
    if input('Type BASELINE to ignore these existing replies: ') != 'BASELINE':
        sys.exit('Cancelled')
    with conn:
        for c in comments:
            conn.execute('INSERT OR IGNORE INTO replies (comment_id,username,comment_text,intent,reply_text,status) VALUES (?,?,?,?,?,?)',
                         (str(c['id']), c.get('username',''),c.get('text',''), 'baseline','', 'baseline_ignored'))
        conn.execute("INSERT INTO app_meta(key,value) VALUES ('baseline_post_id',?)",(post_id,))
        conn.execute("INSERT INTO app_meta(key,value) VALUES ('baseline_time',?)",(start.isoformat(),))
    print('Baseline saved. Only comments timestamped AFTER baseline time are eligible for controlled review.')

def candidates(conn, token, post_id):
    row=conn.execute("SELECT value FROM app_meta WHERE key='baseline_post_id'").fetchone()
    t=conn.execute("SELECT value FROM app_meta WHERE key='baseline_time'").fetchone()
    if not row or not t or row[0] != post_id:
        sys.exit('Run baseline first for this THREADS_POST_ID. No publish permitted.')
    cutoff=utcparse(t[0])
    for c in fetch_all(token, post_id):
        cid=str(c.get('id',''))
        when=utcparse(c.get('timestamp'))
        if (not cid or not when or when <= cutoff or c.get('is_reply_owned_by_me') or
            not c.get('text') or not app.relevant(c['text'])):
            continue
        if conn.execute('SELECT 1 FROM replies WHERE comment_id=?',(cid,)).fetchone():
            continue
        yield c

def preview(conn, token, post_id):
    possible=list(candidates(conn,token,post_id))
    print(f'New relevant comments eligible for review: {len(possible)}')
    for c in possible[:10]:
        intent,reply=app.answer(c['text'])
        print(json.dumps({'comment_id':str(c['id']),'username':c.get('username'),
            'comment':c['text'],'intent':intent,'suggested_reply':reply},ensure_ascii=False))
    print('Preview only. Nothing published.')

def send_one(conn,token,post_id,comment_id):
    if os.getenv('LIVE_MODE','false').lower() != 'true':
        sys.exit('LIVE_MODE=false; refusing to send. Set true ONLY for a supervised single-send test.')
    matches=[c for c in candidates(conn,token,post_id) if str(c['id']) == comment_id]
    if len(matches)!=1:
        sys.exit('Comment not eligible (old, already logged, irrelevant, or not found). Nothing published.')
    c=matches[0]
    intent,reply=app.answer(c['text'])
    if len(reply)>MAX_TEXT_LENGTH:
        sys.exit('Reply >500 characters. Nothing published.')
    print('Comment:',c['text'])
    print('Reply:',reply)
    if input('Type SEND '+comment_id+' to publish exactly this reply: ') != 'SEND '+comment_id:
        sys.exit('Cancelled. Nothing published.')
    reviewed,published=app.process(conn,[c],token=token,live=True,max_replies=1)
    print(f'Published={published}; check `python3 app.py status` for sent/needs_review. Never retry needs_review blindly.')

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['baseline','preview','send-one'])
    p.add_argument('--comment-id',default='')
    args=p.parse_args()
    token,post_id=load()
    conn=db()
    try:
        if args.command=='baseline': baseline(conn,token,post_id)
        elif args.command=='preview': preview(conn,token,post_id)
        elif args.command=='send-one':
            if not args.comment_id.isdigit(): sys.exit('Provide --comment-id numeric value')
            send_one(conn,token,post_id,args.comment_id)
    finally:
        conn.close()
