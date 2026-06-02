import os
import json
import struct
import zlib
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response
from dotenv import load_dotenv
import requests

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import pg8000.native
else:
    import sqlite3

load_dotenv()

app = Flask(__name__)
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
DB_FILE = os.environ.get("DB_FILE", os.path.join(os.path.dirname(__file__), "tracking.db"))


WIX_API_BASE = "https://www.wixapis.com/email-marketing/v1"

STEP_EMAILS = {
    0: "サンクスメール - 判定表ダウンロードありがとうございます",
    1: "Day1 - 【まだ開いていないなら】30秒だけ見てください",
    2: "Day2 - そのまま入力すると、ただの自己評価で終わります",
    3: "Day3 - 80点×4でも、2回に1回は断られます",
    4: "Day4 - 私が30年、60％未満で絶対にペンを出さない理由",
    5: "Day5 - 41％のギャンブルを終わらせる方法",
    6: "Day6 - 「自分にもできますか？」への答え",
    7: "Day7 - 【本日限定！】その1件、契約できたかもしれません",
    8: "Day8 - 動画を見ただけでは、商談は変わりません",
    9: "Day9 - わかっているのに、現場でとっさに動けない",
    10: "Day10 - 今月3名限定：あなたの商談を直接見ます",
}


# ──────────────────────────────────────────
# データベース
# ──────────────────────────────────────────
def get_db():
    if USE_POSTGRES:
        import urllib.parse as urlparse
        url = urlparse.urlparse(DATABASE_URL)
        conn = pg8000.native.Connection(
            host=url.hostname,
            port=url.port or 5432,
            database=url.path[1:],
            user=url.username,
            password=url.password,
            ssl_context=True,
        )
        return conn
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn


def db_execute(conn, sql, params=()):
    """DB種別を吸収して実行"""
    if USE_POSTGRES:
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        sql = sql.replace("datetime('now', '+9 hours')", "NOW() AT TIME ZONE 'Asia/Tokyo'")
        sql = sql.replace("?", "%s")
        sql = sql.replace("INSERT OR REPLACE", "INSERT")
        result = conn.run(sql, *params) if params else conn.run(sql)
        return type('Result', (), {'fetchall': lambda s: result, 'fetchone': lambda s: result[0] if result else None})()
    else:
        return conn.execute(sql, params)


def init_db():
    conn = get_db()
    if USE_POSTGRES:
        conn.run("""CREATE TABLE IF NOT EXISTS email_opens (
            id SERIAL PRIMARY KEY, day INTEGER NOT NULL, ip TEXT, user_agent TEXT,
            opened_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.run("""CREATE TABLE IF NOT EXISTS link_clicks (
            id SERIAL PRIMARY KEY, day INTEGER NOT NULL, url TEXT, ip TEXT,
            clicked_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.run("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT)""")
        
    else:
        conn.execute("""CREATE TABLE IF NOT EXISTS email_opens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day INTEGER NOT NULL,
            ip TEXT, user_agent TEXT, opened_at TEXT DEFAULT (datetime('now', '+9 hours')))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS link_clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day INTEGER NOT NULL,
            url TEXT, ip TEXT, clicked_at TEXT DEFAULT (datetime('now', '+9 hours')))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
        
    conn.close()


# ──────────────────────────────────────────
# 1x1透明PNG（トラッキングピクセル）
# ──────────────────────────────────────────
def make_pixel():
    # 1x1 透明PNG をバイナリで生成
    def png_chunk(name, data):
        chunk = name + data
        return struct.pack('>I', len(data)) + chunk + struct.pack('>I', zlib.crc32(chunk) & 0xffffffff)

    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = png_chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
    idat_data = zlib.compress(b'\x00\x00\x00\x00\x00')
    idat = png_chunk(b'IDAT', idat_data)
    iend = png_chunk(b'IEND', b'')
    return sig + ihdr + idat + iend


PIXEL_PNG = make_pixel()

# 起動時にDBを初期化
init_db()


# ──────────────────────────────────────────
# トラッキングエンドポイント
# ──────────────────────────────────────────
@app.route('/debug')
def debug():
    try:
        conn = get_db()
        if USE_POSTGRES:
            count = conn.run("SELECT COUNT(*) FROM email_opens")[0][0]
        else:
            count = conn.execute("SELECT COUNT(*) FROM email_opens").fetchone()[0]
        conn.close()
        return jsonify({"use_postgres": USE_POSTGRES, "email_opens_count": count, "db_url_set": bool(DATABASE_URL)})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route('/track/open/<int:day>')
def track_open(day):
    """メール開封トラッキングピクセル"""
    try:
        conn = get_db()
        if USE_POSTGRES:
            conn.run("INSERT INTO email_opens (day, ip, user_agent) VALUES (:d, :i, :u)",
                     d=day, i=request.remote_addr, u=request.user_agent.string[:200])
            conn.run("COMMIT")
        else:
            conn.execute("INSERT INTO email_opens (day, ip, user_agent) VALUES (?, ?, ?)",
                         (day, request.remote_addr, request.user_agent.string[:200]))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"track_open error: {e}", flush=True)
    return Response(PIXEL_PNG, mimetype='image/png',
                    headers={'Cache-Control': 'no-cache, no-store, must-revalidate'})


@app.route('/track/click/<int:day>')
def track_click(day):
    """リンククリックトラッキング → リダイレクト"""
    url = request.args.get('url', '/')
    try:
        conn = get_db()
        if USE_POSTGRES:
            conn.run("INSERT INTO link_clicks (day, url, ip) VALUES (:d, :u, :i)",
                     d=day, u=url[:500], i=request.remote_addr)
            conn.run("COMMIT")
        else:
            conn.execute("INSERT INTO link_clicks (day, url, ip) VALUES (?, ?, ?)",
                         (day, url[:500], request.remote_addr))
            
        conn.close()
    except Exception:
        pass
    return redirect(url)


# ──────────────────────────────────────────
# トラッキング統計API
# ──────────────────────────────────────────
@app.route('/api/tracking')
def api_tracking():
    try:
        conn = get_db()
        if USE_POSTGRES:
            rows = [{"day": r[0], "opens": r[1]} for r in conn.run("SELECT day, COUNT(*) as opens FROM email_opens GROUP BY day ORDER BY day")]
            clicks_raw = [{"day": r[0], "clicks": r[1]} for r in conn.run("SELECT day, COUNT(*) as clicks FROM link_clicks GROUP BY day ORDER BY day")]
            sent_raw = conn.run("SELECT value FROM settings WHERE key='total_sent'")
            sent_row = {"value": sent_raw[0][0]} if sent_raw else None
        else:
            rows = conn.execute("SELECT day, COUNT(*) as opens FROM email_opens GROUP BY day ORDER BY day").fetchall()
            clicks_raw = conn.execute("SELECT day, COUNT(*) as clicks FROM link_clicks GROUP BY day ORDER BY day").fetchall()
            sent_row = conn.execute("SELECT value FROM settings WHERE key='total_sent'").fetchone()
        conn.close()
        clicks = clicks_raw

        total_sent = int(sent_row['value']) if sent_row else 0
        opens_map = {r['day']: r['opens'] for r in rows}
        clicks_map = {r['day']: r['clicks'] for r in clicks}

        result = []
        for day, label in sorted(STEP_EMAILS.items()):
            o = opens_map.get(day, 0)
            c = clicks_map.get(day, 0)
            open_rate = round(o / total_sent * 100, 1) if total_sent > 0 else 0
            click_rate = round(c / total_sent * 100, 1) if total_sent > 0 else 0
            result.append({
                "day": day,
                "label": label,
                "sent": total_sent,
                "opens": o,
                "clicks": c,
                "open_rate": open_rate,
                "click_rate": click_rate,
            })
        return jsonify({"tracking": result, "total_sent": total_sent})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/tracking/set_sent', methods=['POST'])
def set_sent():
    """登録者数（送信数）を設定"""
    try:
        data = request.get_json()
        total_sent = int(data.get('total_sent', 0))
        conn = get_db()
        if USE_POSTGRES:
            conn.run("INSERT INTO settings (key, value) VALUES (:k, :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                     k='total_sent', v=str(total_sent))
            conn.run("COMMIT")
        else:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ('total_sent', str(total_sent)))
            
        conn.close()
        return jsonify({"success": True, "total_sent": total_sent})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/tracking/recent')
def api_tracking_recent():
    """最近の開封ログ（直近30件）"""
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT day, opened_at FROM email_opens
            ORDER BY id DESC LIMIT 30
        """).fetchall()
        conn.close()
        return jsonify({"recent": [{"day": r["day"], "opened_at": r["opened_at"]} for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────
# トラッキングピクセルURLヘルパー
# ──────────────────────────────────────────
@app.route('/pixel-urls')
def pixel_urls():
    """各メールに貼るピクセルURLを表示"""
    base = request.host_url.rstrip('/').replace('http://', 'https://')
    urls = {day: f"{base}/track/open/{day}" for day in STEP_EMAILS}
    return render_template('pixel_urls.html', urls=urls, step_emails=STEP_EMAILS, base=base)


# ──────────────────────────────────────────
# Wix Email Marketing（既存機能）
# ──────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        "api_key": os.getenv("WIX_API_KEY", ""),
        "site_id": os.getenv("WIX_SITE_ID", ""),
    }


def save_config(api_key, site_id):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"api_key": api_key, "site_id": site_id}, f)


def wix_headers(api_key, site_id):
    return {
        "Authorization": api_key,
        "wix-site-id": site_id,
        "Content-Type": "application/json",
    }


def fetch_campaigns(api_key, site_id):
    headers = wix_headers(api_key, site_id)
    resp = requests.get(
        f"{WIX_API_BASE}/campaigns",
        headers=headers,
        params={"paging.limit": 100},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("campaigns", [])


def fetch_campaign_stats(api_key, site_id, campaign_id):
    headers = wix_headers(api_key, site_id)
    resp = requests.get(
        f"{WIX_API_BASE}/campaigns/{campaign_id}/statistics",
        headers=headers,
        timeout=10,
    )
    if resp.status_code != 200:
        return {}
    return resp.json().get("statistics", {})


@app.route("/")
def index():
    config = load_config()
    demo = not config["api_key"] or not config["site_id"]
    return render_template("index.html", config=config, demo=demo)


@app.route("/api/campaigns/demo")
def api_campaigns_demo():
    DEMO = [
        {"id":"1","subject":"【まだ開いていないなら】30秒だけ見てください","sent_date":"2026-05-20","sent":150,"opened":68,"clicked":22,"open_rate":45.3,"click_rate":14.7,"status":"SENT"},
        {"id":"2","subject":"そのまま入力すると、ただの自己評価で終わります","sent_date":"2026-05-21","sent":150,"opened":52,"clicked":18,"open_rate":34.7,"click_rate":12.0,"status":"SENT"},
        {"id":"3","subject":"80点×4でも、2回に1回は断られます","sent_date":"2026-05-22","sent":150,"opened":48,"clicked":15,"open_rate":32.0,"click_rate":10.0,"status":"SENT"},
        {"id":"4","subject":"私が30年、60％未満で絶対にペンを出さない理由","sent_date":"2026-05-23","sent":150,"opened":41,"clicked":12,"open_rate":27.3,"click_rate":8.0,"status":"SENT"},
        {"id":"5","subject":"41％のギャンブルを終わらせる方法","sent_date":"2026-05-24","sent":150,"opened":39,"clicked":28,"open_rate":26.0,"click_rate":18.7,"status":"SENT"},
        {"id":"6","subject":"「自分にもできますか？」への答え","sent_date":"2026-05-25","sent":150,"opened":35,"clicked":24,"open_rate":23.3,"click_rate":16.0,"status":"SENT"},
        {"id":"7","subject":"【本日限定！】その1件、契約できたかもしれません","sent_date":"2026-05-26","sent":150,"opened":62,"clicked":31,"open_rate":41.3,"click_rate":20.7,"status":"SENT"},
        {"id":"8","subject":"動画を見ただけでは、商談は変わりません","sent_date":"2026-05-27","sent":150,"opened":28,"clicked":9,"open_rate":18.7,"click_rate":6.0,"status":"SENT"},
        {"id":"9","subject":"わかっているのに、現場でとっさに動けない","sent_date":"2026-05-28","sent":150,"opened":31,"clicked":11,"open_rate":20.7,"click_rate":7.3,"status":"SENT"},
        {"id":"10","subject":"今月3名限定：あなたの商談を直接見ます","sent_date":"2026-05-29","sent":150,"opened":0,"clicked":0,"open_rate":0,"click_rate":0,"status":"SCHEDULED"},
    ]
    return jsonify({"campaigns": DEMO})


@app.route("/settings", methods=["GET", "POST"])
def settings():
    config = load_config()
    error = None
    success = None

    if request.method == "POST":
        api_key = request.form.get("api_key", "").strip()
        site_id = request.form.get("site_id", "").strip()
        try:
            headers = wix_headers(api_key, site_id)
            resp = requests.get(
                f"{WIX_API_BASE}/campaigns",
                headers=headers,
                params={"paging.limit": 1},
                timeout=10,
            )
            if resp.status_code == 200:
                save_config(api_key, site_id)
                success = "接続成功！設定を保存しました。"
                config = {"api_key": api_key, "site_id": site_id}
            elif resp.status_code == 401:
                error = "APIキーが正しくありません。"
            elif resp.status_code == 403:
                error = "アクセス権限がありません。"
            else:
                error = f"接続エラー: HTTP {resp.status_code}"
        except Exception as e:
            error = f"エラー: {str(e)}"

    return render_template("settings.html", config=config, error=error, success=success)


@app.route("/api/campaigns")
def api_campaigns():
    config = load_config()
    if not config["api_key"]:
        return jsonify({"error": "API設定が未完了です"}), 400
    try:
        campaigns = fetch_campaigns(config["api_key"], config["site_id"])
        results = []
        for c in campaigns:
            campaign_id = c.get("campaignId") or c.get("id", "")
            stats = fetch_campaign_stats(config["api_key"], config["site_id"], campaign_id)
            sent = stats.get("sent", 0) or stats.get("totalSent", 0)
            opened = stats.get("opened", 0) or stats.get("totalOpened", 0)
            clicked = stats.get("clicked", 0) or stats.get("totalClicked", 0)
            open_rate = round(opened / sent * 100, 1) if sent > 0 else 0
            click_rate = round(clicked / sent * 100, 1) if sent > 0 else 0
            subject = c.get("emailSubject") or c.get("subject") or c.get("name", "（件名なし）")
            sent_date = (c.get("dateSent") or c.get("publishingData", {}).get("scheduled", {}).get("time", "") or c.get("lastUpdated", ""))
            results.append({
                "id": campaign_id,
                "subject": subject,
                "sent_date": sent_date[:10] if sent_date else "未送信",
                "sent": sent,
                "opened": opened,
                "clicked": clicked,
                "open_rate": open_rate,
                "click_rate": click_rate,
                "status": c.get("status", ""),
            })
        results.sort(key=lambda x: x["sent_date"], reverse=True)
        return jsonify({"campaigns": results})
    except requests.exceptions.HTTPError as e:
        return jsonify({"error": f"Wix APIエラー: {e.response.status_code}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    init_db()
    print("アプリ起動中... http://localhost:5050 をブラウザで開いてください")
    app.run(debug=False, port=5050)
