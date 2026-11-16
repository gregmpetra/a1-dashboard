"""FBM (merchant-fulfilled) order profitability -> data/fbm.json

Called from sync.py after it has pulled financial events and this month's
orders, so it makes no extra Amazon calls.

  Settled orders   financial events ShipmentEventList. An order with no FBA
                   fulfilment fee is merchant-fulfilled (FBM).
  Pending orders   this month's Orders API orders with FulfillmentChannel MFN
                   that have not settled yet. Referral fee is estimated from
                   the SKU's realized referral rate (15% if unknown).
  COGS             units x per-unit cost from data/costs.json.
  Shipping fee     internal shipping report: every CSV in data/shipping/,
                   joined on the Amazon order number (see data/shipping/README.md).

Refunds are not included.
"""
import csv
import glob
import json
import os
import re
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
SHIPPING_DIR = os.path.join(ROOT, 'data', 'shipping')
OUT_PATH = os.path.join(ROOT, 'data', 'fbm.json')

FBA_FEE_TYPES = {'FBAPerUnitFulfillmentFee', 'FBAPerOrderFulfillmentFee', 'FBAWeightBasedFee'}
REFERRAL_FEE_TYPES = {'Commission', 'VariableClosingFee', 'FixedClosingFee'}
DEFAULT_REFERRAL_RATE = 0.15

# Accepted column names in the shipping report (case, spaces, _ and - ignored)
ORDER_COLS = {'amazonorderid', 'amazonorder', 'amazonordernumber', 'orderid', 'ordernumber',
              'orderno', 'order', 'ordernum', 'amazonorderno'}
SHIPMENT_COLS = {'shipmentnumber', 'shipmentid', 'shipment', 'shipmentno', 'internalshipment',
                 'internalshipmentnumber', 'internalshipmentid'}
COST_COLS = {'shippingcost', 'shippingfee', 'shipping', 'postage', 'labelcost', 'shippingcharge',
             'freight', 'freightcost', 'totalshippingcost', 'cost'}
AMAZON_ORDER_RE = re.compile(r'\d{3}-\d{7}-\d{7}')


def _key(header):
    return re.sub(r'[^a-z0-9]', '', (header or '').lower())


def _money(value):
    s = re.sub(r'[^0-9.\-]', '', str(value or ''))
    try:
        return float(s) if s not in ('', '-', '.') else None
    except ValueError:
        return None


def load_shipping_report():
    """{amazon_order_id: {'cost': float, 'shipments': [..]}} plus a summary."""
    by_order, files, rows, skipped = {}, [], 0, 0
    for path in sorted(glob.glob(os.path.join(SHIPPING_DIR, '*.csv'))):
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            cols = {_key(h): h for h in (reader.fieldnames or [])}
            order_col = next((cols[k] for k in cols if k in ORDER_COLS), None)
            ship_col = next((cols[k] for k in cols if k in SHIPMENT_COLS), None)
            cost_col = next((cols[k] for k in cols if k in COST_COLS), None)
            if not order_col or not cost_col:
                print(f"  Shipping report {os.path.basename(path)}: no order/cost column found — skipped")
                continue
            files.append(os.path.basename(path))
            for row in reader:
                m = AMAZON_ORDER_RE.search(str(row.get(order_col) or ''))
                cost = _money(row.get(cost_col))
                if not m or cost is None:
                    skipped += 1
                    continue
                rows += 1
                rec = by_order.setdefault(m.group(0), {'cost': 0.0, 'shipments': []})
                rec['cost'] += abs(cost)
                shipment = str(row.get(ship_col) or '').strip() if ship_col else ''
                if shipment and shipment not in rec['shipments']:
                    rec['shipments'].append(shipment)
    return by_order, {'files': files, 'rows': rows, 'skipped_rows': skipped}


