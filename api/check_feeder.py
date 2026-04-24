# api/check_feeder.py
import json
import os
from http.server import BaseHTTPRequestHandler
import psycopg2
from psycopg2.extras import RealDictCursor

FEEDER_TABLE    = '"ILECO_1_COVERAGE_AREA_FEEDERS_FINALoutput"'
FEEDER_NAME_COL = "feeder_name"
EXCLUDED_FEEDER = "Feeder 12A"


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


def check_feeder_active(cur, feeder_name: str) -> bool:
    """
    Check if this feeder has any active (NEW/ASSIGNED) incidents right now.
    Lets the form show the 'known active outage' alert.
    """
    try:
        cur.execute("""
            SELECT 1 FROM outage_incidents
            WHERE feeder_name = %s
              AND status IN ('NEW', 'ASSIGNED')
            LIMIT 1
        """, (feeder_name,))
        return cur.fetchone() is not None
    except Exception:
        return False  # non-fatal — don't break the main flow


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
            if length > 1024:          # guard against large payloads
                return self._send(413, {"success": False,
                                        "error": "Payload too large"})

            body = json.loads(self.rfile.read(length))

            # ── Validate coordinates ──────────────────────────
            try:
                lat = float(body["lat"])
                lng = float(body["lng"])
            except (KeyError, ValueError, TypeError):
                return self._send(400, {"success": False,
                                        "error": "lat and lng are required"})

            # Rough bounding box for Iloilo province
            if not (10.4 <= lat <= 11.2 and 122.0 <= lng <= 123.0):
                return self._send(200, {
                    "success": True,
                    "feeder":    None,
                    "in_feeder": False,
                    "method":    "out_of_bounds",
                })

            conn = get_conn()
            cur  = conn.cursor(cursor_factory=RealDictCursor)

            # ── 1. Check if point is INSIDE a feeder polygon ──
            cur.execute(f"""
                SELECT {FEEDER_NAME_COL},
                       0.0 AS dist_m
                FROM   {FEEDER_TABLE}
                WHERE  ST_Contains(
                           geom::geometry,
                           ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                       )
                  AND  {FEEDER_NAME_COL} != %s
                LIMIT 1
            """, (lng, lat, EXCLUDED_FEEDER))

            row = cur.fetchone()

            if row:
                feeder_name = row[FEEDER_NAME_COL]
                is_active   = check_feeder_active(cur, feeder_name)
                return self._send(200, {
                    "success":     True,
                    "feeder":      feeder_name,
                    "in_feeder":   True,
                    "method":      "contains",
                    "distance_km": 0,
                    "is_active":   is_active,
                })

            # ── 2. Fallback: nearest feeder ───────────────────
            cur.execute(f"""
                SELECT {FEEDER_NAME_COL},
                       ST_Distance(
                           geom::geography,
                           ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                       ) AS dist_m
                FROM   {FEEDER_TABLE}
                WHERE  {FEEDER_NAME_COL} != %s
                ORDER  BY dist_m ASC
                LIMIT  1
            """, (lng, lat, EXCLUDED_FEEDER))

            nearest = cur.fetchone()

            if nearest:
                dist_m    = float(nearest["dist_m"])
                dist_km   = round(dist_m / 1000, 2)
                feeder_name = nearest[FEEDER_NAME_COL]
                is_active   = check_feeder_active(cur, feeder_name)

                return self._send(200, {
                    "success":     True,
                    "feeder":      feeder_name,
                    "in_feeder":   dist_m <= 5000,   # within 5 km = near enough
                    "method":      "nearest",
                    "distance_km": dist_km,
                    "is_active":   is_active,
                })

            # ── 3. No feeder found at all ─────────────────────
            return self._send(200, {
                "success":   True,
                "feeder":    None,
                "in_feeder": False,
            })

        except json.JSONDecodeError:
            self._send(400, {"success": False, "error": "Invalid JSON"})

        except psycopg2.OperationalError as e:
            # DB unreachable — don't break the form, just return graceful fallback
            self._send(200, {
                "success":   True,
                "feeder":    None,
                "in_feeder": False,
                "error":     "Feeder lookup temporarily unavailable",
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
        pass   # suppress Vercel log noise