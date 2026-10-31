#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
نظام الحضور والانصراف — Attendance & Departure System
Backend server: Python 3.10+ standard library only (no pip install needed) with SQLite.
Set DATABASE_URL to use PostgreSQL (e.g. Neon) instead; that needs `pip install "psycopg[binary]"`.

    py server.py                 # run (seeds demo data on first start)
    py server.py --port 8443     # custom port
    py server.py --reset-demo    # wipe the database and re-create demo data
    py server.py --no-demo       # first start without demo data (creates admin only)

On Vercel the module-level WSGI `app` is the entry point (see pyproject.toml).
"""
import argparse
import hashlib
import hmac
import http.client
import io
import json
import logging
import math
import os
import random
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from xml.sax.saxutils import escape as xml_escape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('ATT_DATA_DIR') or os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'attendance.db')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
CERT_FILE = os.path.join(BASE_DIR, 'certs', 'cert.pem')
KEY_FILE = os.path.join(BASE_DIR, 'certs', 'key.pem')
DATABASE_URL = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL') or ''
USE_PG = DATABASE_URL.startswith(('postgres://', 'postgresql://'))
ON_VERCEL = bool(os.environ.get('VERCEL'))
if USE_PG:
    import psycopg
    IntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError)
else:
    IntegrityError = sqlite3.IntegrityError

SESSION_COOKIE = 'att_session'
SESSION_DAYS = 30
MAX_BODY = 1_000_000
PBKDF2_ITER = 150_000
USE_TLS = False
SSL_CTX = None
BEHIND_PROXY = False  # behind Cloudflare Tunnel / a reverse proxy that terminates HTTPS
PROXY_ANY_PEER = False  # behind a hosted proxy (Railway): listening on all interfaces, reachable only via the proxy

log = logging.getLogger('attendance')

ROLES = ('admin', 'hr', 'manager', 'employee')
ADMIN = {'admin'}
ADMIN_HR = {'admin', 'hr'}
STAFF = {'admin', 'hr', 'manager'}

DEFAULT_SETTINGS = {
    'company_ar': 'شركة النموذج',
    'company_en': 'Demo Company',
    'utc_offset_min': '180',          # local time zone (minutes from UTC). 180 = UTC+3 (Riyadh)
    'late_enabled': '1',
    'early_enabled': '1',
    'overtime_enabled': '1',
    'absent_if_late_over_min': '0',   # 0 = off. Otherwise: late more than N minutes counts as absence
    'require_gps': '1',
    'max_accuracy_m': '150',
    'max_offline_hours': '72',
    'no_checkout_after_min': '120',   # minutes after shift end before "did not check out" is flagged
    'backup_keep': '14',
}
SETTING_KINDS = {
    'company_ar': 'str!', 'company_en': 'str!', 'utc_offset_min': 'int!',
    'late_enabled': 'bool', 'early_enabled': 'bool', 'overtime_enabled': 'bool',
    'absent_if_late_over_min': 'int!', 'require_gps': 'bool', 'max_accuracy_m': 'int!',
    'max_offline_hours': 'int!', 'no_checkout_after_min': 'int!', 'backup_keep': 'int!',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments(
  id INTEGER PRIMARY KEY, name_ar TEXT NOT NULL, name_en TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locations(
  id INTEGER PRIMARY KEY, name_ar TEXT NOT NULL, name_en TEXT NOT NULL,
  lat REAL NOT NULL, lng REAL NOT NULL, radius_m INTEGER NOT NULL DEFAULT 200);
CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY, name_ar TEXT NOT NULL, name_en TEXT NOT NULL,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  work_days TEXT NOT NULL DEFAULT '0,1,2,3,4',
  grace_in_min INTEGER NOT NULL DEFAULT 15, grace_out_min INTEGER NOT NULL DEFAULT 10,
  break_min INTEGER NOT NULL DEFAULT 60, overtime_after_min INTEGER NOT NULL DEFAULT 30);
CREATE TABLE IF NOT EXISTS employees(
  id INTEGER PRIMARY KEY, emp_no TEXT NOT NULL UNIQUE, name_ar TEXT NOT NULL, name_en TEXT NOT NULL,
  department_id INTEGER REFERENCES departments(id), job_title TEXT, phone TEXT, email TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  shift_id INTEGER REFERENCES shifts(id), location_id INTEGER REFERENCES locations(id),
  geofence_required INTEGER NOT NULL DEFAULT 1, exempt_rules INTEGER NOT NULL DEFAULT 0,
  manager_id INTEGER REFERENCES employees(id), hire_date TEXT,
  extra_location_ids TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, pw_hash TEXT NOT NULL,
  role TEXT NOT NULL, employee_id INTEGER REFERENCES employees(id), active INTEGER NOT NULL DEFAULT 1,
  lang TEXT NOT NULL DEFAULT 'ar', failed INTEGER NOT NULL DEFAULT 0, locked_until TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL, created_at TEXT NOT NULL, ip TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS leaves(
  id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
  type TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, note TEXT,
  created_by INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS punches(
  id INTEGER PRIMARY KEY, uuid TEXT UNIQUE, employee_id INTEGER NOT NULL REFERENCES employees(id),
  type TEXT NOT NULL, work_date TEXT NOT NULL, punch_time TEXT NOT NULL, client_time TEXT,
  synced_at TEXT NOT NULL, is_offline INTEGER NOT NULL DEFAULT 0,
  lat REAL, lng REAL, accuracy REAL, address TEXT, distance_m REAL, in_fence INTEGER,
  source TEXT NOT NULL DEFAULT 'mobile', device TEXT, note TEXT, location_id INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS ux_punch_day ON punches(employee_id, work_date, type);
CREATE INDEX IF NOT EXISTS ix_punch_date ON punches(work_date);
CREATE INDEX IF NOT EXISTS ix_punch_time ON punches(employee_id, punch_time);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, user_id INTEGER, username TEXT, action TEXT NOT NULL,
  entity TEXT, entity_id INTEGER, details TEXT, reason TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);
CREATE TABLE IF NOT EXISTS login_fails(ip TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_login_fails ON login_fails(ip, at);
"""
PG_TABLES = ('audit_log', 'punches', 'leaves', 'sessions', 'users', 'employees', 'shifts', 'locations',
             'departments', 'settings', 'login_fails')


# ---------------------------------------------------------------- helpers

class ApiError(Exception):
    def __init__(self, status, code, **params):
        super().__init__(code)
        self.status, self.code, self.params = status, code, params


class Raw:
    """A non-JSON response (file download)."""
    def __init__(self, body, ctype, filename=None):
        self.body, self.ctype, self.filename = body, ctype, filename


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(s):
    s = str(s).strip().replace('Z', '+00:00')
    if '.' in s:  # normalise fractional seconds for Python 3.10
        head, rest = s.split('.', 1)
        m = re.match(r'(\d+)(.*)', rest)
        s = head + (m.group(2) if m else '')
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def offset(st):
    return timedelta(minutes=int(st.get('utc_offset_min', 180)))


def to_local(dt_utc, st):
    return (dt_utc + offset(st)).replace(tzinfo=None)


def from_local(naive, st):
    return (naive - offset(st)).replace(tzinfo=timezone.utc)


def local_now(st):
    return to_local(now_utc(), st)


def hm_to_min(s):
    h, m = s.split(':')
    return int(h) * 60 + int(m)


def js_weekday(d):
    """0 = Sunday ... 6 = Saturday (same as JavaScript)."""
    return (d.weekday() + 1) % 7


def mins(td):
    return int(td.total_seconds() // 60)


def shift_window(shift, d):
    s, e = hm_to_min(shift['start_time']), hm_to_min(shift['end_time'])
    base = datetime(d.year, d.month, d.day)
    start, end = base + timedelta(minutes=s), base + timedelta(minutes=e)
    if e <= s:  # overnight shift ends next day
        end += timedelta(days=1)
    return start, end


def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def daterange(d1, d2):
    d = d1
    while d <= d2:
        yield d
        d += timedelta(days=1)


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, PBKDF2_ITER)
    return f'pbkdf2_sha256${PBKDF2_ITER}${salt.hex()}${dk.hex()}'


DUMMY_HASH = None


def verify_password(pw, stored):
    try:
        _, it, salt, h = stored.split('$')
        dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


def connect():
    if USE_PG:
        return PgDB(pg_connect())
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


# ---------------------------------------------------------------- PostgreSQL (Neon) backend
# The SQL in this file is written for SQLite; PgDB adapts it so the same statements run on PostgreSQL.

class PgRow(dict):
    """dict row that also supports row[0], like sqlite3.Row."""
    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return dict.__getitem__(self, k)


def pg_row_factory(cur):
    names = [c.name for c in cur.description] if cur.description else []
    return lambda values: PgRow(zip(names, values))


PG_NO_ID = {'settings', 'sessions', 'login_fails'}  # tables without an id column
RX_INSERT = re.compile(r'\s*INSERT\s+INTO\s+(\w+)', re.I)
RX_WRITE = re.compile(r'\s*(INSERT|UPDATE|DELETE)\b', re.I)
RX_LIKE = re.compile(r'\bLIKE\b')


def pg_sql(sql):
    """? placeholders -> %s; LIKE -> ILIKE (SQLite's LIKE ignores case)."""
    return RX_LIKE.sub('ILIKE', sql.replace('%', '%%').replace('?', '%s'))


def pg_params(params):
    return [int(p) if isinstance(p, bool) else p for p in params]


class PgCursor:
    def __init__(self, cur, lastrowid=None):
        self._cur, self.lastrowid = cur, lastrowid

    def fetchone(self):
        return self._cur.fetchone() if self._cur.description else None

    def fetchall(self):
        return self._cur.fetchall() if self._cur.description else []

    def __iter__(self):
        return iter(self.fetchall())


class PgDB:
    """The subset of the sqlite3.Connection API this server uses, on a psycopg connection."""
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        ins = RX_INSERT.match(sql)
        write = ins or RX_WRITE.match(sql)
        q = pg_sql(sql)
        returning = bool(ins) and ins.group(1).lower() not in PG_NO_ID
        if returning:
            q += ' RETURNING id'
        cur = self.conn.cursor(row_factory=pg_row_factory)
        if write:  # a failed write must not abort the whole transaction (SQLite keeps it usable)
            cur.execute('SAVEPOINT w')
        try:
            cur.execute(q, pg_params(params))
        except psycopg.Error:
            if write:
                cur.execute('ROLLBACK TO SAVEPOINT w')
            raise
        return PgCursor(cur, cur.fetchone()[0] if returning else None)

    def executemany(self, sql, seq):
        with self.conn.cursor() as cur:
            cur.executemany(pg_sql(sql), [pg_params(p) for p in seq])

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        pg_release(self.conn)


PG_POOL = []  # idle connections kept for reuse: (conn, last_used)
PG_LOCK = threading.Lock()


def pg_connect():
    with PG_LOCK:
        while PG_POOL:
            conn, used = PG_POOL.pop()
            if time.time() - used < 60 and not conn.closed:
                return conn
            conn.close()
    # prepare_threshold=None: Neon's pooled endpoint (PgBouncer) does not keep prepared statements
    return psycopg.connect(DATABASE_URL, prepare_threshold=None, connect_timeout=10)


def pg_release(conn):
    try:
        conn.rollback()  # end anything left uncommitted
    except psycopg.Error:
        conn.close()
        return
    with PG_LOCK:
        if not conn.closed and not conn.broken and len(PG_POOL) < 4:
            PG_POOL.append((conn, time.time()))
            return
    conn.close()


def pg_schema(sql):
    return (sql.replace('INTEGER PRIMARY KEY', 'INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY')
               .replace(' REAL', ' DOUBLE PRECISION').replace(' COLLATE NOCASE', ''))


def pg_sync_ids(db):
    """After inserting explicit ids (demo data) move each identity sequence past them."""
    for t in PG_TABLES:
        if t not in PG_NO_ID:
            db.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                       f"COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)")


def get_settings(db):
    st = dict(DEFAULT_SETTINGS)
    for r in db.execute('SELECT key, value FROM settings'):
        st[r['key']] = r['value']
    return st


def ival(st, key):
    try:
        return int(st.get(key) or 0)
    except ValueError:
        return int(DEFAULT_SETTINGS.get(key, 0))


def audit(db, user, action, entity=None, entity_id=None, details=None, reason=None, ip=None):
    db.execute(
        'INSERT INTO audit_log(at,user_id,username,action,entity,entity_id,details,reason,ip) VALUES(?,?,?,?,?,?,?,?,?)',
        (iso(now_utc()), user['id'] if user else None, user['username'] if user else None, action, entity,
         entity_id, json.dumps(details, ensure_ascii=False, default=str) if details is not None else None,
         reason, ip))


