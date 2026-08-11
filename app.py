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
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'Organico'")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS campaign_name TEXT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS ad_id TEXT")
    cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS direction TEXT DEFAULT 'incoming'")

    conn.commit()
    cur.close()
    conn.close()


init_db()


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


def extract_referral(msg):
    referral = msg.get("referral")

    if not referral:
        return {
            "source": "Organico",
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
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM clients
        WHERE phone_number = %s
    """, (phone_number,))
    existing = cur.fetchone()

    current_time = now_lima()

    if existing:
        final_source = existing.get("source") or "Organico"
        if source == "Meta Ads":
            final_source = "Meta Ads"

        cur.execute("""
            UPDATE clients
            SET
                contact_name = COALESCE(%s, contact_name),
                last_contact = %s,
                total_messages = COALESCE(total_messages, 0) + 1,
                source = %s,
                campaign_name = %s,
                ad_id = %s,
                referral_json = %s,
                updated_at = %s
            WHERE phone_number = %s
        """, (
            contact_name,
            timestamp,
            final_source,
            campaign_name or existing.get("campaign_name"),
            ad_id or existing.get("ad_id"),
            referral_json or existing.get("referral_json"),
            current_time,
            phone_number
        ))
    else:
        cur.execute("""
            INSERT INTO clients(
                phone_number, contact_name,
                first_contact, last_contact, total_messages,
                source, campaign_name, ad_id, referral_json,
                status, advisor, sale_amount, loss_reason,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            phone_number,
            contact_name,
            timestamp,
            timestamp,
            1,
            source,
            campaign_name,
            ad_id,
            referral_json,
            "Nuevo",
            None,
            0,
            None,
            current_time,
            current_time
        ))

    conn.commit()
    cur.close()
    conn.close()


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
    return """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CRM Kitchen Factory</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            min-height: 100vh;
            color: #e5e7eb;
            background:
                radial-gradient(circle at top left, rgba(255,255,255,.08), transparent 28%),
                radial-gradient(circle at bottom right, rgba(255,255,255,.05), transparent 30%),
                linear-gradient(135deg, #0f172a 0%, #111827 48%, #1f2937 100%);
            background-attachment: fixed;
        }
        .wrap {
            max-width: 1500px;
            margin: 0 auto;
            padding: 28px 24px 50px;
        }
        h1 { margin: 0 0 6px; }
        .sub { color: #cbd5e1; margin-bottom: 22px; }
        .cards {
            display: grid;
            grid-template-columns: repeat(5, minmax(160px, 1fr));
            gap: 14px;
            margin-bottom: 20px;
        }
        .card {
            background: rgba(255,255,255,.08);
            border: 1px solid rgba(255,255,255,.12);
            border-radius: 16px;
            padding: 18px;
            backdrop-filter: blur(12px);
            box-shadow: 0 12px 30px rgba(0,0,0,.18);
        }
        .card .label { color: #cbd5e1; font-size: 13px; }
        .card .value { font-size: 30px; font-weight: 700; margin-top: 8px; }
        .panel {
            background: rgba(255,255,255,.08);
            border: 1px solid rgba(255,255,255,.12);
            border-radius: 16px;
            padding: 18px;
            backdrop-filter: blur(12px);
            box-shadow: 0 12px 30px rgba(0,0,0,.18);
            margin-bottom: 20px;
        }
        .filters {
            display: grid;
            grid-template-columns: 1.4fr 1fr 1fr 1fr;
            gap: 10px;
            margin-bottom: 14px;
        }
        input, select {
            width: 100%;
            padding: 9px 10px;
            border: 1px solid rgba(255,255,255,.16);
            border-radius: 9px;
            background: rgba(255,255,255,.94);
            color: #111827;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        th, td {
            padding: 10px 8px;
            border-bottom: 1px solid rgba(255,255,255,.10);
            text-align: left;
            vertical-align: top;
        }
        th {
            background: #111827;
            color: #f8fafc;
            position: sticky;
            top: 0;
            z-index: 1;
        }
        .table-wrap {
            max-height: 620px;
            overflow: auto;
            border: 1px solid rgba(255,255,255,.10);
            border-radius: 12px;
            background: rgba(15,23,42,.45);
        }
        .badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 999px;
            background: rgba(255,255,255,.12);
            color: #f8fafc;
            font-size: 12px;
        }
        .muted { color: #cbd5e1; }
        .mini {
            padding: 6px 8px;
            font-size: 12px;
        }
        @media (max-width: 1000px) {
            .cards { grid-template-columns: repeat(2, 1fr); }
            .filters { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
<div class="wrap">
    <h1 style='font-size:34px; letter-spacing:.2px;'>CRM Kitchen Factory</h1>
    <div class="sub">Seguimiento de clientes y conversaciones recibidas por WhatsApp</div>

    <div class="cards">
        <div class="card"><div class="label">Clientes totales</div><div class="value" id="totalClients">0</div></div>
        <div class="card"><div class="label">Mensajes</div><div class="value" id="totalMessages">0</div></div>
        <div class="card"><div class="label">Meta Ads</div><div class="value" id="metaClients">0</div></div>
        <div class="card"><div class="label">Orgánicos</div><div class="value" id="organicClients">0</div></div>
        <div class="card"><div class="label">Nuevos hoy</div><div class="value" id="todayClients">0</div></div>
    </div>

    <div class="panel">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
            <h2 style="margin:0">Clientes</h2>
            <a href="/export/clients.xlsx"
               style="text-decoration:none;background:#f8fafc;color:#111827;padding:10px 14px;border-radius:9px;font-weight:700;">
               Descargar Excel
            </a>
        </div>

        <div class="filters">
            <input id="search" placeholder="Buscar por nombre o teléfono">
            <select id="sourceFilter">
                <option value="">Todos los orígenes</option>
                <option value="Organico">Orgánico</option>
                <option value="Meta Ads">Meta Ads</option>
            </select>
            <select id="statusFilter">
                <option value="">Todos los estados</option>
                <option>Nuevo</option>
                <option>En atención</option>
                <option>Cotización / Seguimiento</option>
                <option>Venta</option>
                <option>Perdido</option>
            </select>
            <input id="campaignFilter" placeholder="Filtrar por campaña">
        </div>

        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Cliente</th>
                        <th>Teléfono</th>
                        <th>Origen</th>
                        <th>Campaña</th>
                        <th>Primer contacto</th>
                        <th>Último mensaje cliente</th>
                        <th>1ra respuesta asesora</th>
                        <th>Tiempo respuesta</th>
                        <th>Mensajes</th>
                        <th>Estado</th>
                        <th>Asesor</th>
                        <th>Monto venta</th>
                        <th>Motivo pérdida</th>
                    </tr>
                </thead>
                <tbody id="clientsBody"></tbody>
            </table>
        </div>
    </div>
</div>

<script>
let clients = [];

function esc(v) {
    return String(v ?? "")
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;");
}

function todayLima() {
    return new Intl.DateTimeFormat("en-CA", {
        timeZone: "America/Lima",
        year: "numeric",
        month: "2-digit",
        day: "2-digit"
    }).format(new Date());
}

function formatLimaDate(value) {
    if (!value) return "—";

    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;

    return new Intl.DateTimeFormat("es-PE", {
        timeZone: "America/Lima",
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: true
    }).format(d);
}

function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || seconds === "") return "Pendiente";
    const s = Number(seconds);
    if (!Number.isFinite(s)) return "—";
    if (s < 60) return `${s} s`;
    const min = Math.floor(s / 60);
    if (min < 60) return `${min} min ${s % 60} s`;
    const h = Math.floor(min / 60);
    return `${h} h ${min % 60} min`;
}

async function loadDashboard() {
    const r = await fetch("/api/dashboard");
    const d = await r.json();

    document.getElementById("totalClients").textContent = d.total_clients ?? 0;
    document.getElementById("totalMessages").textContent = d.total_messages ?? 0;
    document.getElementById("metaClients").textContent = d.meta_ads_clients ?? 0;
    document.getElementById("organicClients").textContent = d.organic_clients ?? 0;

    const today = todayLima();
    const item = (d.clients_by_day || []).find(x => x.day === today);
    document.getElementById("todayClients").textContent = item ? item.total : 0;
}

async function loadClients() {
    const r = await fetch("/api/clients");
    clients = await r.json();
    renderClients();
}

function renderClients() {
    const q = document.getElementById("search").value.toLowerCase().trim();
    const source = document.getElementById("sourceFilter").value;
    const status = document.getElementById("statusFilter").value;
    const campaign = document.getElementById("campaignFilter").value.toLowerCase().trim();

    const rows = clients.filter(c => {
        const matchesSearch =
            !q ||
            String(c.contact_name || "").toLowerCase().includes(q) ||
            String(c.phone_number || "").toLowerCase().includes(q);

        const matchesSource = !source || c.source === source;
        const matchesStatus = !status || c.status === status;
        const matchesCampaign =
            !campaign ||
            String(c.campaign_name || "").toLowerCase().includes(campaign);

        return matchesSearch && matchesSource && matchesStatus && matchesCampaign;
    });

    document.getElementById("clientsBody").innerHTML = rows.map(c => `
        <tr>
            <td><strong>${esc(c.contact_name || "Sin nombre")}</strong></td>
            <td>${esc(c.phone_number)}</td>
            <td><span class="badge">${esc(c.source || "Sin identificar")}</span></td>
            <td>${esc(c.campaign_name || "—")}</td>
            <td class="muted">${esc(formatLimaDate(c.first_contact))}</td>
            <td class="muted">${esc(formatLimaDate(c.last_contact))}</td>
            <td class="muted">${esc(formatLimaDate(c.first_response_at))}</td>
            <td><strong>${esc(formatDuration(c.response_time_seconds))}</strong></td>
            <td>${esc(c.total_messages || 0)}</td>
            <td>
                <select class="mini" onchange="updateStatus('${esc(c.phone_number)}', this.value)">
                    ${["Nuevo","En atención","Cotización / Seguimiento","Venta","Perdido"].map(s =>
                        `<option ${c.status === s ? "selected" : ""}>${s}</option>`
                    ).join("")}
                </select>
            </td>
            <td>
                <select class="mini" onchange="updateAdvisor('${esc(c.phone_number)}', this.value)">
                    <option value="" ${!c.advisor ? "selected" : ""}>Sin asignar</option>
                    ${["Narly","Raphaella","Ursula"].map(a =>
                        `<option value="${a}" ${c.advisor === a ? "selected" : ""}>${a}</option>`
                    ).join("")}
                </select>
            </td>
            <td>
                <input class="mini"
                       type="number"
                       min="0"
                       step="0.01"
                       value="${esc(c.sale_amount || 0)}"
                       onchange="updateSale('${esc(c.phone_number)}', this.value)">
            </td>
            <td>
                <select class="mini" onchange="updateLossReason('${esc(c.phone_number)}', this.value)">
                    <option value="" ${!c.loss_reason ? "selected" : ""}>—</option>
                    ${["No respondió","Precio","Sin stock","No interesado","Compró en otro lugar","Fuera de cobertura","Otro"].map(r =>
                        `<option value="${r}" ${c.loss_reason === r ? "selected" : ""}>${r}</option>`
                    ).join("")}
                </select>
            </td>
        </tr>
    `).join("");
}

async function updateStatus(phone, status) {
    await fetch(`/api/client/${encodeURIComponent(phone)}/status`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({status})
    });
    await loadClients();
    await loadDashboard();
}

async function updateAdvisor(phone, advisor) {
    await fetch(`/api/client/${encodeURIComponent(phone)}/advisor`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({advisor})
    });
    await loadClients();
}

async function updateSale(phone, sale_amount) {
    await fetch(`/api/client/${encodeURIComponent(phone)}/sale`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({sale_amount})
    });
    await loadClients();
}

async function updateLossReason(phone, loss_reason) {
    await fetch(`/api/client/${encodeURIComponent(phone)}/loss-reason`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({loss_reason})
    });
    await loadClients();
}

["search","sourceFilter","statusFilter","campaignFilter"].forEach(id => {
    document.getElementById(id).addEventListener("input", renderClients);
    document.getElementById(id).addEventListener("change", renderClients);
});

loadDashboard();
loadClients();

setInterval(() => {
    loadDashboard();
    loadClients();
}, 15000);
</script>
</body>
</html>
    """, 200



