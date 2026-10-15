#!/usr/bin/env python3
"""Shared leaderboard for the Sleeping Clock Room escape game.

GET  /api/scores?player=<id>  -> {"rows": [...top 10...]}
POST /api/scores              -> {"newBest": bool, "rows": [...]}
     body: {"player": id, "name": str, "time": seconds, "hints": count}

Each player id keeps only its best score. Player ids are never returned,
so another player cannot overwrite someone else's record.
"""
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = int(os.environ.get('PORT', '51860'))
DB_PATH = os.path.join(os.environ.get('STATE_DIRECTORY', '.'), 'scores.db')
PENALTY = 30          # seconds added per hint, same as the game
KEEP = 1000           # rows kept; worse ones are pruned
POSTS_PER_MINUTE = 6
PLAYER_RE = re.compile(r'^[A-Za-z0-9-]{16,64}$')
# control characters and bidi overrides; ZWNJ/ZWJ stay because Persian needs them
STRIP_RE = re.compile('[\x00-\x1f\x7f‪-‮⁦-⁩]')

lock = threading.Lock()
recent_posts = {}
con = sqlite3.connect(DB_PATH, check_same_thread=False)
con.execute('''CREATE TABLE IF NOT EXISTS scores (
    player TEXT PRIMARY KEY, name TEXT NOT NULL, score INTEGER NOT NULL,
    time INTEGER NOT NULL, hints INTEGER NOT NULL, at INTEGER NOT NULL)''')
con.commit()


def top(player):
    rows = con.execute('SELECT player, name, score, hints FROM scores ORDER BY score, at LIMIT 10').fetchall()
    return [{'name': n, 'score': s, 'hints': h, 'mine': p == player} for p, n, s, h in rows]


def clean_name(value):
    if not isinstance(value, str):
        return None
    value = ' '.join(STRIP_RE.sub('', value).split())
    return value if 1 <= len(value) <= 24 else None


def bounded_int(value, lo, hi):
    ok = isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi
    return value if ok else None


def rate_limited(ip):
    now = time.monotonic()
    with lock:
        if len(recent_posts) > 10000:
            recent_posts.clear()
        stamps = [t for t in recent_posts.get(ip, []) if now - t < 60]
        limited = len(stamps) >= POSTS_PER_MINUTE
        if not limited:
            stamps.append(now)
        recent_posts[ip] = stamps
        return limited


class Handler(BaseHTTPRequestHandler):
    server_version = 'riddle-scores'

    def reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != '/api/scores':
            return self.reply(404, {'error': 'not found'})
        player = parse_qs(url.query).get('player', [None])[0]
        with lock:
            rows = top(player)
        self.reply(200, {'rows': rows})

    def do_POST(self):
        if urlsplit(self.path).path != '/api/scores':
            return self.reply(404, {'error': 'not found'})
        if rate_limited(self.headers.get('X-Real-IP') or self.client_address[0]):
            return self.reply(429, {'error': 'too many requests'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if not 0 < length <= 2048:
            return self.reply(400, {'error': 'body missing or too large'})
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self.reply(400, {'error': 'invalid json'})
        if not isinstance(body, dict):
            return self.reply(400, {'error': 'invalid json'})

        player = body.get('player')
        name = clean_name(body.get('name'))
        secs = bounded_int(body.get('time'), 20, 86400)
        hints = bounded_int(body.get('hints'), 0, 50)
        if not (isinstance(player, str) and PLAYER_RE.match(player)) or None in (name, secs, hints):
            return self.reply(400, {'error': 'invalid score'})

        score = secs + hints * PENALTY
        with lock:
            row = con.execute('SELECT score FROM scores WHERE player = ?', (player,)).fetchone()
            new_best = row is None or score < row[0]
            if new_best:
                con.execute('''INSERT INTO scores (player, name, score, time, hints, at) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(player) DO UPDATE SET name = excluded.name, score = excluded.score,
                    time = excluded.time, hints = excluded.hints, at = excluded.at''',
                            (player, name, score, secs, hints, int(time.time())))
            else:
                con.execute('UPDATE scores SET name = ? WHERE player = ?', (name, player))
            con.execute('DELETE FROM scores WHERE player NOT IN (SELECT player FROM scores ORDER BY score, at LIMIT ?)', (KEEP,))
            con.commit()
            rows = top(player)
        self.reply(200, {'newBest': new_best, 'rows': rows})

    def log_message(self, fmt, *args):
        pass  # no per-request logs: they would only record player IPs


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