def coerce(kind, v, field, db=None):
    """Validate/convert one input value. kind ends with '!' when required."""
    required = kind.endswith('!')
    kind = kind.rstrip('!')
    if kind == 'bool':
        return 1 if v in (True, 1, '1', 'true', 'on', 'yes') else 0
    if v is None or (isinstance(v, str) and v.strip() == '') or v == []:
        if required:
            raise ApiError(400, 'required', field=field)
        return '' if kind.startswith('refs:') else None
    try:
        if kind == 'str':
            v = str(v).strip()
            if len(v) > 200:
                raise ValueError
            return v
        if kind == 'text':
            v = str(v).strip()
            if len(v) > 2000:
                raise ValueError
            return v
        if kind == 'int':
            return int(v)
        if kind == 'float':
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                raise ValueError
            return f
        if kind == 'time':
            v = str(v).strip()[:5]
            if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', v):
                raise ValueError
            return v
        if kind == 'date':
            return date.fromisoformat(str(v)[:10]).isoformat()
        if kind == 'days':
            items = v if isinstance(v, list) else str(v).split(',')
            days = sorted({int(x) for x in items if str(x).strip() != ''})
            if not days or any(d < 0 or d > 6 for d in days):
                raise ValueError
            return ','.join(map(str, days))
        if kind.startswith('enum:'):
            opts = kind[5:].split(',')
            if v not in opts:
                raise ValueError
            return v
        if kind.startswith('refs:'):
            items = v if isinstance(v, list) else str(v).split(',')
            ids = sorted({int(x) for x in items if str(x).strip() != ''})
            if db is not None:
                for i in ids:
                    if not db.execute(f'SELECT 1 FROM {kind[5:]} WHERE id=?', (i,)).fetchone():
                        raise ValueError
            return ','.join(map(str, ids))
        if kind.startswith('ref:'):
            i = int(v)
            if db is not None and not db.execute(f'SELECT 1 FROM {kind[4:]} WHERE id=?', (i,)).fetchone():
                raise ValueError
            return i
    except ApiError:
        raise
    except Exception:
        raise ApiError(400, 'invalid', field=field)
    raise ApiError(400, 'invalid', field=field)


# ---------------------------------------------------------------- routing

ROUTES = []


def route(method, pattern, roles=None):
    """roles: None = public, 'any' = any signed-in user, or a set of roles."""
    def deco(fn):
        ROUTES.append((method, re.compile('^' + pattern + '$'), fn, roles))
        return fn
    return deco


def client_ip(handler):
    ip = handler.client_address[0]
    # trust the header only when the proxy is local, or on a hosted platform (Railway) whose edge is the only way in
    if BEHIND_PROXY and (PROXY_ANY_PEER or ip in ('127.0.0.1', '::1')):
        fwd = (handler.headers.get('X-Real-IP') if PROXY_ANY_PEER else handler.headers.get('CF-Connecting-IP')) \
            or (handler.headers.get('X-Forwarded-For') or '').split(',')[0]
        ip = fwd.strip()[:45] or ip
    return ip


class Ctx:
    def __init__(self, handler, method, path, query):
        self.h, self.method, self.path, self.query = handler, method, path, query
        self.db = connect()
        self.user = None
        self.params = {}
        self.cookies_out = []
        self.ip = client_ip(handler)
        self._body = None
        self.st = get_settings(self.db)

    def q(self, key, default=None):
        v = self.query.get(key)
        return v[0] if v else default

    def qint(self, key, default):
        try:
            return int(self.q(key, default))
        except (TypeError, ValueError):
            return default

    def qdate(self, key, default):
        v = self.q(key)
        if not v:
            return default
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            raise ApiError(400, 'invalid', field=key)

    @property
    def body(self):
        if self._body is None:
            n = int(self.h.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                raise ApiError(413, 'too_large')
            raw = self.h.rfile.read(n) if n else b''
            try:
                self._body = json.loads(raw.decode('utf-8')) if raw else {}
            except Exception:
                raise ApiError(400, 'bad_json')
            if not isinstance(self._body, dict):
                raise ApiError(400, 'bad_json')
        return self._body

    def load_user(self):
        c = SimpleCookie(self.h.headers.get('Cookie') or '')
        if SESSION_COOKIE not in c:
            return None
        th = hashlib.sha256(c[SESSION_COOKIE].value.encode()).hexdigest()
        r = self.db.execute(
            'SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',
            (th, iso(now_utc()))).fetchone()
        if r and r['active']:
            self.user = dict(r)
            self.token_hash = th
        return self.user

    def require_auth(self):
        if not self.user and not self.load_user():
            raise ApiError(401, 'unauthorized')

    def audit(self, action, entity=None, entity_id=None, details=None, reason=None):
        audit(self.db, self.user, action, entity, entity_id, details, reason, self.ip)

    def scope_ids(self):
        """None = all employees; otherwise the set of employee ids this user may see."""
        role = self.user['role']
        if role in ADMIN_HR:
            return None
        own = self.user['employee_id']
        ids = {own} if own else set()
        if role == 'manager' and own:
            ids |= {r[0] for r in self.db.execute('SELECT id FROM employees WHERE manager_id=?', (own,))}
        return ids


def scope_sql(ids, col):
    if ids is None:
        return '', []
    if not ids:
        return ' AND 1=0', []
    return f' AND {col} IN ({",".join("?" * len(ids))})', list(ids)


# ---------------------------------------------------------------- auth

def user_public(db, u):
    emp = None
    if u['employee_id']:
        r = db.execute(
            '''SELECT e.*, d.name_ar dept_ar, d.name_en dept_en FROM employees e
               LEFT JOIN departments d ON d.id=e.department_id WHERE e.id=?''', (u['employee_id'],)).fetchone()
        emp = dict(r) if r else None
    return {'id': u['id'], 'username': u['username'], 'role': u['role'], 'lang': u['lang'],
            'employee_id': u['employee_id'], 'employee': emp}


@route('POST', '/api/login')
def api_login(ctx):
    b = ctx.body
    username = str(b.get('username') or '').strip()
    pw = str(b.get('password') or '')
    now = now_utc()
    since = iso(now - timedelta(minutes=15))  # failures per IP are kept in the database (shared by all instances)
    if ctx.db.execute('SELECT COUNT(*) FROM login_fails WHERE ip=? AND at>?', (ctx.ip, since)).fetchone()[0] >= 20:
        raise ApiError(429, 'too_many_attempts')
    u = ctx.db.execute('SELECT * FROM users WHERE lower(username)=lower(?)', (username,)).fetchone()
    if u and u['locked_until'] and u['locked_until'] > iso(now):
        left = max(1, mins(parse_iso(u['locked_until']) - now))
        raise ApiError(423, 'account_locked', minutes=left)
    ok = verify_password(pw, u['pw_hash'] if u else DUMMY_HASH)
    if not u or not ok:
        ctx.db.execute('INSERT INTO login_fails(ip,at) VALUES(?,?)', (ctx.ip, iso(now)))
        if u:
            failed = u['failed'] + 1
            locked = iso(now + timedelta(minutes=10)) if failed >= 5 else None
            ctx.db.execute('UPDATE users SET failed=?, locked_until=? WHERE id=?',
                           (0 if locked else failed, locked, u['id']))
        audit(ctx.db, None, 'login_failed', 'user', u['id'] if u else None, {'username': username}, ip=ctx.ip)
        ctx.db.commit()
        raise ApiError(401, 'invalid_credentials')
    if not u['active']:
        raise ApiError(403, 'account_disabled')
    token = secrets.token_urlsafe(32)
    ctx.db.execute('UPDATE users SET failed=0, locked_until=NULL WHERE id=?', (u['id'],))
    ctx.db.execute('INSERT INTO sessions(token_hash,user_id,expires_at,created_at,ip) VALUES(?,?,?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(), u['id'],
                    iso(now + timedelta(days=SESSION_DAYS)), iso(now), ctx.ip))
    ctx.db.execute('DELETE FROM sessions WHERE expires_at<?', (iso(now),))
    ctx.db.execute('DELETE FROM login_fails WHERE at<?', (since,))
    ctx.user = dict(u)
    ctx.audit('login', 'user', u['id'])
    ctx.db.commit()
    ctx.cookies_out.append(f'{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; '
                           f'Max-Age={SESSION_DAYS * 86400}' + ('; Secure' if USE_TLS else ''))
    return {'user': user_public(ctx.db, ctx.user)}


@route('POST', '/api/logout', 'any')
def api_logout(ctx):
    ctx.db.execute('DELETE FROM sessions WHERE token_hash=?', (ctx.token_hash,))
    ctx.audit('logout', 'user', ctx.user['id'])
    ctx.db.commit()
    ctx.cookies_out.append(f'{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0')
    return {'ok': True}


@route('GET', '/api/health')
def api_health(ctx):
    return {'ok': True, 'time': iso(now_utc())}


def employee_full(db, emp_id):
    r = db.execute(
        '''SELECT e.*, d.name_ar dept_ar, d.name_en dept_en,
                  s.name_ar shift_ar, s.name_en shift_en, s.start_time, s.end_time, s.work_days,
                  s.grace_in_min, s.grace_out_min, s.break_min,
                  l.name_ar loc_ar, l.name_en loc_en, l.lat loc_lat, l.lng loc_lng, l.radius_m
           FROM employees e LEFT JOIN departments d ON d.id=e.department_id
           LEFT JOIN shifts s ON s.id=e.shift_id LEFT JOIN locations l ON l.id=e.location_id
           WHERE e.id=?''', (emp_id,)).fetchone()
    if not r:
        return None
    emp = dict(r)
    emp['sites'] = employee_sites(db, emp)
    return emp


def employee_sites(db, emp):
    """All locations the employee may punch at: the main one first, then the additional ones."""
    ids = [emp['location_id']] if emp['location_id'] else []
    ids += [int(x) for x in (emp.get('extra_location_ids') or '').split(',') if x and int(x) not in ids]
    if not ids:
        return []
    rows = {r['id']: dict(r) for r in db.execute(
        f'SELECT * FROM locations WHERE id IN ({",".join("?" * len(ids))})', ids)}
    return [rows[i] for i in ids if i in rows]


def nearest_site(sites, lat, lng):
    """(site, distance_m), preferring a site whose radius contains the point."""
    best = None
    for s in sites:
        d = haversine_m(lat, lng, s['lat'], s['lng'])
        key = (d > s['radius_m'], d)
        if best is None or key < best[0]:
            best = (key, s, d)
    return (best[1], best[2]) if best else (None, None)


def open_checkin(db, emp_id, at_utc):
    """Latest check-in within 20h before `at_utc` that has no check-out yet."""
    return db.execute(
        '''SELECT * FROM punches p WHERE employee_id=? AND type='in' AND punch_time<=? AND punch_time>=?
           AND NOT EXISTS(SELECT 1 FROM punches o WHERE o.employee_id=p.employee_id
                          AND o.work_date=p.work_date AND o.type='out')
           ORDER BY punch_time DESC LIMIT 1''',
        (emp_id, iso(at_utc + timedelta(minutes=1)), iso(at_utc - timedelta(hours=20)))).fetchone()


def default_work_date(shift, at_local):
    d = at_local.date()
    s, e = hm_to_min(shift['start_time']), hm_to_min(shift['end_time'])
    t = at_local.hour * 60 + at_local.minute
    if e <= s and t < e:  # after midnight inside an overnight shift -> belongs to yesterday
        d -= timedelta(days=1)
    return d


def punch_public(p):
    return {k: p[k] for k in ('id', 'uuid', 'type', 'work_date', 'punch_time', 'client_time', 'synced_at',
                              'is_offline', 'lat', 'lng', 'accuracy', 'address', 'distance_m', 'in_fence',
                              'source', 'note', 'location_id')}


def today_state(db, emp, st):
    if not emp or not emp.get('shift_id'):
        return None
    now = now_utc()
    oc = open_checkin(db, emp['id'], now)
    wd = oc['work_date'] if oc else default_work_date(emp, to_local(now, st)).isoformat()
    ps = {p['type']: punch_public(p) for p in
          db.execute('SELECT * FROM punches WHERE employee_id=? AND work_date=?', (emp['id'], wd))}
    d = date.fromisoformat(wd)
    scheduled = str(js_weekday(d)) in (emp['work_days'] or '').split(',')
    leave = db.execute('SELECT type, note FROM leaves WHERE employee_id=? AND start_date<=? AND end_date>=?',
                       (emp['id'], wd, wd)).fetchone()
    return {'work_date': wd, 'in': ps.get('in'), 'out': ps.get('out'), 'scheduled': scheduled,
            'leave': dict(leave) if leave else None}


@route('GET', '/api/me', 'any')
def api_me(ctx):
    emp = employee_full(ctx.db, ctx.user['employee_id']) if ctx.user['employee_id'] else None
    st = ctx.st
    return {
        'user': user_public(ctx.db, ctx.user),
        'employee': emp,
        'today': today_state(ctx.db, emp, st),
        'settings': {k: st[k] for k in ('company_ar', 'company_en', 'require_gps', 'max_accuracy_m',
                                        'utc_offset_min', 'max_offline_hours')},
        'server_time': iso(now_utc()),
    }


@route('PUT', '/api/me/lang', 'any')
def api_me_lang(ctx):
    lang = coerce('enum:ar,en!', ctx.body.get('lang'), 'lang')
    ctx.db.execute('UPDATE users SET lang=? WHERE id=?', (lang, ctx.user['id']))
    ctx.db.commit()
    return {'ok': True}


