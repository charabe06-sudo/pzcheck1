import requests

NAME = 'PZ Checker'
COST = 'Free / check'

def check(url):
    result = {
        'url': url,
        'status': 'unknown',
        'redirects_to': None,
        'response_time_ms': None,
        'error': None,
    }

    target = url
    if '://' not in target:
        target = 'https://' + target

    try:
        resp = requests.get(
            target,
            allow_redirects=False,
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
        )
        if resp.status_code < 400:
            result['status'] = 'alive'
        else:
            result['status'] = 'http_' + str(resp.status_code)
        result['redirects_to'] = resp.headers.get('Location')
        result['response_time_ms'] = round(resp.elapsed.total_seconds() * 1000, 2)
    except requests.exceptions.Timeout:
        result['status'] = 'timeout'
        result['error'] = 'Request timed out after 10s'
    except requests.exceptions.ConnectionError:
        result['status'] = 'dead'
        result['error'] = 'Connection failed'
    except Exception as e:
        result['status'] = 'error'
        result['error'] = str(e)

    return result
