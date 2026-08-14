import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, send_file
from io import BytesIO
from openpyxl import Workbook


app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kf_crm_verify_2026")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("Falta la variable de entorno DATABASE_URL en Render.")

LIMA_TZ = ZoneInfo("America/Lima")


# =========================================================
# BASE DE DATOS POSTGRESQL
# =========================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        sslmode="require"
    )


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id BIGSERIAL PRIMARY KEY,
            received_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients(
            id BIGSERIAL PRIMARY KEY,
            phone_number TEXT UNIQUE NOT NULL,
            contact_name TEXT,
            first_contact TEXT,
            last_contact TEXT,
            total_messages INTEGER DEFAULT 0,

            source TEXT DEFAULT 'Organico',
            campaign_name TEXT,
            ad_id TEXT,
            referral_json TEXT,

            status TEXT DEFAULT 'Nuevo',
            advisor TEXT,

            sale_amount NUMERIC(14,2) DEFAULT 0,
            loss_reason TEXT,

            first_response_at TEXT,
            last_advisor_reply TEXT,
            response_time_seconds INTEGER,

            lead_type TEXT DEFAULT 'Nuevo',
            conversation_count INTEGER DEFAULT 1,

            initial_source TEXT,
            initial_campaign TEXT,
            current_source TEXT,
            current_campaign TEXT,

            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS opportunities(
            id BIGSERIAL PRIMARY KEY,
            phone_number TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            last_activity_at TEXT,
            lead_type TEXT DEFAULT 'Nuevo',
            source TEXT DEFAULT 'Sin atribución',
            campaign_name TEXT,
            ad_id TEXT,
            status TEXT DEFAULT 'Nuevo',
            advisor TEXT,
            sale_amount NUMERIC(14,2) DEFAULT 0,
            loss_reason TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            id BIGSERIAL PRIMARY KEY,
            wa_message_id TEXT UNIQUE,

            phone_number_id TEXT,
            from_number TEXT,
            contact_name TEXT,

            message_type TEXT,
            body TEXT,
            timestamp TEXT,

            referral_json TEXT,
            raw_json TEXT,

            source TEXT DEFAULT 'Organico',
            campaign_name TEXT,
            ad_id TEXT,
            direction TEXT DEFAULT 'incoming'
        )
    """)

    # Migraciones seguras por si la tabla ya existía
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS sale_amount NUMERIC(14,2) DEFAULT 0")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS loss_reason TEXT")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS first_response_at TEXT")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS last_advisor_reply TEXT")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS response_time_seconds INTEGER")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS lead_type TEXT DEFAULT 'Nuevo'")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS conversation_count INTEGER DEFAULT 1")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS initial_source TEXT")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS initial_campaign TEXT")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS current_source TEXT")
    cur.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS current_campaign TEXT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'Organico'")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS campaign_name TEXT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS ad_id TEXT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS direction TEXT DEFAULT 'incoming'")
    cur.execute("""
        UPDATE clients
        SET source = 'Sin atribución'
        WHERE source = 'Organico'
    """)
    cur.execute("""
        UPDATE messages
        SET source = 'Sin atribución'
        WHERE source = 'Organico'
    """)
    cur.execute("""
        UPDATE clients
        SET
            initial_source = COALESCE(initial_source, source, 'Sin atribución'),
            initial_campaign = COALESCE(initial_campaign, campaign_name),
            current_source = COALESCE(current_source, source, 'Sin atribución'),
            current_campaign = COALESCE(current_campaign, campaign_name)
    """)

    cur.execute("""
        INSERT INTO opportunities(
            phone_number, opened_at, last_activity_at, lead_type,
            source, campaign_name, ad_id, status, advisor,
            sale_amount, loss_reason, created_at, updated_at
        )
        SELECT
            c.phone_number,
            COALESCE(c.first_contact, c.created_at, %s),
            COALESCE(c.last_contact, c.first_contact, c.created_at, %s),
            COALESCE(c.lead_type, 'Nuevo'),
            COALESCE(c.initial_source, c.source, 'Sin atribución'),
            COALESCE(c.initial_campaign, c.campaign_name),
            c.ad_id,
            COALESCE(c.status, 'Nuevo'),
            c.advisor,
            COALESCE(c.sale_amount, 0),
            c.loss_reason,
            COALESCE(c.created_at, %s),
            COALESCE(c.updated_at, %s)
        FROM clients c
        WHERE NOT EXISTS (
            SELECT 1 FROM opportunities o WHERE o.phone_number = c.phone_number
        )
    """, (now_lima(), now_lima(), now_lima(), now_lima()))

    conn.commit()
    cur.close()
    conn.close()


# =========================================================
# UTILIDADES
# =========================================================

def utc_to_lima(timestamp):
    if not timestamp:
        return None

    try:
        dt_utc = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        return dt_utc.astimezone(LIMA_TZ).isoformat()
    except Exception:
        return str(timestamp)


def now_lima():
    return datetime.now(LIMA_TZ).isoformat()


def parse_iso_lima(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LIMA_TZ)
        return dt.astimezone(LIMA_TZ)
    except Exception:
        return None


# Inicializar la base de datos después de definir now_lima() y utilidades de fecha
init_db()


def classify_returning_lead(previous_contact, new_contact, current_type="Nuevo", current_conversations=1):
    """
    Nueva conversación: más de 24 h desde el último mensaje.
    Recurrente: vuelve entre 24 h y 30 días.
    Reactivado: vuelve después de 30 días.
    """
    prev = parse_iso_lima(previous_contact)
    new = parse_iso_lima(new_contact)

    if not prev or not new:
        return current_type or "Nuevo", int(current_conversations or 1)

    gap_seconds = (new - prev).total_seconds()
    if gap_seconds <= 24 * 3600:
        return current_type or "Nuevo", int(current_conversations or 1)

    conversations = int(current_conversations or 1) + 1
    if gap_seconds >= 30 * 24 * 3600:
        return "Reactivado", conversations
    return "Recurrente", conversations


def normalize_manual_response(value):
    if not value:
        return now_lima()

    dt = parse_iso_lima(value)
    if not dt:
        raise ValueError("Fecha/hora inválida")
    return dt.isoformat()


def latest_opportunity(cur, phone_number):
    cur.execute("""
        SELECT * FROM opportunities
        WHERE phone_number = %s
        ORDER BY opened_at DESC, id DESC
        LIMIT 1
    """, (phone_number,))
    return cur.fetchone()


def create_opportunity(cur, phone_number, opened_at, lead_type, source, campaign_name, ad_id, status="Nuevo", advisor=None, sale_amount=0, loss_reason=None):
    current = now_lima()
    cur.execute("""
        INSERT INTO opportunities(
            phone_number, opened_at, last_activity_at, lead_type, source, campaign_name, ad_id,
            status, advisor, sale_amount, loss_reason, created_at, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (phone_number, opened_at, opened_at, lead_type, source, campaign_name, ad_id, status, advisor, sale_amount, loss_reason, current, current))
    row=cur.fetchone()
    return row["id"] if row else None


def touch_latest_opportunity(cur, phone_number, activity_at, source=None, campaign_name=None, ad_id=None):
    opp=latest_opportunity(cur, phone_number)
    if not opp: return
    fs=opp.get("source") or "Sin atribución"
    fc=opp.get("campaign_name")
    fa=opp.get("ad_id")
    if source=="Meta Ads":
        fs="Meta Ads"; fc=campaign_name or fc; fa=ad_id or fa
    cur.execute("""
        UPDATE opportunities
        SET last_activity_at=%s, source=%s, campaign_name=%s, ad_id=%s, updated_at=%s
        WHERE id=%s
    """, (activity_at, fs, fc, fa, now_lima(), opp["id"]))


def extract_referral(msg):
    referral = msg.get("referral")

    if not referral:
        return {
            "source": "Sin atribución",
            "campaign_name": None,
            "ad_id": None,
            "referral_json": None
        }

    source_id = referral.get("source_id")
    headline = referral.get("headline")
    body = referral.get("body")
    ctwa_clid = referral.get("ctwa_clid")

    return {
        "source": "Meta Ads",
        "campaign_name": headline or body or "Anuncio Meta",
        "ad_id": source_id or ctwa_clid,
        "referral_json": json.dumps(referral, ensure_ascii=False)
    }


# =========================================================
# CLIENTES
# =========================================================

def update_client(phone_number, contact_name, timestamp, source,
                  campaign_name, ad_id, referral_json):
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT * FROM clients WHERE phone_number=%s", (phone_number,))
    existing=cur.fetchone(); current_time=now_lima()
    normalized_source="Meta Ads" if source=="Meta Ads" else "Sin atribución"

    if existing:
        lead_type, conversation_count = classify_returning_lead(
            existing.get("last_contact"), timestamp, existing.get("lead_type") or "Nuevo", existing.get("conversation_count") or 1
        )
        prev=parse_iso_lima(existing.get("last_contact")); curdt=parse_iso_lima(timestamp)
        new_opp=bool(prev and curdt and (curdt-prev).total_seconds()>24*3600)
        current_source=existing.get("current_source") or existing.get("source") or "Sin atribución"
        current_campaign=existing.get("current_campaign") or existing.get("campaign_name")
        if source=="Meta Ads":
            current_source="Meta Ads"; current_campaign=campaign_name or current_campaign
        cur.execute("""
            UPDATE clients SET contact_name=COALESCE(%s,contact_name), last_contact=%s,
                total_messages=COALESCE(total_messages,0)+1, source=%s, campaign_name=%s,
                ad_id=COALESCE(%s,ad_id), referral_json=COALESCE(%s,referral_json),
                lead_type=%s, conversation_count=%s,
                initial_source=COALESCE(initial_source,source,%s),
                initial_campaign=COALESCE(initial_campaign,campaign_name),
                current_source=%s, current_campaign=%s, updated_at=%s
            WHERE phone_number=%s
        """, (contact_name,timestamp,current_source,current_campaign,ad_id,referral_json,lead_type,conversation_count,
              existing.get("initial_source") or existing.get("source") or "Sin atribución",current_source,current_campaign,current_time,phone_number))
        if new_opp:
            create_opportunity(cur,phone_number,timestamp,lead_type,normalized_source,campaign_name,ad_id,status="Nuevo",advisor=existing.get("advisor"))
        else:
            touch_latest_opportunity(cur,phone_number,timestamp,source,campaign_name,ad_id)
    else:
        cur.execute("""
            INSERT INTO clients(phone_number,contact_name,first_contact,last_contact,total_messages,source,campaign_name,ad_id,referral_json,
                status,advisor,sale_amount,loss_reason,lead_type,conversation_count,initial_source,initial_campaign,current_source,current_campaign,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (phone_number,contact_name,timestamp,timestamp,1,normalized_source,campaign_name,ad_id,referral_json,"Nuevo",None,0,None,"Nuevo",1,
              normalized_source,campaign_name,normalized_source,campaign_name,current_time,current_time))
        create_opportunity(cur,phone_number,timestamp,"Nuevo",normalized_source,campaign_name,ad_id,status="Nuevo")
    conn.commit(); cur.close(); conn.close()


# =========================================================
# MENSAJES
# =========================================================

def save_message(value):
    contacts = value.get("contacts") or []
    contact_name = None

    if contacts:
        contact_name = ((contacts[0].get("profile") or {}).get("name"))

    metadata = value.get("metadata") or {}
    phone_number_id = metadata.get("phone_number_id")

    for msg in value.get("messages") or []:
        message_id = msg.get("id")
        from_number = msg.get("from")
        message_type = msg.get("type")

        body = None
        if message_type == "text":
            body = (msg.get("text") or {}).get("body")
        elif message_type:
            body = json.dumps(msg.get(message_type) or {}, ensure_ascii=False)

        timestamp = utc_to_lima(msg.get("timestamp"))
        referral_data = extract_referral(msg)

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO messages(
                wa_message_id, phone_number_id, from_number, contact_name,
                message_type, body, timestamp,
                referral_json, raw_json,
                source, campaign_name, ad_id, direction
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (wa_message_id) DO NOTHING
            RETURNING id
        """, (
            message_id,
            phone_number_id,
            from_number,
            contact_name,
            message_type,
            body,
            timestamp,
            referral_data["referral_json"],
            json.dumps(msg, ensure_ascii=False),
            referral_data["source"],
            referral_data["campaign_name"],
            referral_data["ad_id"],
            "incoming"
        ))

        inserted = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        # Solo incrementa el lead si el mensaje era realmente nuevo
        if inserted and from_number:
            update_client(
                phone_number=from_number,
                contact_name=contact_name,
                timestamp=timestamp,
                source=referral_data["source"],
                campaign_name=referral_data["campaign_name"],
                ad_id=referral_data["ad_id"],
                referral_json=referral_data["referral_json"]
            )



# =========================================================
# RESPUESTAS DE ASESORAS (WHATSAPP BUSINESS APP / COEXISTENCE)
# =========================================================

def calculate_response_seconds(first_contact, response_at):
    if not first_contact or not response_at:
        return None

    try:
        first_dt = datetime.fromisoformat(first_contact)
        response_dt = datetime.fromisoformat(response_at)
        seconds = int((response_dt - first_dt).total_seconds())
        return max(seconds, 0)
    except Exception:
        return None


def save_outgoing_echo(value):
    """
    Guarda mensajes enviados desde WhatsApp Business App / dispositivos vinculados
    cuando Meta los entrega mediante smb_message_echoes.
    """
    metadata = value.get("metadata") or {}
    phone_number_id = metadata.get("phone_number_id")

    for msg in value.get("messages") or []:
        message_id = msg.get("id")
        customer_number = (
            msg.get("to")
            or msg.get("recipient")
            or msg.get("recipient_id")
        )
        message_type = msg.get("type")

        body = None
        if message_type == "text":
            body = (msg.get("text") or {}).get("body")
        elif message_type:
            body = json.dumps(msg.get(message_type) or {}, ensure_ascii=False)

        timestamp = utc_to_lima(msg.get("timestamp"))

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO messages(
                wa_message_id, phone_number_id, from_number, contact_name,
                message_type, body, timestamp,
                referral_json, raw_json,
                source, campaign_name, ad_id, direction
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (wa_message_id) DO NOTHING
            RETURNING id
        """, (
            message_id,
            phone_number_id,
            customer_number,
            None,
            message_type,
            body,
            timestamp,
            None,
            json.dumps(msg, ensure_ascii=False),
            None,
            None,
            None,
            "outgoing"
        ))

        inserted = cur.fetchone()

        if inserted and customer_number:
            cur.execute("""
                SELECT first_contact, first_response_at
                FROM clients
                WHERE phone_number = %s
            """, (customer_number,))
            client = cur.fetchone()

            if client:
                response_seconds = None
                if not client.get("first_response_at"):
                    response_seconds = calculate_response_seconds(
                        client.get("first_contact"),
                        timestamp
                    )

                cur.execute("""
                    UPDATE clients
                    SET
                        last_advisor_reply = %s,
                        first_response_at = COALESCE(first_response_at, %s),
                        response_time_seconds = COALESCE(response_time_seconds, %s),
                        updated_at = %s
                    WHERE phone_number = %s
                """, (
                    timestamp,
                    timestamp,
                    response_seconds,
                    now_lima(),
                    customer_number
                ))

        conn.commit()
        cur.close()
        conn.close()


# =========================================================
# WEBHOOK META / PÁGINAS
# =========================================================

@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": "Kitchen Factory WhatsApp CRM",
        "status": "online",
        "database": "postgresql"
    })


@app.get("/privacy")
def privacy():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Política de Privacidad - Kitchen Factory</title>
    </head>
    <body style="font-family: Arial, sans-serif; max-width: 850px; margin: 40px auto; padding: 20px; line-height: 1.6;">
        <h1>Política de Privacidad</h1>
        <h2>Kitchen Factory</h2>
        <p>Kitchen Factory respeta la privacidad de sus clientes y se compromete a proteger la información personal proporcionada a través de nuestros canales de atención, incluido WhatsApp.</p>
        <h3>Información que recopilamos</h3>
        <p>Podemos recopilar información como nombre, número de teléfono, mensajes enviados y demás información proporcionada voluntariamente durante la comunicación con nuestra empresa.</p>
        <h3>Uso de la información</h3>
        <p>La información recopilada se utiliza para atender consultas, gestionar solicitudes, brindar soporte, realizar seguimiento de clientes y mejorar nuestros servicios.</p>
        <h3>Protección de datos</h3>
        <p>Kitchen Factory adopta medidas razonables para proteger la información personal y evitar accesos, usos o divulgaciones no autorizadas.</p>
        <h3>Compartición de información</h3>
        <p>No comercializamos ni vendemos los datos personales de nuestros clientes. La información podrá ser procesada mediante proveedores tecnológicos necesarios para brindar nuestros servicios.</p>
        <h3>Derechos del usuario</h3>
        <p>Los usuarios pueden solicitar información, actualización, corrección o eliminación de sus datos personales comunicándose con Kitchen Factory.</p>
        <h3>Contacto</h3>
        <p>Para consultas relacionadas con privacidad y protección de datos, puede comunicarse con Kitchen Factory a través de nuestros canales oficiales de atención.</p>
        <p><strong>Última actualización: agosto de 2026.</strong></p>
    </body>
    </html>
    """, 200


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
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO events(received_at, payload)
        VALUES (%s, %s)
    """, (
        now_lima(),
        json.dumps(payload, ensure_ascii=False)
    ))
    conn.commit()
    cur.close()
    conn.close()

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            field = change.get("field")
            value = change.get("value") or {}

            if field == "messages":
                save_message(value)
            elif field == "smb_message_echoes":
                save_outgoing_echo(value)

    return jsonify({"status": "received"}), 200


# =========================================================
# DASHBOARD CRM
# =========================================================

@app.get("/dashboard")
def dashboard():
    return r'''<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CRM Kitchen Factory</title><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><style>*{box-sizing:border-box}:root{--bg:#09111f;--panel:#121d31;--line:rgba(255,255,255,.09);--text:#f8fafc;--muted:#aab6ca}body{margin:0;font-family:Arial,sans-serif;color:var(--text);background:radial-gradient(circle at 10% 0,rgba(245,158,11,.11),transparent 25%),radial-gradient(circle at 90% 0,rgba(59,130,246,.10),transparent 25%),linear-gradient(145deg,#07101d,#0d182a 55%,#101b2e);min-height:100vh}.app{max-width:1680px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:18px}.brand h1{margin:0;font-size:31px}.brand p{margin:5px 0 0;color:var(--muted)}.tabs{display:flex;gap:7px;padding:6px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.04)}.tabbtn{border:0;background:transparent;color:var(--muted);padding:10px 15px;border-radius:9px;font-weight:700;cursor:pointer}.tabbtn.active{background:#fff;color:#111827}.tab{display:none}.tab.active{display:block}.cards{display:grid;grid-template-columns:repeat(8,minmax(130px,1fr));gap:12px;margin-bottom:15px}.card,.panel{background:linear-gradient(180deg,rgba(255,255,255,.075),rgba(255,255,255,.045));border:1px solid var(--line);border-radius:15px;box-shadow:0 12px 28px rgba(0,0,0,.15)}.card{padding:16px}.label{font-size:12px;color:var(--muted)}.value{font-size:28px;font-weight:800;margin-top:7px}.value.small{font-size:20px}.panel{padding:16px}.grid3{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:14px;margin-bottom:14px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}.panel h3,.panel h2{margin:0 0 14px}.chart{height:310px}.sectionhead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.btn{background:#fff;color:#111827;text-decoration:none;font-weight:800;padding:10px 14px;border-radius:9px}.filters{display:grid;grid-template-columns:1.3fr 1fr 1fr 1fr 1fr 1fr 1fr;gap:10px;margin:14px 0}.quickfilters{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0 12px}.quickfilters button{cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.06);color:var(--text);padding:7px 10px;border-radius:8px;font-weight:700}input,select{width:100%;padding:9px 10px;border:1px solid #d5dbe5;border-radius:9px;background:#f8fafc;color:#111827}.tablewrap{max-height:620px;overflow:auto;border:1px solid var(--line);border-radius:12px;background:rgba(5,12,24,.45)}table{width:100%;border-collapse:collapse;font-size:12.5px;min-width:1450px}th,td{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}th{position:sticky;top:0;background:#0d1728;z-index:2}.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:rgba(255,255,255,.11)}.muted{color:var(--muted)}.mini{padding:6px 8px;font-size:12px}.rank{min-width:0}.empty{text-align:center;color:var(--muted);padding:18px}@media(max-width:1350px){.cards{grid-template-columns:repeat(4,1fr)}.grid3{grid-template-columns:1fr 1fr}}@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid2,.grid3,.filters{grid-template-columns:1fr}.top{flex-direction:column;align-items:flex-start}}</style></head><body><div class="app"><div class="top"><div class="brand"><h1>CRM Kitchen Factory</h1><p>Ventas, leads, WhatsApp y desempeño comercial</p></div><div class="tabs"><button class="tabbtn active" data-tab="dash">Dashboard</button><button class="tabbtn" data-tab="clients">Clientes</button></div></div><section id="dash" class="tab active"><div class="cards"><div class="card"><div class="label">Leads totales</div><div class="value" id="totalClients">0</div></div><div class="card"><div class="label">Nuevos hoy</div><div class="value" id="newToday">0</div></div><div class="card"><div class="label">Mensajes</div><div class="value" id="totalMessages">0</div></div><div class="card"><div class="label">Ventas</div><div class="value" id="salesCount">0</div></div><div class="card"><div class="label">Conversión</div><div class="value" id="conversion">0%</div></div><div class="card"><div class="label">Monto vendido</div><div class="value small" id="salesAmount">S/ 0</div></div><div class="card"><div class="label">Meta Ads</div><div class="value" id="metaClients">0</div></div><div class="card"><div class="label">Resp. promedio</div><div class="value small" id="avgResponse">—</div></div></div><div class="grid2"><div class="panel"><h3>Evolución mensual de leads</h3><div class="chart"><canvas id="monthChart"></canvas></div></div><div class="panel"><h3>Leads últimos 30 días</h3><div class="chart"><canvas id="dayChart"></canvas></div></div></div><div class="grid3"><div class="panel"><h3>Embudo comercial</h3><div class="chart"><canvas id="statusChart"></canvas></div></div><div class="panel"><h3>Tipo de lead</h3><div class="chart"><canvas id="leadTypeChart"></canvas></div></div><div class="panel"><h3>Origen de leads</h3><div class="chart"><canvas id="sourceChart"></canvas></div></div></div><div class="grid2"><div class="panel"><h3>Desempeño por responsable / área</h3><div class="chart"><canvas id="advisorChart"></canvas></div></div><div class="panel"><h3>Top campañas</h3><div class="chart"><canvas id="campaignChart"></canvas></div></div></div><div class="panel"><h3>Ranking por responsable / área</h3><div style="overflow:auto"><table class="rank"><thead><tr><th>Responsable / Área</th><th>Leads</th><th>Ventas</th><th>Conversión</th><th>Monto vendido</th></tr></thead><tbody id="advisorRanking"></tbody></table></div></div></section><section id="clients" class="tab"><div class="panel"><div class="sectionhead"><div><h2 style="margin:0">Gestión de clientes</h2><div class="muted" style="margin-top:4px">Seguimiento comercial y atención por WhatsApp</div></div><a class="btn" href="/export/clients.xlsx" onclick="this.href='/export/clients.xlsx'+dateQuery()">Descargar Excel</a></div><div class="quickfilters"><button onclick="setQuickDate('today')">Hoy</button><button onclick="setQuickDate('week')">Esta semana</button><button onclick="setQuickDate('month')">Este mes</button><button onclick="setQuickDate('prevmonth')">Mes anterior</button><button onclick="clearDates()">Todo</button></div><div class="filters"><input id="search" placeholder="Buscar por nombre o teléfono"><select id="sourceFilter"><option value="">Todos los orígenes</option><option value="Sin atribución">Sin atribución</option><option value="Meta Ads">Meta Ads</option></select><select id="statusFilter"><option value="">Todos los estados</option><option>Nuevo</option><option>En atención</option><option>Cotización / Seguimiento</option><option>Venta</option><option>Perdido</option><option>Resuelto</option></select><select id="leadTypeFilter"><option value="">Todos los tipos</option><option>Nuevo</option><option>Recurrente</option><option>Reactivado</option></select><input id="dateFrom" type="date" title="Desde"><input id="dateTo" type="date" title="Hasta"><input id="campaignFilter" placeholder="Filtrar por campaña"></div><div class="tablewrap"><table><thead><tr><th>Cliente</th><th>Teléfono</th><th>Origen inicial</th><th>Origen actual</th><th>Campaña actual</th><th>Tipo lead</th><th>Conversaciones</th><th>Primer contacto</th><th>Último mensaje cliente</th><th>1ra respuesta</th><th>Tiempo respuesta</th><th>Marcar respuesta</th><th>Mensajes</th><th>Estado</th><th>Responsable / Área</th><th>Monto venta</th><th>Motivo pérdida</th></tr></thead><tbody id="clientsBody"></tbody></table></div></div></section></div><script>let clients=[],charts={};const esc=v=>String(v??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"','&quot;');function fmtDate(v){if(!v)return"—";const d=new Date(v);if(Number.isNaN(d.getTime()))return v;return new Intl.DateTimeFormat("es-PE",{timeZone:"America/Lima",day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit",hour12:true}).format(d)}function fmtDur(s){if(s===null||s===undefined||s==="")return"Pendiente";s=Number(s);if(!Number.isFinite(s))return"—";if(s<60)return`${Math.round(s)} s`;let m=Math.floor(s/60);if(m<60)return`${m} min`;return`${Math.floor(m/60)} h ${m%60} min`}function money(v){return new Intl.NumberFormat("es-PE",{style:"currency",currency:"PEN"}).format(Number(v||0))}document.querySelectorAll(".tabbtn").forEach(b=>b.onclick=()=>{document.querySelectorAll(".tabbtn").forEach(x=>x.classList.remove("active"));document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));b.classList.add("active");document.getElementById(b.dataset.tab).classList.add("active")});function dc(n){if(charts[n])charts[n].destroy()}function dateQuery(){const p=new URLSearchParams();if(dateFrom&&dateFrom.value)p.set("from",dateFrom.value);if(dateTo&&dateTo.value)p.set("to",dateTo.value);const s=p.toString();return s?`?${s}`:""}async function loadDashboard(){let d=await (await fetch("/api/dashboard"+dateQuery())).json();totalClients.textContent=d.total_clients??0;newToday.textContent=d.new_today??0;totalMessages.textContent=d.total_messages??0;salesCount.textContent=d.sales_count??0;conversion.textContent=`${d.conversion_rate??0}%`;salesAmount.textContent=money(d.total_sales_amount);metaClients.textContent=d.meta_ads_clients??0;avgResponse.textContent=fmtDur(d.avg_response_seconds);const tc="#aab6ca",gc="rgba(255,255,255,.07)";dc("month");charts.month=new Chart(monthChart,{type:"line",data:{labels:(d.clients_by_month||[]).map(x=>x.month),datasets:[{data:(d.clients_by_month||[]).map(x=>x.leads),label:"Leads",tension:.35,fill:true},{data:(d.clients_by_month||[]).map(x=>x.sales),label:"Ventas",tension:.35,fill:false}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:tc}}},scales:{x:{ticks:{color:tc},grid:{color:gc}},y:{beginAtZero:true,ticks:{color:tc,precision:0},grid:{color:gc}}}}});dc("day");charts.day=new Chart(dayChart,{type:"line",data:{labels:(d.clients_by_day||[]).map(x=>x.day),datasets:[{data:(d.clients_by_day||[]).map(x=>x.total),label:"Leads",tension:.35,fill:true}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:tc},grid:{color:gc}},y:{beginAtZero:true,ticks:{color:tc,precision:0},grid:{color:gc}}}}});dc("status");charts.status=new Chart(statusChart,{type:"doughnut",data:{labels:(d.clients_by_status||[]).map(x=>x.status),datasets:[{data:(d.clients_by_status||[]).map(x=>x.total)}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom",labels:{color:tc}}}}});dc("leadType");charts.leadType=new Chart(leadTypeChart,{type:"doughnut",data:{labels:(d.clients_by_lead_type||[]).map(x=>x.lead_type),datasets:[{data:(d.clients_by_lead_type||[]).map(x=>x.total)}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom",labels:{color:tc}}}}});dc("source");charts.source=new Chart(sourceChart,{type:"doughnut",data:{labels:(d.clients_by_source||[]).map(x=>x.source),datasets:[{data:(d.clients_by_source||[]).map(x=>x.total)}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom",labels:{color:tc}}}}});dc("advisor");charts.advisor=new Chart(advisorChart,{type:"bar",data:{labels:(d.clients_by_advisor||[]).map(x=>x.advisor),datasets:[{label:"Leads",data:(d.clients_by_advisor||[]).map(x=>x.leads)},{label:"Ventas",data:(d.clients_by_advisor||[]).map(x=>x.sales)}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:tc}}},scales:{x:{ticks:{color:tc},grid:{color:gc}},y:{beginAtZero:true,ticks:{color:tc,precision:0},grid:{color:gc}}}}});dc("campaign");charts.campaign=new Chart(campaignChart,{type:"bar",data:{labels:(d.clients_by_campaign||[]).map(x=>x.campaign),datasets:[{data:(d.clients_by_campaign||[]).map(x=>x.leads),label:"Leads"}]},options:{indexAxis:"y",responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{beginAtZero:true,ticks:{color:tc,precision:0},grid:{color:gc}},y:{ticks:{color:tc},grid:{color:gc}}}}});advisorRanking.innerHTML=(d.clients_by_advisor||[]).map(x=>`<tr><td><strong>${esc(x.advisor)}</strong></td><td>${x.leads}</td><td>${x.sales}</td><td>${x.leads?((x.sales/x.leads)*100).toFixed(1):"0.0"}%</td><td>${money(x.amount)}</td></tr>`).join("")||'<tr><td class="empty" colspan="5">Aún no hay información asignada.</td></tr>'}async function loadClients(){clients=await (await fetch("/api/clients"+dateQuery())).json();renderClients()}function renderClients(){const q=search.value.toLowerCase().trim(),so=sourceFilter.value,st=statusFilter.value,lt=leadTypeFilter.value,ca=campaignFilter.value.toLowerCase().trim();const rows=clients.filter(c=>(!q||String(c.contact_name||"").toLowerCase().includes(q)||String(c.phone_number||"").includes(q))&&(!so||c.source===so)&&(!st||c.status===st)&&(!lt||c.lead_type===lt)&&(!ca||String(c.campaign_name||"").toLowerCase().includes(ca)));clientsBody.innerHTML=rows.map(c=>`<tr><td><strong>${esc(c.contact_name||"Sin nombre")}</strong></td><td>${esc(c.phone_number)}</td><td><span class="badge">${esc(c.initial_source||"Sin atribución")}</span></td><td><span class="badge">${esc(c.current_source||"Sin atribución")}</span></td><td>${esc(c.current_campaign||c.campaign_name||"—")}</td><td><span class="badge">${esc(c.lead_type||"Nuevo")}</span></td><td>${esc(c.conversation_count||1)}</td><td class="muted">${esc(fmtDate(c.first_contact))}</td><td class="muted">${esc(fmtDate(c.last_contact))}</td><td class="muted">${esc(fmtDate(c.first_response_at))}</td><td><strong>${esc(fmtDur(c.response_time_seconds))}</strong></td><td>${c.first_response_at?`<div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap"><span class="badge">Registrada</span><button class="mini" style="cursor:pointer;font-weight:700" onclick="manualResponse('${esc(c.phone_number)}','${esc(c.first_response_at||"")}')">Editar hora</button><button class="mini" style="cursor:pointer;font-weight:700" onclick="undoResponse('${esc(c.phone_number)}')">Deshacer</button></div>`:`<div style="display:flex;gap:5px;flex-wrap:wrap"><button class="mini" style="cursor:pointer;font-weight:700" onclick="markResponse('${esc(c.phone_number)}')">Ahora</button><button class="mini" style="cursor:pointer;font-weight:700" onclick="manualResponse('${esc(c.phone_number)}','')">Hora manual</button></div>`}</td><td>${esc(c.total_messages||0)}</td><td><select class="mini" onchange="updateStatus('${esc(c.phone_number)}',this.value)">${["Nuevo","En atención","Cotización / Seguimiento","Venta","Perdido","Resuelto"].map(s=>`<option ${c.status===s?"selected":""}>${s}</option>`).join("")}</select></td><td><select class="mini" onchange="updateAdvisor('${esc(c.phone_number)}',this.value)"><option value="" ${!c.advisor?"selected":""}>Sin asignar</option>${["Narly","Raphaella","Ursula","Ecommerce","Post Venta / Servicio Técnico","Inmobiliaria"].map(a=>`<option value="${a}" ${c.advisor===a?"selected":""}>${a}</option>`).join("")}</select></td><td><input class="mini" type="number" min="0" step="0.01" value="${esc(c.sale_amount||0)}" onchange="updateSale('${esc(c.phone_number)}',this.value)"></td><td><select class="mini" onchange="updateLossReason('${esc(c.phone_number)}',this.value)"><option value="" ${!c.loss_reason?"selected":""}>—</option>${["No respondió","Precio","Sin stock","No interesado","Compró en otro lugar","Fuera de cobertura","Otro"].map(r=>`<option value="${r}" ${c.loss_reason===r?"selected":""}>${r}</option>`).join("")}</select></td></tr>`).join("")}async function upd(url,obj){await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(obj)});await Promise.all([loadClients(),loadDashboard()])}const updateStatus=(p,v)=>upd(`/api/client/${encodeURIComponent(p)}/status`,{status:v}),updateAdvisor=(p,v)=>upd(`/api/client/${encodeURIComponent(p)}/advisor`,{advisor:v}),updateSale=(p,v)=>upd(`/api/client/${encodeURIComponent(p)}/sale`,{sale_amount:v}),updateLossReason=(p,v)=>upd(`/api/client/${encodeURIComponent(p)}/loss-reason`,{loss_reason:v}),markResponse=(p)=>upd(`/api/client/${encodeURIComponent(p)}/mark-response`,{}),undoResponse=(p)=>{if(confirm("¿Deshacer la respuesta registrada? Solo se borrará la hora y el tiempo de respuesta.")){return upd(`/api/client/${encodeURIComponent(p)}/undo-response`,{})}},manualResponse=(p,current)=>{let def="";if(current){const d=new Date(current);if(!Number.isNaN(d.getTime())){const z=n=>String(n).padStart(2,"0");def=`${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())}T${z(d.getHours())}:${z(d.getMinutes())}`}}let v=prompt("Ingresa fecha y hora de la primera respuesta (AAAA-MM-DDTHH:MM)",def);if(v)return upd(`/api/client/${encodeURIComponent(p)}/mark-response`,{response_at:v})};function isoDate(d){const z=n=>String(n).padStart(2,"0");return`${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())}`}function applyDates(){Promise.all([loadClients(),loadDashboard()])}function clearDates(){dateFrom.value="";dateTo.value="";applyDates()}function setQuickDate(type){const n=new Date(),a=new Date(n),b=new Date(n);if(type==="today"){}else if(type==="week"){const day=(n.getDay()+6)%7;a.setDate(n.getDate()-day)}else if(type==="month"){a.setDate(1)}else if(type==="prevmonth"){a.setMonth(n.getMonth()-1,1);b.setDate(0)}dateFrom.value=isoDate(a);dateTo.value=isoDate(b);applyDates()}["search","sourceFilter","statusFilter","leadTypeFilter","campaignFilter"].forEach(id=>{document.getElementById(id).addEventListener("input",renderClients);document.getElementById(id).addEventListener("change",renderClients)});["dateFrom","dateTo"].forEach(id=>document.getElementById(id).addEventListener("change",applyDates));loadDashboard();loadClients();setInterval(()=>{loadDashboard();loadClients()},30000)</script></body></html>''', 200



# =========================================================
# API - CLIENTES
# =========================================================

@app.get("/api/clients")
def api_clients():
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    conn = get_db()
    cur = conn.cursor()

    where = []
    params = []

    if date_from:
        where.append("LEFT(first_contact, 10) >= %s")
        params.append(date_from)
    if date_to:
        where.append("LEFT(first_contact, 10) <= %s")
        params.append(date_to)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    cur.execute(f"""
        SELECT
            id, phone_number, contact_name,
            first_contact, last_contact, total_messages,
            source, campaign_name, ad_id,
            status, advisor,
            COALESCE(sale_amount, 0) AS sale_amount,
            loss_reason,
            first_response_at,
            last_advisor_reply,
            response_time_seconds,
            COALESCE(lead_type, 'Nuevo') AS lead_type,
            COALESCE(conversation_count, 1) AS conversation_count,
            COALESCE(initial_source, source, 'Sin atribución') AS initial_source,
            initial_campaign,
            COALESCE(current_source, source, 'Sin atribución') AS current_source,
            current_campaign
        FROM clients
        {where_sql}
        ORDER BY last_contact DESC NULLS LAST
    """, params)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify(rows)


@app.get("/api/opportunities")
def api_opportunities():
    conn=get_db(); cur=conn.cursor()
    cur.execute("""
        SELECT id,phone_number,opened_at,last_activity_at,lead_type,source,campaign_name,status,advisor,sale_amount,loss_reason
        FROM opportunities ORDER BY opened_at DESC,id DESC
    """)
    rows=cur.fetchall(); cur.close(); conn.close(); return jsonify(rows)


@app.get("/api/messages")
def api_messages():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id, wa_message_id,
            phone_number_id, from_number, contact_name,
            message_type, body, timestamp,
            source, campaign_name, ad_id, referral_json,
            direction
        FROM messages
        ORDER BY id DESC
        LIMIT 500
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify(rows)


@app.get("/api/events")
def api_events():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, received_at, payload
        FROM events
        ORDER BY id DESC
        LIMIT 100
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify(rows)


@app.post("/api/client/<phone>/sale")
def update_sale(phone):
    data = request.get_json(silent=True) or {}

    try:
        amount = float(data.get("sale_amount") or 0)
    except (TypeError, ValueError):
        amount = 0

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE clients
        SET sale_amount = %s, updated_at = %s
        WHERE phone_number = %s
    """, (amount, now_lima(), phone))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"ok": True, "sale_amount": amount})