# =========================================================
# API - CLIENTES
# =========================================================

@app.get("/api/clients")
def api_clients():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id, phone_number, contact_name,
            first_contact, last_contact, total_messages,
            source, campaign_name, ad_id,
            status, advisor,
            COALESCE(sale_amount, 0) AS sale_amount,
            loss_reason,
            first_response_at,
            last_advisor_reply,
            response_time_seconds
        FROM clients
        ORDER BY last_contact DESC NULLS LAST
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify(rows)


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
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            contact_name,
            phone_number,
            source,
            campaign_name,
            first_contact,
            last_contact,
            first_response_at,
            response_time_seconds,
            total_messages,
            status,
            advisor,
            sale_amount,
            loss_reason
        FROM clients
        ORDER BY last_contact DESC NULLS LAST
    """)
    rows = cur.fetchall()

    cur.close()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Clientes CRM"

    headers = [
        "Cliente",
        "Teléfono",
        "Origen",
        "Campaña",
        "Primer contacto",
        "Último mensaje cliente",
        "Primera respuesta asesora",
        "Tiempo de respuesta (seg)",
        "Mensajes",
        "Estado",
        "Asesora",
        "Monto de venta",
        "Motivo de pérdida"
    ]
    ws.append(headers)

    for row in rows:
        ws.append([
            row.get("contact_name"),
            row.get("phone_number"),
            row.get("source"),
            row.get("campaign_name"),
            row.get("first_contact"),
            row.get("last_contact"),
            row.get("first_response_at"),
            row.get("response_time_seconds"),
            row.get("total_messages"),
            row.get("status"),
            row.get("advisor"),
            float(row.get("sale_amount") or 0),
            row.get("loss_reason")
        ])

    for cell in ws[1]:
        cell.font = cell.font.copy(bold=True)

    widths = {
        "A": 24, "B": 18, "C": 16, "D": 28, "E": 24, "F": 24,
        "G": 24, "H": 22, "I": 12, "J": 22, "K": 16, "L": 16, "M": 24
    }
    for col, width in widths.items():
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
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS total FROM clients")
    total_clients = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) AS total FROM messages")
    total_messages = cur.fetchone()["total"]

    cur.execute("SELECT COUNT(*) AS total FROM clients WHERE source = 'Meta Ads'")
    meta_clients = cur.fetchone()["total"]

    cur.execute("""
        SELECT COUNT(*) AS total
        FROM clients
        WHERE source != 'Meta Ads' OR source IS NULL
    """)
    organic_clients = cur.fetchone()["total"]

    cur.execute("""
        SELECT COALESCE(source, 'Sin identificar') AS source, COUNT(*) AS total
        FROM clients
        GROUP BY source
        ORDER BY total DESC
    """)
    by_source = cur.fetchall()

    cur.execute("""
        SELECT COALESCE(campaign_name, 'Sin campaña') AS campaign, COUNT(*) AS total
        FROM clients
        GROUP BY campaign_name
        ORDER BY total DESC
    """)
    by_campaign = cur.fetchall()

    cur.execute("""
        SELECT LEFT(first_contact, 10) AS day, COUNT(*) AS total
        FROM clients
        WHERE first_contact IS NOT NULL
        GROUP BY day
        ORDER BY day DESC
        LIMIT 30
    """)
    by_day = cur.fetchall()

    cur.execute("""
        SELECT SUBSTRING(first_contact FROM 12 FOR 2) AS hour, COUNT(*) AS total
        FROM clients
        WHERE first_contact IS NOT NULL
        GROUP BY hour
        ORDER BY hour
    """)
    by_hour = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify({
        "total_clients": total_clients,
        "total_messages": total_messages,
        "meta_ads_clients": meta_clients,
        "organic_clients": organic_clients,
        "clients_by_source": by_source,
        "clients_by_campaign": by_campaign,
        "clients_by_day": by_day,
        "clients_by_hour": by_hour
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
        "Perdido"
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


@app.post("/api/client/<phone_number>/advisor")
def assign_advisor(phone_number):
    data = request.get_json(silent=True) or {}
    advisor = (data.get("advisor") or "").strip() or None

    allowed = {None, "Narly", "Raphaella", "Ursula"}
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
