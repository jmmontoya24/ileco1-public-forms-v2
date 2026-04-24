# api/submit_meter_concern.py
import json
import os
import html as html_lib
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import psycopg2
from psycopg2.extras import RealDictCursor


def get_conn():
    return psycopg2.connect(
        host=os.environ["CLOUD_DB_HOST"],
        port=int(os.environ.get("CLOUD_DB_PORT", "6543")),
        database=os.environ["CLOUD_DB_NAME"],
        user=os.environ["CLOUD_DB_USER"],
        password=os.environ["CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=10,
    )


def cors_headers():
    return [
        ("Access-Control-Allow-Origin",  "*"),
        ("Access-Control-Allow-Headers", "Content-Type"),
        ("Access-Control-Allow-Methods", "POST, OPTIONS"),
        ("Content-Type", "application/json"),
    ]


def generate_reference():
    return f"MC-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in cors_headers():
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self):
        conn = cur = None
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 500_000:  # 500 KB JSON limit (files are already in Supabase)
                return self._send(413, {"success": False, "error": "Payload too large"})

            body = json.loads(self.rfile.read(length))

            # ── Validate required fields ──────────────────────
            required = ["account_number", "consumer_name", "contact_number",
                        "meter_number", "service_address", "barangay",
                        "concern_type", "date_noticed"]
            for f in required:
                if not str(body.get(f, "")).strip():
                    return self._send(400, {
                        "success": False,
                        "error": f"Missing required field: {f}"
                    })

            uploaded_files = body.get("uploaded_files", [])
            if not uploaded_files:
                return self._send(400, {
                    "success": False,
                    "error": "At least one photo is required"
                })

            # ── Sanitize ──────────────────────────────────────
            esc = html_lib.escape
            concern_type = body["concern_type"]

            valid_concern_types = [
                "not_working", "high_consumption", "running_fast_slow",
                "noise_burning", "tampered_seal", "others"
            ]
            if concern_type not in valid_concern_types:
                return self._send(400, {"success": False, "error": "Invalid concern_type"})

            priority_map = {
                "noise_burning":     "critical",
                "not_working":       "high",
                "tampered_seal":     "high",
                "high_consumption":  "medium",
                "running_fast_slow": "medium",
                "others":            "medium",
            }
            priority    = priority_map.get(concern_type, "medium")
            is_critical = concern_type == "noise_burning"
            reference   = generate_reference()

            # Parse time_noticed safely
            time_noticed = body.get("time_noticed") or None
            if time_noticed == "":
                time_noticed = None

            # ── DB insert ─────────────────────────────────────
            conn = get_conn()
            cur  = conn.cursor(cursor_factory=RealDictCursor)

            cur.execute("""
                INSERT INTO meter_concerns
                    (reference_number, account_number, consumer_name,
                     contact_number, meter_number, service_address,
                     barangay, concern_type, other_concern, date_noticed,
                     time_noticed, additional_details, is_critical, priority,
                     uploaded_files, status, created_at, updated_at)
                VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',NOW(),NOW())
                RETURNING id
            """, (
                reference,
                esc(body["account_number"].strip()),
                esc(body["consumer_name"].strip()),
                esc(body["contact_number"].strip()),
                esc(body["meter_number"].strip()),
                esc(body["service_address"].strip()),
                esc(body["barangay"].strip()),
                concern_type,
                esc(body.get("other_concern", "").strip()),
                body["date_noticed"].strip(),
                time_noticed,
                esc(body.get("additional_details", "").strip()),
                is_critical,
                priority,
                json.dumps(uploaded_files),
            ))

            concern_id = cur.fetchone()["id"]

            # ── Store individual file records ─────────────────
            for f in uploaded_files:
                cur.execute("""
                    INSERT INTO concern_evidence
                        (meter_concern_id, file_name, file_path,
                         file_url, file_type, file_size, uploaded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """, (
                    concern_id,
                    f.get("file_name", "upload"),
                    f.get("file_path", ""),
                    f.get("file_url", ""),
                    f.get("file_type", "application/octet-stream"),
                    int(f.get("file_size", 0)),
                ))

            # ── Activity log ──────────────────────────────────
            cur.execute("""
                INSERT INTO concern_activity_log
                    (meter_concern_id, activity_type,
                     performed_by, description, created_at)
                VALUES (%s, 'created', %s, %s, NOW())
            """, (
                concern_id,
                esc(body["consumer_name"].strip()),
                f"Submitted via public form: {concern_type}",
            ))

            conn.commit()

            return self._send(201, {
                "success":          True,
                "reference_number": reference,
                "concern_id":       concern_id,
                "priority":         priority,
                "is_critical":      is_critical,
                "files_uploaded":   len(uploaded_files),
            })

        except json.JSONDecodeError:
            self._send(400, {"success": False, "error": "Invalid JSON"})

        except psycopg2.OperationalError:
            self._send(503, {"success": False, "error": "Database temporarily unavailable"})

        except Exception as e:
            if conn:
                conn.rollback()
            self._send(500, {"success": False, "error": str(e)})

        finally:
            if cur:  cur.close()
            if conn: conn.close()

    def _send(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        for k, v in cors_headers():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass