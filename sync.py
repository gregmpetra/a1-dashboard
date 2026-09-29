import os, json, requests, time
from datetime import datetime, timedelta, date

# ── Credentials (set as Railway environment variables) ──────────────────────
CLIENT_ID     = os.environ.get('A1_SPAPI_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('A1_SPAPI_CLIENT_SECRET', '')
REFRESH_TOKEN = os.environ.get('A1_SPAPI_REFRESH_TOKEN', '')
MARKETPLACE_ID = os.environ.get('MARKETPLACE_ID', 'ATVPDKIKX0DER')  # US default
REGION         = os.environ.get('SPAPI_REGION', 'us-east-1')
ENDPOINT       = 'https://sellercentral.amazon.com'

COGS_MAP = {}  # No COGS data for A1 American yet


PRODUCT_NAMES = {}  # Raw SKUs for A1 American


def get_access_token():
    """Exchange refresh token for access token."""
    r = requests.post('https://api.amazon.com/auth/o2/token', data={
        'grant_type':    'refresh_token',
        'refresh_token': REFRESH_TOKEN,
        'client_id':     CLIENT_ID,
        'client_secret': CLIENT_SECRET,
    })
    r.raise_for_status()
    return r.json()['access_token']

def spapi_get(access_token, path, params=None):
    """Make a SP-API GET request."""
    headers = {
        'x-amz-access-token': access_token,
        'x-amz-date': datetime.now().strftime('%Y%m%dT%H%M%SZ'),
        'Content-Type': 'application/json',
    }
    url = f'https://sellingpartnerapi-na.amazon.com{path}'
    r = requests.get(url, headers=headers, params=params)
    if r.status_code == 429:
        time.sleep(2)
        r = requests.get(url, headers=headers, params=params)
    r.raise_for_status()
    return r.json()

def get_sales_and_traffic(access_token, start_date, end_date):
    """Pull daily sales & traffic from Brand Analytics."""
    params = {
        'marketplaceIds': MARKETPLACE_ID,
        'interval':       'DAY',
        'granularity':    'DAY',
        'dataStartTime':  start_date,
        'dataEndTime':    end_date,
    }
    data = spapi_get(access_token, '/sales/v1/orderMetrics', params)
    return data.get('payload', [])

def get_financial_events(access_token, start_date, end_date):
    """Pull all financial events in 30-day chunks to avoid API limits."""
    from datetime import datetime, timedelta
    all_events = []
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end   = datetime.strptime(end_date,   '%Y-%m-%d')
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=30), end)
        params = {
            'PostedAfter':  chunk_start.strftime('%Y-%m-%dT00:00:00Z'),
            'PostedBefore': chunk_end.strftime('%Y-%m-%dT23:59:59Z'),
        }
        print(f"  Fetching {params['PostedAfter'][:10]} → {params['PostedBefore'][:10]}")
        while True:
            try:
                data = spapi_get(access_token, '/finances/v0/financialEvents', params)
                payload = data.get('payload', {}).get('FinancialEvents', {})
                if payload:
                    payload['_chunk_month'] = chunk_start.strftime('%Y-%m')
                    all_events.append(payload)
                next_token = data.get('payload', {}).get('NextToken')
                if not next_token:
                    break
                params = {'NextToken': next_token}
                time.sleep(0.5)
            except Exception as e:
                print(f"  Warning: {e} — retrying with 14-day window")
                # Retry with smaller 14-day sub-chunks
                sub_start = chunk_start
                while sub_start < chunk_end:
                    sub_end = min(sub_start + timedelta(days=14), chunk_end)
                    try:
                        sub_params = {
                            'PostedAfter':  sub_start.strftime('%Y-%m-%dT00:00:00Z'),
                            'PostedBefore': sub_end.strftime('%Y-%m-%dT23:59:59Z'),
                        }
                        sub_data = spapi_get(access_token, '/finances/v0/financialEvents', sub_params)
                        sub_payload = sub_data.get('payload', {}).get('FinancialEvents', {})
                        if sub_payload:
                            all_events.append(sub_payload)
                    except Exception as e2:
                        print(f"  Sub-chunk failed: {e2} — skipping")
                    sub_start = sub_end + timedelta(days=1)
                    time.sleep(0.5)
                break
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(0.3)
    return all_events

