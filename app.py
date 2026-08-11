import os
import json
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify


app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kf_crm_verify_2026")
DB_PATH = os.getenv("DB_PATH", "crm.db")

LIMA_TZ = ZoneInfo("America/Lima")


# =========================================================
# BASE DE DATOS
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, definition):
    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = [row["name"] for row in columns]

    if column not in existing:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db():
    conn = get_db()

    # Guarda absolutamente todos los eventos recibidos de Meta
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)

    # Clientes / leads del CRM
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clients(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

            created_at TEXT,
            updated_at TEXT
        )
    """)

    # Mensajes individuales
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

    # Agregamos columnas nuevas si la tabla messages ya existía
    ensure_column(
        conn,
        "messages",
        "source",
        "TEXT DEFAULT 'Organico'"
    )

    ensure_column(
        conn,
        "messages",
        "campaign_name",
        "TEXT"
    )

    ensure_column(
        conn,
        "messages",
        "ad_id",
        "TEXT"
    )

    ensure_column(
        conn,
        "messages",
        "direction",
        "TEXT DEFAULT 'incoming'"
    )

    conn.commit()
    conn.close()


# IMPORTANTE:
# Render ejecuta "gunicorn app:app".
# Por eso debemos crear las tablas cuando app.py se importa.
init_db()


# =========================================================
# UTILIDADES
# =========================================================

def utc_to_lima(timestamp):
    if not timestamp:
        return None

    try:
        dt_utc = datetime.fromtimestamp(
            int(timestamp),
            tz=timezone.utc
        )

        dt_lima = dt_utc.astimezone(LIMA_TZ)

        return dt_lima.isoformat()

    except Exception:
        return str(timestamp)


def now_lima():
    return datetime.now(LIMA_TZ).isoformat()


def extract_referral(msg):
    """
    Si el cliente llegó desde un anuncio de Meta / Click to WhatsApp,
    WhatsApp puede mandar información dentro de referral.
    """

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
    source_url = referral.get("source_url")
    ctwa_clid = referral.get("ctwa_clid")

    campaign_name = headline or body or "Anuncio Meta"

    return {
        "source": "Meta Ads",
        "campaign_name": campaign_name,
        "ad_id": source_id or ctwa_clid,
        "referral_json": json.dumps(
            referral,
            ensure_ascii=False
        )
    }


# =========================================================
# CLIENTES
# =========================================================

def update_client(
    phone_number,
    contact_name,
    timestamp,
    source,
    campaign_name,
    ad_id,
    referral_json
):
    conn = get_db()

    existing = conn.execute("""
        SELECT *
        FROM clients
        WHERE phone_number = ?
    """, (phone_number,)).fetchone()

    current_time = now_lima()

    if existing:
        # Si un cliente primero fue orgánico y luego llega por campaña,
        # conservamos el origen publicitario nuevo.
        final_source = existing["source"]

        if source == "Meta Ads":
            final_source = "Meta Ads"

        final_campaign = (
            campaign_name
            or existing["campaign_name"]
        )

        final_ad_id = (
            ad_id
            or existing["ad_id"]
        )

        final_referral = (
            referral_json
            or existing["referral_json"]
        )

        conn.execute("""
            UPDATE clients
            SET
                contact_name = COALESCE(?, contact_name),
                last_contact = ?,
                total_messages = total_messages + 1,

                source = ?,
                campaign_name = ?,
                ad_id = ?,
                referral_json = ?,

                updated_at = ?

            WHERE phone_number = ?
        """, (
            contact_name,
            timestamp,
            final_source,
            final_campaign,
            final_ad_id,
            final_referral,
            current_time,
            phone_number
        ))

    else:
        conn.execute("""
            INSERT INTO clients(
                phone_number,
                contact_name,

                first_contact,
                last_contact,
                total_messages,

                source,
                campaign_name,
                ad_id,
                referral_json,

                status,
                advisor,

                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

            current_time,
            current_time
        ))

    conn.commit()
    conn.close()


# =========================================================
# MENSAJES
# =========================================================

def save_message(value):
    contacts = value.get("contacts") or []

    contact_name = None

    if contacts:
        contact_name = (
            contacts[0].get("profile") or {}
        ).get("name")

    metadata = value.get("metadata") or {}

    phone_number_id = metadata.get("phone_number_id")

    for msg in value.get("messages") or []:

        message_id = msg.get("id")
        from_number = msg.get("from")
        message_type = msg.get("type")

        body = None

        if message_type == "text":
            body = (
                msg.get("text") or {}
            ).get("body")

        elif message_type:
            body = json.dumps(
                msg.get(message_type) or {},
                ensure_ascii=False
            )

        timestamp = utc_to_lima(
            msg.get("timestamp")
        )

        referral_data = extract_referral(msg)

        conn = get_db()

        conn.execute("""
            INSERT OR IGNORE INTO messages(
                wa_message_id,
                phone_number_id,
                from_number,
                contact_name,

                message_type,
                body,
                timestamp,

                referral_json,
                raw_json,

                source,
                campaign_name,
                ad_id,
                direction
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message_id,
            phone_number_id,
            from_number,
            contact_name,

            message_type,
            body,
            timestamp,

            referral_data["referral_json"],

            json.dumps(
                msg,
                ensure_ascii=False
            ),

            referral_data["source"],
            referral_data["campaign_name"],
            referral_data["ad_id"],

            "incoming"
        ))

        conn.commit()
        conn.close()

        if from_number:
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
# WEBHOOK META
# =========================================================

@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": "Kitchen Factory WhatsApp CRM",
        "status": "online"
    })



# =========================================================
# POLÍTICA DE PRIVACIDAD
# =========================================================

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

    if (
        mode == "subscribe"
        and token == VERIFY_TOKEN
    ):
        return challenge or "", 200

    return "Forbidden", 403


@app.post("/webhook/whatsapp")
def receive_webhook():

    payload = request.get_json(
        silent=True
    ) or {}

    # Guardamos evento completo
    conn = get_db()

    conn.execute("""
        INSERT INTO events(
            received_at,
            payload
        )
        VALUES (?, ?)
    """, (
        now_lima(),
        json.dumps(
            payload,
            ensure_ascii=False
        )
    ))

    conn.commit()
    conn.close()

    # Procesamos mensajes
    for entry in payload.get("entry") or []:

        for change in entry.get("changes") or []:

            if change.get("field") == "messages":

                value = change.get("value") or {}

                save_message(value)

    return jsonify({
        "status": "received"
    }), 200



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
            background: #f4f6f8;
            color: #1f2937;
        }
        .wrap {
            max-width: 1500px;
            margin: 0 auto;
            padding: 24px;
        }
        h1 { margin: 0 0 6px; }
        .sub { color: #6b7280; margin-bottom: 22px; }
        .cards {
            display: grid;
            grid-template-columns: repeat(5, minmax(160px, 1fr));
            gap: 14px;
            margin-bottom: 20px;
        }
        .card {
            background: white;
            border-radius: 12px;
            padding: 18px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08);
        }
        .card .label { color: #6b7280; font-size: 13px; }
        .card .value { font-size: 30px; font-weight: 700; margin-top: 8px; }
        .panel {
            background: white;
            border-radius: 12px;
            padding: 18px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08);
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
            border: 1px solid #d1d5db;
            border-radius: 8px;
            background: white;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        th, td {
            padding: 10px 8px;
            border-bottom: 1px solid #e5e7eb;
            text-align: left;
            vertical-align: top;
        }
        th {
            background: #f9fafb;
            position: sticky;
            top: 0;
            z-index: 1;
        }
        .table-wrap {
            max-height: 620px;
            overflow: auto;
            border: 1px solid #e5e7eb;
            border-radius: 10px;
        }
        .badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 999px;
            background: #eef2f7;
            font-size: 12px;
        }
        .muted { color: #6b7280; }
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
    <h1>CRM Kitchen Factory</h1>
    <div class="sub">Seguimiento de clientes y conversaciones recibidas por WhatsApp</div>

    <div class="cards">
        <div class="card"><div class="label">Clientes totales</div><div class="value" id="totalClients">0</div></div>
        <div class="card"><div class="label">Mensajes</div><div class="value" id="totalMessages">0</div></div>
        <div class="card"><div class="label">Meta Ads</div><div class="value" id="metaClients">0</div></div>
        <div class="card"><div class="label">Orgánicos</div><div class="value" id="organicClients">0</div></div>
        <div class="card"><div class="label">Nuevos hoy</div><div class="value" id="todayClients">0</div></div>
    </div>

    <div class="panel">
        <h2 style="margin-top:0">Clientes</h2>

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
                        <th>Último contacto</th>
                        <th>Mensajes</th>
                        <th>Estado</th>
                        <th>Asesor</th>
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
            <td class="muted">${esc(c.first_contact || "—")}</td>
            <td class="muted">${esc(c.last_contact || "—")}</td>
            <td>${esc(c.total_messages || 0)}</td>
            <td>
                <select class="mini" onchange="updateStatus('${esc(c.phone_number)}', this.value)">
                    ${["Nuevo","En atención","Venta","Perdido"].map(s =>
                        `<option ${c.status === s ? "selected" : ""}>${s}</option>`
                    ).join("")}
                </select>
            </td>
            <td>
                <input class="mini"
                       value="${esc(c.advisor || "")}"
                       placeholder="Asesor"
                       onchange="updateAdvisor('${esc(c.phone_number)}', this.value)">
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

    rows = conn.execute("""
        SELECT
            id,
            phone_number,
            contact_name,

            first_contact,
            last_contact,
            total_messages,

            source,
            campaign_name,
            ad_id,

            status,
            advisor

        FROM clients

        ORDER BY last_contact DESC
    """).fetchall()

    conn.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


# =========================================================
# API - MENSAJES
# =========================================================

@app.get("/api/messages")
def api_messages():

    conn = get_db()

    rows = conn.execute("""
        SELECT
            id,
            wa_message_id,

            phone_number_id,
            from_number,
            contact_name,

            message_type,
            body,
            timestamp,

            source,
            campaign_name,
            ad_id,
            referral_json,

            direction

        FROM messages

        ORDER BY id DESC

        LIMIT 500
    """).fetchall()

    conn.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


# =========================================================
# API - EVENTOS
# =========================================================

@app.get("/api/events")
def api_events():

    conn = get_db()

    rows = conn.execute("""
        SELECT
            id,
            received_at,
            payload

        FROM events

        ORDER BY id DESC

        LIMIT 100
    """).fetchall()

    conn.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


# =========================================================
# API - DASHBOARD
# =========================================================

@app.get("/api/dashboard")
def api_dashboard():

    conn = get_db()

    total_clients = conn.execute("""
        SELECT COUNT(*)
        FROM clients
    """).fetchone()[0]

    total_messages = conn.execute("""
        SELECT COUNT(*)
        FROM messages
    """).fetchone()[0]

    meta_clients = conn.execute("""
        SELECT COUNT(*)
        FROM clients
        WHERE source = 'Meta Ads'
    """).fetchone()[0]

    organic_clients = conn.execute("""
        SELECT COUNT(*)
        FROM clients
        WHERE source != 'Meta Ads'
           OR source IS NULL
    """).fetchone()[0]

    by_source = conn.execute("""
        SELECT
            COALESCE(source, 'Sin identificar') AS source,
            COUNT(*) AS total

        FROM clients

        GROUP BY source

        ORDER BY total DESC
    """).fetchall()

    by_campaign = conn.execute("""
        SELECT
            COALESCE(
                campaign_name,
                'Sin campaña'
            ) AS campaign,

            COUNT(*) AS total

        FROM clients

        GROUP BY campaign_name

        ORDER BY total DESC
    """).fetchall()

    by_day = conn.execute("""
        SELECT
            substr(first_contact, 1, 10) AS day,
            COUNT(*) AS total

        FROM clients

        WHERE first_contact IS NOT NULL

        GROUP BY day

        ORDER BY day DESC

        LIMIT 30
    """).fetchall()

    by_hour = conn.execute("""
        SELECT
            substr(first_contact, 12, 2) AS hour,
            COUNT(*) AS total

        FROM clients

        WHERE first_contact IS NOT NULL

        GROUP BY hour

        ORDER BY hour
    """).fetchall()

    conn.close()

    return jsonify({
        "total_clients": total_clients,
        "total_messages": total_messages,

        "meta_ads_clients": meta_clients,
        "organic_clients": organic_clients,

        "clients_by_source": [
            dict(row)
            for row in by_source
        ],

        "clients_by_campaign": [
            dict(row)
            for row in by_campaign
        ],

        "clients_by_day": [
            dict(row)
            for row in by_day
        ],

        "clients_by_hour": [
            dict(row)
            for row in by_hour
        ]
    })


# =========================================================
# CAMBIAR ESTADO DEL CLIENTE
# =========================================================

@app.post("/api/client/<phone_number>/status")
def update_client_status(phone_number):

    data = request.get_json(
        silent=True
    ) or {}

    status = data.get(
        "status",
        "Nuevo"
    )

    conn = get_db()

    conn.execute("""
        UPDATE clients

        SET
            status = ?,
            updated_at = ?

        WHERE phone_number = ?
    """, (
        status,
        now_lima(),
        phone_number
    ))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "phone_number": phone_number,
        "status": status
    })


# =========================================================
# ASIGNAR ASESOR
# =========================================================

@app.post("/api/client/<phone_number>/advisor")
def assign_advisor(phone_number):

    data = request.get_json(
        silent=True
    ) or {}

    advisor = data.get("advisor")

    conn = get_db()

    conn.execute("""
        UPDATE clients

        SET
            advisor = ?,
            updated_at = ?

        WHERE phone_number = ?
    """, (
        advisor,
        now_lima(),
        phone_number
    ))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "phone_number": phone_number,
        "advisor": advisor
    })


# =========================================================
# INICIO LOCAL
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8000"
            )
        )
    )