def settled_orders(fin_events):
    """Aggregate ShipmentEventList by order; keep orders with no FBA fee."""
    orders, seen = {}, set()
    for events in fin_events:
        for se in events.get('ShipmentEventList', []) or []:
            oid = se.get('AmazonOrderId')
            posted = se.get('PostedDate', '')
            if not oid:
                continue
            for item in se.get('ShipmentItemList', []) or []:
                sku = item.get('SellerSKU', '')
                dedupe = (oid, posted, sku, item.get('OrderItemId'), item.get('QuantityShipped'))
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                rec = orders.setdefault(oid, {'date': posted[:10], 'skus': {}, 'sales': 0.0,
                                              'referral': 0.0, 'fba': False})
                rec['date'] = min(rec['date'], posted[:10]) if rec['date'] else posted[:10]
                qty = int(item.get('QuantityShipped', 0) or 0)
                if sku and qty:
                    rec['skus'][sku] = rec['skus'].get(sku, 0) + qty
                for charge in item.get('ItemChargeList', []) or []:
                    ctype = charge.get('ChargeType', '')
                    amt = float((charge.get('ChargeAmount') or {}).get('CurrencyAmount', 0) or 0)
                    if ctype in ('Principal', 'ShippingCharge', 'Shipping'):
                        rec['sales'] += amt
                for fee in item.get('ItemFeeList', []) or []:
                    ftype = fee.get('FeeType', '')
                    amt = float((fee.get('FeeAmount') or {}).get('CurrencyAmount', 0) or 0)
                    if ftype in FBA_FEE_TYPES and amt:
                        rec['fba'] = True
                    elif ftype in REFERRAL_FEE_TYPES:
                        rec['referral'] += abs(amt)
    return {oid: r for oid, r in orders.items() if not r['fba'] and r['sales'] > 0}


def pending_orders(order_details, referral_rate_by_sku):
    out = {}
    for o in order_details or []:
        if o.get('channel') != 'MFN' or not o.get('order_id'):
            continue
        skus, sales, referral = {}, 0.0, 0.0
        for it in o.get('items', []):
            sku, qty = it.get('sku'), int(it.get('qty') or 0)
            amount = float(it.get('price') or 0) + float(it.get('shipping') or 0)
            if sku and qty:
                skus[sku] = skus.get(sku, 0) + qty
            sales += amount
            referral += amount * referral_rate_by_sku.get(sku, DEFAULT_REFERRAL_RATE)
        if sales > 0:
            out[o['order_id']] = {'date': (o.get('purchase_date') or '')[:10], 'skus': skus,
                                  'sales': sales, 'referral': referral}
    return out


def build(fin_events, order_details, cogs_map, referral_rate_by_sku):
    shipping, report = load_shipping_report()
    settled = settled_orders(fin_events)
    pending = {k: v for k, v in pending_orders(order_details, referral_rate_by_sku).items()
               if k not in settled}

    rows = []
    for status, group in (('settled', settled), ('pending', pending)):
        for oid, r in group.items():
            cogs = sum(q * cogs_map[s] for s, q in r['skus'].items() if s in cogs_map)
            missing = sorted(s for s in r['skus'] if s not in cogs_map)
            ship = shipping.get(oid)
            shipping_fee = round(ship['cost'], 2) if ship else None
            profit = r['sales'] - r['referral'] - cogs - (shipping_fee or 0)
            rows.append({
                'order_id': oid,
                'date': r['date'],
                'month': r['date'][:7],
                'status': status,
                'skus': r['skus'],
                'units': sum(r['skus'].values()),
                'sales': round(r['sales'], 2),
                'referral_fee': round(r['referral'], 2),
                'referral_estimated': status == 'pending',
                'cogs': round(cogs, 2),
                'cogs_missing': missing,
                'shipping_fee': shipping_fee,
                'shipment_numbers': ship['shipments'] if ship else [],
                'profit': round(profit, 2),
                'complete': shipping_fee is not None and not missing and status == 'settled',
            })
    rows.sort(key=lambda x: (x['date'], x['order_id']), reverse=True)
    matched = sum(1 for r in rows if r['shipping_fee'] is not None)
    report['matched_orders'] = matched
    return {
        '_generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        '_notes': 'Settled = financial events with no FBA fee; pending = this month\'s MFN orders not yet settled '
                  '(referral fee estimated). Refunds not included.',
        'shipping_report': report,
        'orders': rows,
    }


def write(fin_events, order_details, cogs_map, referral_rate_by_sku):
    out = build(fin_events, order_details, cogs_map, referral_rate_by_sku)
    with open(OUT_PATH, 'w') as f:
        json.dump(out, f, indent=2)
    r = out['shipping_report']
    print(f"✓ data/fbm.json written — {len(out['orders'])} FBM orders, "
          f"{r['matched_orders']} matched to shipping report ({len(r['files'])} file(s))")
    return out
