"""Amazon catalog sync -> data/catalog.json

Runs after sync.py in the daily GitHub Action (separate step, never blocks
the data.json commit). Read-only: it only calls GET/estimate endpoints and
never creates or changes anything on Amazon.

Sources per column:
  sku, title, status, amazon_price  Listings Items API (searchListingsItems)
  image_url                         Catalog Items API (searchCatalogItems), listing mainImage as fallback
  amazon_qty                        data.json inventory.in_stock (FBA fulfillable, from sync.py)
  amazon_fees                       realized per-unit fees from data.json when the SKU has sales,
                                    otherwise Product Fees API estimate at the current price
  last_cogs, cogs_date              data/costs.json (flagged sample while it has "_sample": true)
  warehouse_qty                     null until the internal inventory feed exists

Needs the A1_SPAPI_* secrets plus A1_SPAPI_SELLER_ID (Seller Central >
Settings > Account Info > Merchant Token).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

from sync import get_access_token, MARKETPLACE_ID

SELLER_ID = os.environ.get('A1_SPAPI_SELLER_ID', '').strip()
API = 'https://sellingpartnerapi-na.amazon.com'
ROOT = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(ROOT, 'data', 'catalog.json')

# SP-API developer roles each call needs (Developer Central > app > Edit App).
ROLE_FOR_API = {
    'listings': 'Product Listing',
    'catalog':  'Product Listing',
    'fees':     'Pricing',
}


class ApiError(Exception):
    def __init__(self, api, status, body):
        super().__init__(f'{api}: HTTP {status}: {body}')
        self.api, self.status, self.body = api, status, body


def call(token, method, path, api, params=None, body=None, tries=6):
    """SP-API request with backoff on 429/5xx. Raises ApiError on 4xx."""
    headers = {
        'x-amz-access-token': token,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    r = None
    for attempt in range(tries):
        r = requests.request(method, API + path, headers=headers,
                             params=params, json=body, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(30, 2 ** attempt))
            continue
        if r.status_code >= 400:
            raise ApiError(api, r.status_code, r.text[:400])
        return r.json()
    raise ApiError(api, r.status_code if r is not None else 0,
                   (r.text[:400] if r is not None else 'no response'))


def describe_error(e):
    if isinstance(e, ApiError) and e.status == 403:
        return (f'HTTP 403 — the SP-API app is probably missing the '
                f'"{ROLE_FOR_API.get(e.api, "?")}" role (or the refresh token '
                f'was issued before the role was added; re-authorize after adding it)')
    return str(e)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ── Listings Items ─────────────────────────────────────────────────────────
def fetch_listings(token):
    params = {
        'marketplaceIds': MARKETPLACE_ID,
        'includedData': 'summaries,offers,fulfillmentAvailability',
        'pageSize': 20,
    }
    items = []
    while True:
        data = call(token, 'GET', f'/listings/2021-08-01/items/{SELLER_ID}', 'listings', params=params)
        items.extend(data.get('items', []))
        next_token = (data.get('pagination') or {}).get('nextToken')
        if not next_token:
            return items
        params = {**params, 'pageToken': next_token}
        time.sleep(0.25)  # 5 req/s


def parse_listing(item):
    summaries = item.get('summaries') or [{}]
    summ = next((s for s in summaries if s.get('marketplaceId') == MARKETPLACE_ID), summaries[0])
    price = currency = None
    for offer in item.get('offers') or []:
        if offer.get('marketplaceId', MARKETPLACE_ID) != MARKETPLACE_ID:
            continue
        if offer.get('offerType', 'B2C') != 'B2C':
            continue
        p = offer.get('price') or {}
        amount = p.get('amount', p.get('Amount'))
        if amount is not None:
            price = round(float(amount), 2)
            currency = p.get('currencyCode') or p.get('currency')
            break
    channels = [str(f.get('fulfillmentChannelCode', '')) for f in item.get('fulfillmentAvailability') or []]
    return {
        'sku': item.get('sku'),
        'asin': summ.get('asin'),
        'title': summ.get('itemName'),
        'status': summ.get('status') or [],
        'image': (summ.get('mainImage') or {}).get('link'),
        'price': price,
        'currency': currency,
        'fba': any(c.startswith('AMAZON') for c in channels) if channels else True,
    }


# ── Catalog Items (images) ─────────────────────────────────────────────────
def pick_image(images_by_marketplace):
    imgs = []
    for group in images_by_marketplace or []:
        if group.get('marketplaceId', MARKETPLACE_ID) == MARKETPLACE_ID:
            imgs.extend(group.get('images') or [])
    main = [i for i in imgs if i.get('variant') == 'MAIN'] or imgs
    if not main:
        return None
    # Smallest image that is still at least 200px wide, else the largest
    big_enough = sorted((i for i in main if (i.get('width') or 0) >= 200), key=lambda i: i.get('width') or 0)
    best = big_enough[0] if big_enough else max(main, key=lambda i: i.get('width') or 0)
    return best.get('link')


def fetch_catalog(token, asins):
    out = {}
    for batch in chunks(sorted(asins), 20):
        data = call(token, 'GET', '/catalog/2022-04-01/items', 'catalog', params={
            'identifiers': ','.join(batch),
            'identifiersType': 'ASIN',
            'marketplaceIds': MARKETPLACE_ID,
            'includedData': 'images,summaries',
            'pageSize': 20,
        })
        for it in data.get('items', []):
            summ = next((s for s in it.get('summaries') or [] if s.get('marketplaceId') == MARKETPLACE_ID), {})
            out[it.get('asin')] = {'image': pick_image(it.get('images')), 'title': summ.get('itemName')}
        time.sleep(0.6)  # 2 req/s
    return out


# ── Product Fees (estimates) ───────────────────────────────────────────────
def fetch_fee_estimates(token, listings):
    """listings: [{sku, price, currency, fba}] -> {sku: total fee per unit}"""
    out = {}
    for batch in chunks(listings, 20):
        body = [{
            'FeesEstimateRequest': {
                'MarketplaceId': MARKETPLACE_ID,
                'IsAmazonFulfilled': bool(l['fba']),
                'PriceToEstimateFees': {
                    'ListingPrice': {'CurrencyCode': l['currency'] or 'USD', 'Amount': l['price']},
                },
                'Identifier': l['sku'],
            },
            'IdType': 'SellerSKU',
            'IdValue': l['sku'],
        } for l in batch]
        data = call(token, 'POST', '/products/fees/v0/feesEstimate', 'fees', body=body)
        results = data if isinstance(data, list) else (data.get('payload') or [])
        for res in results:
            sku = ((res.get('FeesEstimateIdentifier') or {}).get('IdValue'))
            total = ((res.get('FeesEstimate') or {}).get('TotalFeesEstimate') or {}).get('Amount')
            if sku and res.get('Status') == 'Success' and total is not None:
                out[sku] = round(abs(float(total)), 2)
        time.sleep(2.1)  # 0.5 req/s
    return out


# ── Build ──────────────────────────────────────────────────────────────────
def realized_fees(data_json):
    sb = data_json.get('sku_breakdown') or {}
    units = sb.get('units') or {}
    fba = sb.get('fba_fees_by_sku') or {}
    ref = sb.get('referral_fees_by_sku') or {}
    out = {}
    for sku, u in units.items():
        total = (fba.get(sku) or 0) + (ref.get(sku) or 0)
        if u and u > 0 and total:
            out[sku] = round(abs(total) / u, 2)
    return out


def run():
    if not SELLER_ID:
        print('A1_SPAPI_SELLER_ID is not set — add it as a GitHub secret '
              '(Seller Central > Settings > Account Info > Merchant Token). '
              'data/catalog.json left unchanged.')
        return 1

    data_json = load_json(os.path.join(ROOT, 'data.json'), {})
    costs = load_json(os.path.join(ROOT, 'data', 'costs.json'), {})
    costs_are_sample = bool(costs.get('_sample'))
    inventory = data_json.get('inventory') or {}
    realized = realized_fees(data_json)

    print('Getting access token...')
    token = get_access_token()

    status = {}
    print('Pulling Listings Items...')
    try:
        listings = [parse_listing(i) for i in fetch_listings(token)]
        listings = [l for l in listings if l['sku']]
        status['listings'] = f'ok ({len(listings)} listings)'
        print(f'  ✓ {len(listings)} listings')
    except Exception as e:
        print(f'  ✗ Listings Items failed: {describe_error(e)}')
        print('  data/catalog.json left unchanged.')
        return 1

    # Keep live listings plus anything Amazon is still holding stock for
    def amz_qty(sku):
        inv = inventory.get(sku) or {}
        return int(inv.get('in_stock') or 0)
    listings = [l for l in listings if l['status'] or amz_qty(l['sku']) > 0]

    print('Pulling Catalog Items images...')
    catalog = {}
    try:
        catalog = fetch_catalog(token, {l['asin'] for l in listings if l['asin']})
        status['catalog'] = f'ok ({len(catalog)} ASINs)'
        print(f'  ✓ {len(catalog)} ASINs')
    except Exception as e:
        status['catalog'] = describe_error(e)
        print(f'  ✗ Catalog Items failed: {status["catalog"]} — using listing images')

    need_estimate = [l for l in listings if l['sku'] not in realized and l['price']]
    estimates = {}
    if need_estimate:
        print(f'Estimating fees for {len(need_estimate)} SKUs without sales...')
        try:
            estimates = fetch_fee_estimates(token, need_estimate)
            status['fees'] = f'ok ({len(estimates)} estimates)'
            print(f'  ✓ {len(estimates)} estimates')
        except Exception as e:
            status['fees'] = describe_error(e)
            print(f'  ✗ Product Fees failed: {status["fees"]}')
    else:
        status['fees'] = 'not needed'

    rows = []
    for l in sorted(listings, key=lambda x: x['sku']):
        sku = l['sku']
        cat = catalog.get(l['asin']) or {}
        cost = costs.get(sku) if isinstance(costs.get(sku), dict) else None
        if sku in realized:
            fees, fees_source = realized[sku], 'realized'
        elif sku in estimates:
            fees, fees_source = estimates[sku], 'estimate'
        else:
            fees, fees_source = None, None
        rows.append({
            'sku': sku,
            'asin': l['asin'],
            'title': l['title'] or cat.get('title') or sku,
            'image_url': cat.get('image') or l['image'],
            'status': l['status'],
            'amazon_qty': amz_qty(sku),
            'warehouse_qty': None,
            'last_cogs': cost.get('cogs') if cost else None,
            'cogs_date': cost.get('costed_on') if cost else None,
            'amazon_price': l['price'],
            'amazon_fees': fees,
            'fees_source': fees_source,
        })

    sample_columns = ['cost'] if costs_are_sample else []
    out = {
        '_sample': bool(sample_columns),
        '_sample_columns': sample_columns,
        '_synced_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        '_sync': status,
        '_sources': {
            'title/status/price': 'Listings Items API',
            'image_url': 'Catalog Items API (listing mainImage fallback)',
            'amazon_qty': 'data.json inventory.in_stock',
            'amazon_fees': 'realized from data.json, else Product Fees API estimate',
            'last_cogs/cogs_date': 'data/costs.json' + (' (sample)' if costs_are_sample else ''),
            'warehouse_qty': 'not connected (internal inventory feed)',
        },
        'rows': rows,
    }
    with open(CATALOG_PATH, 'w') as f:
        json.dump(out, f, indent=2)
    print(f'✓ data/catalog.json written — {len(rows)} rows')

    # Non-zero so the Actions step shows a warning if an optional API failed
    return 0 if all(s.startswith('ok') or s == 'not needed' for s in status.values()) else 1


if __name__ == '__main__':
    sys.exit(run())
