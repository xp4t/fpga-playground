#!/usr/bin/env python3
"""Check backend authentication without printing the shared secret."""
import argparse
import json
import os
import urllib.error
import urllib.request


def check_url(url, secret=None):
    headers = {'Origin': 'https://fpgaplayground.com'}
    if secret:
        headers['X-Backend-Secret'] = secret
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    secret = os.environ.get('BACKEND_SECRET')
    if not secret or secret.startswith(('REPLACE', 'replace')):
        parser.error('Load the real BACKEND_SECRET into this process first')
    endpoint = args.url.rstrip('/') + '/api/health'
    status, _ = check_url(endpoint)
    assert status == 403, f'Unauthenticated API returned {status}, expected 403'
    status, data = check_url(endpoint, secret)
    assert status == 200, f'Authenticated health returned {status}'
    print(json.dumps(json.loads(data), indent=2))
    print('Backend authentication checks passed.')