def get_last_settlement_date(access_token):
    """Get the end date of the most recently closed settlement period."""
    try:
        from datetime import datetime, timedelta
        # financialEventGroups needs date range
        end = datetime.now()
        start = end - timedelta(days=60)
        data = spapi_get(access_token, '/finances/v0/financialEventGroups', {
            'FinancialEventGroupStartedAfter': start.strftime('%Y-%m-%dT00:00:00Z'),
            'FinancialEventGroupStartedBefore': end.strftime('%Y-%m-%dT23:59:59Z'),
        })
        groups = data.get('payload', {}).get('FinancialEventGroupList', [])
        # Find most recently CLOSED group
        closed = [g for g in groups if g.get('ProcessingStatus') == 'Closed']
        if closed:
            last_closed = sorted(closed, key=lambda x: x.get('FinancialEventGroupEnd',''))[-1]
            date_str = last_closed.get('FinancialEventGroupEnd', '')[:10]
            print(f"  Last closed settlement: {date_str}")
            return date_str
    except Exception as e:
        print(f"  Could not get settlement date: {e}")
    return None

def get_inventory(access_token):
    """Pull FBA inventory levels."""
    try:
        params = {
            'granularityType': 'Marketplace',
            'granularityId':   MARKETPLACE_ID,
            'marketplaceIds':  MARKETPLACE_ID,
        }
        data = spapi_get(access_token, '/fba/inventory/v1/summaries', params)
        return data.get('payload', {}).get('inventorySummaries', [])
    except Exception as e:
        print(f"  Inventory fetch failed: {e} — skipping")
        return []

def get_recent_orders(access_token, start_date):
    """Pull recent orders from Orders API to capture unsettled revenue."""
    try:
        params = {
            'MarketplaceIds':    MARKETPLACE_ID,
            'CreatedAfter':      start_date + 'T00:00:00Z',
            'OrderStatuses':     'Unshipped,PartiallyShipped,Shipped,InvoiceUnconfirmed',
        }
        all_orders = []
        while True:
            data = spapi_get(access_token, '/orders/v0/orders', params)
            orders = data.get('payload', {}).get('Orders', [])
            all_orders.extend(orders)
            next_token = data.get('payload', {}).get('NextToken')
            if not next_token:
                break
            params = {'NextToken': next_token}
            time.sleep(0.5)
        print(f"  ✓ {len(all_orders)} recent orders fetched")
        return all_orders
    except Exception as e:
        print(f"  Orders API failed: {e} — skipping")
        return []

def get_order_items(access_token, order_id):
    """Get line items for a specific order."""
    try:
        data = spapi_get(access_token, f'/orders/v0/orders/{order_id}/orderItems')
        return data.get('payload', {}).get('OrderItems', [])
    except Exception:
        return []

def get_sales_metrics(access_token, start_date, end_date, granularity='Total'):
    """Pull order metrics from Sales API - accurate daily/period sales figures."""
    try:
        interval = f"{start_date}T00:00:00Z--{end_date}T23:59:59Z"
        params = {
            'marketplaceIds': MARKETPLACE_ID,
            'interval': interval,
            'granularity': granularity,
        }
        data = spapi_get(access_token, '/sales/v1/orderMetrics', params)
        return data.get('payload', [])
    except Exception as e:
        print(f"  Sales metrics failed: {e}")
        return []

def calc_unsettled_revenue(access_token, settled_monthly, start_of_open_period):
    """
    Pull orders from the open settlement period.
    Returns total revenue, daily breakdown, and units per SKU.
    """
    from datetime import datetime, date
    print(f"  Fetching unsettled orders from {start_of_open_period}...")
    orders = get_recent_orders(access_token, start_of_open_period)
    
    unsettled_total = 0
    daily_sales = {}   # date -> amount
    units_by_sku = {}  # sku -> units (for open period)
    
    for order in orders:
        order_total = float(order.get('OrderTotal', {}).get('Amount', 0))
        status = order.get('OrderStatus', '')
        purchase_date = order.get('PurchaseDate', '')[:10]  # YYYY-MM-DD
        order_id = order.get('AmazonOrderId', '')
        
        if status in ('Shipped', 'InvoiceUnconfirmed', 'Unshipped', 'PartiallyShipped') and order_total > 0:
            unsettled_total += order_total
            daily_sales[purchase_date] = daily_sales.get(purchase_date, 0) + order_total
            
            # Get line items for unit counts per SKU
            if order_id:
                items = get_order_items(access_token, order_id)
                for item in items:
                    sku = item.get('SellerSKU', '')
                    qty = int(item.get('QuantityOrdered', 0))
                    if sku and qty > 0:
                        units_by_sku[sku] = units_by_sku.get(sku, 0) + qty

    print(f"  Unsettled revenue estimate: ${unsettled_total:.2f}")
    return round(unsettled_total, 2), daily_sales, units_by_sku

