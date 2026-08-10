
import os
import json
import sqlite3
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kf_crm_verify_2026")
DB_PATH = os.getenv("DB_PATH", "crm.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wa_message_id TEXT UNIQUE,
            phone_number_id TEXT,
            from_number TEXT,
            contact_name TEXT,
            message_type TEXT,
            body TEXT,
            timestamp TEXT,
            referral_json TEXT,
            raw_json TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_message(value):
    contacts = value.get("contacts") or []
    contact_name = None
    if contacts:
        contact_name = (contacts[0].get("profile") or {}).get("name")

    phone_number_id = (value.get("metadata") or {}).get("phone_number_id")

    for msg in value.get("messages") or []:
        mtype = msg.get("type")
        body = None
        if mtype == "text":
            body = (msg.get("text") or {}).get("body")
        elif mtype:
            body = json.dumps(msg.get(mtype) or {}, ensure_ascii=False)

        ts = msg.get("timestamp")
        if ts:
            try:
                ts = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            except Exception:
                pass

        referral = msg.get("referral")

        conn = get_db()
        conn.execute("""
            INSERT OR IGNORE INTO messages(
                wa_message_id, phone_number_id, from_number, contact_name,
                message_type, body, timestamp, referral_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            msg.get("id"),
            phone_number_id,
            msg.get("from"),
            contact_name,
            mtype,
            body,
            ts,
            json.dumps(referral, ensure_ascii=False) if referral else None,
            json.dumps(msg, ensure_ascii=False)
        ))
        conn.commit()
        conn.close()

@app.get("/")
def home():
    return jsonify({"ok": True, "service": "Kitchen Factory WhatsApp CRM Webhook"})

@app.get("/webhook/whatsapp")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200
    return "Forbidden", 403

@app.post("/webhook/whatsapp")
def receive_webhook():
    payload = request.get_json(silent=True) or {}

    conn = get_db()
    conn.execute(
        "INSERT INTO events(received_at, payload) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") == "messages":
                save_message(change.get("value") or {})

    return jsonify({"status": "received"}), 200

@app.get("/api/messages")
def api_messages():
    conn = get_db()
    rows = conn.execute("""
        SELECT id, wa_message_id, phone_number_id, from_number, contact_name,
               message_type, body, timestamp, referral_json
        FROM messages
        ORDER BY id DESC
        LIMIT 200
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
