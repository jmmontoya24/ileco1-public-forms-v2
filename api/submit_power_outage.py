import json
import os
import re
import html
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import psycopg2
from psycopg2.extras import RealDictCursor


def get_db():
    return psycopg2.connect(
        host=os.environ["CLOUD_DB_HOST"],
        port=os.environ.get("CLOUD_DB_PORT", "6543"),
        database=os.environ["CLOUD_DB_NAME"],
        user=os.environ["CLOUD_DB_USER"],
        password=os.environ["CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=10,
    )


def classify_priority(details):
    if not details:
        return "HIGH"
    critical_keywords = [
        "fire", "explosion", "burning", "smoke", "accident",
        "fallen wire", "electric shock", "live wire", "transformer burst",
        "emergency", "danger", "hazard", "sparking", "exposed wire",
        "electrocuted", "injured", "death", "pole down", "wire down",
        "short circuit", "arcing", "flames",
    ]
    dl = details.lower()
    for kw in critical_keywords:
        if kw in dl:
            return "CRITICAL"
    return "HIGH"


class handler(BaseHTTPRequestHandler):

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Content-Type", "application/json")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
        except Exception:
            self._respond(400, {"success": False, "error": "Invalid JSON"})
            return

        # ── Validate required fields ──────────────────────────────────────
        required = ["full_name", "contact_number", "address", "details", "town", "barangay"]
        for field in required:
            if not data.get(field, "").strip():
                self._respond(400, {"success": False, "error": f"Missing: {field}"})
                return

        contact_number = data["contact_number"].strip()
        if not re.match(r"^09\d{9}$", contact_number):
            self._respond(400, {"success": False, "error": "Contact number must be 09XXXXXXXXX"})
            return

        try:
            lat = float(data["latitude"])
            lng = float(data["longitude"])
            if not (4.0 <= lat <= 21.0 and 116.0 <= lng <= 127.0):
                raise ValueError("Out of PH bounds")
        except Exception:
            self._respond(400, {"success": False, "error": "Invalid coordinates"})
            return

        # ── Sanitize ──────────────────────────────────────────────────────
        full_name    = html.escape(data["full_name"].strip())
        address      = html.escape(data["address"].strip())
        town         = html.escape(data["town"].strip())
        barangay     = html.escape(data["barangay"].strip())
        details      = html.escape(data["details"].strip())
        landmark     = html.escape(data.get("landmark", "").strip())
        account_num  = html.escape(data.get("account_number", "").strip())
        email        = html.escape(data.get("email", "").strip())
        incident_type = data.get("incident_type", "power_outage")
        affected_area = data.get("affected_area", "unknown")
        incident_time = data.get("incident_time")
        duration      = data.get("duration")
        source        = data.get("source", "Web Form - Vercel")

        priority = classify_priority(details)
        critical_types = ["fallen_wire", "fire_hazard", "transformer_issue", "sparking"]
        if incident_type in critical_types:
            priority = "CRITICAL"

        # ── Database ──────────────────────────────────────────────────────
        conn = cur = None
        try:
            conn = get_db()
            cur  = conn.cursor(cursor_factory=RealDictCursor)

            # Duplicate check (same phone + location within 5 min)
            cur.execute("""
                SELECT report_id FROM outage_reports
                WHERE contact_number = %s
                AND ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,
                    100
                )
                AND timestamp > NOW() - INTERVAL '5 minutes'
                LIMIT 1
            """, (contact_number, lng, lat))
            if cur.fetchone():
                self._respond(429, {"success": False, "error": "Duplicate report detected."})
                return

            # Find or create incident
            cur.execute("""
                SELECT incident_id, priority FROM outage_incidents
                WHERE barangay=%s AND town=%s AND status IN ('NEW','ASSIGNED')
                ORDER BY first_report_time DESC LIMIT 1
            """, (barangay, town))
            existing = cur.fetchone()

            if existing:
                incident_id  = existing["incident_id"]
                new_priority = "CRITICAL" if priority == "CRITICAL" else existing["priority"]
                cur.execute("""
                    UPDATE outage_incidents
                    SET report_count = report_count + 1,
                        priority     = %s,
                        last_report_time = NOW(),
                        updated_at       = NOW()
                    WHERE incident_id = %s
                """, (new_priority, incident_id))
            else:
                job_order_id = (
                    f"JO-{datetime.now():%Y%m%d}-"
                    f"{barangay[:3].upper()}-"
                    f"{uuid.uuid4().hex[:4].upper()}"
                )
                cur.execute("""
                    INSERT INTO outage_incidents
                    (incident_type, barangay, town, report_count,
                     confidence_level, status, priority,
                     first_report_time, last_report_time, job_order_id,
                     geom, created_at, updated_at)
                    VALUES (%s,%s,%s,1,'UNVERIFIED','NEW',%s,
                            NOW(),NOW(),%s,
                            ST_SetSRID(ST_MakePoint(%s,%s),4326),
                            NOW(),NOW())
                    RETURNING incident_id
                """, (incident_type, barangay, town, priority, job_order_id, lng, lat))
                incident_id = cur.fetchone()["incident_id"]

            # Insert report
            cur.execute("""
                INSERT INTO outage_reports
                (incident_id, full_name, contact_number, email, account_number,
                 address, town, barangay, details, landmark,
                 incident_type, affected_area, incident_time, duration,
                 priority, status, source, timestamp, status_changed_at, geom)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        'NEW',%s,NOW(),NOW(),
                        ST_SetSRID(ST_MakePoint(%s,%s),4326))
                RETURNING report_id
            """, (
                incident_id, full_name, contact_number, email, account_num,
                address, town, barangay, details, landmark,
                incident_type, affected_area, incident_time, duration,
                priority, source, lng, lat,
            ))
            report_id = cur.fetchone()["report_id"]
            conn.commit()

            self._respond(201, {
                "success":     True,
                "message":     "Report submitted successfully",
                "report_id":   report_id,
                "incident_id": incident_id,
                "priority":    priority,
            })

        except Exception as e:
            if conn:
                conn.rollback()
            self._respond(500, {"success": False, "error": str(e)})
        finally:
            if cur:  cur.close()
            if conn: conn.close()

    def _respond(self, status, body):
        self.send_response(status)
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())