def process_financial_events(events_list):
    """Bucket all financial events into P&L categories."""
    buckets = {
        # Order related
        'gross_sales': 0, 'shipping_income': 0, 'promotions': 0,
        'refunds': 0, 'refund_fee_credit': 0,
        'fba_fees': 0, 'referral_fees': 0, 'other_order': 0,
        # Logistics
        'fba_storage': 0, 'fba_storage_lt': 0,
        'fba_removal': 0, 'fba_disposal': 0, 'inbound_transport': 0,
        'liquidations': 0, 'reimbursements': 0,
        'awd_storage': 0, 'awd_transport': 0, 'awd_processing': 0,
        # Marketing
        'vine': 0, 'subscription': 0, 'ad_spend': 0,
        # Legacy catch-all
        'other': 0,
    }
    units_by_sku = {}
    sales_by_sku = {}
    fba_fees_by_sku = {}
    referral_fees_by_sku = {}
    monthly_fees = {}
    monthly_awd  = {}
    monthly_sales_map = {}
    monthly_sales_by_sku = {}   # {month: {sku: amount}}
    monthly_units_by_sku = {}   # {month: {sku: qty}}
    monthly_fba_by_sku   = {}   # {month: {sku: amount}}
    monthly_ref_by_sku   = {}   # {month: {sku: amount}}
    monthly_promos        = {}
    monthly_refunds       = {}
    monthly_refund_cr     = {}
    monthly_reimbursements = {}
    monthly_shipping      = {}
    monthly_subscription  = {}
    monthly_vine          = {}
    monthly_awd_storage   = {}
    monthly_awd_transport = {}
    monthly_awd_processing = {}
    monthly_fba_storage   = {}
    monthly_fba_storage_lt = {}
    monthly_inbound       = {}
    monthly_liquidations  = {}

    for events in events_list:
        # Order events
        for order in events.get('ShipmentEventList', []):
            posted = order.get('PostedDate', '')[:7]  # YYYY-MM
            for item in order.get('ShipmentItemList', []):
                sku = item.get('SellerSKU', '')
                qty = int(item.get('QuantityShipped', 0))
                for charge in item.get('ItemChargeList', []):
                    amt = float(charge.get('ChargeAmount', {}).get('CurrencyAmount', 0))
                    ctype = charge.get('ChargeType', '')
                    if ctype == 'Principal':
                        buckets['gross_sales'] += amt
                        sales_by_sku[sku] = sales_by_sku.get(sku, 0) + amt
                        monthly_sales_map[posted] = monthly_sales_map.get(posted, 0) + amt
                        # Only count paid units (not Vine $0 orders) for avg price calc
                        paid_qty = qty if amt > 0 else 0
                        units_by_sku[sku] = units_by_sku.get(sku, 0) + paid_qty
                        # Monthly SKU tracking
                        if posted:
                            if posted not in monthly_sales_by_sku:
                                monthly_sales_by_sku[posted] = {}
                            monthly_sales_by_sku[posted][sku] = monthly_sales_by_sku[posted].get(sku, 0) + amt
                            if posted not in monthly_units_by_sku:
                                monthly_units_by_sku[posted] = {}
                            monthly_units_by_sku[posted][sku] = monthly_units_by_sku[posted].get(sku, 0) + paid_qty
                    elif ctype == 'Shipping':
                        buckets['shipping_income'] += amt
                for fee in item.get('ItemFeeList', []):
                    amt = float(fee.get('FeeAmount', {}).get('CurrencyAmount', 0))
                    ftype = fee.get('FeeType', '')
                    if ftype in ('FBAPerUnitFulfillmentFee','FBAPerOrderFulfillmentFee','FBAWeightBasedFee'):
                        buckets['fba_fees'] += amt
                        monthly_fees[posted] = monthly_fees.get(posted, 0) + amt
                        fba_fees_by_sku[sku] = fba_fees_by_sku.get(sku, 0) + amt
                        if posted:
                            if posted not in monthly_fba_by_sku: monthly_fba_by_sku[posted] = {}
                            monthly_fba_by_sku[posted][sku] = monthly_fba_by_sku[posted].get(sku, 0) + amt
                    elif ftype in ('Commission', 'VariableClosingFee', 'FixedClosingFee'):
                        buckets['referral_fees'] += amt
                        monthly_fees[posted] = monthly_fees.get(posted, 0) + amt
                        referral_fees_by_sku[sku] = referral_fees_by_sku.get(sku, 0) + amt
                        if posted:
                            if posted not in monthly_ref_by_sku: monthly_ref_by_sku[posted] = {}
                            monthly_ref_by_sku[posted][sku] = monthly_ref_by_sku[posted].get(sku, 0) + amt
                for promo in item.get('PromotionList', []):
                    amt = float(promo.get('PromotionAmount', {}).get('CurrencyAmount', 0))
                    buckets['promotions'] += amt

        # Refund events
        for refund in events.get('RefundEventList', []):
            r_posted = refund.get('PostedDate', '')[:7] or chunk_month
            for item in refund.get('ShipmentItemAdjustmentList', []):
                for charge in item.get('ItemChargeAdjustmentList', []):
                    amt = float(charge.get('ChargeAmount', {}).get('CurrencyAmount', 0))
                    buckets['refunds'] += amt
                    if r_posted: monthly_refunds[r_posted] = monthly_refunds.get(r_posted,0) + amt
                for fee in item.get('ItemFeeAdjustmentList', []):
                    amt = float(fee.get('FeeAmount', {}).get('CurrencyAmount', 0))
                    buckets['refund_fee_credit'] += amt
                    if r_posted: monthly_refund_cr[r_posted] = monthly_refund_cr.get(r_posted,0) + amt

        # Service fee events (AWD, subscription, etc)
        # Use chunk month as fallback for AWD/service fee events that have no PostedDate
        chunk_month = events.get('_chunk_month', '')
        for fee_event in events.get('ServiceFeeEventList', []):
            raw_date = (fee_event.get('PostedDate') or 
                       fee_event.get('TransactionPostedDate') or '')
            posted = str(raw_date)[:7] if raw_date else chunk_month
            for fee in fee_event.get('FeeList', []):
                amt = float(fee.get('FeeAmount', {}).get('CurrencyAmount', 0))
                fname = fee.get('FeeType', '')
                if fname == 'AmazonUpstreamStorageTransportationFee':
                    buckets['awd_transport'] += amt
                    monthly_awd[posted] = monthly_awd.get(posted, 0) + amt
                    if posted: monthly_awd_transport[posted] = monthly_awd_transport.get(posted,0) + amt
                elif fname == 'AmazonUpstreamProcessingFee':
                    buckets['awd_processing'] += amt
                    monthly_awd[posted] = monthly_awd.get(posted, 0) + amt
                    if posted: monthly_awd_processing[posted] = monthly_awd_processing.get(posted,0) + amt
                elif fname in ('FBAStorageFee',):
                    buckets['fba_storage'] += amt
                    monthly_awd[posted] = monthly_awd.get(posted, 0) + amt
                    if posted: monthly_fba_storage[posted] = monthly_fba_storage.get(posted,0) + amt
                elif fname in ('STARStorageFee', 'FBALongTermStorageFee', 'FBALongTermStorageFee24'):
                    buckets['fba_storage_lt'] += amt
                    monthly_awd[posted] = monthly_awd.get(posted, 0) + amt
                    if posted: monthly_fba_storage_lt[posted] = monthly_fba_storage_lt.get(posted,0) + amt
                elif fname == 'Subscription':
                    buckets['subscription'] += amt
                    if posted: monthly_subscription[posted] = monthly_subscription.get(posted,0) + amt
                elif 'Vine' in fname or 'vine' in fname.lower():
                    buckets['vine'] += amt
                    if posted: monthly_vine[posted] = monthly_vine.get(posted,0) + amt
                elif 'Removal' in fname:
                    buckets['fba_removal'] += amt
                    monthly_awd[posted] = monthly_awd.get(posted, 0) + amt
                elif 'Disposal' in fname:
                    buckets['fba_disposal'] += amt
                    monthly_awd[posted] = monthly_awd.get(posted, 0) + amt
                elif 'Liquidat' in fname:
                    buckets['liquidations'] += amt
                    monthly_awd[posted] = monthly_awd.get(posted, 0) + amt
                elif fname in ('FBAInboundTransportationFee', 'FBAInboundConvenienceFee'):
                    buckets['inbound_transport'] += amt
                    monthly_awd[posted] = monthly_awd.get(posted, 0) + amt
                    if posted: monthly_inbound[posted] = monthly_inbound.get(posted,0) + amt
                else:
                    buckets['other'] += amt   # Reimbursements
        for reimb in events.get('ReimbursementEventList', []):
            amt = float(reimb.get('AmountTotal', {}).get('CurrencyAmount', 0))
            buckets['reimbursements'] += amt
            r_posted = chunk_month
            if r_posted: monthly_reimbursements[r_posted] = monthly_reimbursements.get(r_posted,0) + amt

    return (buckets, units_by_sku, sales_by_sku, monthly_sales_map, monthly_fees, monthly_awd,
            fba_fees_by_sku, referral_fees_by_sku,
            monthly_sales_by_sku, monthly_units_by_sku, monthly_fba_by_sku, monthly_ref_by_sku,
            monthly_promos, monthly_refunds, monthly_refund_cr, monthly_reimbursements,
            monthly_shipping, monthly_subscription, monthly_vine,
            monthly_awd_storage, monthly_awd_transport, monthly_awd_processing,
            monthly_fba_storage, monthly_fba_storage_lt, monthly_inbound, monthly_liquidations)