@app.post("/api/client/<phone>/loss-reason")
def update_loss_reason(phone):
    data = request.get_json(silent=True) or {}
    reason = (data.get("loss_reason") or "").strip() or None

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE clients
        SET loss_reason = %s, updated_at = %s
        WHERE phone_number = %s
    """, (reason, now_lima(), phone))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"ok": True, "loss_reason": reason})



@app.get("/export/clients.xlsx")
def export_clients_excel():
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    where = []
    params = []
    if date_from:
        where.append("LEFT(first_contact, 10) >= %s")
        params.append(date_from)
    if date_to:
        where.append("LEFT(first_contact, 10) <= %s")
        params.append(date_to)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    conn = get_db()
    cur = conn.cursor()

    cur.execute(f"""
        SELECT
            contact_name, phone_number, source, campaign_name,
            first_contact, last_contact, first_response_at,
            response_time_seconds, total_messages,
            COALESCE(lead_type, 'Nuevo') AS lead_type,
            COALESCE(conversation_count, 1) AS conversation_count,
            COALESCE(initial_source, source, 'Sin atribución') AS initial_source,
            initial_campaign,
            COALESCE(current_source, source, 'Sin atribución') AS current_source,
            current_campaign,
            status, advisor, sale_amount, loss_reason
        FROM clients
        {where_sql}
        ORDER BY last_contact DESC NULLS LAST
    """, params)
    rows = cur.fetchall()

    cur.close()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Clientes CRM"

    headers = [
        "Cliente", "Teléfono", "Origen", "Campaña",
        "Primer contacto", "Último mensaje cliente",
        "Primera respuesta", "Tiempo respuesta (seg)",
        "Mensajes", "Tipo de lead", "Conversaciones",
        "Estado", "Responsable / Área",
        "Monto de venta", "Motivo de pérdida"
    ]
    ws.append(headers)

    for row in rows:
        ws.append([
            row.get("contact_name"), row.get("phone_number"),
            row.get("source"), row.get("campaign_name"),
            row.get("first_contact"), row.get("last_contact"),
            row.get("first_response_at"), row.get("response_time_seconds"),
            row.get("total_messages"), row.get("lead_type"),
            row.get("conversation_count"), row.get("status"),
            row.get("advisor"), float(row.get("sale_amount") or 0),
            row.get("loss_reason")
        ])

    for cell in ws[1]:
        cell.font = cell.font.copy(bold=True)

    for col, width in {
        "A":24,"B":18,"C":18,"D":28,"E":24,"F":24,"G":24,"H":22,
        "I":12,"J":16,"K":16,"L":22,"M":20,"N":16,"O":24
    }.items():
        ws.column_dimensions[col].width = width

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"CRM_Kitchen_Factory_{datetime.now(LIMA_TZ).strftime('%Y-%m-%d_%H-%M')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.get("/api/dashboard")
def api_dashboard():
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    where = []
    params = []
    if date_from:
        where.append("LEFT(first_contact, 10) >= %s")
        params.append(date_from)
    if date_to:
        where.append("LEFT(first_contact, 10) <= %s")
        params.append(date_to)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    and_sql = (" AND " + " AND ".join(where)) if where else ""

    conn = get_db()
    cur = conn.cursor()

    cur.execute(f"SELECT COUNT(*) AS total FROM clients{where_sql}", params)
    total_clients = cur.fetchone()["total"]

    cur.execute(f"SELECT COALESCE(SUM(total_messages), 0) AS total FROM clients{where_sql}", params)
    total_messages = cur.fetchone()["total"]

    cur.execute(f"SELECT COUNT(*) AS total FROM clients WHERE source = 'Meta Ads'{and_sql}", params)
    meta_clients = cur.fetchone()["total"]

    cur.execute(f"SELECT COUNT(*) AS total FROM clients WHERE source = 'Sin atribución'{and_sql}", params)
    unattributed_clients = cur.fetchone()["total"]

    cur.execute(f"SELECT COUNT(*) AS total FROM clients WHERE status = 'Venta'{and_sql}", params)
    sales_count = cur.fetchone()["total"]

    cur.execute(f"SELECT COUNT(*) AS total FROM clients WHERE status = 'Perdido'{and_sql}", params)
    lost_count = cur.fetchone()["total"]

    cur.execute(f"SELECT COALESCE(SUM(sale_amount), 0) AS total FROM clients WHERE status = 'Venta'{and_sql}", params)
    total_sales_amount = float(cur.fetchone()["total"] or 0)

    conversion_rate = round((sales_count / total_clients * 100), 2) if total_clients else 0

    cur.execute(f"""
        SELECT AVG(response_time_seconds) AS avg_response
        FROM clients
        WHERE response_time_seconds IS NOT NULL {and_sql}
    """, params)
    avg_response_seconds = cur.fetchone()["avg_response"]
    avg_response_seconds = round(float(avg_response_seconds), 1) if avg_response_seconds is not None else None

    today = datetime.now(LIMA_TZ).strftime("%Y-%m-%d")
    cur.execute(f"""
        SELECT COUNT(*) AS total
        FROM clients
        WHERE LEFT(first_contact, 10) = %s {and_sql}
    """, [today] + params)
    new_today = cur.fetchone()["total"]

    cur.execute(f"""
        SELECT COALESCE(source, 'Sin atribución') AS source, COUNT(*) AS total
        FROM clients
        {where_sql}
        GROUP BY source
        ORDER BY total DESC
    """, params)
    by_source = cur.fetchall()

    cur.execute(f"""
        SELECT COALESCE(status, 'Sin estado') AS status, COUNT(*) AS total
        FROM clients
        {where_sql}
        GROUP BY status
        ORDER BY total DESC
    """, params)
    by_status = cur.fetchall()

    cur.execute(f"""
        SELECT COALESCE(lead_type, 'Nuevo') AS lead_type, COUNT(*) AS total
        FROM clients
        {where_sql}
        GROUP BY lead_type
        ORDER BY total DESC
    """, params)
    by_lead_type = cur.fetchall()

    cur.execute(f"""
        SELECT COALESCE(advisor, 'Sin asignar') AS advisor,
               COUNT(*) AS leads,
               COUNT(*) FILTER (WHERE status = 'Venta') AS sales,
               COALESCE(SUM(sale_amount) FILTER (WHERE status = 'Venta'), 0) AS amount
        FROM clients
        {where_sql}
        GROUP BY advisor
        ORDER BY leads DESC
    """, params)
    by_advisor = cur.fetchall()
    for row in by_advisor:
        row["amount"] = float(row["amount"] or 0)

    cur.execute(f"""
        SELECT COALESCE(campaign_name, 'Sin campaña') AS campaign,
               COUNT(*) AS leads,
               COUNT(*) FILTER (WHERE status = 'Venta') AS sales,
               COALESCE(SUM(sale_amount) FILTER (WHERE status = 'Venta'), 0) AS amount
        FROM clients
        {where_sql}
        GROUP BY campaign_name
        ORDER BY leads DESC
        LIMIT 12
    """, params)
    by_campaign = cur.fetchall()
    for row in by_campaign:
        row["amount"] = float(row["amount"] or 0)

    cur.execute(f"""
        SELECT LEFT(first_contact, 10) AS day, COUNT(*) AS total
        FROM clients
        WHERE first_contact IS NOT NULL {and_sql}
        GROUP BY day
        ORDER BY day DESC
        LIMIT 30
    """, params)
    by_day = list(reversed(cur.fetchall()))

    cur.execute("""
        SELECT LEFT(opened_at,7) AS month, COUNT(*) AS leads,
               COUNT(*) FILTER (WHERE status='Venta') AS sales,
               COALESCE(SUM(sale_amount) FILTER (WHERE status='Venta'),0) AS amount
        FROM opportunities WHERE opened_at IS NOT NULL
        GROUP BY month ORDER BY month
    """)
    by_month=cur.fetchall()
    for row in by_month:
        row["amount"]=float(row["amount"] or 0)

    cur.close()
    conn.close()

    return jsonify({
        "total_clients": total_clients,
        "total_messages": total_messages,
        "meta_ads_clients": meta_clients,
        "unattributed_clients": unattributed_clients,
        "organic_clients": unattributed_clients,
        "new_today": new_today,
        "sales_count": sales_count,
        "lost_count": lost_count,
        "total_sales_amount": total_sales_amount,
        "conversion_rate": conversion_rate,
        "avg_response_seconds": avg_response_seconds,
        "clients_by_source": by_source,
        "clients_by_status": by_status,
        "clients_by_lead_type": by_lead_type,
        "clients_by_advisor": by_advisor,
        "clients_by_campaign": by_campaign,
        "clients_by_day": by_day,
        "clients_by_month": by_month
    })


@app.post("/api/client/<phone_number>/status")
def update_client_status(phone_number):
    data = request.get_json(silent=True) or {}
    status = data.get("status", "Nuevo")

    allowed = {
        "Nuevo",
        "En atención",
        "Cotización / Seguimiento",
        "Venta",
        "Perdido",
        "Resuelto"
    }
    if status not in allowed:
        return jsonify({"ok": False, "error": "Estado no válido"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE clients
        SET status = %s, updated_at = %s
        WHERE phone_number = %s
    """, (status, now_lima(), phone_number))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"ok": True, "phone_number": phone_number, "status": status})



@app.post("/api/client/<phone_number>/mark-response")
def mark_manual_response(phone_number):
    data = request.get_json(silent=True) or {}

    try:
        response_at = normalize_manual_response(data.get("response_at"))
    except ValueError:
        return jsonify({"ok": False, "error": "Fecha/hora inválida"}), 400

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT first_contact
        FROM clients
        WHERE phone_number = %s
    """, (phone_number,))
    client = cur.fetchone()

    if not client:
        cur.close()
        conn.close()
        return jsonify({"ok": False, "error": "Cliente no encontrado"}), 404

    seconds = calculate_response_seconds(client.get("first_contact"), response_at)

    cur.execute("""
        UPDATE clients
        SET
            first_response_at = %s,
            last_advisor_reply = %s,
            response_time_seconds = %s,
            updated_at = %s
        WHERE phone_number = %s
    """, (
        response_at,
        response_at,
        seconds,
        now_lima(),
        phone_number
    ))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "ok": True,
        "first_response_at": response_at,
        "response_time_seconds": seconds
    })


@app.post("/api/client/<phone_number>/undo-response")
def undo_response(phone_number):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE clients
        SET
            first_response_at = NULL,
            last_advisor_reply = NULL,
            response_time_seconds = NULL,
            updated_at = %s
        WHERE phone_number = %s
    """, (now_lima(), phone_number))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "ok": True,
        "phone_number": phone_number,
        "response_reset": True
    })


@app.post("/api/client/<phone_number>/advisor")
def assign_advisor(phone_number):
    data = request.get_json(silent=True) or {}
    advisor = (data.get("advisor") or "").strip() or None

    allowed = {None, "Narly", "Raphaella", "Ursula", "Ecommerce", "Post Venta / Servicio Técnico", "Inmobiliaria"}
    if advisor not in allowed:
        return jsonify({"ok": False, "error": "Asesora no válida"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE clients
        SET advisor = %s, updated_at = %s
        WHERE phone_number = %s
    """, (advisor, now_lima(), phone_number))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"ok": True, "phone_number": phone_number, "advisor": advisor})


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000"))
    )
