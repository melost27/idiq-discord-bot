"""
Bot IDIQ -> Discord  (LM Sterling)

Revisa el "Member Status" del IDIQ Partner Portal cada 30 segundos y avisa en Discord:
  1) Cuando aparece una cuenta NUEVA de IdentityIQ (nombre, apellido, telefono, Shared View).
  2) Cuando una cuenta que estaba en "No Consent" pasa a "View" (cuenta READY).

Solo escribe en Discord cuando hay actividad. Solo lee: nombre, apellido, telefono, fecha de inscripcion, status y Shared View.
NUNCA lee ni envia tarjeta, SSN, email ni direccion.
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- configuracion
PORTAL = os.getenv("IDIQ_PORTAL_URL", "https://partner.idiq.com").rstrip("/")
IDIQ_USER = os.getenv("IDIQ_USER", "")
IDIQ_PASSWORD = os.getenv("IDIQ_PASSWORD", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
CHECK_EVERY_SEC = int(os.getenv("CHECK_EVERY_SEC", "30"))
# Mandar a Discord un aviso si el bot falla seguido (0 = nunca, solo actividad)
NOTIFY_ERRORS = os.getenv("NOTIFY_ERRORS", "0") == "1"
# En la primera corrida guarda todo lo que ya existe SIN avisar, excepto lo inscrito
# en los ultimos N dias (0 = no avisar nada viejo).
FIRST_RUN_NOTIFY_DAYS = int(os.getenv("FIRST_RUN_NOTIFY_DAYS", "0"))
# Avisar tambien cuando una cuenta pasa de "No Consent" a "View"
NOTIFY_WHEN_READY = os.getenv("NOTIFY_WHEN_READY", "1") == "1"
# Mencion opcional en Discord, ej: "@here" o "<@&ID_DEL_ROL>"
DISCORD_MENTION = os.getenv("DISCORD_MENTION", "")
USER_AGENT = os.getenv("USER_AGENT", (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"))
# SEND_TEST=1: al arrancar manda 2 avisos de PRUEBA (uno READY y uno NO READY). Quitalo despues.
SEND_TEST = os.getenv("SEND_TEST", "0") == "1"
_test_sent = False
IDIQ_SUPPORT_PHONE = os.getenv("IDIQ_SUPPORT_PHONE", "877-875-4347")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "members.db"
SESSION_PATH = DATA_DIR / "session.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("idiq-bot")

# Columnas que leemos (por el titulo que muestra el portal)
COLUMNS = {
    "membership_no": "Membership No:",
    "first_name": "First Name",
    "last_name": "Last Name",
    "enrollment_date": "Enrollment Date:",
    "status": "Current Status:",
    "phone": "Phone:",
    "shared_view": "Shared View",
}


# ---------------------------------------------------------------- parser
def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().rstrip(":").lower()


def parse_member_table(html: str) -> list[dict]:
    """Recibe el HTML de la tabla Member Status y devuelve solo las columnas permitidas."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one('table[data-table-name="Member Status"]') or soup.find("table")
    if table is None:
        raise RuntimeError("No encontre la tabla Member Status")

    headers = [_norm(th.get_text()) for th in table.select("thead th")]
    idx = {}
    for key, title in COLUMNS.items():
        try:
            idx[key] = headers.index(_norm(title))
        except ValueError:
            raise RuntimeError(f"La columna '{title}' no aparece en Member Status. "
                               f"Revisa el engrane de columnas del portal. Columnas: {headers}")

    members = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) <= max(idx.values()):
            continue
        row = {k: " ".join(tds[i].get_text().split()) for k, i in idx.items()}
        if not row["membership_no"]:
            continue
        members.append(row)
    return members


def is_ready(shared_view: str) -> bool:
    return shared_view.strip().lower() == "view"


