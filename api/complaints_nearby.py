# api/complaints_nearby.py
import json
import os
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
        connect_timeout=8,
    )


def cors_headers():
    return [
        ("Access-Control-Allow-Origin",  "*"),
        ("Access-Control-Allow-Headers", "Content-Type"),
        ("Access-Control-Allow-Methods", "POST, OPTIONS"),
        ("Content-Type", "application/json"),
    ]


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
            if length > 1024:
                return self._send(413, {"success": False, "error": "Payload too large"})

            body = json.loads(self.rfile.read(length))

            try:
                lat    = float(body["lat"])
                lng    = float(body["lng"])
                radius = min(int(body.get("radius", 1000)), 5000)
            except (KeyError, ValueError, TypeError):
                return self._send(400, {
                    "success": False,
                    "error": "lat and lng are required"
                })

            if not (4.0 <= lat <= 21.0 and 116.0 <= lng <= 127.0):
                return self._send(200, {"success": True, "complaints": [], "count": 0})

            conn = get_conn()
            cur  = conn.cursor(cursor_factory=RealDictCursor)

            cur.execute("""
                SELECT
                    i.incident_id,
                    i.incident_type                          AS type,
                    i.barangay,
                    i.town,
                    i.status,
                    i.priority,
                    i.report_count,
                    ST_Y(i.geom::geometry)                   AS lat,
                    ST_X(i.geom::geometry)                   AS lng,
                    ST_Distance(
                        i.geom::geography,
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                    )                                        AS distance_meters,
                    r.details
                FROM outage_incidents i
                LEFT JOIN LATERAL (
                    SELECT details FROM outage_reports
                    WHERE  incident_id = i.incident_id
                    ORDER  BY timestamp DESC
                    LIMIT  1
                ) r ON TRUE
                WHERE
                    i.status IN ('NEW', 'ASSIGNED', 'IN_PROGRESS')
                    AND i.geom IS NOT NULL
                    AND ST_DWithin(
                        i.geom::geography,
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                        %s
                    )
                ORDER BY distance_meters ASC
                LIMIT 10
            """, (lng, lat, lng, lat, radius))

            rows = cur.fetchall()

            complaints = [{
                "incident_id":     row["incident_id"],
                "type":            row["type"],
                "barangay":        row["barangay"],
                "town":            row["town"],
                "status":          row["status"],
                "priority":        row["priority"],
                "report_count":    row["report_count"],
                "lat":             float(row["lat"]) if row["lat"] else None,
                "lng":             float(row["lng"]) if row["lng"] else None,
                "distance_meters": float(row["distance_meters"]),
                "details":         row["details"],
            } for row in rows]

            return self._send(200, {
                "success":    True,
                "complaints": complaints,
                "count":      len(complaints),
            })

        except json.JSONDecodeError:
            self._send(400, {"success": False, "error": "Invalid JSON"})

        except psycopg2.OperationalError:
            self._send(200, {
                "success":    True,
                "complaints": [],
                "error":      "Database temporarily unavailable",
            })

        except Exception as e:
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