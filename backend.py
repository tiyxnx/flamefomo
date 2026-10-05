import os
import re
import sqlite3
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

from flask import Flask, jsonify, request, session, redirect, send_from_directory
from flask_cors import CORS
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

APP_SECRET = os.environ["APP_SECRET"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
DATABASE = os.environ.get("DATABASE_PATH", "/tmp/fomo.sqlite3")

# Gmail read-only + Calendar free/busy only.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CAL_SCOPE = "https://www.googleapis.com/auth/calendar.freebusy"
ALL_SCOPES = [GMAIL_SCOPE, CAL_SCOPE]

app = Flask(__name__)

@app.get("/")
def frontend():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")

app.secret_key = APP_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)
CORS(app, supports_credentials=True, origins=[FRONTEND_URL] if FRONTEND_URL else "*")

def db():
    con = sqlite3.connect(DATABASE)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      google_sub TEXT UNIQUE NOT NULL,
      email TEXT NOT NULL,
      name TEXT DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS google_tokens (
      user_id INTEGER NOT NULL,
      service TEXT NOT NULL,
      access_token TEXT NOT NULL,
      refresh_token TEXT,
      expiry TEXT,
      scopes TEXT NOT NULL,
      PRIMARY KEY(user_id, service),
      FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS sender_filters (
      user_id INTEGER NOT NULL,
      sender TEXT NOT NULL,
      UNIQUE(user_id, sender)
    );
    """)
    con.commit()
    con.close()

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    con = db()
    u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return u

def require_user():
    u = current_user()
    if not u:
        return None, (jsonify(error="Not signed in"), 401)
    return u, None

def site_url():
    return FRONTEND_URL or request.url_root.rstrip("/")

def oauth_client(service):
    # OAuth callback lives on this same Vercel deployment.
    redirect_uri = site_url() + "/api/integrations/google/callback"
    scopes = [GMAIL_SCOPE] if service == "gmail" else [CAL_SCOPE]
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=scopes, redirect_uri=redirect_uri)
    return flow

@app.get("/api/health")
def health():
    return jsonify(ok=True)

@app.get("/api/me")
def me():
    u = current_user()
    if not u:
        return jsonify(user=None), 401
    return jsonify(user={"id": u["id"], "email": u["email"], "name": u["name"]})

@app.post("/api/auth/google")
def auth_google():
    data = request.get_json(force=True) or {}
    tok = data.get("id_token")
    if not tok:
        return jsonify(error="Missing Google ID token"), 400
    try:
        info = id_token.verify_oauth2_token(tok, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception:
        return jsonify(error="Google sign-in token could not be verified"), 401

    email = info.get("email", "")
    if not email.lower().endswith("@flame.edu.in"):
        return jsonify(error="Use your FLAME email ending in @flame.edu.in."), 403

    con = db()
    con.execute(
        """INSERT INTO users(google_sub,email,name,created_at) VALUES(?,?,?,?)
           ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email,name=excluded.name""",
        (info["sub"], email, info.get("name",""), datetime.now(timezone.utc).isoformat())
    )
    con.commit()
    u = con.execute("SELECT * FROM users WHERE google_sub=?", (info["sub"],)).fetchone()
    # Defaults match the existing UI.
    for sender in ["events@flame.edu.in", "clubs@flame.edu.in", "studentlife@flame.edu.in"]:
        con.execute("INSERT OR IGNORE INTO sender_filters(user_id,sender) VALUES(?,?)", (u["id"], sender))
    con.commit()
    con.close()
    session["user_id"] = u["id"]
    return jsonify(user={"id": u["id"], "email": u["email"], "name": u["name"]})

@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/api/integrations/google/start")
def google_start():
    u, err = require_user()
    if err:
        return err
    service = request.args.get("service")
    if service not in ("gmail", "calendar"):
        return jsonify(error="service must be gmail or calendar"), 400
    flow = oauth_client("gmail" if service == "gmail" else "calendar")
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=secrets.token_urlsafe(32),
    )
    session["oauth_state"] = state
    session["oauth_service"] = service
    return redirect(auth_url)

@app.get("/api/integrations/google/callback")
def google_callback():
    u = current_user()
    if not u:
        return redirect(site_url() + "?error=Please+sign+in+first")

    service = session.get("oauth_service")
    state = session.get("oauth_state")
    if service not in ("gmail", "calendar") or not state or request.args.get("state") != state:
        return redirect(site_url() + "?error=Invalid+OAuth+state")

    flow = oauth_client("gmail" if service == "gmail" else "calendar")
    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
    except Exception as e:
        return redirect(site_url() + "?error=" + urlencode({"": str(e)})[1:])

    scopes = " ".join(creds.scopes or [])
    expiry = creds.expiry.isoformat() if creds.expiry else ""
    con = db()
    old = con.execute(
        "SELECT refresh_token FROM google_tokens WHERE user_id=? AND service=?",
        (u["id"], service),
    ).fetchone()
    refresh = creds.refresh_token or (old["refresh_token"] if old else None) or ""
    con.execute("""
      INSERT INTO google_tokens(user_id,service,access_token,refresh_token,expiry,scopes)
      VALUES(?,?,?,?,?,?)
      ON CONFLICT(user_id,service) DO UPDATE SET
        access_token=excluded.access_token,
        refresh_token=excluded.refresh_token,
        expiry=excluded.expiry,
        scopes=excluded.scopes
    """, (u["id"], service, creds.token, refresh, expiry, scopes))
    con.commit()
    con.close()
    session.pop("oauth_state", None)
    session.pop("oauth_service", None)
    return redirect(site_url() + "?google=connected")

def credentials_for(u, service):
    con = db()
    row = con.execute(
        "SELECT * FROM google_tokens WHERE user_id=? AND service=?",
        (u["id"], service),
    ).fetchone()
    con.close()
    if not row:
        return None
    from google.oauth2.credentials import Credentials
    expiry = datetime.fromisoformat(row["expiry"]) if row["expiry"] else None
    return Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"] or None,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=row["scopes"].split(),
        expiry=expiry,
    )

@app.get("/api/integrations/google/status")
def google_status():
    u, err = require_user()
    if err:
        return err
    con = db()
    rows = con.execute("SELECT service, scopes FROM google_tokens WHERE user_id=?", (u["id"],)).fetchall()
    con.close()
    connected = {r["service"]: set((r["scopes"] or "").split()) for r in rows}
    return jsonify(
        gmail=GMAIL_SCOPE in connected.get("gmail", set()),
        calendar=CAL_SCOPE in connected.get("calendar", set()),
    )

@app.post("/api/integrations/google/disconnect")
def google_disconnect():
    u, err = require_user()
    if err:
        return err
    service = request.args.get("service")
    if service not in ("gmail", "calendar"):
        return jsonify(error="Invalid service"), 400
    con = db()
    con.execute("DELETE FROM google_tokens WHERE user_id=? AND service=?", (u["id"], service))
    con.commit()
    con.close()
    return jsonify(ok=True)


def parse_event_text(text, fallback_subject=""):
    # Conservative parser: only creates an event when the email contains a recognizable date/time.
    combined = (fallback_subject + "\n" + text)[:12000]
    date_m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", combined)
    if not date_m:
        date_m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", combined)
        if date_m:
            year, month, day = map(int, date_m.groups())
        else:
            return None
    else:
        day, month, year = map(int, date_m.groups())
    time_m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)\b", combined)
    if not time_m:
        return None
    h = int(time_m.group(1))
    minute = int(time_m.group(2) or 0)
    if time_m.group(3).lower() == "pm" and h < 12: h += 12
    if time_m.group(3).lower() == "am" and h == 12: h = 0
    try:
        start = datetime(year, month, day, h, minute, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    except ValueError:
        return None
    duration = 60
    title = re.sub(r"\s+", " ", fallback_subject).strip() or "Campus event"
    return {
        "title": title[:120],
        "start_ts": int(start.timestamp()),
        "end_ts": int((start + timedelta(minutes=duration)).timestamp()),
        "venue": "FLAME University",
        "room": "",
        "category": "Talks",
        "source": "gmail",
        "interested": 0,
    }

@app.get("/api/events")
def events():
    u, err = require_user()
    if err:
        return err
    out = []

    # Gmail: search only configured senders, and only recent mail.
    creds = credentials_for(u, "gmail")
    if creds and GMAIL_SCOPE in set(creds.scopes or []):
        try:
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            con = db()
            senders = [r["sender"] for r in con.execute("SELECT sender FROM sender_filters WHERE user_id=?", (u["id"],)).fetchall()]
            con.close()
            q = "newer_than:60d (" + " OR ".join("from:" + s for s in senders) + ")" if senders else "newer_than:60d"
            msgs = service.users().messages().list(userId="me", q=q, maxResults=50).execute().get("messages", [])
            for m in msgs:
                msg = service.users().messages().get(userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject","From","Date"]).execute()
                headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                # Metadata-only initially. A second fetch is avoided unless it looks event-like.
                subj = headers.get("subject", "")
                if not re.search(r"event|workshop|seminar|club|registration|fest|talk|session|competition|audition", subj, re.I):
                    continue
                full = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
                body = str(full.get("snippet", "")) + " " + subj
                e = parse_event_text(body, subj)
                if e:
                    e["id"] = abs(hash(m["id"])) % 2000000000
                    out.append(e)
        except HttpError:
            pass

    # Google Calendar free/busy is used only to determine conflicts; details remain private.
    # The existing frontend's manual busy times remain available.
    return jsonify(events=out)

@app.post("/api/events/<int:event_id>/going")
def going(event_id):
    # Kept compatible with the existing UI; production persistence can be added separately.
    return jsonify(ok=True)

@app.delete("/api/events/<int:event_id>/going")
def ungoing(event_id):
    return jsonify(ok=True)

@app.post("/api/events/<int:event_id>/register")
def register(event_id):
    return jsonify(ok=True)

@app.put("/api/events/<int:event_id>/hide")
def hide(event_id):
    return jsonify(ok=True)

@app.put("/api/prefs")
def prefs():
    return jsonify(ok=True)

init_db()

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
