"""
Mercari API utilities - local implementation to avoid cffi dependency issues.
Uses PyJWT with cryptography for DPOP generation.
"""

import base64
import json
import uuid as uuid_lib
from time import time
import httpx

# Try cryptography first (needs cffi), fallback to pure-python ecdsa
try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    CRYPTO_BACKEND = "cryptography"
except ImportError:
    try:
        import ecdsa
        import hashlib
        CRYPTO_BACKEND = "ecdsa"
    except ImportError:
        CRYPTO_BACKEND = None
        print("[MercariAPI] No crypto backend available - Mercari API calls will fail")


def int_to_bytes(n):
    return n.to_bytes((n.bit_length() + 7) // 8, byteorder='big')


def int_to_base64url(n):
    return bytes_to_base64url(int_to_bytes(n))


def str_to_base64url(s):
    return bytes_to_base64url(s.encode('utf-8'))


def bytes_to_base64url(b):
    return base64.urlsafe_b64encode(b).decode('utf-8').rstrip('=')


def generate_dpop(*, uuid, method, url):
    """Generate DPOP token for Mercari API authentication."""
    if CRYPTO_BACKEND == "cryptography":
        return _generate_dpop_cryptography(uuid=uuid, method=method, url=url)
    elif CRYPTO_BACKEND == "ecdsa":
        return _generate_dpop_ecdsa(uuid=uuid, method=method, url=url)
    else:
        raise ImportError("No crypto backend available for DPOP generation")


def _generate_dpop_cryptography(*, uuid, method, url):
    """Generate DPOP using cryptography library."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    public_numbers = public_key.public_numbers()

    header = {
        "typ": "dpop+jwt",
        "alg": "ES256",
        "jwk": {
            "crv": "P-256",
            "kty": "EC",
            "x": int_to_base64url(public_numbers.x),
            "y": int_to_base64url(public_numbers.y),
        }
    }

    payload = {
        "iat": int(time()),
        "jti": uuid,
        "htu": url,
        "htm": method.upper(),
    }

    data_to_sign = f"{str_to_base64url(json.dumps(header))}.{str_to_base64url(json.dumps(payload))}"

    signature = private_key.sign(
        data_to_sign.encode('utf-8'),
        ec.ECDSA(hashes.SHA256())
    )

    r, s = utils.decode_dss_signature(signature)
    # Pad to 32 bytes each
    r_bytes = r.to_bytes(32, byteorder='big')
    s_bytes = s.to_bytes(32, byteorder='big')
    signature_string = bytes_to_base64url(r_bytes + s_bytes)

    return f"{data_to_sign}.{signature_string}"


def _generate_dpop_ecdsa(*, uuid, method, url):
    """Generate DPOP using pure-python ecdsa library."""
    import ecdsa
    import hashlib

    private_key = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
    public_key = private_key.get_verifying_key()

    # Get x, y coordinates
    point = public_key.pubkey.point
    x = point.x()
    y = point.y()

    header = {
        "typ": "dpop+jwt",
        "alg": "ES256",
        "jwk": {
            "crv": "P-256",
            "kty": "EC",
            "x": int_to_base64url(x),
            "y": int_to_base64url(y),
        }
    }

    payload = {
        "iat": int(time()),
        "jti": uuid,
        "htu": url,
        "htm": method.upper(),
    }

    data_to_sign = f"{str_to_base64url(json.dumps(header))}.{str_to_base64url(json.dumps(payload))}"

    signature = private_key.sign(
        data_to_sign.encode('utf-8'),
        hashfunc=hashlib.sha256
    )

    signature_string = bytes_to_base64url(signature)
    return f"{data_to_sign}.{signature_string}"


# Mercari API URLs
ROOT_URL = "https://api.mercari.jp/"
SEARCH_URL = f"{ROOT_URL}v2/entities:search"
ITEM_INFO_URL = f"{ROOT_URL}items/get"


class MercariItemStatus:
    ON_SALE = "ITEM_STATUS_ON_SALE"
    TRADING = "ITEM_STATUS_TRADING"
    SOLD_OUT = "ITEM_STATUS_SOLD_OUT"
    STOP = "ITEM_STATUS_STOP"
    CANCEL = "ITEM_STATUS_CANCEL"


class MercariItem:
    """Represents a Mercari item."""
    def __init__(self, data):
        self.id = data.get('id')
        self.name = data.get('name')
        self.price = data.get('price')
        self.status = data.get('status')
        self.description = data.get('description')
        self.photos = data.get('photos', [])
        self.thumbnails = data.get('thumbnails', [])
        self.created = data.get('created')
        self.updated = data.get('updated')
        self._data = data

    @property
    def image_url(self):
        if self.thumbnails:
            return self.thumbnails[0]
        if self.photos:
            return self.photos[0]
        return None


def _make_request(url, data, method="GET"):
    """Make authenticated request to Mercari API."""
    dpop = generate_dpop(
        uuid="MercariBot",
        method=method,
        url=url,
    )

    headers = {
        'DPOP': dpop,
        'X-Platform': 'web',
        'Accept': '*/*',
        'Accept-Encoding': 'deflate, gzip',
        'Content-Type': 'application/json; charset=utf-8',
        'User-Agent': 'python-mercari',
    }

    if method == "GET":
        response = httpx.get(url, headers=headers, params=data, timeout=15)
    else:
        response = httpx.post(url, headers=headers, json=data, timeout=15)

    response.raise_for_status()
    return response.json()


def get_item_info(item_id, country_code=None):
    """
    Get item info from Mercari API.

    Args:
        item_id: The item ID (e.g., 'm12345678')
        country_code: Optional country code. If None, returns JPY prices.

    Returns:
        MercariItem object
    """
    data = {
        "id": item_id,
        "include_item_attributes": True,
        "include_auction": True,
    }

    # Only add country_code if specified - omitting returns JPY prices
    if country_code:
        data["country_code"] = country_code

    result = _make_request(ITEM_INFO_URL, data, method="GET")
    return MercariItem(result.get('data', {}))


def search(keywords, limit=120, status="STATUS_ON_SALE"):
    """
    Search Mercari for items.

    Args:
        keywords: Search keywords
        limit: Max items per page
        status: STATUS_ON_SALE, STATUS_SOLD_OUT, or STATUS_DEFAULT

    Yields:
        MercariItem objects
    """
    data = {
        "userId": f"BOT_{uuid_lib.uuid4()}",
        "pageSize": limit,
        "pageToken": "v1:0",
        "searchSessionId": f"BOT_{uuid_lib.uuid4()}",
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
        "searchCondition": {
            "keyword": keywords,
            "sort": "SORT_CREATED_TIME",
            "order": "ORDER_DESC",
            "status": [status],
        },
        "withAuction": True,
        "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"]
    }

    has_next_page = True
    while has_next_page:
        result = _make_request(SEARCH_URL, data, method="POST")
        items = result.get("items", [])

        if not items:
            break

        for item_data in items:
            yield MercariItem(item_data)

        next_token = result.get("meta", {}).get("nextPageToken")
        if next_token:
            data["pageToken"] = next_token
        else:
            has_next_page = False