def load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), 'data', 'config.json')
    defaults = {
        'deficit_start': 0,
        'goal_monthly': 100000,
        'account_name': 'A1 American',
        'launch_date': '2026-01-01',
    }
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            defaults.update(json.load(f))
    return defaults

def run():
    cfg = load_config()
    print("Getting access token...")
    token = get_access_token()
    print("✓ Access token obtained")

    today      = date.today().isoformat()
    today_dt   = date.today()
    start_date = f"{today[:4]}-01-01"  # Always pull current year only

    # Get last closed settlement period date (more precise than hardcoded 14 days)
    print("Getting last closed settlement date...")
    last_settlement = get_last_settlement_date(token)
    from datetime import timedelta
    last_closed_date = last_settlement if last_settlement else (today_dt - timedelta(days=14)).isoformat()
    print(f"Pulling financial events {start_date} → {last_closed_date}...")
    fin_events = get_financial_events(token, start_date, last_closed_date)
    print(f"✓ {len(fin_events)} event batches received")

    (buckets, units_by_sku, sales_by_sku, monthly_sales_map, monthly_fees, monthly_awd,
     fba_fees_by_sku, referral_fees_by_sku,
     monthly_sales_by_sku, monthly_units_by_sku, monthly_fba_by_sku, monthly_ref_by_sku,
     monthly_promos, monthly_refunds, monthly_refund_cr, monthly_reimbursements,
     monthly_shipping, monthly_subscription, monthly_vine,
     monthly_awd_storage, monthly_awd_transport, monthly_awd_processing,
     monthly_fba_storage, monthly_fba_storage_lt, monthly_inbound, monthly_liquidations) = process_financial_events(fin_events)

    # Pull inventory
    print("Pulling inventory...")
    inventory_raw = get_inventory(token)
    inventory = {}
    for item in inventory_raw:
        sku = item.get('sellerSku', '')
        qty = item.get('inventoryDetails', {}).get('fulfillableQuantity', 0) or \
              item.get('totalQuantity', 0)
        if sku:
            inventory[sku] = int(qty)
    print(f"✓ {len(inventory)} SKUs in inventory")

    # COGS calculation
    total_cogs = sum(units_by_sku.get(sku, 0) * cost for sku, cost in COGS_MAP.items())

    # Load ads data from CSV if present (until Ads API is connected)
    ad_spend = 0; ad_sales = 0; ad_clicks = 0; ad_orders = 0
    monthly_ads = {}; weekly_ads = {}; campaigns = []; sku_ads = {}
    ads_path = os.path.join(os.path.dirname(__file__), 'data')
    import glob, pandas as pd
    ads_files = [f for f in glob.glob(os.path.join(ads_path, '*.csv'))
                 if 'Campaign name' in pd.read_csv(f, nrows=1, on_bad_lines='skip').columns]
    if ads_files:
        ads_df = pd.concat([pd.read_csv(f, on_bad_lines='skip') for f in ads_files])
        for col in ['Total cost','Sales','Clicks','Purchases']:
            ads_df[col] = pd.to_numeric(ads_df[col], errors='coerce').fillna(0)
        # Handle both daily (Date column) and summary (Date range column) formats
        if 'Date' in ads_df.columns:
            ads_df['Date'] = pd.to_datetime(ads_df['Date'], errors='coerce')
        elif 'Date range' in ads_df.columns:
            # Parse "Apr 15, 2026 - Jul 23, 2026" -> use start date
            def parse_dr(s):
                try: return pd.to_datetime(str(s).split(' - ')[0].strip())
                except: return pd.NaT
            ads_df['Date'] = ads_df['Date range'].apply(parse_dr)
        if 'Date' in ads_df.columns:
            ads_df['month'] = ads_df['Date'].dt.strftime('%Y-%m')
            ads_df['week']  = ads_df['Date'].dt.to_period('W').apply(
                lambda x: str(x.start_time.date()))
            for m, grp in ads_df.groupby('month'):
                sp = float(grp['Total cost'].sum())
                sa = float(grp['Sales'].sum())
                monthly_ads[m] = {'spend': round(sp,2), 'sales': round(sa,2),
                    'clicks': int(grp['Clicks'].sum()), 'orders': int(grp['Purchases'].sum()),
                    'roas': round(sa/sp,2) if sp > 0 else 0}
            for w, grp in ads_df.groupby('week'):
                sp = float(grp['Total cost'].sum())
                sa = float(grp['Sales'].sum())
                weekly_ads[w] = {'spend': round(sp,2), 'sales': round(sa,2),
                    'clicks': int(grp['Clicks'].sum()), 'orders': int(grp['Purchases'].sum()),
                    'roas': round(sa/sp,2) if sp > 0 else 0}
        # SKU-level ad attribution
        if 'Advertised product SKU' in ads_df.columns:
            sku_grp = ads_df.groupby('Advertised product SKU').agg(
                spend=('Total cost', 'sum'),
                sales=('Sales', 'sum'),
                orders=('Purchases', 'sum')
            ).reset_index()
            for _, row in sku_grp.iterrows():
                raw_sku = str(row['Advertised product SKU'])
                name = PRODUCT_NAMES.get(raw_sku, raw_sku)
                sp2 = float(row['spend'])
                sa2 = float(row['sales'])
                sku_ads[name] = {
                    'spend': round(sp2, 2),
                    'sales': round(sa2, 2),
                    'orders': int(row['orders']),
                    'roas': round(sa2/sp2, 2) if sp2 > 0 else 0,
                }
        ad_spend  = round(float(ads_df['Total cost'].sum()), 2)
        ad_sales  = round(float(ads_df['Sales'].sum()), 2)
        ad_clicks = int(ads_df['Clicks'].sum())
        ad_orders = int(ads_df['Purchases'].sum())
        camp = ads_df.groupby('Campaign name').agg(
            spend=('Total cost','sum'), sales=('Sales','sum'),
            clicks=('Clicks','sum'), orders=('Purchases','sum')
        ).reset_index()
        camp['roas'] = (camp['sales'] / camp['spend'].replace(0, float('nan'))).round(2)
        campaigns = camp.sort_values('spend', ascending=False).head(10).to_dict('records')

    roas = round(ad_sales / ad_spend, 2) if ad_spend > 0 else 0

    # Net result
    gross_sales     = round(buckets['gross_sales'], 2)
    total_amz_fees  = round(sum([
                                  buckets['fba_fees'], buckets['referral_fees'],
                                  buckets['fba_storage'], buckets['fba_storage_lt'],
                                  buckets['fba_removal'], buckets['fba_disposal'],
                                  buckets['inbound_transport'], buckets['liquidations'],
                                  buckets['awd_storage'], buckets['awd_transport'],
                                  buckets['awd_processing'],
                                  buckets['vine'], buckets['subscription'],
                              ]), 2)
    net_result = round(gross_sales + buckets['shipping_income'] + buckets['promotions'] +
                       buckets['refunds'] + buckets['refund_fee_credit'] +
                       total_amz_fees + buckets['reimbursements'] -
                       total_cogs - ad_spend, 2)

    deficit_remaining = round(max(0, cfg['deficit_start'] - max(0, -net_result)), 2)

    # MTD calculation — supplement with Orders API for unsettled period
    today_dt  = date.today()
    mtd_start = today_dt.replace(day=1).isoformat()
    mtd_settled = sum(v for m, v in monthly_sales_map.items()
                      if m == today_dt.strftime('%Y-%m'))

    # Get unsettled orders for current open period (start of current month)
    from datetime import timedelta
    open_period_start = today_dt.replace(day=1).isoformat()
    print("Fetching unsettled orders for open period...")
    unsettled, daily_orders, unsettled_units_by_sku = calc_unsettled_revenue(token, monthly_sales_map, open_period_start)

    # Use higher of settled MTD or orders API MTD (avoid double counting)
    mtd_sales = max(mtd_settled, unsettled)
    print(f"  MTD settled: ${mtd_settled:.2f} | Orders API: ${unsettled:.2f} | Using: ${mtd_sales:.2f}")

    # Update current month with better figure
    current_month = today_dt.strftime('%Y-%m')
    if mtd_sales > monthly_sales_map.get(current_month, 0):
        monthly_sales_map[current_month] = mtd_sales

    # Build daily sales from Orders API for rolling period calculations
    # Merge with any settled daily data
    daily_sales_map = {}
    for date_str, amount in daily_orders.items():
        daily_sales_map[date_str] = round(amount, 2)

    # Calculate rolling 30 and 14 day sales from daily data
    from datetime import timedelta
    rolling_30_start = (today_dt - timedelta(days=30)).isoformat()
    rolling_14_start = (today_dt - timedelta(days=14)).isoformat()
    # Use Sales API for accurate rolling period figures
    print("Fetching rolling period sales from Sales API...")
    from datetime import timedelta
    day_30_ago = (today_dt - timedelta(days=30)).isoformat()
    day_14_ago = (today_dt - timedelta(days=14)).isoformat()
    day_7_ago  = (today_dt - timedelta(days=7)).isoformat()

    metrics_30  = get_sales_metrics(token, day_30_ago, today)
    metrics_14  = get_sales_metrics(token, day_14_ago, today)
    metrics_7   = get_sales_metrics(token, day_7_ago,  today)
    metrics_mtd = get_sales_metrics(token, today_dt.replace(day=1).isoformat(), today)
    ytd_start = f"{today[:4]}-01-01"  # Always current year for YTD
    metrics_ytd = get_sales_metrics(token, ytd_start, today)

    rolling_30_sales = float(metrics_30[0]['totalSales']['amount'])  if metrics_30  else 0
    rolling_14_sales = float(metrics_14[0]['totalSales']['amount'])  if metrics_14  else 0
    rolling_7_sales  = float(metrics_7[0]['totalSales']['amount'])   if metrics_7   else 0
    mtd_from_api     = float(metrics_mtd[0]['totalSales']['amount']) if metrics_mtd else mtd_sales
    ytd_from_api     = float(metrics_ytd[0]['totalSales']['amount']) if metrics_ytd else gross_sales

    rolling_30_units = int(metrics_30[0]['unitCount'])  if metrics_30  else 0
    rolling_14_units = int(metrics_14[0]['unitCount'])  if metrics_14  else 0
    mtd_units        = int(metrics_mtd[0]['unitCount']) if metrics_mtd else 0
    ytd_units        = int(metrics_ytd[0]['unitCount']) if metrics_ytd else 0

    # Merge unsettled units (from Orders API) into settled units_by_sku
    for sku, qty in unsettled_units_by_sku.items():
        units_by_sku[sku] = units_by_sku.get(sku, 0) + qty
    print(f"  Merged {len(unsettled_units_by_sku)} SKUs from unsettled orders into unit counts")

    # Use Sales API figures — more accurate as they include ordered not just settled
    mtd_sales = max(mtd_sales, mtd_from_api)

    print(f"  YTD (Sales API): ${ytd_from_api:.2f} | MTD: ${mtd_from_api:.2f}")
    print(f"  Rolling 30d: ${rolling_30_sales:.2f} | Rolling 14d: ${rolling_14_sales:.2f}")

    # Inventory days of stock
    inventory_status = {}
    total_units_sold  = sum(units_by_sku.values())
    period_days = max(1, (date.today() - date.fromisoformat(start_date)).days)
    for sku, qty_in_stock in inventory.items():
        daily_rate = units_by_sku.get(sku, 0) / period_days
        days_left  = round(qty_in_stock / daily_rate) if daily_rate > 0 else 999
        inventory_status[PRODUCT_NAMES.get(sku, sku)] = {
            'sku': sku, 'in_stock': qty_in_stock,
            'daily_rate': round(daily_rate, 2),
            'days_remaining': days_left,
            'reorder_alert': days_left < 45,
        }

    # Monthly net estimate
    monthly_net = {}
    for m in sorted(set(list(monthly_sales_map.keys()) + list(monthly_fees.keys()))):
        s  = monthly_sales_map.get(m, 0)
        f  = monthly_fees.get(m, 0)
        a  = monthly_awd.get(m, 0)
        ad = monthly_ads.get(m, {}).get('spend', 0)
        cogs_est = total_cogs * (s / max(1, gross_sales)) if gross_sales > 0 else 0
        monthly_net[m] = round(s + f + a - ad - cogs_est, 2)

    # Build weekly sales from daily orders
    weekly_sales_out = {}
    for date_str, amount in daily_sales_map.items():
        try:
            from datetime import timedelta as td2
            d_obj = __import__('datetime').date.fromisoformat(date_str)
            week_start = (d_obj - __import__('datetime').timedelta(days=d_obj.weekday())).isoformat()
            weekly_sales_out[week_start] = round(weekly_sales_out.get(week_start, 0) + amount, 2)
        except Exception:
            pass
    weekly_sales_out = dict(sorted(weekly_sales_out.items()))

    # Merge duplicate SKUs that map to same product name
    def merge_by_name(d, round_vals=False):
        result = {}
        for k, v in d.items():
            name = PRODUCT_NAMES.get(k, k)
            result[name] = result.get(name, 0) + (round(v, 2) if round_vals else v)
        return result

    out = {
        'generated_at': datetime.now().isoformat() + 'Z',
        'source': 'sp-api',
        'config': cfg,
        'date_range': {'start': start_date, 'end': today},
        'summary': {
            'gross_sales':      gross_sales,
            'shipping_income':  round(buckets['shipping_income'], 2),
            'promotions':       round(buckets['promotions'], 2),
            'refunds':          round(buckets['refunds'], 2),
            'refund_fee_credit':round(buckets['refund_fee_credit'], 2),
            'fba_fees':         round(buckets['fba_fees'], 2),
            'referral_fees':    round(buckets['referral_fees'], 2),
            'fba_storage':      round(buckets['fba_storage'], 2),
            'fba_storage_lt':   round(buckets['fba_storage_lt'], 2),
            'fba_removal':      round(buckets['fba_removal'], 2),
            'fba_disposal':     round(buckets['fba_disposal'], 2),
            'inbound_transport':round(buckets['inbound_transport'], 2),
            'liquidations':     round(buckets['liquidations'], 2),
            'awd_storage':      round(buckets['awd_storage'], 2),
            'awd_transport':    round(buckets['awd_transport'], 2),
            'awd_processing':   round(buckets['awd_processing'], 2),
            'vine':             round(buckets['vine'], 2),
            'subscription':     round(buckets['subscription'], 2),
            'reimbursements':   round(buckets['reimbursements'], 2),
            'total_amazon_fees':total_amz_fees,
            'cogs':             -round(total_cogs, 2),
            'ad_spend':         -ad_spend,
            'net_result':       net_result,
        },
        'ads': {
            'spend': ad_spend, 'sales': ad_sales,
            'clicks': ad_clicks, 'orders': ad_orders,
            'roas': roas, 'campaigns': campaigns,
            'monthly': monthly_ads, 'weekly': weekly_ads,
        },
        'progress': {
            'deficit_start':     cfg['deficit_start'],
            'deficit_remaining': deficit_remaining,
            'pct_recovered':     round(min(100, max(0,
                (cfg['deficit_start'] - deficit_remaining) / max(1, cfg['deficit_start']) * 100)), 1),
            'goal_monthly':      cfg['goal_monthly'],
            'mtd_sales':         round(mtd_sales, 2),
            'mtd_days':          today_dt.day,
            'rolling_30d_sales': round(rolling_30_sales, 2),
            'rolling_14d_sales': round(rolling_14_sales, 2),
            'rolling_7d_sales':  round(rolling_7_sales, 2),
            'rolling_30d_units': rolling_30_units,
            'rolling_14d_units': rolling_14_units,
            'mtd_units':         mtd_units,
            'ytd_sales_api':     round(ytd_from_api, 2),
            'ytd_units_api':     ytd_units,
        },
        'sku_breakdown': {
            'sales': merge_by_name(sales_by_sku, round_vals=True),
            'units': merge_by_name(units_by_sku),
            'cogs_by_sku': (lambda d: {k: round(v,2) for k,v in d.items()})(
                {PRODUCT_NAMES.get(k,k): (lambda n,c: n*c)(units_by_sku.get(k,0), c)
                 for k, c in COGS_MAP.items() if units_by_sku.get(k, 0) > 0}),
            'fba_fees_by_sku': merge_by_name(fba_fees_by_sku, round_vals=True),
            'referral_fees_by_sku': merge_by_name(referral_fees_by_sku, round_vals=True),
            'ads_by_sku': sku_ads,
            'monthly_sales_by_sku': {m: merge_by_name(skus, round_vals=True)
                                      for m, skus in monthly_sales_by_sku.items()},
            'monthly_units_by_sku': {m: merge_by_name(skus)
                                      for m, skus in monthly_units_by_sku.items()},
            'monthly_fba_by_sku':   {m: merge_by_name(skus, round_vals=True)
                                      for m, skus in monthly_fba_by_sku.items()},
            'monthly_ref_by_sku':   {m: merge_by_name(skus, round_vals=True)
                                      for m, skus in monthly_ref_by_sku.items()},
        },
        'inventory': inventory_status,
        'trends': {
            'monthly_sales':  monthly_sales_map,
            'monthly_fees':   monthly_fees,
            'monthly_awd':    monthly_awd,
            'monthly_net':    monthly_net,
            'weekly_sales':   weekly_sales_out,
            'monthly_cogs':   {m: round(total_cogs * (monthly_sales_map.get(m,0) / max(1, gross_sales)), 2)
                               for m in monthly_sales_map},
            'monthly_ads':    {m: round(monthly_ads.get(m, {}).get('spend', 0), 2)
                               for m in monthly_sales_map},
            'monthly_promos':        monthly_promos,
            'monthly_refunds':       monthly_refunds,
            'monthly_refund_cr':     monthly_refund_cr,
            'monthly_reimbursements': monthly_reimbursements,
            'monthly_shipping':      monthly_shipping,
            'monthly_subscription':  monthly_subscription,
            'monthly_vine':          monthly_vine,
            'monthly_awd_storage':   monthly_awd_storage,
            'monthly_awd_transport': monthly_awd_transport,
            'monthly_awd_processing': monthly_awd_processing,
            'monthly_fba_storage':   monthly_fba_storage,
            'monthly_fba_storage_lt': monthly_fba_storage_lt,
            'monthly_inbound':       monthly_inbound,
            'monthly_liquidations':  monthly_liquidations,
        },
    }

    out_path = os.path.join(os.path.dirname(__file__), 'data.json')
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)
    print(f"✓ data.json written via SP-API — net result: ${net_result:+.2f}")
    print(f"  Gross sales: ${gross_sales:,.2f}  |  MTD: ${mtd_sales:,.2f}  |  ROAS: {roas:.2f}x")
    return out

if __name__ == '__main__':
    run()