# ---------------------------------------------------------------- base de datos
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS members (
        membership_no TEXT PRIMARY KEY,
        first_name TEXT, last_name TEXT, phone TEXT,
        enrollment_date TEXT, status TEXT, shared_view TEXT,
        first_seen TEXT, ready_notified INTEGER DEFAULT 0)""")
    return con


def process(members: list[dict], con: sqlite3.Connection, first_run: bool) -> list[tuple[str, dict]]:
    """Guarda los miembros y devuelve la lista de avisos [(tipo, miembro)]."""
    events = []
    cutoff = datetime.now() - timedelta(days=FIRST_RUN_NOTIFY_DAYS)
    now = datetime.now().isoformat(timespec="seconds")

    for m in members:
        prev = con.execute("SELECT shared_view, ready_notified FROM members WHERE membership_no=?",
                           (m["membership_no"],)).fetchone()
        ready = is_ready(m["shared_view"])

        if prev is None:
            notify = True
            if first_run:
                try:
                    enrolled = datetime.strptime(m["enrollment_date"], "%m/%d/%Y")
                    notify = FIRST_RUN_NOTIFY_DAYS > 0 and enrolled >= cutoff.replace(hour=0, minute=0, second=0)
                except ValueError:
                    notify = False
            con.execute("""INSERT INTO members VALUES (?,?,?,?,?,?,?,?,?)""",
                        (m["membership_no"], m["first_name"], m["last_name"], m["phone"],
                         m["enrollment_date"], m["status"], m["shared_view"], now,
                         1 if ready else 0))
            if notify:
                events.append(("new", m))
        else:
            prev_view, ready_notified = prev
            if ready and not ready_notified and NOTIFY_WHEN_READY:
                events.append(("ready", m))
                ready_notified = 1
            con.execute("""UPDATE members SET first_name=?, last_name=?, phone=?, status=?,
                           shared_view=?, ready_notified=? WHERE membership_no=?""",
                        (m["first_name"], m["last_name"], m["phone"], m["status"],
                         m["shared_view"], 1 if ready_notified else 0, m["membership_no"]))
    con.commit()
    # orden: mas viejo primero, para que en Discord se lea cronologico
    events.reverse()
    return events


# ---------------------------------------------------------------- discord
def format_phone(p: str) -> str:
    d = "".join(c for c in p if c.isdigit())
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return p or "—"


def build_message(kind: str, m: dict) -> dict:
    ready = is_ready(m["shared_view"])
    name = f'{m["first_name"]} {m["last_name"]}'.strip()
    if kind == "ready":
        title = "✅ Cuenta READY — ya dio acceso (View)"
        color = 0x2ECC71
    elif ready:
        title = "🆕 Nueva cuenta IdentityIQ — ✅ READY"
        color = 0x2ECC71
    else:
        title = "🆕 Nueva cuenta IdentityIQ — ⏳ NO READY"
        color = 0xF39C12

    view_txt = "✅ View activo" if ready else f'❌ {m["shared_view"] or "Sin view"}'
    fields = [
        {"name": "Nombre", "value": m["first_name"] or "—", "inline": True},
        {"name": "Apellido", "value": m["last_name"] or "—", "inline": True},
        {"name": "Teléfono", "value": format_phone(m["phone"]), "inline": True},
        {"name": "Shared View", "value": view_txt, "inline": True},
        {"name": "Status", "value": m["status"] or "—", "inline": True},
        {"name": "Inscrito", "value": m["enrollment_date"] or "—", "inline": True},
    ]
    if not ready and kind == "new":
        fields.append({
            "name": "⚠️ Acción para el equipo",
            "value": (
                "Todavía **no tenemos acceso a sus puntajes** (No Consent).\n"
                "**1.** Confirma con el cliente si **él puede ver sus puntajes** "
                "dentro de su cuenta de IdentityIQ.\n"
                "**2.** Si **SÍ los ve** → pídele una **captura de pantalla** de sus puntajes. "
                "Con eso es suficiente.\n"
                "**3.** Si **NO los ve** → debe llamar a **IdentityIQ al "
                f"{IDIQ_SUPPORT_PHONE}** para que le ayuden a verificar su información."
            ),
            "inline": False,
        })
        first = m["first_name"].split()[0].capitalize() if m["first_name"] else ""
        fields.append({
            "name": "💬 Plantilla para enviarle al cliente (copiar y pegar)",
            "value": (
                f"Hola {first}, gracias por crear tu cuenta de IdentityIQ 🙌\n\n"
                "Entra a tu cuenta en identityiq.com y dime: ¿puedes ver tus puntajes de crédito?\n\n"
                "✅ Si SÍ los ves: mándame una captura de pantalla donde se vean tus puntajes y con eso seguimos.\n"
                "❌ Si NO los ves: llama a IdentityIQ al "
                f"{IDIQ_SUPPORT_PHONE} y diles que necesitas verificar tu cuenta porque no puedes ver tus puntajes. "
                "Cuando lo resuelvas, mándame la captura.\n\n"
                "— Equipo LM Sterling"
            ),
            "inline": False,
        })
    return {
        "content": DISCORD_MENTION or None,
        "username": "IDIQ Bot · LM Sterling",
        "embeds": [{"title": title, "description": f"**{name}**", "color": color,
                    "fields": fields,
                    "footer": {"text": f'Membership #{m["membership_no"]}'}}],
        "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
    }


def send_discord(payload: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        log.info("SIN WEBHOOK -> %s", json.dumps(payload, ensure_ascii=False))
        return
    for _ in range(3):
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
        if r.status_code == 429:  # rate limit
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        r.raise_for_status()
        time.sleep(0.6)
        return


def send_error(text: str) -> None:
    """Solo se usa si NOTIFY_ERRORS=1."""
    if not NOTIFY_ERRORS:
        return
    try:
        send_discord({"username": "IDIQ Bot · LM Sterling", "content": f"⚠️ {text}"})
    except Exception:  # noqa
        log.exception("No pude mandar el error a Discord")


# ---------------------------------------------------------------- portal
class Portal:
    """Mantiene UNA sesion abierta del portal y solo refresca la tabla Member Status,
    en vez de abrir el navegador e iniciar sesion en cada revision."""

    TABLE_SEL = 'table[data-table-name="Member Status"]'

    def __init__(self):
        self._pw = None
        self.browser = None
        self.page = None
        self.data_url = None  # endpoint que usa el portal para cargar Member Status
        self.started_at = 0.0

    # ---- ciclo de vida
    def start(self) -> str:
        from playwright.sync_api import sync_playwright

        self.close()
        self._pw = sync_playwright().start()
        launch_extra = {}
        if os.getenv("BROWSER_PATH"):
            launch_extra["executable_path"] = os.getenv("BROWSER_PATH")
        self.browser = self._pw.chromium.launch(
            headless=True, **launch_extra,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"])
        ctx_kwargs = {"locale": "en-US", "viewport": {"width": 1400, "height": 900},
                      "user_agent": USER_AGENT, "timezone_id": "America/Chicago"}
        if SESSION_PATH.exists():
            ctx_kwargs["storage_state"] = str(SESSION_PATH)
        ctx = self.browser.new_context(**ctx_kwargs)
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = ctx.new_page()
        page.set_default_timeout(60_000)
        self.page = page

        self._goto(f"{PORTAL}/Dashboard")
        if "/Account/Login" in page.url or page.locator("#LoginName").count():
            self._login()

        with page.expect_response(lambda r: "/Dashboard/BillingSummary" in r.url,
                                  timeout=120_000) as resp:
            page.get_by_text("Member Status", exact=True).first.click()
        self.data_url = resp.value.url
        page.wait_for_function(
            f"""() => {{ const t=document.querySelector('{self.TABLE_SEL}');
                        return t && t.querySelectorAll('tbody tr').length > 0; }}""",
            timeout=120_000)
        html = page.locator(self.TABLE_SEL).evaluate("t => t.outerHTML")
        ctx.storage_state(path=str(SESSION_PATH))
        self.started_at = time.time()
        log.info("Sesion lista en el portal. Revisando cada %ss.", CHECK_EVERY_SEC)
        return html

    def _login(self):
        log.info("Iniciando sesion en el portal…")
        if not IDIQ_USER or not IDIQ_PASSWORD:
            raise RuntimeError("Faltan IDIQ_USER / IDIQ_PASSWORD")
        page = self.page
        page.fill("#LoginName", IDIQ_USER)
        page.fill("#Password", IDIQ_PASSWORD)
        try:
            with page.expect_navigation(timeout=60_000):
                page.click("form[action='/Account/Login'] button[type=submit]")
        except Exception:  # noqa
            pass
        self._settle()
        if "/Account/Login" in page.url:
            self._goto(f"{PORTAL}/Dashboard")
        if "/Account/Login" in page.url:
            raise RuntimeError("No pude iniciar sesion en IDIQ (revisa usuario/contraseña). "
                               + self._page_hint())
        log.info("Sesion iniciada.")

    def _settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa
            pass

    def _page_hint(self) -> str:
        """Texto corto de la pagina actual para entender errores en los Logs."""
        try:
            txt = " ".join(self.page.inner_text("body", timeout=5_000).split())[:250]
        except Exception:  # noqa
            txt = "(sin texto)"
        return f"URL={self.page.url} | Pagina: {txt}"

    def _goto(self, url: str):
        """Abre una URL tolerando que el portal corte/redirija la carga (ERR_ABORTED)."""
        last = None
        for attempt in range(1, 4):
            try:
                self.page.goto(url, wait_until="commit", timeout=60_000)
            except Exception as e:  # noqa
                last = e
                log.info("La carga de %s se interrumpio (intento %d): %s",
                         url, attempt, str(e).splitlines()[0])
            self._settle()
            body = ""
            try:
                body = self.page.content()
            except Exception:  # noqa
                pass
            if "Incapsula incident" in body or "Request unsuccessful" in body:
                raise RuntimeError("IDIQ bloqueo al servidor (proteccion anti-bots Incapsula). "
                                   "Hay que correr el bot desde otra conexion.")
            # Listo cuando cargo el login o el dashboard del portal
            if self.page.url.startswith(PORTAL) and (
                    self.page.locator("#LoginName").count() or "Member Status" in body):
                return
            time.sleep(3 * attempt)
        raise RuntimeError(f"No pude abrir {url}: {last}. {self._page_hint()}")

    def close(self):
        for obj, fn in ((self.browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception:  # noqa
                pass
        self.browser = self._pw = self.page = None

    # ---- lectura
    def member_table_html(self) -> str:
        # Reiniciar el navegador cada 6 horas para que no se ponga lento
        if self.page is None or not self.data_url or time.time() - self.started_at > 6 * 3600:
            return self.start()

        result = self.page.evaluate(
            """async (url) => {
                 const u = new URL(url); u.searchParams.set('_', Date.now());
                 const r = await fetch(u.toString(), {credentials: 'include',
                                       headers: {'X-Requested-With': 'XMLHttpRequest'}});
                 return {status: r.status, url: r.url, text: await r.text()};
               }""", self.data_url)
        if (result["status"] != 200 or "/Account/Login" in result["url"]
                or 'data-table-name="Member Status"' not in result["text"]):
            log.info("La sesion vencio, entrando de nuevo…")
            if SESSION_PATH.exists():
                SESSION_PATH.unlink()
            return self.start()
        return result["text"]


# ---------------------------------------------------------------- loop
def check(portal: Portal, con: sqlite3.Connection) -> None:
    first_run = con.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0
    members = parse_member_table(portal.member_table_html())
    events = process(members, con, first_run)
    if first_run:
        log.info("Primera corrida: %d cuentas guardadas sin avisar.", len(members))
    global _test_sent
    if SEND_TEST and not _test_sent:
        _test_sent = True
        samples = [next((m for m in members if is_ready(m["shared_view"])), None),
                   next((m for m in members if not is_ready(m["shared_view"])), None)]
        for m in filter(None, samples):
            msg = build_message("new", m)
            msg["embeds"][0]["title"] = "🧪 PRUEBA · " + msg["embeds"][0]["title"]
            send_discord(msg)
        log.info("Mensajes de prueba enviados a Discord. Quita la variable SEND_TEST.")
    # Solo se escribe en Discord cuando hay actividad (cuenta nueva o cambio a View)
    for kind, m in events:
        log.info("Aviso %s: %s %s", kind, m["first_name"], m["last_name"])
        send_discord(build_message(kind, m))


def main() -> None:
    once = "--once" in sys.argv
    portal = Portal()
    con = db()
    fails = 0
    last_heartbeat = 0.0
    try:
        while True:
            started = time.time()
            try:
                check(portal, con)
                fails = 0
            except Exception as e:  # noqa
                fails += 1
                log.exception("Error revisando el portal (intento %d)", fails)
                portal.close()  # la proxima vuelta abre todo de nuevo
                if SESSION_PATH.exists():
                    SESSION_PATH.unlink()
                if fails == 10:  # ~5 min fallando seguido
                    send_error(f"El bot no ha podido leer el portal IDIQ: {e}")
                time.sleep(min(300, 30 * fails))  # esperar mas si sigue fallando
            if once:
                break
            if time.time() - last_heartbeat > 3600:  # una linea por hora en los Logs
                log.info("Bot activo.")
                last_heartbeat = time.time()
            time.sleep(max(1, CHECK_EVERY_SEC - (time.time() - started)))
    finally:
        portal.close()


if __name__ == "__main__":
    main()