@route('PUT', '/api/me/password', 'any')
def api_me_password(ctx):
    old, new = str(ctx.body.get('old_password') or ''), str(ctx.body.get('new_password') or '')
    if not verify_password(old, ctx.user['pw_hash']):
        raise ApiError(400, 'wrong_password')
    check_password_policy(new)
    ctx.db.execute('UPDATE users SET pw_hash=? WHERE id=?', (hash_password(new), ctx.user['id']))
    ctx.db.execute('DELETE FROM sessions WHERE user_id=? AND token_hash<>?', (ctx.user['id'], ctx.token_hash))
    ctx.audit('password_changed', 'user', ctx.user['id'])
    ctx.db.commit()
    return {'ok': True}


def check_password_policy(pw):
    if len(pw) < 8 or not re.search(r'[A-Za-z]', pw) or not re.search(r'\d', pw):
        raise ApiError(400, 'weak_password')


# ---------------------------------------------------------------- punching

def reject(ctx, emp_id, code, **params):
    """Log a rejected punch attempt (important event) and abort."""
    ctx.db.rollback()
    ctx.audit('punch_rejected', 'employee', emp_id, dict(params, code=code))
    ctx.db.commit()
    raise ApiError(422, code, **params)


@route('POST', '/api/punch', 'any')
def api_punch(ctx):
    b, db, st = ctx.body, ctx.db, ctx.st
    emp_id = ctx.user['employee_id']
    if not emp_id:
        raise ApiError(400, 'no_employee')
    typ = b.get('type')
    if typ not in ('in', 'out'):
        raise ApiError(400, 'invalid', field='type')
    puuid = str(b.get('uuid') or uuid.uuid4())[:64]
    dup = db.execute('SELECT * FROM punches WHERE uuid=?', (puuid,)).fetchone()
    if dup:  # an offline punch re-sent: idempotent
        if dup['employee_id'] != emp_id:
            raise ApiError(409, 'conflict')
        emp = employee_full(db, emp_id)
        return {'ok': True, 'duplicate': True, 'punch': punch_public(dup), 'today': today_state(db, emp, st)}

    emp = employee_full(db, emp_id)
    if not emp or emp['status'] != 'active':
        reject(ctx, emp_id, 'employee_inactive')
    if not emp['shift_id']:
        reject(ctx, emp_id, 'no_shift')

    now = now_utc()
    offline = bool(b.get('offline'))
    client_time = None
    if b.get('client_time'):
        try:
            client_time = parse_iso(b['client_time'])
        except Exception:
            raise ApiError(400, 'invalid', field='client_time')
    punch_time = now
    if offline and client_time:
        if client_time > now + timedelta(minutes=5):
            reject(ctx, emp_id, 'bad_time')
        if now - client_time > timedelta(hours=ival(st, 'max_offline_hours')):
            reject(ctx, emp_id, 'offline_too_old', hours=ival(st, 'max_offline_hours'))
        punch_time = client_time

    lat, lng, acc = b.get('lat'), b.get('lng'), b.get('accuracy')
    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
        acc = float(acc) if acc is not None else None
        if lat is not None and not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError
    except (TypeError, ValueError):
        raise ApiError(400, 'invalid', field='location')

    sites = emp['sites']
    fence = bool(emp['geofence_required']) and bool(sites)
    if lat is None and (fence or ival(st, 'require_gps')):
        reject(ctx, emp_id, 'gps_required', reason=str(b.get('loc_error') or '')[:100])
    distance = in_fence = site = None
    if lat is not None and sites:
        site, distance = nearest_site(sites, lat, lng)
        distance = round(distance, 1)
        in_fence = 1 if distance <= site['radius_m'] else 0
    if fence:
        if acc is not None and acc > ival(st, 'max_accuracy_m'):
            reject(ctx, emp_id, 'low_accuracy', accuracy=round(acc), max=ival(st, 'max_accuracy_m'))
        if not in_fence:
            reject(ctx, emp_id, 'out_of_fence', distance=round(distance), radius=site['radius_m'])

    if typ == 'out':
        oc = open_checkin(db, emp_id, punch_time)
        if oc:
            work_date = oc['work_date']
        else:
            wd = default_work_date(emp, to_local(punch_time, st)).isoformat()
            if db.execute("SELECT 1 FROM punches WHERE employee_id=? AND work_date=? AND type='out'",
                          (emp_id, wd)).fetchone():
                reject(ctx, emp_id, 'already_checked_out')
            reject(ctx, emp_id, 'no_check_in')
    else:
        work_date = default_work_date(emp, to_local(punch_time, st)).isoformat()
        if db.execute("SELECT 1 FROM punches WHERE employee_id=? AND work_date=? AND type='in'",
                      (emp_id, work_date)).fetchone():
            reject(ctx, emp_id, 'already_checked_in')

    try:
        cur = db.execute(
            '''INSERT INTO punches(uuid,employee_id,type,work_date,punch_time,client_time,synced_at,is_offline,
                                   lat,lng,accuracy,address,distance_m,in_fence,source,device,location_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (puuid, emp_id, typ, work_date, iso(punch_time), iso(client_time) if client_time else None,
             iso(now), 1 if offline else 0, lat, lng, acc, str(b.get('address') or '')[:300] or None,
             distance, in_fence, 'mobile', str(ctx.h.headers.get('User-Agent') or '')[:200],
             site['id'] if site else None))
    except IntegrityError:
        reject(ctx, emp_id, 'already_checked_in' if typ == 'in' else 'already_checked_out')
    ctx.audit('punch_' + typ, 'punch', cur.lastrowid,
              {'employee_id': emp_id, 'work_date': work_date, 'offline': offline, 'distance_m': distance,
               'location_id': site['id'] if site else None})
    db.commit()
    p = db.execute('SELECT * FROM punches WHERE id=?', (cur.lastrowid,)).fetchone()
    return {'ok': True, 'punch': punch_public(p), 'today': today_state(db, emp, st)}


@route('GET', '/api/my/punches', 'any')
def api_my_punches(ctx):
    emp_id = ctx.user['employee_id']
    if not emp_id:
        return {'items': []}
    today = local_now(ctx.st).date()
    d1 = ctx.qdate('from', today - timedelta(days=30))
    d2 = ctx.qdate('to', today)
    rows = report_rows(ctx.db, ctx.st, d1, d2, {emp_id}, gps=True)
    rows.reverse()
    return {'items': rows}


# ---------------------------------------------------------------- generic CRUD

def check_shift(db, data, row_id):
    s = data.get('start_time')
    if s is not None and s == data.get('end_time'):
        raise ApiError(400, 'invalid', field='end_time')


def check_location(db, data, row_id):
    if data.get('lat') is not None and not -90 <= data['lat'] <= 90:
        raise ApiError(400, 'invalid', field='lat')
    if data.get('lng') is not None and not -180 <= data['lng'] <= 180:
        raise ApiError(400, 'invalid', field='lng')
    if data.get('radius_m') is not None and not 10 <= data['radius_m'] <= 50000:
        raise ApiError(400, 'invalid', field='radius_m')


def check_leave(db, data, row_id):
    if data.get('start_date') and data.get('end_date') and data['end_date'] < data['start_date']:
        raise ApiError(400, 'invalid', field='end_date')


def check_employee(db, data, row_id):
    if row_id and data.get('manager_id') == row_id:
        raise ApiError(400, 'invalid', field='manager_id')
    if data.get('emp_no'):
        r = db.execute('SELECT id FROM employees WHERE emp_no=? AND id<>?', (data['emp_no'], row_id or 0)).fetchone()
        if r:
            raise ApiError(409, 'duplicate', field='emp_no')


ENTITIES = {
    'departments': dict(
        table='departments', alias='t', fields={'name_ar': 'str!', 'name_en': 'str!'},
        search=['t.name_ar', 't.name_en'], order='t.id', read=STAFF, write=ADMIN_HR),
    'locations': dict(
        table='locations', alias='t',
        fields={'name_ar': 'str!', 'name_en': 'str!', 'lat': 'float!', 'lng': 'float!', 'radius_m': 'int!'},
        search=['t.name_ar', 't.name_en'], order='t.id', read=STAFF, write=ADMIN_HR, check=check_location),
    'shifts': dict(
        table='shifts', alias='t',
        fields={'name_ar': 'str!', 'name_en': 'str!', 'start_time': 'time!', 'end_time': 'time!',
                'work_days': 'days!', 'grace_in_min': 'int!', 'grace_out_min': 'int!', 'break_min': 'int!',
                'overtime_after_min': 'int!'},
        search=['t.name_ar', 't.name_en'], order='t.id', read=STAFF, write=ADMIN_HR, check=check_shift),
    'employees': dict(
        table='employees', alias='e',
        select='''SELECT e.*, d.name_ar dept_ar, d.name_en dept_en, s.name_ar shift_ar, s.name_en shift_en,
                         l.name_ar loc_ar, l.name_en loc_en, m.name_ar mgr_ar, m.name_en mgr_en
                  FROM employees e LEFT JOIN departments d ON d.id=e.department_id
                  LEFT JOIN shifts s ON s.id=e.shift_id LEFT JOIN locations l ON l.id=e.location_id
                  LEFT JOIN employees m ON m.id=e.manager_id''',
        fields={'emp_no': 'str!', 'name_ar': 'str!', 'name_en': 'str!', 'department_id': 'ref:departments',
                'job_title': 'str', 'phone': 'str', 'email': 'str',
                'status': 'enum:active,inactive,suspended!', 'shift_id': 'ref:shifts',
                'location_id': 'ref:locations', 'extra_location_ids': 'refs:locations',
                'geofence_required': 'bool', 'exempt_rules': 'bool',
                'manager_id': 'ref:employees', 'hire_date': 'date'},
        search=['e.emp_no', 'e.name_ar', 'e.name_en', 'e.job_title', 'e.phone', 'e.email'],
        filters=['department_id', 'status', 'shift_id', 'location_id'],
        order='e.emp_no', read=STAFF, write=ADMIN_HR, scope_col='e.id', check=check_employee),
    'leaves': dict(
        table='leaves', alias='l',
        select='''SELECT l.*, e.emp_no, e.name_ar emp_ar, e.name_en emp_en, e.department_id
                  FROM leaves l JOIN employees e ON e.id=l.employee_id''',
        fields={'employee_id': 'ref:employees!', 'type': 'enum:leave,mission,exempt!', 'start_date': 'date!',
                'end_date': 'date!', 'note': 'text'},
        search=['e.emp_no', 'e.name_ar', 'e.name_en', 'l.note'], filters=['employee_id', 'type'],
        order='l.start_date DESC, l.id DESC', read=STAFF, write=STAFF, scope_col='l.employee_id',
        scope_write='employee_id', check=check_leave),
}
ENT_RX = '(?P<entity>' + '|'.join(ENTITIES) + ')'


def ent_spec(ctx, write=False):
    spec = ENTITIES[ctx.params['entity']]
    if ctx.user['role'] not in (spec['write'] if write else spec['read']):
        raise ApiError(403, 'forbidden')
    return spec


def validate(db, spec, body, partial):
    data = {}
    for f, kind in spec['fields'].items():
        if partial and f not in body:
            continue
        data[f] = coerce(kind, body.get(f), f, db)
    return data


@route('GET', '/api/' + ENT_RX, STAFF)
def api_ent_list(ctx):
    spec = ent_spec(ctx)
    a = spec['alias']
    base = spec.get('select') or f'SELECT {a}.* FROM {spec["table"]} {a}'
    where, args = ' WHERE 1=1', []
    q = (ctx.q('q') or '').strip()
    if q and spec.get('search'):
        where += ' AND (' + ' OR '.join(f'{c} LIKE ?' for c in spec['search']) + ')'
        args += [f'%{q}%'] * len(spec['search'])
    for f in spec.get('filters', []):
        v = ctx.q(f)
        if v not in (None, ''):
            where += f' AND {a}.{f}=?'
            args.append(v)
    if spec.get('scope_col'):
        s, sa = scope_sql(ctx.scope_ids(), spec['scope_col'])
        where += s
        args += sa
    total = ctx.db.execute(f'SELECT COUNT(*) FROM ({base}{where}) t_count', args).fetchone()[0]
    size = max(1, min(ctx.qint('size', 25), 2000))
    page = max(1, ctx.qint('page', 1))
    rows = ctx.db.execute(f'{base}{where} ORDER BY {spec["order"]} LIMIT ? OFFSET ?',
                          args + [size, (page - 1) * size]).fetchall()
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'size': size}


def scoped_write_check(ctx, spec, data, row=None):
    col = spec.get('scope_write')
    if not col or ctx.user['role'] in ADMIN_HR:
        return
    ids = ctx.scope_ids()
    for v in (data.get(col), row[col] if row else None):
        if v is not None and v not in ids:
            raise ApiError(403, 'forbidden')


@route('POST', '/api/' + ENT_RX, STAFF)
def api_ent_create(ctx):
    spec = ent_spec(ctx, write=True)
    data = validate(ctx.db, spec, ctx.body, partial=False)
    scoped_write_check(ctx, spec, data)
    if spec.get('check'):
        spec['check'](ctx.db, data, None)
    if spec['table'] == 'leaves':
        data['created_by'] = ctx.user['id']
        data['created_at'] = iso(now_utc())
    cols = list(data)
    cur = ctx.db.execute(f'INSERT INTO {spec["table"]}({",".join(cols)}) VALUES({",".join("?" * len(cols))})',
                         [data[c] for c in cols])
    ctx.audit('create', spec['table'], cur.lastrowid, data)
    ctx.db.commit()
    return {'id': cur.lastrowid}


@route('PUT', '/api/' + ENT_RX + r'/(?P<id>\d+)', STAFF)
def api_ent_update(ctx):
    spec = ent_spec(ctx, write=True)
    rid = int(ctx.params['id'])
    row = ctx.db.execute(f'SELECT * FROM {spec["table"]} WHERE id=?', (rid,)).fetchone()
    if not row:
        raise ApiError(404, 'not_found')
    data = validate(ctx.db, spec, ctx.body, partial=True)
    scoped_write_check(ctx, spec, data, row)
    merged = dict(row)
    merged.update(data)
    if spec.get('check'):
        spec['check'](ctx.db, merged, rid)
    diff = {k: [row[k], v] for k, v in data.items() if row[k] != v}
    if diff:
        ctx.db.execute(f'UPDATE {spec["table"]} SET {",".join(f"{c}=?" for c in diff)} WHERE id=?',
                       [data[c] for c in diff] + [rid])
        ctx.audit('update', spec['table'], rid, diff, reason=ctx.body.get('reason'))
        ctx.db.commit()
    return {'ok': True}


@route('DELETE', '/api/' + ENT_RX + r'/(?P<id>\d+)', STAFF)
def api_ent_delete(ctx):
    spec = ent_spec(ctx, write=True)
    rid = int(ctx.params['id'])
    row = ctx.db.execute(f'SELECT * FROM {spec["table"]} WHERE id=?', (rid,)).fetchone()
    if not row:
        raise ApiError(404, 'not_found')
    scoped_write_check(ctx, spec, {}, row)
    if spec['table'] == 'employees' and ctx.db.execute(
            'SELECT 1 FROM punches WHERE employee_id=? LIMIT 1', (rid,)).fetchone():
        raise ApiError(409, 'has_records')
    try:
        ctx.db.execute(f'DELETE FROM {spec["table"]} WHERE id=?', (rid,))
    except IntegrityError:
        raise ApiError(409, 'in_use')
    ctx.audit('delete', spec['table'], rid, dict(row))
    ctx.db.commit()
    return {'ok': True}


@route('GET', '/api/lookups', STAFF)
def api_lookups(ctx):
    db = ctx.db
    s, sa = scope_sql(ctx.scope_ids(), 'id')
    return {
        'departments': [dict(r) for r in db.execute('SELECT * FROM departments ORDER BY id')],
        'shifts': [dict(r) for r in db.execute('SELECT * FROM shifts ORDER BY id')],
        'locations': [dict(r) for r in db.execute('SELECT * FROM locations ORDER BY id')],
        'employees': [dict(r) for r in db.execute(
            f'SELECT id, emp_no, name_ar, name_en, department_id, shift_id, location_id, extra_location_ids, status '
            f'FROM employees WHERE 1=1{s} '
            'ORDER BY emp_no', sa)],
    }


# ---------------------------------------------------------------- users

def user_rows(db, where='', args=()):
    return db.execute(
        f'''SELECT u.id, u.username, u.role, u.employee_id, u.active, u.lang, u.created_at, u.locked_until,
                   e.emp_no, e.name_ar emp_ar, e.name_en emp_en
            FROM users u LEFT JOIN employees e ON e.id=u.employee_id {where} ORDER BY u.id''', args).fetchall()


def check_role_allowed(ctx, role):
    if ctx.user['role'] == 'hr' and role in ('admin', 'hr'):
        raise ApiError(403, 'forbidden')


@route('GET', '/api/users', ADMIN_HR)
def api_users(ctx):
    q = (ctx.q('q') or '').strip()
    where, args = 'WHERE 1=1', []
    if q:
        where += ' AND (u.username LIKE ? OR e.name_ar LIKE ? OR e.name_en LIKE ? OR e.emp_no LIKE ?)'
        args += [f'%{q}%'] * 4
    if ctx.q('role'):
        where += ' AND u.role=?'
        args.append(ctx.q('role'))
    rows = [dict(r) for r in user_rows(ctx.db, where, args)]
    size = max(1, min(ctx.qint('size', 25), 500))
    page = max(1, ctx.qint('page', 1))
    return {'items': rows[(page - 1) * size: page * size], 'total': len(rows), 'page': page, 'size': size}


@route('POST', '/api/users', ADMIN_HR)
def api_user_create(ctx):
    b = ctx.body
    username = coerce('str!', b.get('username'), 'username')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,50}', username):
        raise ApiError(400, 'invalid', field='username')
    role = coerce('enum:' + ','.join(ROLES) + '!', b.get('role'), 'role')
    check_role_allowed(ctx, role)
    emp_id = coerce('ref:employees', b.get('employee_id'), 'employee_id', ctx.db)
    if role == 'employee' and not emp_id:
        raise ApiError(400, 'required', field='employee_id')
    pw = str(b.get('password') or '')
    check_password_policy(pw)
    if ctx.db.execute('SELECT 1 FROM users WHERE lower(username)=lower(?)', (username,)).fetchone():
        raise ApiError(409, 'duplicate', field='username')
    cur = ctx.db.execute(
        'INSERT INTO users(username,pw_hash,role,employee_id,active,lang,created_at) VALUES(?,?,?,?,?,?,?)',
        (username, hash_password(pw), role, emp_id, coerce('bool', b.get('active', True), 'active'),
         coerce('enum:ar,en', b.get('lang'), 'lang') or 'ar', iso(now_utc())))
    ctx.audit('create', 'users', cur.lastrowid, {'username': username, 'role': role, 'employee_id': emp_id})
    ctx.db.commit()
    return {'id': cur.lastrowid}


@route('PUT', r'/api/users/(?P<id>\d+)', ADMIN_HR)
def api_user_update(ctx):
    uid = int(ctx.params['id'])
    row = ctx.db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not row:
        raise ApiError(404, 'not_found')
    check_role_allowed(ctx, row['role'])
    b = ctx.body
    changes = {}
    if 'role' in b:
        changes['role'] = coerce('enum:' + ','.join(ROLES) + '!', b['role'], 'role')
        check_role_allowed(ctx, changes['role'])
    if 'employee_id' in b:
        changes['employee_id'] = coerce('ref:employees', b['employee_id'], 'employee_id', ctx.db)
    if 'active' in b:
        changes['active'] = coerce('bool', b['active'], 'active')
    if uid == ctx.user['id'] and (changes.get('active') == 0 or changes.get('role', 'admin') != row['role']):
        raise ApiError(400, 'cannot_change_self')
    diff = {k: [row[k], v] for k, v in changes.items() if row[k] != v}
    if b.get('password'):
        check_password_policy(str(b['password']))
        changes['pw_hash'] = hash_password(str(b['password']))
        changes['failed'], changes['locked_until'] = 0, None
        diff['password'] = ['***', 'reset']
    if changes:
        ctx.db.execute(f'UPDATE users SET {",".join(f"{c}=?" for c in changes)} WHERE id=?',
                       list(changes.values()) + [uid])
        if 'pw_hash' in changes or changes.get('active') == 0:
            ctx.db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        ctx.audit('update', 'users', uid, diff)
        ctx.db.commit()
    return {'ok': True}


@route('DELETE', r'/api/users/(?P<id>\d+)', ADMIN_HR)
def api_user_delete(ctx):
    uid = int(ctx.params['id'])
    row = ctx.db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not row:
        raise ApiError(404, 'not_found')
    if uid == ctx.user['id']:
        raise ApiError(400, 'cannot_change_self')
    check_role_allowed(ctx, row['role'])
    ctx.db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
    ctx.db.execute('DELETE FROM users WHERE id=?', (uid,))
    ctx.audit('delete', 'users', uid, {'username': row['username'], 'role': row['role']})
    ctx.db.commit()
    return {'ok': True}


# ---------------------------------------------------------------- settings / audit / backups

@route('GET', '/api/settings', STAFF)
def api_settings(ctx):
    return {'settings': ctx.st}


@route('PUT', '/api/settings', ADMIN)
def api_settings_update(ctx):
    diff = {}
    for k, kind in SETTING_KINDS.items():
        if k in ctx.body:
            v = str(coerce(kind, ctx.body[k], k))
            if k == 'utc_offset_min' and not -720 <= int(v) <= 840:
                raise ApiError(400, 'invalid', field=k)
            if kind == 'int!' and int(v) < 0:
                raise ApiError(400, 'invalid', field=k)
            if ctx.st.get(k) != v:
                diff[k] = [ctx.st.get(k), v]
                ctx.db.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                               'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, v))
    if diff:
        ctx.audit('update', 'settings', None, diff)
        ctx.db.commit()
    return {'ok': True}


@route('GET', '/api/audit', ADMIN_HR)
def api_audit(ctx):
    where, args = ' WHERE 1=1', []
    for f in ('action', 'entity'):
        if ctx.q(f):
            where += f' AND {f}=?'
            args.append(ctx.q(f))
    q = (ctx.q('q') or '').strip()
    if q:
        where += ' AND (username LIKE ? OR details LIKE ? OR reason LIKE ?)'
        args += [f'%{q}%'] * 3
    if ctx.q('from'):
        where += ' AND at>=?'
        args.append(iso(from_local(datetime.combine(ctx.qdate('from', None), datetime.min.time()), ctx.st)))
    if ctx.q('to'):
        where += ' AND at<?'
        args.append(iso(from_local(datetime.combine(ctx.qdate('to', None) + timedelta(days=1),
                                                    datetime.min.time()), ctx.st)))
    total = ctx.db.execute('SELECT COUNT(*) FROM audit_log' + where, args).fetchone()[0]
    size = max(1, min(ctx.qint('size', 50), 500))
    page = max(1, ctx.qint('page', 1))
    rows = ctx.db.execute('SELECT * FROM audit_log' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?',
                          args + [size, (page - 1) * size]).fetchall()
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'size': size}


def make_backup(label='auto'):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f'attendance-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{label}.db'
    src = connect()
    dst = sqlite3.connect(os.path.join(BACKUP_DIR, name))
    try:
        src.backup(dst)
        keep = ival(get_settings(src), 'backup_keep') or 14
    finally:
        dst.close()
        src.close()
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith('.db'))
    for f in files[:-keep] if len(files) > keep else []:
        os.remove(os.path.join(BACKUP_DIR, f))
    log.info('backup created: %s', name)
    return name


def backup_loop():
    while True:
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            today = datetime.now().strftime('%Y%m%d')
            if not any(today in f for f in os.listdir(BACKUP_DIR)):
                make_backup('auto')
        except Exception:
            log.exception('automatic backup failed')
        time.sleep(3600)


@route('GET', '/api/backups', ADMIN)
def api_backups(ctx):
    if USE_PG:  # PostgreSQL (Neon) keeps its own restore history; there are no backup files
        return {'items': [], 'managed': True}
    os.makedirs(BACKUP_DIR, exist_ok=True)
    items = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.endswith('.db'):
            p = os.path.join(BACKUP_DIR, f)
            items.append({'name': f, 'size': os.path.getsize(p),
                          'at': iso(datetime.fromtimestamp(os.path.getmtime(p), timezone.utc))})
    return {'items': items}


@route('POST', '/api/backups', ADMIN)
def api_backup_create(ctx):
    if USE_PG:
        raise ApiError(400, 'backups_managed')
    name = make_backup('manual')
    ctx.audit('backup', 'backup', None, {'file': name})
    ctx.db.commit()
    return {'name': name}


@route('GET', r'/api/backups/(?P<name>attendance-[\w-]+\.db)', ADMIN)
def api_backup_download(ctx):
    p = os.path.join(BACKUP_DIR, ctx.params['name'])
    if not os.path.isfile(p):
        raise ApiError(404, 'not_found')
    ctx.audit('backup_download', 'backup', None, {'file': ctx.params['name']})
    ctx.db.commit()
    with open(p, 'rb') as fh:
        return Raw(fh.read(), 'application/octet-stream', ctx.params['name'])


# ---------------------------------------------------------------- punch records (admin edit)

@route('GET', '/api/punches', STAFF)
def api_punches(ctx):
    today = local_now(ctx.st).date()
    d1, d2 = ctx.qdate('from', today - timedelta(days=7)), ctx.qdate('to', today)
    where, args = ' WHERE p.work_date BETWEEN ? AND ?', [d1.isoformat(), d2.isoformat()]
    if ctx.q('employee_id'):
        where += ' AND p.employee_id=?'
        args.append(ctx.qint('employee_id', 0))
    if ctx.q('department_id'):
        where += ' AND e.department_id=?'
        args.append(ctx.qint('department_id', 0))
    if ctx.q('source'):
        where += ' AND (p.source=? OR p.is_offline=1)' if ctx.q('source') == 'offline' else ' AND p.source=?'
        args.append(ctx.q('source'))
    q = (ctx.q('q') or '').strip()
    if q:
        where += ' AND (e.emp_no LIKE ? OR e.name_ar LIKE ? OR e.name_en LIKE ?)'
        args += [f'%{q}%'] * 3
    s, sa = scope_sql(ctx.scope_ids(), 'p.employee_id')
    where += s
    args += sa
    base = ' FROM punches p JOIN employees e ON e.id=p.employee_id'
    total = ctx.db.execute('SELECT COUNT(*)' + base + where, args).fetchone()[0]
    size = max(1, min(ctx.qint('size', 25), 500))
    page = max(1, ctx.qint('page', 1))
    rows = ctx.db.execute(
        'SELECT p.*, e.emp_no, e.name_ar emp_ar, e.name_en emp_en' + base + where +
        ' ORDER BY p.punch_time DESC LIMIT ? OFFSET ?', args + [size, (page - 1) * size]).fetchall()
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'size': size}


def need_reason(ctx):
    reason = str(ctx.body.get('reason') or '').strip()
    if len(reason) < 3:
        raise ApiError(400, 'reason_required')
    return reason[:500]


def check_punch_order(db, emp_id, work_date, typ, pt_iso, exclude_id=0):
    """A check-out may not be earlier than the check-in of the same shift day (and vice versa)."""
    other = db.execute('SELECT punch_time FROM punches WHERE employee_id=? AND work_date=? AND type=? AND id<>?',
                       (emp_id, work_date, 'out' if typ == 'in' else 'in', exclude_id)).fetchone()
    if other and ((typ == 'in' and pt_iso > other['punch_time']) or (typ == 'out' and pt_iso < other['punch_time'])):
        raise ApiError(400, 'invalid', field='time')


def local_input_to_utc(ctx, v, field):
    try:
        return from_local(datetime.fromisoformat(str(v)[:16]), ctx.st)
    except Exception:
        raise ApiError(400, 'invalid', field=field)


@route('POST', '/api/punches', ADMIN_HR)
def api_punch_manual(ctx):
    b = ctx.body
    reason = need_reason(ctx)
    emp_id = coerce('ref:employees!', b.get('employee_id'), 'employee_id', ctx.db)
    typ = coerce('enum:in,out!', b.get('type'), 'type')
    wd = coerce('date!', b.get('work_date'), 'work_date')
    pt = local_input_to_utc(ctx, b.get('time'), 'time')
    check_punch_order(ctx.db, emp_id, wd, typ, iso(pt))
    now = now_utc()
    try:
        cur = ctx.db.execute(
            '''INSERT INTO punches(uuid,employee_id,type,work_date,punch_time,synced_at,source,note)
               VALUES(?,?,?,?,?,?,?,?)''', (str(uuid.uuid4()), emp_id, typ, wd, iso(pt), iso(now), 'manual', reason))
    except IntegrityError:
        raise ApiError(409, 'already_checked_in' if typ == 'in' else 'already_checked_out')
    ctx.audit('create', 'punches', cur.lastrowid, {'employee_id': emp_id, 'type': typ, 'work_date': wd,
                                                   'punch_time': iso(pt)}, reason)
    ctx.db.commit()
    return {'id': cur.lastrowid}


@route('PUT', r'/api/punches/(?P<id>\d+)', ADMIN_HR)
def api_punch_edit(ctx):
    pid = int(ctx.params['id'])
    row = ctx.db.execute('SELECT * FROM punches WHERE id=?', (pid,)).fetchone()
    if not row:
        raise ApiError(404, 'not_found')
    reason = need_reason(ctx)
    pt = iso(local_input_to_utc(ctx, ctx.body.get('time'), 'time'))
    check_punch_order(ctx.db, row['employee_id'], row['work_date'], row['type'], pt, pid)
    if pt != row['punch_time']:
        ctx.db.execute("UPDATE punches SET punch_time=?, source=CASE WHEN source='manual' THEN source ELSE 'edited' END,"
                       " note=? WHERE id=?", (pt, reason, pid))
        ctx.audit('update', 'punches', pid, {'punch_time': [row['punch_time'], pt],
                                             'employee_id': row['employee_id'], 'work_date': row['work_date']},
                  reason)
        ctx.db.commit()
    return {'ok': True}


@route('DELETE', r'/api/punches/(?P<id>\d+)', ADMIN_HR)
def api_punch_delete(ctx):
    pid = int(ctx.params['id'])
    row = ctx.db.execute('SELECT * FROM punches WHERE id=?', (pid,)).fetchone()
    if not row:
        raise ApiError(404, 'not_found')
    reason = need_reason(ctx)
    ctx.db.execute('DELETE FROM punches WHERE id=?', (pid,))
    ctx.audit('delete', 'punches', pid, dict(row), reason)
    ctx.db.commit()
    return {'ok': True}


# ---------------------------------------------------------------- reports

def gps_info(p):
    return {'lat': p['lat'], 'lng': p['lng'], 'accuracy': p['accuracy'], 'address': p['address'],
            'distance_m': p['distance_m'], 'in_fence': p['in_fence']} if p['lat'] is not None else None


def compute_day(emp, shift, d, pin, pout, leave, st, now_l, gps):
    scheduled = bool(shift) and str(js_weekday(d)) in shift['work_days'].split(',')
    if not (scheduled or pin or pout):
        return None
    row = {
        'date': d.isoformat(), 'employee_id': emp['id'], 'emp_no': emp['emp_no'],
        'name_ar': emp['name_ar'], 'name_en': emp['name_en'], 'department_id': emp['department_id'],
        'dept_ar': emp['dept_ar'], 'dept_en': emp['dept_en'], 'shift_id': emp['shift_id'],
        'shift_ar': shift['name_ar'] if shift else None, 'shift_en': shift['name_en'] if shift else None,
        'shift_start': shift['start_time'] if shift else None, 'shift_end': shift['end_time'] if shift else None,
        'scheduled': scheduled, 'status': None, 'flags': [], 'in_time': None, 'out_time': None,
        'late_min': 0, 'early_min': 0, 'work_min': 0, 'overtime_min': 0,
        'in_offline': bool(pin and pin['is_offline']), 'out_offline': bool(pout and pout['is_offline']),
        'in_source': pin['source'] if pin else None, 'out_source': pout['source'] if pout else None,
        'leave_note': leave['note'] if leave else None,
    }
    if gps:
        row['in_gps'] = gps_info(pin) if pin else None
        row['out_gps'] = gps_info(pout) if pout else None
    in_l = to_local(parse_iso(pin['punch_time']), st) if pin else None
    out_l = to_local(parse_iso(pout['punch_time']), st) if pout else None
    row['in_time'] = in_l.strftime('%Y-%m-%d %H:%M') if in_l else None
    row['out_time'] = out_l.strftime('%Y-%m-%d %H:%M') if out_l else None
    flags = row['flags']
    start, end = shift_window(shift, d) if shift else (None, None)
    if in_l and out_l:
        row['work_min'] = max(0, mins(out_l - in_l) - (shift['break_min'] if shift else 0))

    if leave:
        row['status'] = leave['type']
        return row
    if not pin:
        if not scheduled:
            return None
        row['status'] = 'not_yet' if now_l < start + timedelta(minutes=shift['grace_in_min']) else 'absent'
        return row

    if not scheduled:
        flags.append('offday')
    elif not emp['exempt_rules']:
        if ival(st, 'late_enabled') and in_l > start + timedelta(minutes=shift['grace_in_min']):
            row['late_min'] = mins(in_l - start)
            flags.append('late')
        if out_l and ival(st, 'early_enabled') and out_l < end - timedelta(minutes=shift['grace_out_min']):
            row['early_min'] = mins(end - out_l)
            flags.append('early_leave')
    if out_l and ival(st, 'overtime_enabled'):
        if scheduled:
            extra = mins(out_l - end)
            if extra >= shift['overtime_after_min']:
                row['overtime_min'] = extra
        else:
            row['overtime_min'] = row['work_min']
    if not out_l:
        limit = (end if scheduled else in_l + timedelta(hours=12)) + timedelta(
            minutes=ival(st, 'no_checkout_after_min'))
        if now_l > limit:
            flags.append('no_checkout')
    if pin['is_offline'] or (pout and pout['is_offline']):
        flags.append('offline')
    if pin['source'] != 'mobile' or (pout and pout['source'] != 'mobile'):
        flags.append('manual')

    thr = ival(st, 'absent_if_late_over_min')
    if thr and row['late_min'] > thr:
        row['status'] = 'absent'
        flags.append('late_absent')
    elif 'no_checkout' in flags:
        row['status'] = 'no_checkout'
    elif not out_l:
        row['status'] = 'working'
    elif 'late' in flags:
        row['status'] = 'late'
    elif 'early_leave' in flags:
        row['status'] = 'early_leave'
    elif 'offday' in flags:
        row['status'] = 'offday'
    else:
        row['status'] = 'present'
    return row


def report_rows(db, st, d1, d2, scope, department_id=None, shift_id=None, employee_id=None, q=None, gps=False):
    if (d2 - d1).days > 92:
        raise ApiError(400, 'range_too_long', days=92)
    now_l = local_now(st)
    d2 = min(d2, now_l.date())
    if d2 < d1:
        return []
    sql = '''SELECT e.*, d.name_ar dept_ar, d.name_en dept_en FROM employees e
             LEFT JOIN departments d ON d.id=e.department_id WHERE e.status='active' '''
    args = []
    for col, v in (('e.department_id', department_id), ('e.shift_id', shift_id), ('e.id', employee_id)):
        if v:
            sql += f' AND {col}=?'
            args.append(int(v))
    if q:
        sql += ' AND (e.emp_no LIKE ? OR e.name_ar LIKE ? OR e.name_en LIKE ?)'
        args += [f'%{q}%'] * 3
    s, sa = scope_sql(scope, 'e.id')
    emps = [dict(r) for r in db.execute(sql + s + ' ORDER BY e.emp_no', args + sa)]
    if not emps:
        return []
    ids = {e['id'] for e in emps}
    shifts = {r['id']: dict(r) for r in db.execute('SELECT * FROM shifts')}
    punches = {}
    for p in db.execute('SELECT * FROM punches WHERE work_date BETWEEN ? AND ?', (d1.isoformat(), d2.isoformat())):
        if p['employee_id'] in ids:
            punches.setdefault((p['employee_id'], p['work_date']), {})[p['type']] = p
    leaves = {}
    for lv in db.execute('SELECT * FROM leaves WHERE end_date>=? AND start_date<=? ORDER BY id',
                         (d1.isoformat(), d2.isoformat())):
        if lv['employee_id'] in ids:
            leaves.setdefault(lv['employee_id'], []).append(lv)
    rows = []
    for d in daterange(d1, d2):
        ds = d.isoformat()
        for e in emps:
            pp = punches.get((e['id'], ds), {})
            lv = next((x for x in leaves.get(e['id'], []) if x['start_date'] <= ds <= x['end_date']), None)
            r = compute_day(e, shifts.get(e['shift_id']), d, pp.get('in'), pp.get('out'), lv, st, now_l, gps)
            if r:
                rows.append(r)
    return rows


COUNTERS = ('scheduled_days', 'present_days', 'absent_days', 'late_days', 'late_min', 'early_days', 'early_min',
            'leave_days', 'mission_days', 'exempt_days', 'no_checkout_days', 'offday_days', 'work_min',
            'overtime_min')


def add_row(acc, r):
    if r['scheduled']:
        acc['scheduled_days'] += 1
    st = r['status']
    if st in ('leave', 'mission', 'exempt'):
        acc[st + '_days'] += 1
    elif st == 'absent':
        acc['absent_days'] += 1
    if r['in_time'] and st not in ('leave', 'mission', 'exempt', 'absent'):
        acc['present_days'] += 1
    for flag, key in (('late', 'late_days'), ('early_leave', 'early_days'), ('no_checkout', 'no_checkout_days'),
                      ('offday', 'offday_days')):
        if flag in r['flags']:
            acc[key] += 1
    for k in ('late_min', 'early_min', 'work_min', 'overtime_min'):
        acc[k] += r[k]


def finish(acc):
    expected = acc['scheduled_days'] - acc['leave_days'] - acc['mission_days'] - acc['exempt_days']
    acc['attendance_rate'] = round(100 * min(acc['present_days'], expected) / expected, 1) if expected > 0 else None
    return acc


def aggregate(rows):
    per_emp, per_dept, per_shift = {}, {}, {}
    total = dict.fromkeys(COUNTERS, 0)
    for r in rows:
        if r['employee_id'] not in per_emp:
            per_emp[r['employee_id']] = dict(dict.fromkeys(COUNTERS, 0), employee_id=r['employee_id'],
                                             emp_no=r['emp_no'], name_ar=r['name_ar'], name_en=r['name_en'],
                                             dept_ar=r['dept_ar'], dept_en=r['dept_en'], shift_ar=r['shift_ar'],
                                             shift_en=r['shift_en'])
        dk = r['department_id'] or 0
        if dk not in per_dept:
            per_dept[dk] = dict(dict.fromkeys(COUNTERS, 0), name_ar=r['dept_ar'] or '—', name_en=r['dept_en'] or '—',
                                employees=set())
        sk = r['shift_id'] or 0
        if sk not in per_shift:
            per_shift[sk] = dict(dict.fromkeys(COUNTERS, 0), name_ar=r['shift_ar'] or '—',
                                 name_en=r['shift_en'] or '—', employees=set())
        for acc in (per_emp[r['employee_id']], per_dept[dk], per_shift[sk], total):
            add_row(acc, r)
        per_dept[dk]['employees'].add(r['employee_id'])
        per_shift[sk]['employees'].add(r['employee_id'])
    for g in list(per_dept.values()) + list(per_shift.values()):
        g['employees'] = len(g['employees'])
    total['employees'] = len(per_emp)
    return ([finish(x) for x in per_emp.values()], [finish(x) for x in per_dept.values()],
            [finish(x) for x in per_shift.values()], finish(total))


def can_see_gps(ctx):
    return ctx.user['role'] in STAFF


def daily_data(ctx):
    d = ctx.qdate('date', local_now(ctx.st).date())
    rows = report_rows(ctx.db, ctx.st, d, d, ctx.scope_ids(), ctx.q('department_id'), ctx.q('shift_id'),
                       ctx.q('employee_id'), (ctx.q('q') or '').strip() or None, gps=can_see_gps(ctx))
    status = ctx.q('status')
    if status:
        rows = [r for r in rows if r['status'] == status or status in r['flags']]
    summary = {}
    for r in rows:
        summary[r['status']] = summary.get(r['status'], 0) + 1
        for f in r['flags']:
            summary['flag_' + f] = summary.get('flag_' + f, 0) + 1
    summary['total'] = len(rows)
    return d, rows, summary


@route('GET', '/api/reports/daily', STAFF)
def api_report_daily(ctx):
    d, rows, summary = daily_data(ctx)
    return {'date': d.isoformat(), 'rows': rows, 'summary': summary}


def monthly_data(ctx):
    today = local_now(ctx.st).date()
    d1 = ctx.qdate('from', today.replace(day=1))
    d2 = ctx.qdate('to', today)
    if d2 < d1:
        raise ApiError(400, 'invalid', field='to')
    rows = report_rows(ctx.db, ctx.st, d1, d2, ctx.scope_ids(), ctx.q('department_id'), ctx.q('shift_id'),
                       ctx.q('employee_id'), (ctx.q('q') or '').strip() or None)
    emps, depts, shifts, total = aggregate(rows)
    return d1, d2, emps, depts, shifts, total


@route('GET', '/api/reports/monthly', STAFF)
def api_report_monthly(ctx):
    d1, d2, emps, depts, shifts, total = monthly_data(ctx)
    return {'from': d1.isoformat(), 'to': d2.isoformat(), 'employees': emps, 'departments': depts,
            'shifts': shifts, 'total': total}


@route('GET', '/api/dashboard', STAFF)
def api_dashboard(ctx):
    st, db = ctx.st, ctx.db
    today = local_now(st).date()
    scope = ctx.scope_ids()
    rows = report_rows(db, st, today - timedelta(days=6), today, scope)
    trows = [r for r in rows if r['date'] == today.isoformat()]
    yrows = [r for r in rows if r['date'] == (today - timedelta(days=1)).isoformat()]
    counts = {}
    for r in trows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    counts['late_flag'] = sum(1 for r in trows if 'late' in r['flags'])
    counts['scheduled'] = sum(1 for r in trows if r['scheduled'])
    counts['present'] = sum(1 for r in trows if r['in_time'] and r['status'] not in ('leave', 'mission', 'exempt'))

    def brief(r):
        return {k: r[k] for k in ('employee_id', 'emp_no', 'name_ar', 'name_en', 'dept_ar', 'dept_en', 'shift_ar',
                                  'shift_en', 'date', 'in_time', 'late_min', 'shift_start', 'shift_end')}
    s, sa = scope_sql(scope, 'a.entity_id')
    rejected = [dict(r) for r in db.execute(
        "SELECT a.at, a.details, a.entity_id, e.emp_no, e.name_ar, e.name_en FROM audit_log a "
        "LEFT JOIN employees e ON e.id=a.entity_id WHERE action='punch_rejected' AND at>=?" + s + " ORDER BY a.id DESC LIMIT 20",
        [iso(now_utc() - timedelta(hours=24))] + sa)]
    for r in rejected:
        r['details'] = json.loads(r['details'] or '{}')
    by_dept = {}
    for r in trows:
        k = r['department_id'] or 0
        g = by_dept.setdefault(k, {'name_ar': r['dept_ar'] or '—', 'name_en': r['dept_en'] or '—',
                                   'scheduled': 0, 'present': 0, 'absent': 0, 'late': 0})
        g['scheduled'] += 1 if r['scheduled'] else 0
        g['present'] += 1 if r['in_time'] and r['status'] not in ('leave', 'mission', 'exempt') else 0
        g['absent'] += 1 if r['status'] == 'absent' else 0
        g['late'] += 1 if 'late' in r['flags'] else 0
    trend = []
    for d in daterange(today - timedelta(days=6), today):
        dr = [r for r in rows if r['date'] == d.isoformat()]
        acc = dict.fromkeys(COUNTERS, 0)
        for r in dr:
            add_row(acc, r)
        finish(acc)
        trend.append({'date': d.isoformat(), 'rate': acc['attendance_rate'], 'present': acc['present_days'],
                      'absent': acc['absent_days'], 'late': acc['late_days']})
    return {
        'date': today.isoformat(), 'counts': counts,
        'alerts': {
            'absent': [brief(r) for r in trows if r['status'] == 'absent'],
            'late': [brief(r) for r in trows if 'late' in r['flags']],
            'no_checkout': [brief(r) for r in trows + yrows if 'no_checkout' in r['flags']],
            'rejected': rejected,
        },
        'by_department': list(by_dept.values()), 'trend': trend,
    }


# ---------------------------------------------------------------- Excel export (minimal XLSX writer)

LABELS = {
    'ar': {
        'daily_title': 'التقرير اليومي', 'monthly_title': 'التقرير الشهري', 'date': 'التاريخ',
        'emp_no': 'الرقم الوظيفي', 'name': 'الاسم', 'dept': 'القسم', 'shift': 'الوردية',
        'shift_time': 'وقت الوردية', 'in': 'الحضور', 'out': 'الانصراف', 'status': 'الحالة',
        'late_min': 'التأخير (د)', 'early_min': 'الانصراف المبكر (د)', 'work': 'ساعات العمل',
        'ot': 'الإضافي (س)', 'in_gps': 'موقع الحضور', 'out_gps': 'موقع الانصراف', 'notes': 'ملاحظات',
        'scheduled_days': 'أيام مجدولة', 'present_days': 'أيام الحضور', 'absent_days': 'أيام الغياب',
        'late_days': 'أيام التأخير', 'late_total': 'إجمالي التأخير (د)', 'early_days': 'انصراف مبكر',
        'leave_days': 'إجازة', 'mission_days': 'مهمة رسمية', 'exempt_days': 'مستثنى',
        'no_checkout_days': 'بدون انصراف', 'work_hours': 'ساعات العمل', 'ot_hours': 'ساعات إضافية',
        'rate': 'نسبة الحضور %', 'employees': 'الموظفون', 'by_dept': 'حسب القسم', 'by_shift': 'حسب الوردية',
        'total': 'الإجمالي', 'absentees': 'المتغيبون', 'period': 'الفترة', 'sheet_emp': 'الموظفون',
        'flags': {'late': 'متأخر', 'early_leave': 'انصراف مبكر', 'no_checkout': 'لم يسجل الانصراف',
                  'offline': 'تسجيل دون اتصال', 'manual': 'معدّل يدويًا', 'offday': 'يوم راحة',
                  'late_absent': 'غياب بسبب التأخير'},
        'status_map': {'present': 'حاضر', 'late': 'متأخر', 'early_leave': 'انصرف مبكرًا', 'absent': 'غائب',
                       'leave': 'إجازة', 'mission': 'مهمة رسمية', 'exempt': 'مستثنى',
                       'no_checkout': 'لم يسجل الانصراف', 'working': 'على رأس العمل', 'not_yet': 'لم يحضر بعد',
                       'offday': 'عمل في يوم راحة'},
    },
    'en': {
        'daily_title': 'Daily Report', 'monthly_title': 'Monthly Report', 'date': 'Date',
        'emp_no': 'Employee No.', 'name': 'Name', 'dept': 'Department', 'shift': 'Shift',
        'shift_time': 'Shift time', 'in': 'Check-in', 'out': 'Check-out', 'status': 'Status',
        'late_min': 'Late (min)', 'early_min': 'Early leave (min)', 'work': 'Work hours',
        'ot': 'Overtime (h)', 'in_gps': 'Check-in location', 'out_gps': 'Check-out location', 'notes': 'Notes',
        'scheduled_days': 'Scheduled days', 'present_days': 'Present days', 'absent_days': 'Absent days',
        'late_days': 'Late days', 'late_total': 'Total late (min)', 'early_days': 'Early leave',
        'leave_days': 'Leave', 'mission_days': 'Official mission', 'exempt_days': 'Exempt',
        'no_checkout_days': 'No check-out', 'work_hours': 'Work hours', 'ot_hours': 'Overtime hours',
        'rate': 'Attendance %', 'employees': 'Employees', 'by_dept': 'By department', 'by_shift': 'By shift',
        'total': 'Total', 'absentees': 'Absentees', 'period': 'Period', 'sheet_emp': 'Employees',
        'flags': {'late': 'Late', 'early_leave': 'Early leave', 'no_checkout': 'No check-out',
                  'offline': 'Offline punch', 'manual': 'Manually edited', 'offday': 'Day off',
                  'late_absent': 'Absent (late)'},
        'status_map': {'present': 'Present', 'late': 'Late', 'early_leave': 'Left early', 'absent': 'Absent',
                       'leave': 'Leave', 'mission': 'Official mission', 'exempt': 'Exempt',
                       'no_checkout': 'No check-out', 'working': 'At work', 'not_yet': 'Not arrived yet',
                       'offday': 'Worked on day off'},
    },
}

_BAD_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def col_letter(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def make_xlsx(sheets, rtl):
    """sheets: list of dict(name, rows: list[list], styles: {row_index: style_id}, widths: list)."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + ''.join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets))) + '</Types>')
        z.writestr('_rels/.rels',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        names = []
        for s in sheets:
            n = re.sub(r'[\[\]:*?/\\]', ' ', s['name'])[:31] or 'Sheet'
            while n in names:
                n = n[:28] + str(len(names))
            names.append(n)
        z.writestr('xl/workbook.xml',
                   f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{ns}" xmlns:r="{rel}">'
                   '<sheets>' + ''.join(f'<sheet name="{xml_escape(n, {chr(34): "&quot;"})}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                                         for i, n in enumerate(names)) + '</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + ''.join(f'<Relationship Id="rId{i + 1}" Type="{rel}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                             for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml',
                   f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{ns}">'
                   '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="14"/><name val="Calibri"/></font></fonts>'
                   '<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                   '<fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill>'
                   '<fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/><bgColor indexed="64"/></patternFill></fill></fills>'
                   '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
                   '<border><left style="thin"><color rgb="FFCBD5E1"/></left><right style="thin"><color rgb="FFCBD5E1"/></right>'
                   '<top style="thin"><color rgb="FFCBD5E1"/></top><bottom style="thin"><color rgb="FFCBD5E1"/></bottom><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
                   '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
                   '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
                   '<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
                   '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for i, s in enumerate(sheets):
            out = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{ns}">'
                   f'<sheetViews><sheetView workbookViewId="0"{" rightToLeft=" + chr(34) + "1" + chr(34) if rtl else ""}/></sheetViews>']
            if s.get('widths'):
                out.append('<cols>' + ''.join(f'<col min="{j + 1}" max="{j + 1}" width="{w}" customWidth="1"/>'
                                              for j, w in enumerate(s['widths'])) + '</cols>')
            out.append('<sheetData>')
            for ri, row in enumerate(s['rows']):
                style = s.get('styles', {}).get(ri, 3 if row else 0)
                cells = []
                for ci, v in enumerate(row):
                    ref = f'{col_letter(ci)}{ri + 1}'
                    if v is None or v == '':
                        cells.append(f'<c r="{ref}" s="{style}"/>')
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        cells.append(f'<c r="{ref}" s="{style}"><v>{v}</v></c>')
                    else:
                        txt = xml_escape(_BAD_XML.sub('', str(v)))
                        cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{txt}</t></is></c>')
                out.append(f'<row r="{ri + 1}">' + ''.join(cells) + '</row>')
            out.append('</sheetData></worksheet>')
            z.writestr(f'xl/worksheets/sheet{i + 1}.xml', ''.join(out))
    return buf.getvalue()


def hours(m):
    return round(m / 60, 2)


def gps_text(g):
    return f'{g["lat"]:.6f}, {g["lng"]:.6f} (±{round(g["accuracy"] or 0)}m)' if g else ''


@route('GET', r'/api/reports/daily\.xlsx', STAFF)
def api_report_daily_xlsx(ctx):
    lang = 'en' if ctx.q('lang') == 'en' else 'ar'
    L = LABELS[lang]
    n = 'name_' + lang
    d, rows, summary = daily_data(ctx)
    company = ctx.st['company_' + lang]
    head = [L['emp_no'], L['name'], L['dept'], L['shift'], L['shift_time'], L['in'], L['out'], L['status'],
            L['late_min'], L['early_min'], L['work'], L['ot'], L['in_gps'], L['out_gps'], L['notes']]
    data = [[company], [f'{L["daily_title"]} — {d.isoformat()}'], [], head]
    rows.sort(key=lambda r: (r['shift_id'] or 0, r['emp_no']))
    for r in rows:
        data.append([r['emp_no'], r[n], r['dept_' + lang], r['shift_' + lang],
                     f'{r["shift_start"]} - {r["shift_end"]}' if r['shift_start'] else '',
                     (r['in_time'] or '')[11:], (r['out_time'] or '')[11:], L['status_map'].get(r['status'], r['status']),
                     r['late_min'] or '', r['early_min'] or '', hours(r['work_min']) if r['work_min'] else '',
                     hours(r['overtime_min']) if r['overtime_min'] else '',
                     gps_text(r.get('in_gps')), gps_text(r.get('out_gps')),
                     ' / '.join(L['flags'].get(f, f) for f in r['flags']) + (f' {r["leave_note"]}' if r['leave_note'] else '')])
    absent = [r for r in rows if r['status'] == 'absent']
    sheets = [{'name': L['daily_title'], 'rows': data, 'styles': {0: 2, 1: 2, 2: 0, 3: 1},
               'widths': [13, 26, 18, 18, 14, 10, 10, 16, 11, 14, 11, 11, 30, 30, 30]}]
    ab = [[f'{L["absentees"]} — {d.isoformat()}'], [], [L['name'], L['emp_no'], L['dept'], L['shift'], L['date']]]
    ab += [[r[n], r['emp_no'], r['dept_' + lang], r['shift_' + lang], r['date']] for r in absent]
    sheets.append({'name': L['absentees'], 'rows': ab, 'styles': {0: 2, 1: 0, 2: 1}, 'widths': [26, 13, 18, 18, 13]})
    ctx.audit('export', 'report_daily', None, {'date': d.isoformat(), 'format': 'xlsx'})
    ctx.db.commit()
    return Raw(make_xlsx(sheets, lang == 'ar'),
               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', f'daily-{d.isoformat()}.xlsx')


@route('GET', r'/api/reports/monthly\.xlsx', STAFF)
def api_report_monthly_xlsx(ctx):
    lang = 'en' if ctx.q('lang') == 'en' else 'ar'
    L = LABELS[lang]
    n = 'name_' + lang
    d1, d2, emps, depts, shifts, total = monthly_data(ctx)
    company = ctx.st['company_' + lang]
    cols = [L['scheduled_days'], L['present_days'], L['absent_days'], L['late_days'], L['late_total'],
            L['early_days'], L['leave_days'], L['mission_days'], L['exempt_days'], L['no_checkout_days'],
            L['work_hours'], L['ot_hours'], L['rate']]

    def nums(a):
        return [a['scheduled_days'], a['present_days'], a['absent_days'], a['late_days'], a['late_min'],
                a['early_days'], a['leave_days'], a['mission_days'], a['exempt_days'], a['no_checkout_days'],
                hours(a['work_min']), hours(a['overtime_min']),
                a['attendance_rate'] if a['attendance_rate'] is not None else '']
    title = [[company], [f'{L["monthly_title"]} — {L["period"]}: {d1.isoformat()} / {d2.isoformat()}'], []]
    s1 = title + [[L['emp_no'], L['name'], L['dept'], L['shift']] + cols]
    s1 += [[e['emp_no'], e[n], e['dept_' + lang], e['shift_' + lang]] + nums(e) for e in emps]
    s1.append([L['total'], '', '', ''] + nums(total))
    s2 = title + [[L['dept'], L['employees']] + cols]
    s2 += [[g[n], g['employees']] + nums(g) for g in depts]
    s3 = title + [[L['shift'], L['employees']] + cols]
    s3 += [[g[n], g['employees']] + nums(g) for g in shifts]
    styles = {0: 2, 1: 2, 2: 0, 3: 1}
    w = [13, 26, 18, 18] + [12] * len(cols)
    sheets = [{'name': L['sheet_emp'], 'rows': s1, 'styles': {**styles, len(s1) - 1: 4}, 'widths': w},
              {'name': L['by_dept'], 'rows': s2, 'styles': styles, 'widths': [22, 11] + [12] * len(cols)},
              {'name': L['by_shift'], 'rows': s3, 'styles': styles, 'widths': [22, 11] + [12] * len(cols)}]
    ctx.audit('export', 'report_monthly', None, {'from': d1.isoformat(), 'to': d2.isoformat(), 'format': 'xlsx'})
    ctx.db.commit()
    return Raw(make_xlsx(sheets, lang == 'ar'),
               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
               f'monthly-{d1.isoformat()}_{d2.isoformat()}.xlsx')


# ---------------------------------------------------------------- HTTP handler

MIME = {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
        '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml', '.png': 'image/png',
        '.ico': 'image/x-icon', '.txt': 'text/plain; charset=utf-8'}

CSP = ("default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline' https://unpkg.com; "
       "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org https://unpkg.com; "
       "connect-src 'self' https://nominatim.openstreetmap.org; font-src 'self'; frame-ancestors 'none'; "
       "base-uri 'self'; form-action 'self'")


class Handler(BaseHTTPRequestHandler):
    server_version = 'Attendance/1.0'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        log.debug('%s %s', self.client_address[0], fmt % args)

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Permissions-Policy', 'geolocation=(self), camera=(), microphone=()')
        self.send_header('Content-Security-Policy', CSP)
        if USE_TLS:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        super().end_headers()

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def send_body(self, status, body, ctype, extra=()):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def setup(self):
        """With TLS on, the same port also accepts plain http:// and redirects it to https://."""
        self.plain_http = False
        if SSL_CTX is not None:
            self.request.settimeout(30)
            try:
                first = self.request.recv(1, socket.MSG_PEEK)
                if first and first[0] == 0x16:  # TLS handshake record
                    self.request = SSL_CTX.wrap_socket(self.request, server_side=True)
                else:
                    self.plain_http = True
            except (OSError, ssl.SSLError) as e:  # client rejected the certificate, timed out, etc.
                log.debug('TLS setup failed from %s: %s', self.client_address[0], e)
                self.request.close()
                raise
        super().setup()

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, ssl.SSLError, socket.timeout):
            pass

    def dispatch(self, method):
        if self.plain_http:
            host = (self.headers.get('Host') or '').split(':')[0] or lan_ip()
            self.send_response(301)
            self.send_header('Location', f'https://{host}:{self.server.server_address[1]}{self.path}')
            self.send_header('Content-Length', '0')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            return
        url = urlparse(self.path)
        if url.path.startswith('/api/'):
            self.handle_api(method, url.path, parse_qs(url.query))
        elif method == 'GET':
            self.serve_static(url.path)
        else:
            self.send_body(405, b'', 'text/plain')

    def handle_api(self, method, path, query):
        ctx = None
        extra = [('Cache-Control', 'no-store')]
        try:
            ctx = Ctx(self, method, path, query)
            for m, rx, fn, roles in ROUTES:
                mt = rx.match(path)
                if not mt or m != method:
                    continue
                ctx.params = mt.groupdict()
                if method != 'GET' and self.headers.get('X-Requested-With') != 'att':
                    raise ApiError(403, 'csrf')
                if roles is not None:
                    ctx.require_auth()
                    if roles != 'any' and ctx.user['role'] not in roles:
                        raise ApiError(403, 'forbidden')
                result = fn(ctx)
                extra += [('Set-Cookie', c) for c in ctx.cookies_out]
                if isinstance(result, Raw):
                    if result.filename:
                        extra.append(('Content-Disposition',
                                      f"attachment; filename=\"{result.filename}\"; filename*=UTF-8''{quote(result.filename)}"))
                    self.send_body(200, result.body, result.ctype, extra)
                else:
                    self.send_body(200, json.dumps(result, ensure_ascii=False, default=str).encode('utf-8'),
                                   'application/json; charset=utf-8', extra)
                return
            raise ApiError(404, 'not_found')
        except ApiError as e:
            if ctx:
                extra += [('Set-Cookie', c) for c in ctx.cookies_out]
            self.send_body(e.status, json.dumps({'error': e.code, 'params': e.params}, ensure_ascii=False).encode(),
                           'application/json; charset=utf-8', extra)
        except IntegrityError as e:
            log.warning('integrity error on %s %s: %s', method, path, e)
            self.send_body(409, b'{"error":"conflict","params":{}}', 'application/json', extra)
        except Exception:
            log.error('server error on %s %s\n%s', method, path, traceback.format_exc())
            self.send_body(500, b'{"error":"server_error","params":{}}', 'application/json', extra)
        finally:
            if ctx:
                ctx.db.close()

    def serve_static(self, path):
        if path == '/' or '.' not in os.path.basename(path):
            path = '/index.html'
        full = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip('/').replace('/', os.sep)))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            self.send_body(404, b'Not found', 'text/plain')
            return
        stt = os.stat(full)
        etag = f'"{int(stt.st_mtime)}-{stt.st_size}"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        with open(full, 'rb') as fh:
            body = fh.read()
        ext = os.path.splitext(full)[1].lower()
        extra = [('ETag', etag), ('Cache-Control', 'no-cache')]
        if path == '/sw.js':
            extra.append(('Service-Worker-Allowed', '/'))
        self.send_body(200, body, MIME.get(ext, 'application/octet-stream'), extra)


# ---------------------------------------------------------------- database init & demo data

def init_db(demo=True):
    """Create/upgrade the schema and, on an empty database, the first accounts."""
    if USE_PG:
        db = connect()
        db.execute('SELECT pg_advisory_xact_lock(7240531)')  # serverless instances may start at the same time
        for stmt in pg_schema(SCHEMA).split(';'):
            if stmt.strip():
                db.execute(stmt)
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username_ci ON users(lower(username))')
    else:
        os.makedirs(DATA_DIR, exist_ok=True)
        db = connect()
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        for table, col, ddl in (('employees', 'extra_location_ids', "TEXT NOT NULL DEFAULT ''"),
                                ('punches', 'location_id', 'INTEGER')):
            if col not in {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')
    for k, v in DEFAULT_SETTINGS.items():
        db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING', (k, v))
    if not db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
        if demo:
            print('  Creating demo data ... / جارٍ إنشاء البيانات التجريبية ...')
            seed_demo(db)
        else:
            create_initial_admin(db)
    db.commit()
    db.close()


def reset_db():
    if USE_PG:
        db = connect()
        db.execute(f'DROP TABLE IF EXISTS {", ".join(PG_TABLES)} CASCADE')
        db.commit()
        db.close()
        return
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(DB_PATH + suffix):
            os.remove(DB_PATH + suffix)


def seed_demo(db):
    rnd = random.Random(42)
    st = get_settings(db)
    now_l = local_now(st)
    today = now_l.date()
    db.executemany('INSERT INTO departments(id,name_ar,name_en) VALUES(?,?,?)', [
        (1, 'تقنية المعلومات', 'Information Technology'), (2, 'الموارد البشرية', 'Human Resources'),
        (3, 'العمليات', 'Operations'), (4, 'المبيعات', 'Sales')])
    db.executemany('INSERT INTO locations(id,name_ar,name_en,lat,lng,radius_m) VALUES(?,?,?,?,?,?)', [
        (1, 'المقر الرئيسي - الرياض', 'Head Office - Riyadh', 24.7136, 46.6753, 200),
        (2, 'المستودع - جنوب الرياض', 'Warehouse - South Riyadh', 24.6408, 46.7728, 300)])
    db.executemany('''INSERT INTO shifts(id,name_ar,name_en,start_time,end_time,work_days,grace_in_min,grace_out_min,
                      break_min,overtime_after_min) VALUES(?,?,?,?,?,?,?,?,?,?)''', [
        (1, 'الوردية الصباحية', 'Morning Shift', '08:00', '16:00', '0,1,2,3,4', 15, 10, 60, 30),
        (2, 'الوردية المسائية', 'Evening Shift', '14:00', '22:00', '0,1,2,3,4,6', 15, 10, 45, 30),
        (3, 'الوردية الليلية', 'Night Shift', '22:00', '06:00', '0,1,2,3,4', 10, 10, 30, 30)])
    emps = [
        # id, no, ar, en, dept, title, shift, loc, manager
        (1, '1001', 'أحمد العتيبي', 'Ahmed Al-Otaibi', 1, 'مدير تقنية المعلومات / IT Manager', 1, 1, None),
        (2, '1002', 'سارة القحطاني', 'Sara Al-Qahtani', 1, 'مطورة برمجيات / Software Developer', 1, 1, 1),
        (3, '1003', 'محمد الشهري', 'Mohammed Al-Shehri', 1, 'مهندس شبكات / Network Engineer', 1, 1, 1),
        (4, '1004', 'نورة الدوسري', 'Noura Al-Dosari', 2, 'أخصائية موارد بشرية / HR Specialist', 1, 1, None),
        (5, '1005', 'خالد الحربي', 'Khalid Al-Harbi', 3, 'مشرف عمليات / Operations Supervisor', 2, 2, None),
        (6, '1006', 'فاطمة الزهراني', 'Fatimah Al-Zahrani', 4, 'مندوبة مبيعات / Sales Representative', 1, 1, None),
        (7, '1007', 'عبدالله المطيري', 'Abdullah Al-Mutairi', 3, 'فني صيانة / Maintenance Technician', 3, 2, 5),
        (8, '1008', 'ريم السبيعي', 'Reem Al-Subaie', 4, 'مديرة حسابات / Account Manager', 1, 1, None),
        (9, '1009', 'يوسف الغامدي', 'Yousef Al-Ghamdi', 3, 'سائق / Driver', 2, 2, 5),
        (10, '1010', 'هند العنزي', 'Hind Al-Enezi', 1, 'دعم فني / IT Support', 2, 1, 1),
        (11, '1011', 'فيصل القرني', 'Faisal Al-Qarni', 3, 'حارس أمن / Security Guard', 3, 2, 5),
        (12, '1012', 'لمى الشمري', 'Lama Al-Shammari', 2, 'أخصائية توظيف / Recruiter', 1, 1, None),
    ]
    for (i, no, ar, en, dept, title, sh, loc, mgr) in emps:
        db.execute('''INSERT INTO employees(id,emp_no,name_ar,name_en,department_id,job_title,phone,email,status,
                      shift_id,location_id,geofence_required,exempt_rules,manager_id,hire_date)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (i, no, ar, en, dept, title, f'05{rnd.randint(10000000, 99999999)}',
                    en.lower().replace(' ', '.').replace('-', '') + '@example.com', 'active', sh, loc,
                    1, 0, mgr, (today - timedelta(days=rnd.randint(200, 2000))).isoformat()))
    db.execute("UPDATE employees SET geofence_required=0 WHERE id=9")  # driver: no geofence
    db.execute("UPDATE employees SET exempt_rules=1 WHERE id=1")       # manager exempt from late rules
    created = iso(now_utc())
    users = [('admin', 'Admin@123', 'admin', None), ('hr', 'Hr@12345', 'hr', 4), ('manager', 'Manager@123', 'manager', 1)]
    users += [(no, 'Emp@1234', 'employee', i) for (i, no, *_rest) in emps if i not in (1, 4)]
    for u, p, r, e in users:
        db.execute('INSERT INTO users(username,pw_hash,role,employee_id,active,lang,created_at) VALUES(?,?,?,?,1,?,?)',
                   (u, hash_password(p), r, e, 'ar', created))
    leaves = [(6, 'leave', today - timedelta(days=2), today + timedelta(days=3), 'إجازة سنوية / Annual leave'),
              (3, 'mission', today - timedelta(days=10), today - timedelta(days=8), 'مهمة رسمية - جدة / Mission - Jeddah'),
              (12, 'exempt', today - timedelta(days=5), today - timedelta(days=5), 'دورة تدريبية / Training course'),
              (8, 'leave', today - timedelta(days=15), today - timedelta(days=14), 'إجازة مرضية / Sick leave')]
    for e, t, s, en_, note in leaves:
        db.execute('INSERT INTO leaves(employee_id,type,start_date,end_date,note,created_by,created_at) VALUES(?,?,?,?,?,1,?)',
                   (e, t, s.isoformat(), en_.isoformat(), note, created))
    shifts = {r['id']: dict(r) for r in db.execute('SELECT * FROM shifts')}
    locs = {r['id']: dict(r) for r in db.execute('SELECT * FROM locations')}
    leave_days = {(e, d.isoformat()) for e, _, s, en_, _ in leaves for d in daterange(s, en_)}

    def point(loc):
        rr = loc['radius_m'] * 0.6 * math.sqrt(rnd.random())
        a = rnd.random() * 2 * math.pi
        lat = loc['lat'] + rr * math.cos(a) / 111320
        lng = loc['lng'] + rr * math.sin(a) / (111320 * math.cos(math.radians(loc['lat'])))
        return lat, lng, round(rnd.uniform(5, 40), 1), round(haversine_m(lat, lng, loc['lat'], loc['lng']), 1)

    def add(emp_id, typ, wd, t_local, loc):
        lat, lng, acc, dist = point(loc)
        pt = from_local(t_local, st)
        offline = rnd.random() < 0.05
        synced = pt + timedelta(minutes=rnd.randint(5, 90)) if offline else pt
        if synced > now_utc():
            synced = now_utc()
        db.execute('''INSERT INTO punches(uuid,employee_id,type,work_date,punch_time,client_time,synced_at,is_offline,
                      lat,lng,accuracy,distance_m,in_fence,source,device) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (str(uuid.uuid4()), emp_id, typ, wd, iso(pt), iso(pt), iso(synced), int(offline),
                    lat, lng, acc, dist, 1, 'mobile', 'demo-seed'))

    for d in daterange(today - timedelta(days=35), today):
        for (i, no, *_r, sh, loc, mgr) in emps:
            shift = shifts[sh]
            if str(js_weekday(d)) not in shift['work_days'].split(',') or (i, d.isoformat()) in leave_days:
                continue
            r = rnd.random()
            if r < 0.06:
                continue  # absent
            start, end = shift_window(shift, d)
            if r < 0.22:
                t_in = start + timedelta(minutes=shift['grace_in_min'] + rnd.randint(1, 50))
            else:
                t_in = start + timedelta(minutes=rnd.randint(-15, shift['grace_in_min'] - 2))
            r2 = rnd.random()
            if r2 < 0.08:
                t_out = end - timedelta(minutes=shift['grace_out_min'] + rnd.randint(5, 70))
            elif r2 < 0.12 and d < today - timedelta(days=1):
                t_out = None  # forgot to check out
            elif r2 < 0.35:
                t_out = end + timedelta(minutes=rnd.randint(30, 150))
            else:
                t_out = end + timedelta(minutes=rnd.randint(-5, 20))
            t_in += timedelta(seconds=rnd.randint(0, 59))
            if t_in > now_l:
                continue
            add(i, 'in', d.isoformat(), t_in, locs[loc])
            if t_out and t_out + timedelta(seconds=30) < now_l:
                add(i, 'out', d.isoformat(), t_out + timedelta(seconds=rnd.randint(0, 59)), locs[loc])
    audit(db, None, 'demo_seed', details={'employees': len(emps)})
    if USE_PG:
        pg_sync_ids(db)
    db.commit()


def create_initial_admin(db):
    pw = os.environ.get('ATT_ADMIN_PASSWORD') or ('Adm' + secrets.token_hex(4) + '1')
    db.execute("INSERT INTO users(username,pw_hash,role,active,lang,created_at) VALUES('admin',?,'admin',1,'ar',?)",
               (hash_password(pw), iso(now_utc())))
    db.commit()
    print(f'  Initial admin account -> username: admin  password: {pw}')


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if isinstance(err, (ssl.SSLError, ConnectionError, socket.timeout, OSError)):
            log.debug('connection from %s dropped: %s', client_address[0], err)
        else:
            log.error('unhandled error from %s\n%s', client_address[0], traceback.format_exc())


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def startup(debug=False, demo=True):
    """Logging, database schema and first accounts. Shared by main() and the WSGI app."""
    global DUMMY_HASH
    handlers = [logging.StreamHandler(sys.stdout)] if sys.stdout else []  # no console under pythonw
    if not ON_VERCEL:  # Vercel's file system is read-only; its log viewer shows stdout
        os.makedirs(DATA_DIR, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(DATA_DIR, 'server.log'), encoding='utf-8'))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)
    if ON_VERCEL and not USE_PG:
        raise RuntimeError('DATABASE_URL is not set: connect a Neon database to the Vercel project')
    DUMMY_HASH = hash_password(secrets.token_hex(8))
    init_db(demo)


# ---------------------------------------------------------------- WSGI entry point (Vercel)

class WsgiHandler(Handler):
    """Runs Handler for one WSGI request instead of a socket connection; the response is collected."""
    def __init__(self, environ):
        self.client_address = (environ.get('REMOTE_ADDR') or '0.0.0.0', 0)
        self.command = environ['REQUEST_METHOD']
        qs = environ.get('QUERY_STRING')
        self.path = (environ.get('PATH_INFO') or '/') + ('?' + qs if qs else '')
        self.headers = http.client.HTTPMessage()
        for k, v in environ.items():
            if k.startswith('HTTP_'):
                self.headers[k[5:].replace('_', '-')] = v
        for k in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
            if environ.get(k):
                self.headers[k.replace('_', '-')] = environ[k]
        self.rfile = environ['wsgi.input']
        self.wfile = io.BytesIO()
        self.request_version = 'HTTP/1.1'
        self.plain_http = False
        self.status, self.out_headers, self._headers_buffer = 500, [], []

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        self.out_headers.append((keyword, str(value)))

    def flush_headers(self):
        self._headers_buffer = []


APP_READY = False
APP_LOCK = threading.Lock()


def startup_error_text(e):
    if not USE_PG:
        why = ('DATABASE_URL is not set. Connect the Neon database to this Vercel project '
               '(Storage tab), then redeploy.\n'
               'لم يتم ضبط DATABASE_URL. اربط قاعدة Neon بهذا المشروع من تبويب Storage ثم أعد النشر.')
    else:
        why = ('Could not connect to the database (DATABASE_URL is set). Check that the Neon database is active '
               'and the connection string is correct; details are in the Vercel function logs.\n'
               'تعذّر الاتصال بقاعدة البيانات. تحقق من قاعدة Neon ومن رابط الاتصال؛ التفاصيل في سجلات Vercel.\n\n'
               f'{type(e).__name__}')
    return 'Attendance system: startup failed / فشل تشغيل النظام\n\n' + why


def app(environ, start_response):
    """WSGI application: Vercel runs this behind its HTTPS edge."""
    global APP_READY, BEHIND_PROXY, USE_TLS, PROXY_ANY_PEER
    if not APP_READY:
        with APP_LOCK:
            if not APP_READY:
                BEHIND_PROXY = USE_TLS = PROXY_ANY_PEER = True  # client IP from X-Real-IP, secure cookies
                try:
                    startup(demo=os.environ.get('ATT_DEMO') == '1')
                    APP_READY = True
                except Exception as e:  # show why instead of a bare 500; the next request tries again
                    log.error('startup failed\n%s', traceback.format_exc())
                    print(f'startup failed: {e!r}', flush=True)
                    start_response('503 Service Unavailable', [('Content-Type', 'text/plain; charset=utf-8')])
                    return [startup_error_text(e).encode('utf-8')]
    h = WsgiHandler(environ)
    if h.command in ('GET', 'POST', 'PUT', 'DELETE'):
        h.dispatch(h.command)
    else:
        h.send_body(405, b'', 'text/plain')
    start_response(f'{h.status} {HTTPStatus(h.status).phrase}', h.out_headers)
    return [h.wfile.getvalue()]


def main():
    global USE_TLS, SSL_CTX, BEHIND_PROXY, PROXY_ANY_PEER
    ap = argparse.ArgumentParser(description='Attendance system server')
    ap.add_argument('--host', default=os.environ.get('ATT_HOST'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('ATT_PORT') or os.environ.get('PORT') or '8000'))
    ap.add_argument('--reset-demo', action='store_true', help='delete the database and recreate demo data')
    ap.add_argument('--no-demo', action='store_true', help='on first start create only an admin account')
    ap.add_argument('--behind-proxy', action='store_true', default=os.environ.get('ATT_BEHIND_PROXY') == '1',
                    help='run behind Cloudflare Tunnel or another HTTPS reverse proxy '
                         '(listens on 127.0.0.1 unless --host is given, e.g. 0.0.0.0 on Railway)')
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()

    if args.reset_demo:
        reset_db()
    startup(args.debug, demo=not args.no_demo)

    if args.behind_proxy:
        BEHIND_PROXY = USE_TLS = True  # the proxy serves HTTPS: secure cookies + HSTS
        args.host = args.host or '127.0.0.1'
        PROXY_ANY_PEER = args.host not in ('127.0.0.1', '::1', 'localhost')
    else:
        args.host = args.host or '0.0.0.0'
    httpd = Server((args.host, args.port), Handler)
    httpd.daemon_threads = True
    scheme = 'http'
    if args.behind_proxy:
        print(f'\n  Behind proxy: listening on http://{args.host}:{args.port} for the tunnel / reverse proxy.')
    elif os.path.isfile(CERT_FILE) and os.path.isfile(KEY_FILE):
        sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sslctx.minimum_version = ssl.TLSVersion.TLSv1_2
        sslctx.load_cert_chain(CERT_FILE, KEY_FILE)
        SSL_CTX = sslctx  # sockets are wrapped per connection in Handler.setup (allows http -> https redirect)
        USE_TLS = True
        scheme = 'https'
    if not USE_PG:
        threading.Thread(target=backup_loop, daemon=True).start()
    print(f'\n  Attendance system running / النظام يعمل:\n    {scheme}://localhost:{args.port}\n' +
          ('' if args.behind_proxy else f'    {scheme}://{lan_ip()}:{args.port}   (phones on the same network)\n'))
    if not USE_TLS and not args.behind_proxy:
        print('  NOTE: phones need HTTPS for GPS. Run gen_cert.bat (see README) to enable HTTPS.\n')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n  Stopped.')


if __name__ == '__main__':
    main()
