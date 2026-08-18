import os
import json
import hmac
import re
import secrets
import struct
import zlib
from flask import Flask, render_template, request, jsonify, redirect, Response
from dotenv import load_dotenv
import requests
from urllib.parse import urlparse

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import pg8000.native
else:
    import sqlite3

app = Flask(__name__)
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
DB_FILE = os.environ.get("DB_FILE", os.path.join(os.path.dirname(__file__), "tracking.db"))

WIX_API_BASE = "https://www.wixapis.com/email-marketing/v1"

STEP_EMAILS = {
    0: "\u30b5\u30f3\u30af\u30b9\u30e1\u30fc\u30eb",
    1: "Day1",
    2: "Day2",
    3: "Day3",
    4: "Day4",
    5: "Day5",
    6: "Day6",
    7: "Day7",
    8: "Day8",
    9: "Day9",
    10: "Day10",
}
VALID_DAYS = set(STEP_EMAILS)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SAFE_REDIRECT_SCHEMES = {"http", "https"}
ADMIN_TOKEN_HEADER = "X-Admin-Token"

def is_valid_day(day):
    return day in VALID_DAYS

def allowed_click_hosts():
    return {
        host.strip().lower()
        for host in os.getenv("ALLOWED_CLICK_HOSTS", "").split(",")
        if host.strip()
    }

def clean_redirect_url(raw_url):
    url = (raw_url or "/").strip()
    if not url or "\r" in url or "\n" in url:
        return "/"

    parsed = urlparse(url)
    if parsed.scheme:
        if parsed.scheme.lower() not in SAFE_REDIRECT_SCHEMES or not parsed.netloc:
            return "/"
        allowed_hosts = allowed_click_hosts()
        if allowed_hosts and (parsed.hostname or "").lower() not in allowed_hosts:
            return "/"
        return url

    if parsed.netloc:
        return "/"

    return url if url.startswith("/") else "/"

def admin_token():
    return os.getenv("ADMIN_TOKEN", "").strip()

def request_admin_token():
    data = request.get_json(silent=True) or {}
    return (
        request.headers.get(ADMIN_TOKEN_HEADER)
        or request.form.get("admin_token")
        or data.get("admin_token")
        or ""
    ).strip()

def require_admin():
    expected = admin_token()
    provided = request_admin_token()
    if not expected:
        return jsonify({"error": "ADMIN_TOKEN is not configured"}), 403
    if not hmac.compare_digest(provided, expected):
        return jsonify({"error": "invalid admin token"}), 403
    return None

def new_tracking_token():
    return secrets.token_urlsafe(24)

def ensure_tracking_token(conn):
    token = new_tracking_token()
    while True:
        if USE_POSTGRES:
            exists = conn.run("SELECT COUNT(*) FROM subscribers WHERE tracking_token = :t", t=token)[0][0]
        else:
            exists = conn.execute("SELECT COUNT(*) FROM subscribers WHERE tracking_token = ?", (token,)).fetchone()[0]
        if exists == 0:
            return token
        token = new_tracking_token()

def get_subscriber_by_token(conn, token):
    token = (token or "").strip()
    if not token:
        return None
    if USE_POSTGRES:
        rows = conn.run("SELECT id, email FROM subscribers WHERE tracking_token = :t", t=token)
        return {"id": rows[0][0], "email": rows[0][1]} if rows else None
    row = conn.execute("SELECT id, email FROM subscribers WHERE tracking_token = ?", (token,)).fetchone()
    return {"id": row["id"], "email": row["email"]} if row else None

def column_exists(conn, table, column):
    if USE_POSTGRES:
        rows = conn.run(
            """SELECT COUNT(*) FROM information_schema.columns
               WHERE table_name = :table AND column_name = :column""",
            table=table,
            column=column,
        )
        return rows[0][0] > 0
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)

def ensure_column(conn, table, column, definition):
    if not column_exists(conn, table, column):
        if USE_POSTGRES:
            conn.run(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def migrate_db(conn):
    ensure_column(conn, "subscribers", "tracking_token", "TEXT")
    ensure_column(conn, "email_opens", "subscriber_id", "INTEGER")
    ensure_column(conn, "link_clicks", "subscriber_id", "INTEGER")

    if USE_POSTGRES:
        conn.run("CREATE UNIQUE INDEX IF NOT EXISTS subscribers_tracking_token_idx ON subscribers (tracking_token)")
        rows = conn.run("SELECT id FROM subscribers WHERE tracking_token IS NULL OR tracking_token = ''")
        for row in rows:
            conn.run("UPDATE subscribers SET tracking_token = :t WHERE id = :i", t=ensure_tracking_token(conn), i=row[0])
        conn.run("COMMIT")
    else:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS subscribers_tracking_token_idx ON subscribers (tracking_token)")
        rows = conn.execute("SELECT id FROM subscribers WHERE tracking_token IS NULL OR tracking_token = ''").fetchall()
        for row in rows:
            conn.execute("UPDATE subscribers SET tracking_token = ? WHERE id = ?", (ensure_tracking_token(conn), row["id"]))
        conn.commit()

def get_db():
    if USE_POSTGRES:
        import urllib.parse as urlparse
        url = urlparse.urlparse(DATABASE_URL)
        conn = pg8000.native.Connection(
            host=url.hostname, port=url.port or 5432,
            database=url.path[1:], user=url.username,
            password=url.password, ssl_context=True,
        )
        return conn
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    conn = get_db()
    if USE_POSTGRES:
        conn.run("""CREATE TABLE IF NOT EXISTS email_opens (
            id SERIAL PRIMARY KEY, day INTEGER NOT NULL, ip TEXT, user_agent TEXT,
            subscriber_id INTEGER,
            opened_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.run("""CREATE TABLE IF NOT EXISTS link_clicks (
            id SERIAL PRIMARY KEY, day INTEGER NOT NULL, url TEXT, ip TEXT,
            subscriber_id INTEGER,
            clicked_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.run("""CREATE TABLE IF NOT EXISTS subscribers (
            id SERIAL PRIMARY KEY, email TEXT, tracking_token TEXT,
            registered_at TIMESTAMPTZ DEFAULT NOW())""")
    else:
        conn.execute("""CREATE TABLE IF NOT EXISTS email_opens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day INTEGER NOT NULL,
            ip TEXT, user_agent TEXT, subscriber_id INTEGER,
            opened_at TEXT DEFAULT (datetime('now', '+9 hours')))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS link_clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day INTEGER NOT NULL,
            url TEXT, ip TEXT, subscriber_id INTEGER,
            clicked_at TEXT DEFAULT (datetime('now', '+9 hours')))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, tracking_token TEXT,
            registered_at TEXT DEFAULT (datetime('now', '+9 hours')))""")
    migrate_db(conn)
    conn.close()

def make_pixel():
    def png_chunk(name, data):
        chunk = name + data
        return struct.pack('>I', len(data)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff)
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = png_chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
    idat = png_chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00\x00'))
    iend = png_chunk(b'IEND', b'')
    return sig + ihdr + idat + iend

PIXEL_PNG = make_pixel()
init_db()

@app.route('/api/subscriber/register', methods=['POST'])
def register_subscriber():
    try:
        data = request.get_json() or {}
        email = (data.get('email', '') or '').strip().lower()
        if not email or not EMAIL_RE.match(email):
            return jsonify({"error": "valid email is required"}), 400

        conn = get_db()
        # 同じメールアドレスが既に登録済みなら二重カウントしない
        if USE_POSTGRES:
            existing = conn.run("SELECT COUNT(*) FROM subscribers WHERE email = :e", e=email)[0][0]
        else:
            existing = conn.execute("SELECT COUNT(*) FROM subscribers WHERE email = ?", (email,)).fetchone()[0]
        if existing > 0:
            if USE_POSTGRES:
                token = conn.run("SELECT tracking_token FROM subscribers WHERE email = :e", e=email)[0][0]
            else:
                token = conn.execute("SELECT tracking_token FROM subscribers WHERE email = ?", (email,)).fetchone()[0]
            conn.close()
            return jsonify({"success": True, "duplicate": True, "tracking_token": token})
        token = ensure_tracking_token(conn)
        if USE_POSTGRES:
            conn.run("INSERT INTO subscribers (email, tracking_token) VALUES (:e, :t)", e=email, t=token)
            conn.run("COMMIT")
        else:
            conn.execute("INSERT INTO subscribers (email, tracking_token) VALUES (?, ?)", (email, token))
            conn.commit()
        conn.close()
        return jsonify({"success": True, "tracking_token": token})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def get_sent_count_for_day(conn, day):
    try:
        if USE_POSTGRES:
            result = conn.run(
                "SELECT COUNT(*) FROM subscribers WHERE registered_at <= NOW() - INTERVAL '1 day' * :d",
                d=day)
            return result[0][0]
        else:
            result = conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE registered_at <= datetime('now', '+9 hours', :days)",
                (f'-{day} days',)).fetchone()
            return result[0] if result else 0
    except:
        return 0

@app.route('/debug')
def debug():
    try:
        conn = get_db()
        if USE_POSTGRES:
            opens = conn.run("SELECT COUNT(*) FROM email_opens")[0][0]
            subs = conn.run("SELECT COUNT(*) FROM subscribers")[0][0]
        else:
            opens = conn.execute("SELECT COUNT(*) FROM email_opens").fetchone()[0]
            subs = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
        conn.close()
        return jsonify({"use_postgres": USE_POSTGRES, "email_opens_count": opens, "subscribers_count": subs})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/track/open/<int:day>')
def track_open(day):
    if not is_valid_day(day):
        return Response(PIXEL_PNG, mimetype='image/png', headers={'Cache-Control': 'no-cache, no-store, must-revalidate'})

    try:
        conn = get_db()
        ip = request.remote_addr
        ua = request.user_agent.string[:200]
        subscriber = get_subscriber_by_token(conn, request.args.get("sid"))
        subscriber_id = subscriber["id"] if subscriber else None
        # sidがある場合は購読者単位、ない古いURLはIP単位でユニーク開封を数える
        if USE_POSTGRES:
            if subscriber_id:
                already = conn.run("SELECT COUNT(*) FROM email_opens WHERE day=:d AND subscriber_id=:s", d=day, s=subscriber_id)
            else:
                already = conn.run("SELECT COUNT(*) FROM email_opens WHERE day=:d AND subscriber_id IS NULL AND ip=:i", d=day, i=ip)
            if already[0][0] == 0:
                conn.run(
                    "INSERT INTO email_opens (day, ip, user_agent, subscriber_id) VALUES (:d, :i, :u, :s)",
                    d=day, i=ip, u=ua, s=subscriber_id)
                conn.run("COMMIT")
        else:
            if subscriber_id:
                already = conn.execute("SELECT COUNT(*) FROM email_opens WHERE day=? AND subscriber_id=?", (day, subscriber_id)).fetchone()[0]
            else:
                already = conn.execute("SELECT COUNT(*) FROM email_opens WHERE day=? AND subscriber_id IS NULL AND ip=?", (day, ip)).fetchone()[0]
            if already == 0:
                conn.execute("INSERT INTO email_opens (day, ip, user_agent, subscriber_id) VALUES (?, ?, ?, ?)", (day, ip, ua, subscriber_id))
                conn.commit()
        conn.close()
    except Exception as e:
        print(f"track_open error: {e}", flush=True)
    return Response(PIXEL_PNG, mimetype='image/png', headers={'Cache-Control': 'no-cache, no-store, must-revalidate'})

@app.route('/track/click/<int:day>')
def track_click(day):
    url = clean_redirect_url(request.args.get('url', '/'))
    if not is_valid_day(day):
        return redirect(url)

    try:
        conn = get_db()
        subscriber = get_subscriber_by_token(conn, request.args.get("sid"))
        subscriber_id = subscriber["id"] if subscriber else None
        if USE_POSTGRES:
            conn.run("INSERT INTO link_clicks (day, url, ip, subscriber_id) VALUES (:d, :u, :i, :s)", d=day, u=url[:500], i=request.remote_addr, s=subscriber_id)
            conn.run("COMMIT")
        else:
            conn.execute("INSERT INTO link_clicks (day, url, ip, subscriber_id) VALUES (?, ?, ?, ?)", (day, url[:500], request.remote_addr, subscriber_id))
            conn.commit()
        conn.close()
    except:
        pass
    return redirect(url)

@app.route('/api/tracking')
def api_tracking():
    try:
        conn = get_db()
        if USE_POSTGRES:
            opens_rows = [{"day": r[0], "opens": r[1]} for r in conn.run("SELECT day, COUNT(*) FROM email_opens GROUP BY day ORDER BY day")]
            clicks_rows = [{"day": r[0], "clicks": r[1]} for r in conn.run("SELECT day, COUNT(*) FROM link_clicks GROUP BY day ORDER BY day")]
            total_subscribers = conn.run("SELECT COUNT(*) FROM subscribers")[0][0]
        else:
            opens_rows = [{"day": r[0], "opens": r[1]} for r in conn.execute("SELECT day, COUNT(*) FROM email_opens GROUP BY day ORDER BY day").fetchall()]
            clicks_rows = [{"day": r[0], "clicks": r[1]} for r in conn.execute("SELECT day, COUNT(*) FROM link_clicks GROUP BY day ORDER BY day").fetchall()]
            total_subscribers = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]

        opens_map = {r["day"]: r["opens"] for r in opens_rows}
        clicks_map = {r["day"]: r["clicks"] for r in clicks_rows}

        result = []
        for day, label in sorted(STEP_EMAILS.items()):
            sent = get_sent_count_for_day(conn, day)
            o = opens_map.get(day, 0)
            c = clicks_map.get(day, 0)
            open_rate = round(o / sent * 100, 1) if sent > 0 else 0
            click_rate = round(c / sent * 100, 1) if sent > 0 else 0
            result.append({"day": day, "label": label, "sent": sent, "opens": o, "clicks": c, "open_rate": open_rate, "click_rate": click_rate})
        conn.close()
        return jsonify({"tracking": result, "total_subscribers": total_subscribers})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/tracking/reset_opens', methods=['POST'])
def reset_opens():
    auth_error = require_admin()
    if auth_error:
        return auth_error

    try:
        conn = get_db()
        if USE_POSTGRES:
            conn.run("DELETE FROM email_opens")
            conn.run("DELETE FROM link_clicks")
            conn.run("COMMIT")
        else:
            conn.execute("DELETE FROM email_opens")
            conn.execute("DELETE FROM link_clicks")
            conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tracking/reset', methods=['POST'])
def reset_tracking():
    auth_error = require_admin()
    if auth_error:
        return auth_error

    try:
        conn = get_db()
        if USE_POSTGRES:
            conn.run("DELETE FROM email_opens")
            conn.run("DELETE FROM link_clicks")
            conn.run("DELETE FROM subscribers")
            conn.run("COMMIT")
        else:
            conn.execute("DELETE FROM email_opens")
            conn.execute("DELETE FROM link_clicks")
            conn.execute("DELETE FROM subscribers")
            conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/pixel-urls')
def pixel_urls():
    base = request.host_url.rstrip('/').replace('http://', 'https://')
    urls = {day: f"{base}/track/open/{day}" for day in STEP_EMAILS}
    token_urls = {day: f"{base}/track/open/{day}?sid=購読者トークン" for day in STEP_EMAILS}
    return render_template('pixel_urls.html', urls=urls, token_urls=token_urls, step_emails=STEP_EMAILS, base=base)

def load_config():
    config = {"api_key": os.getenv("WIX_API_KEY", ""), "site_id": os.getenv("WIX_SITE_ID", "")}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved_config = json.load(f)
        config.update({
            "api_key": saved_config.get("api_key", config["api_key"]),
            "site_id": saved_config.get("site_id", config["site_id"]),
        })
    return config

def save_config(api_key, site_id):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"api_key": api_key, "site_id": site_id}, f)

def wix_headers(api_key, site_id):
    return {"Authorization": api_key, "wix-site-id": site_id, "Content-Type": "application/json"}

@app.route("/")
def index():
    config = load_config()
    demo = not config["api_key"] or not config["site_id"]
    return render_template("index.html", config=config, demo=demo)

@app.route("/api/campaigns/demo")
def api_campaigns_demo():
    return jsonify({"campaigns": []})

@app.route("/settings", methods=["GET", "POST"])
def settings():
    config = load_config()
    error = None
    success = None
    if request.method == "POST":
        api_key = request.form.get("api_key", "").strip()
        site_id = request.form.get("site_id", "").strip()
        try:
            resp = requests.get(f"{WIX_API_BASE}/campaigns", headers=wix_headers(api_key, site_id), params={"paging.limit": 1}, timeout=10)
            if resp.status_code == 200:
                save_config(api_key, site_id)
                success = "\u63a5\u7d9a\u6210\u529f\uff01"
                config = {"api_key": api_key, "site_id": site_id}
            else:
                error = f"\u30a8\u30e9\u30fc: HTTP {resp.status_code}"
        except Exception as e:
            error = f"\u30a8\u30e9\u30fc: {str(e)}"
    return render_template("settings.html", config=config, error=error, success=success)

if __name__ == "__main__":
    init_db()
    app.run(debug=False, port=5050)
