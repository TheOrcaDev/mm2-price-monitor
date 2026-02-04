"""
MM2 Price Monitor with Approval System
- Monitors StarPets prices
- Sends Discord embeds with APPROVE/DECLINE buttons
- Approve = Update BuyBlox to StarPets -1%
- Decline = Snooze item for 24 hours
"""

import os
import json
import time
import threading
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
import websocket

# Environment Variables
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # For buttons to work
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # Channel for price alerts
DISCORD_STOCK_CHANNEL_ID = os.getenv("DISCORD_STOCK_CHANNEL_ID")  # Channel for stock alerts
DISCORD_STOCK_ROLE_ID = os.getenv("DISCORD_STOCK_ROLE_ID", "1468341515393957984")  # Role to ping for stock alerts
DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY")  # For signature verification
ROLE_ID = os.getenv("DISCORD_ROLE_ID", "1468305257757933853")
ALLOWED_ROLE_IDS = os.getenv("ALLOWED_ROLE_IDS", "").split(",")  # Roles that can approve/decline
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")  # Upstash REST API
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
DISCORD_BUNDLE_CHANNEL_ID = os.getenv("DISCORD_BUNDLE_CHANNEL_ID", "1468338873754194004")  # Channel for bundle approvals
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "988112765489127424")  # User who can use $approveall/$declineall
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))  # Default 10 minutes
UNDERCUT_PERCENT = float(os.getenv("UNDERCUT_PERCENT", "0.01"))
PORT = int(os.getenv("PORT", "3000"))

# Files
PRICE_FILE = "starpets_prices.json"
SNOOZED_FILE = "snoozed_items.json"
PENDING_FILE = "pending_approvals.json"
ACTION_LOG_FILE = "actions.log"
STOCK_FILE = "stock_status.json"
BUNDLES_FILE = "bundles.json"  # Confirmed bundle compositions
PENDING_BUNDLES_FILE = "pending_bundles.json"  # Awaiting bundle confirmation
SNOOZED_STOCK_FILE = "snoozed_stock.json"  # Snoozed out-of-stock items

app = Flask(__name__)

# Upstash Redis REST API helpers
def redis_get(key):
    """Get value from Upstash Redis"""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        resp = requests.get(
            f"{UPSTASH_REDIS_REST_URL}/get/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            timeout=5
        )
        if resp.status_code == 200:
            result = resp.json().get('result')
            return result
    except:
        pass
    return None


def redis_set(key, value):
    """Set value in Upstash Redis"""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return False
    try:
        resp = requests.post(
            f"{UPSTASH_REDIS_REST_URL}",
            headers={
                "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
                "Content-Type": "application/json"
            },
            json=["SET", key, value],
            timeout=5
        )
        return resp.status_code == 200
    except:
        return False


print(f"Upstash Redis: {'Configured' if UPSTASH_REDIS_REST_URL else 'Not configured'}")


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def log_action(action, item_name, username, old_price=None, new_price=None):
    """Log approve/decline actions to file"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if action == "APPROVE":
        entry = f"[{timestamp}] APPROVED: {item_name} | ${old_price:.2f} -> ${new_price:.2f} | by {username}\n"
    else:
        entry = f"[{timestamp}] DECLINED: {item_name} | by {username}\n"

    try:
        with open(ACTION_LOG_FILE, 'a') as f:
            f.write(entry)
    except:
        pass
    log(entry.strip())


# ============ FILE HELPERS ============

def load_json(filename, default=None):
    if default is None:
        default = {}
    # Try Redis first
    if UPSTASH_REDIS_REST_URL:
        try:
            data = redis_get(f"mm2:{filename}")
            if data:
                return json.loads(data)
        except:
            pass
    # Fallback to file
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except:
            return default
    return default


def save_json(filename, data):
    # Save to Redis if available
    if UPSTASH_REDIS_REST_URL:
        try:
            redis_set(f"mm2:{filename}", json.dumps(data))
        except:
            pass
    # Also save to file as backup
    with open(filename, 'w') as f:
        json.dump(data, f)


# ============ SNOOZED ITEMS ============

def is_snoozed(item_key):
    """Check if item is snoozed (declined in last 24h)"""
    snoozed = load_json(SNOOZED_FILE)
    if item_key in snoozed:
        snooze_until = datetime.fromisoformat(snoozed[item_key])
        if datetime.now() < snooze_until:
            return True
        # Expired, remove it
        del snoozed[item_key]
        save_json(SNOOZED_FILE, snoozed)
    return False


def snooze_item(item_key, hours=24):
    """Snooze item for X hours"""
    snoozed = load_json(SNOOZED_FILE)
    snoozed[item_key] = (datetime.now() + timedelta(hours=hours)).isoformat()
    save_json(SNOOZED_FILE, snoozed)


# ============ SNOOZED STOCK ITEMS ============

def is_stock_snoozed(variant_id):
    """Check if stock item is snoozed"""
    snoozed = load_json(SNOOZED_STOCK_FILE)
    key = str(variant_id)
    if key in snoozed:
        snooze_until = datetime.fromisoformat(snoozed[key])
        if datetime.now() < snooze_until:
            return True
        del snoozed[key]
        save_json(SNOOZED_STOCK_FILE, snoozed)
    return False


def snooze_stock_item(variant_id, hours=24):
    """Snooze stock item for X hours"""
    snoozed = load_json(SNOOZED_STOCK_FILE)
    snoozed[str(variant_id)] = (datetime.now() + timedelta(hours=hours)).isoformat()
    save_json(SNOOZED_STOCK_FILE, snoozed)


# ============ PENDING APPROVALS ============

def add_pending(approval_id, data):
    pending = load_json(PENDING_FILE)
    pending[approval_id] = data
    save_json(PENDING_FILE, pending)


def get_pending(approval_id):
    pending = load_json(PENDING_FILE)
    return pending.get(approval_id)


def has_pending_for_item(item_key):
    """Check if there's already a pending approval for this item"""
    pending = load_json(PENDING_FILE)
    for data in pending.values():
        if data.get('item_key') == item_key:
            return True
    return False


def remove_pending(approval_id):
    pending = load_json(PENDING_FILE)
    if approval_id in pending:
        del pending[approval_id]
        save_json(PENDING_FILE, pending)


# ============ API CALLS ============

def get_starpets_prices():
    """Fetch StarPets prices"""
    api_url = "https://mm2-market.apineural.com/api/store/items/all"
    headers = {'content-type': 'application/json', 'origin': 'https://starpets.gg', 'referer': 'https://starpets.gg/'}

    items = {}
    for page in range(1, 25):
        try:
            payload = {
                'filter': {'types': [{'type': 'weapon'}, {'type': 'pet'}, {'type': 'misc'}]},
                'page': page, 'amount': 72, 'currency': 'usd', 'sort': {'popularity': 'desc'}
            }
            resp = requests.post(api_url, headers=headers, json=payload, timeout=60)
            data = resp.json().get('items', [])
            if not data:
                break

            for item in data:
                name = item.get('name', '').strip()
                price = item.get('price')
                rarity = item.get('rare', '')
                is_chroma = item.get('chroma', False) == True or rarity == 'chroma'
                item_type = item.get('type', 'weapon')  # weapon, pet, misc
                item_id = item.get('id', '')

                if price is None or rarity not in ['godly', 'ancient', 'vintage', 'legendary', 'chroma']:
                    continue

                key = f"{name.lower()}|{'chroma' if is_chroma else 'regular'}"
                if key not in items or float(price) < items[key]['price']:
                    # Build StarPets URL
                    name_slug = name.lower().replace(' ', '-').replace("'", '')
                    sp_url = f"https://starpets.gg/mm2/shop/{item_type}/{name_slug}/{item_id}"

                    items[key] = {
                        'name': name,
                        'price': float(price),
                        'rarity': rarity,
                        'is_chroma': is_chroma,
                        'sp_url': sp_url
                    }

            if len(data) < 72:
                break
            time.sleep(0.3)
        except Exception as e:
            log(f"StarPets error page {page}: {e}")
            break
    return items


def get_buyblox_prices():
    """Fetch BuyBlox prices with product info"""
    items = {}
    for page in [1, 2, 3, 4]:
        try:
            resp = requests.get(f'https://buyblox.gg/collections/mm2/products.json?page={page}&limit=250', timeout=60)
            products = resp.json().get('products', [])
            if not products:
                break

            for p in products:
                title = p['title'].strip()
                price = float(p['variants'][0]['price'])
                variant_id = p['variants'][0]['id']
                product_id = p['id']
                # Get product image
                image_url = p.get('images', [{}])[0].get('src', '') if p.get('images') else ''

                is_chroma = 'chroma' in title.lower()
                base_name = title.lower().replace('chroma ', '') if is_chroma else title.lower()

                key = f"{base_name}|{'chroma' if is_chroma else 'regular'}"
                items[key] = {
                    'name': title, 'price': price, 'variant_id': variant_id,
                    'product_id': product_id, 'image': image_url, 'is_chroma': is_chroma
                }
            time.sleep(0.3)
        except Exception as e:
            log(f"BuyBlox error page {page}: {e}")
            break
    return items


def update_shopify_price(variant_id, new_price):
    """Update BuyBlox price via Shopify API"""
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        return False

    url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/variants/{variant_id}.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"}
    payload = {"variant": {"id": variant_id, "price": str(new_price)}}

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=30)
        return resp.status_code == 200
    except Exception as e:
        log(f"Shopify update error: {e}")
        return False


def check_stock():
    """Check Shopify inventory and notify when items go out of stock"""
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        return

    log("Checking stock levels...")
    previous_stock = load_json(STOCK_FILE)
    current_stock = {}
    out_of_stock = []

    try:
        # Get all products with pagination
        headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
        all_products = []
        page_info = None

        while True:
            if page_info:
                url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/products.json?limit=250&page_info={page_info}"
            else:
                url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/products.json?limit=250&fields=id,title,variants,vendor,product_type,tags"

            resp = requests.get(url, headers=headers, timeout=60)
            if resp.status_code != 200:
                log(f"Shopify stock check error: {resp.status_code}")
                return

            products = resp.json().get('products', [])
            all_products.extend(products)

            # Check for next page
            link_header = resp.headers.get('Link', '')
            if 'rel="next"' in link_header:
                # Extract page_info from Link header
                import re
                match = re.search(r'page_info=([^>]+)>; rel="next"', link_header)
                if match:
                    page_info = match.group(1)
                else:
                    break
            else:
                break

        # Filter to MM2 products by checking collection
        # First, get MM2 collection ID
        mm2_collection_id = None
        try:
            coll_url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/custom_collections.json"
            coll_resp = requests.get(coll_url, headers=headers, timeout=30)
            if coll_resp.status_code == 200:
                for coll in coll_resp.json().get('custom_collections', []):
                    if 'mm2' in coll.get('handle', '').lower() or 'murder' in coll.get('title', '').lower():
                        mm2_collection_id = coll['id']
                        log(f"Found MM2 collection: {coll.get('title')} (ID: {mm2_collection_id})")
                        break
        except Exception as e:
            log(f"Error getting collections: {e}")

        # Get products from MM2 collection if found, otherwise use keyword filter
        if mm2_collection_id:
            try:
                collect_url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/collects.json?collection_id={mm2_collection_id}&limit=250"
                collect_resp = requests.get(collect_url, headers=headers, timeout=30)
                mm2_product_ids = set()
                if collect_resp.status_code == 200:
                    for collect in collect_resp.json().get('collects', []):
                        mm2_product_ids.add(collect['product_id'])
                products = [p for p in all_products if p['id'] in mm2_product_ids]
                log(f"Found {len(products)} MM2 products from collection")
            except Exception as e:
                log(f"Error getting collection products: {e}")
                products = all_products
        else:
            # Fallback to keyword filter
            mm2_keywords = ['murder mystery 2', 'mm2', 'murder-mystery-2', 'murder mystery']
            mm2_products = []
            for p in all_products:
                vendor = (p.get('vendor') or '').lower()
                product_type = (p.get('product_type') or '').lower()
                tags = (p.get('tags') or '').lower()
                title = (p.get('title') or '').lower()

                is_mm2 = any(kw in vendor or kw in product_type or kw in tags or kw in title for kw in mm2_keywords)
                if is_mm2:
                    mm2_products.append(p)
            products = mm2_products
            log(f"Found {len(products)} MM2 products (keyword filter)")

        for product in products:
            title = product['title']
            for variant in product.get('variants', []):
                inventory = variant.get('inventory_quantity', 0)
                variant_id = variant['id']
                key = str(variant_id)

                current_stock[key] = {
                    'title': title,
                    'inventory': inventory
                }

                # Check if out of stock
                now_out_of_stock = inventory <= 0

                # Notify for any item that is out of stock (not snoozed)
                if now_out_of_stock and not is_stock_snoozed(variant_id):
                    image_url = product.get('images', [{}])[0].get('src', '') if product.get('images') else ''
                    log(f"Out of stock: {title}")
                    out_of_stock.append({'title': title, 'variant_id': variant_id, 'image': image_url})

        # Save current stock
        save_json(STOCK_FILE, current_stock)

        # Send notifications for out of stock items
        if out_of_stock and DISCORD_BOT_TOKEN:
            for item in out_of_stock:  # Send all
                send_stock_alert(item['title'], item['variant_id'], item.get('image'))
                time.sleep(1)

        log(f"Stock check done. {len(out_of_stock)} items went out of stock.")

    except Exception as e:
        log(f"Stock check error: {e}")


# ============ BUNDLE SYSTEM ============

def is_bundle_product(title):
    """Check if product is a bundle/set"""
    title_lower = title.lower()
    return 'set' in title_lower or 'bundle' in title_lower


def extract_items_from_description(description):
    """Try to extract item names from bundle description"""
    if not description:
        return []

    # Clean HTML tags but preserve newlines
    clean_desc = re.sub(r'<br\s*/?>', '\n', description)
    clean_desc = re.sub(r'<[^>]+>', ' ', clean_desc)
    clean_desc = clean_desc.strip()

    items = []

    # Pattern 1: "Includes:" followed by newline-separated items like "Amerilaser (Gun)"
    include_match = re.search(r'includes?[:\s]*\n(.+?)(?:\n\n|why|$)', clean_desc, re.IGNORECASE | re.DOTALL)
    if include_match:
        items_section = include_match.group(1)
        lines = items_section.strip().split('\n')
        for line in lines:
            line = line.strip()
            # Remove type info like "(Gun)", "(Knife)", "(Pet)"
            item_name = re.sub(r'\s*\([^)]+\)\s*$', '', line).strip()
            if item_name and len(item_name) > 1 and len(item_name) < 50:
                items.append(item_name.lower())

    # Pattern 2: "with X and Y" or "with X, Y and Z"
    if not items:
        with_match = re.search(r'with\s+(.+?)(?:\.|$)', clean_desc, re.IGNORECASE)
        if with_match:
            items_str = with_match.group(1)
            parts = re.split(r'\s+and\s+|,\s*', items_str)
            for part in parts:
                part = part.strip().rstrip('.')
                if part and len(part) > 2 and len(part) < 50:
                    items.append(part.lower())

    # Pattern 3: "includes X, Y and Z" (inline)
    if not items:
        include_match = re.search(r'includes?\s+([^.]+)', clean_desc, re.IGNORECASE)
        if include_match:
            items_str = include_match.group(1)
            parts = re.split(r'\s+and\s+|,\s*', items_str)
            for part in parts:
                part = part.strip().rstrip('.')
                # Remove type info
                part = re.sub(r'\s*\([^)]+\)\s*$', '', part).strip()
                if part and len(part) > 2 and len(part) < 50:
                    items.append(part.lower())

    return items[:10]  # Limit to 10 items max


def match_items_to_products(item_names, all_products):
    """Match extracted item names to actual products"""
    matched = []
    for item_name in item_names:
        item_lower = item_name.lower().strip()
        for product in all_products:
            product_title = product['title'].lower()
            # Check if item name matches product title
            if item_lower in product_title or product_title in item_lower:
                matched.append({
                    'product_id': product['id'],
                    'variant_id': product['variants'][0]['id'],
                    'title': product['title'],
                    'price': float(product['variants'][0]['price'])
                })
                break
    return matched


def get_bundle(bundle_product_id):
    """Get confirmed bundle composition"""
    bundles = load_json(BUNDLES_FILE)
    return bundles.get(str(bundle_product_id))


def save_bundle(bundle_product_id, name, item_ids):
    """Save confirmed bundle composition"""
    bundles = load_json(BUNDLES_FILE)
    bundles[str(bundle_product_id)] = {
        'name': name,
        'item_ids': item_ids  # List of variant IDs
    }
    save_json(BUNDLES_FILE, bundles)


def add_pending_bundle(approval_id, data):
    """Add pending bundle confirmation"""
    pending = load_json(PENDING_BUNDLES_FILE)
    pending[approval_id] = data
    save_json(PENDING_BUNDLES_FILE, pending)


def get_pending_bundle(approval_id):
    """Get pending bundle confirmation"""
    pending = load_json(PENDING_BUNDLES_FILE)
    return pending.get(approval_id)


def remove_pending_bundle(approval_id):
    """Remove pending bundle confirmation"""
    pending = load_json(PENDING_BUNDLES_FILE)
    if approval_id in pending:
        del pending[approval_id]
        save_json(PENDING_BUNDLES_FILE, pending)


def calculate_bundle_price(item_variant_ids, all_products):
    """Calculate sum of individual item prices"""
    total = 0
    for variant_id in item_variant_ids:
        for product in all_products:
            for variant in product.get('variants', []):
                if str(variant['id']) == str(variant_id):
                    total += float(variant['price'])
                    break
    return round(total, 2)


def send_bundle_confirmation_request(bundle_product, detected_items, approval_id):
    """Send Discord message asking to confirm bundle contents"""
    if not DISCORD_BOT_TOKEN or not DISCORD_BUNDLE_CHANNEL_ID:
        return False

    url = f"https://discord.com/api/v10/channels/{DISCORD_BUNDLE_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    items_text = "\n".join([f"- {item['title']} (${item['price']:.2f})" for item in detected_items]) if detected_items else "Could not detect items"
    total_price = sum(item['price'] for item in detected_items) if detected_items else 0

    embed = {
        "title": f"Bundle Detected: {bundle_product['title']}",
        "color": 0x5865F2,
        "fields": [
            {"name": "Bundle Price", "value": f"${float(bundle_product['variants'][0]['price']):.2f}", "inline": True},
            {"name": "Items Total", "value": f"${total_price:.2f}", "inline": True},
            {"name": "Detected Items", "value": items_text or "None detected", "inline": False},
        ],
        "footer": {"text": "APPROVE if correct, DECLINE to enter items manually"}
    }

    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 3, "label": "Approve", "custom_id": f"bundle_approve_{approval_id}"},
            {"type": 2, "style": 4, "label": "Decline", "custom_id": f"bundle_decline_{approval_id}"}
        ]
    }]

    payload = {"embeds": [embed], "components": components}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        return resp.status_code in [200, 201]
    except:
        return False


def send_bundle_price_alert(bundle_name, bundle_price, calculated_price, bundle_variant_id, approval_id):
    """Send alert when bundle price doesn't match sum of items"""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return

    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    diff = bundle_price - calculated_price
    color = 0xED4245 if bundle_price > calculated_price else 0x57F287

    embed = {
        "title": f"Bundle Price Mismatch: {bundle_name}",
        "color": color,
        "fields": [
            {"name": "Current Bundle Price", "value": f"${bundle_price:.2f}", "inline": True},
            {"name": "Items Total", "value": f"${calculated_price:.2f}", "inline": True},
            {"name": "Difference", "value": f"${abs(diff):.2f}", "inline": True},
        ]
    }

    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 3, "label": "Update", "custom_id": f"bundle_update_{approval_id}"},
            {"type": 2, "style": 2, "label": "Ignore", "custom_id": f"bundle_ignore_{approval_id}"}
        ]
    }]

    # Save pending for the update action
    add_pending(approval_id, {
        'type': 'bundle_price',
        'name': bundle_name,
        'variant_id': bundle_variant_id,
        'old_price': bundle_price,
        'new_price': calculated_price
    })

    payload = {"embeds": [embed], "components": components}

    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except:
        pass


def check_bundles():
    """Check all bundles for price mismatches"""
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        return

    bundles = load_json(BUNDLES_FILE)
    if not bundles:
        return

    log("Checking bundle prices...")

    try:
        # Get all products
        url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/products.json?limit=250"
        headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code != 200:
            return

        all_products = resp.json().get('products', [])

        for bundle_id, bundle_data in bundles.items():
            # Find bundle product
            bundle_product = None
            for p in all_products:
                if str(p['id']) == str(bundle_id):
                    bundle_product = p
                    break

            if not bundle_product:
                log(f"Bundle {bundle_data['name']} not found - may be deleted")
                continue

            bundle_price = float(bundle_product['variants'][0]['price'])
            bundle_variant_id = bundle_product['variants'][0]['id']

            # Calculate sum of items
            calculated = calculate_bundle_price(bundle_data['item_ids'], all_products)

            # Check if any item in bundle is missing
            for item_id in bundle_data['item_ids']:
                found = False
                for p in all_products:
                    for v in p.get('variants', []):
                        if str(v['id']) == str(item_id):
                            found = True
                            break
                    if found:
                        break
                if not found:
                    log(f"Bundle item {item_id} deleted from {bundle_data['name']}")
                    # Send alert about deleted item
                    send_bundle_item_deleted_alert(bundle_data['name'], item_id)

            # Check price mismatch (allow small tolerance)
            if abs(bundle_price - calculated) > 0.05:
                approval_id = f"bundle_{int(time.time())}_{hash(bundle_id) % 10000}"
                send_bundle_price_alert(bundle_data['name'], bundle_price, calculated, bundle_variant_id, approval_id)
                time.sleep(1)

        log("Bundle check done")
    except Exception as e:
        log(f"Bundle check error: {e}")


def send_bundle_item_deleted_alert(bundle_name, item_id):
    """Alert when an item in a bundle was deleted"""
    if not DISCORD_BOT_TOKEN or not DISCORD_BUNDLE_CHANNEL_ID:
        return

    url = f"https://discord.com/api/v10/channels/{DISCORD_BUNDLE_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    payload = {
        "embeds": [{
            "title": f"Bundle Item Deleted",
            "color": 0xED4245,
            "description": f"An item (ID: {item_id}) in **{bundle_name}** was deleted. Please update the bundle configuration."
        }]
    }

    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except:
        pass


def get_mm2_product_ids():
    """Get set of MM2 product IDs from collection"""
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        return set()

    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    mm2_product_ids = set()

    try:
        # Find MM2 collection
        coll_url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/custom_collections.json"
        coll_resp = requests.get(coll_url, headers=headers, timeout=30)
        if coll_resp.status_code == 200:
            for coll in coll_resp.json().get('custom_collections', []):
                if 'mm2' in coll.get('handle', '').lower() or 'murder' in coll.get('title', '').lower():
                    mm2_collection_id = coll['id']
                    # Get products in collection
                    collect_url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/collects.json?collection_id={mm2_collection_id}&limit=250"
                    collect_resp = requests.get(collect_url, headers=headers, timeout=30)
                    if collect_resp.status_code == 200:
                        for collect in collect_resp.json().get('collects', []):
                            mm2_product_ids.add(collect['product_id'])
                    break
    except:
        pass

    return mm2_product_ids


def detect_new_bundles():
    """Detect new bundle/set products that need configuration"""
    log("Checking for new bundles...")

    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        log("No Shopify credentials for bundle detection")
        return

    bundles = load_json(BUNDLES_FILE)
    pending = load_json(PENDING_BUNDLES_FILE)
    log(f"Known bundles: {len(bundles)}, Pending: {len(pending)}")

    try:
        # Get MM2 product IDs first
        mm2_product_ids = get_mm2_product_ids()
        log(f"MM2 product IDs found: {len(mm2_product_ids)}")
        if not mm2_product_ids:
            log("No MM2 collection found for bundle detection")
            return

        url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/products.json?limit=250"
        headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code != 200:
            return

        all_products = resp.json().get('products', [])

        # Filter to MM2 products only
        all_products = [p for p in all_products if p['id'] in mm2_product_ids]
        log(f"MM2 products to check: {len(all_products)}")

        # Find bundles/sets
        bundle_products = [p for p in all_products if is_bundle_product(p['title'])]
        log(f"Bundle/Set products found: {len(bundle_products)}")

        for product in all_products:
            product_id = str(product['id'])

            # Skip if already configured or pending
            if product_id in bundles:
                continue
            if any(p.get('bundle_product_id') == product_id for p in pending.values()):
                continue

            # Check if it's a bundle
            if not is_bundle_product(product['title']):
                continue

            log(f"New bundle detected: {product['title']}")

            # Try to extract items from description
            description = product.get('body_html', '')
            item_names = extract_items_from_description(description)
            detected_items = match_items_to_products(item_names, all_products)

            approval_id = f"newbundle_{int(time.time())}_{hash(product_id) % 10000}"

            add_pending_bundle(approval_id, {
                'bundle_product_id': product_id,
                'bundle_name': product['title'],
                'bundle_variant_id': product['variants'][0]['id'],
                'detected_items': [{'variant_id': i['variant_id'], 'title': i['title'], 'price': i['price']} for i in detected_items]
            })

            send_bundle_confirmation_request(product, detected_items, approval_id)
            time.sleep(1)

    except Exception as e:
        log(f"Bundle detection error: {e}")


def send_stock_alert(item_name, variant_id, image_url=None):
    """Send Discord notification for out of stock item"""
    channel = DISCORD_STOCK_CHANNEL_ID or DISCORD_CHANNEL_ID
    if not DISCORD_BOT_TOKEN or not channel:
        return

    url = f"https://discord.com/api/v10/channels/{channel}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    item_name_url = item_name.lower().replace(' ', '-').replace("'", '')
    buyblox_url = f"https://buyblox.gg/products/{item_name_url}"

    snooze_id = f"stock_snooze_{variant_id}"

    embed = {
        "title": f"Out of Stock: {item_name}",
        "color": 0xFEE75C,  # Yellow
        "description": f"[View on BuyBlox]({buyblox_url})"
    }

    if image_url:
        embed["thumbnail"] = {"url": image_url}

    payload = {
        "content": f"<@&{DISCORD_STOCK_ROLE_ID}>" if DISCORD_STOCK_ROLE_ID else "",
        "embeds": [embed],
        "components": [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 2,  # Gray
                "label": "Snooze",
                "custom_id": snooze_id
            }]
        }]
    }

    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
        log(f"Sent stock alert for {item_name}")
    except Exception as e:
        log(f"Failed to send stock alert: {e}")


# ============ DISCORD ============

def send_approval_request(item_data, bb_data, sp_price, approval_id, change_type="lower"):
    """Send Discord embed with approve/decline buttons
    change_type: 'lower' = SP cheaper, suggest lowering | 'higher' = SP more expensive, suggest raising
    """

    if change_type == "lower":
        new_price = round(sp_price * (1 - UNDERCUT_PERCENT), 2)
        color = 0xED4245  # Red - need to lower price
        title_prefix = "Lower Price"
    else:
        new_price = round(sp_price * (1 - UNDERCUT_PERCENT), 2)  # Match StarPets -1%
        color = 0x57F287  # Green - can raise price
        title_prefix = "Raise Price"

    # Build product URLs
    item_name_url = bb_data['name'].lower().replace(' ', '-').replace("'", '')
    buyblox_url = f"https://buyblox.gg/products/{item_name_url}"
    starpets_url = item_data.get('sp_url', 'https://starpets.gg/mm2')

    embed = {
        "title": f"{title_prefix}: {bb_data['name']}",
        "color": color,
        "fields": [
            {"name": "BuyBlox", "value": f"${bb_data['price']:.2f}", "inline": True},
            {"name": "StarPets", "value": f"${sp_price:.2f}", "inline": True},
            {"name": "New Price", "value": f"${new_price:.2f}", "inline": True},
            {"name": "Links", "value": f"[BuyBlox]({buyblox_url}) | [StarPets]({starpets_url})", "inline": False},
        ]
    }

    if bb_data.get('image'):
        embed["thumbnail"] = {"url": bb_data['image']}

    # Create buttons
    components = [{
        "type": 1,  # Action Row
        "components": [
            {
                "type": 2,  # Button
                "style": 3,  # Green
                "label": "Approve",
                "custom_id": f"approve_{approval_id}"
            },
            {
                "type": 2,  # Button
                "style": 4,  # Red
                "label": "Decline",
                "custom_id": f"decline_{approval_id}"
            }
        ]
    }]

    # Send via bot (required for buttons)
    if DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID:
        url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "content": f"<@&{ROLE_ID}>" if ROLE_ID else "",
            "embeds": [embed],
            "components": components
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code in [200, 201]:
                msg_id = resp.json().get('id')
                log(f"Sent approval request for {bb_data['name']}")
                return msg_id
            else:
                log(f"Discord error: {resp.status_code} - {resp.text}")
        except Exception as e:
            log(f"Discord error: {e}")

    # Fallback to webhook (no buttons, just info)
    elif DISCORD_WEBHOOK:
        payload = {
            "content": f"<@&{ROLE_ID}>\n**Price Change Detected - Manual Action Required**\n"
                       f"Item: {bb_data['name']}\n"
                       f"BuyBlox: ${bb_data['price']:.2f} | StarPets: ${sp_price:.2f}\n"
                       f"Recommended: ${new_price:.2f}",
            "embeds": [embed]
        }
        try:
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        except:
            pass

    return None


# ============ FLASK ENDPOINTS ============

@app.route('/')
def home():
    return jsonify({"status": "running", "service": "MM2 Price Monitor"})


@app.route('/reset')
def reset():
    """Clear pending approvals and saved prices to trigger fresh notifications"""
    save_json(PENDING_FILE, {})
    save_json(PRICE_FILE, {})
    log("Reset: Cleared pending approvals and saved prices")
    return jsonify({"status": "reset", "message": "Will send fresh notifications on next check"})


@app.route('/setbundle/<bundle_id>/<item_ids>')
def setbundle(bundle_id, item_ids):
    """Manually set bundle items: /setbundle/123456/111,222,333"""
    try:
        ids = [id.strip() for id in item_ids.split(',')]

        # Get bundle name from Shopify
        url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/products/{bundle_id}.json"
        headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code == 200:
            product = resp.json().get('product', {})
            bundle_name = product.get('title', f'Bundle {bundle_id}')
            save_bundle(bundle_id, bundle_name, ids)
            log(f"Bundle set: {bundle_name} = {ids}")
            return jsonify({"status": "ok", "bundle": bundle_name, "items": ids})
        else:
            return jsonify({"status": "error", "message": "Bundle product not found"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/bundles')
def list_bundles():
    """List all configured bundles"""
    bundles = load_json(BUNDLES_FILE)
    return jsonify(bundles)


@app.route('/resetbundles')
def reset_bundles():
    """Clear bundle data to re-detect all bundles"""
    save_json(BUNDLES_FILE, {})
    save_json(PENDING_BUNDLES_FILE, {})
    log("Reset: Cleared bundles - will re-detect on next check")
    return jsonify({"status": "reset", "message": "Will re-detect bundles on next check"})


@app.route('/resetstock')
def reset_stock():
    """Clear snoozed stock and mark all as in-stock to trigger fresh notifications"""
    # Get current stock and mark everything as in-stock (inventory=1)
    # So next check will detect out-of-stock items as changed
    current = load_json(STOCK_FILE)
    for key in current:
        current[key]['inventory'] = 1  # Mark as in-stock
    save_json(STOCK_FILE, current)
    save_json(SNOOZED_STOCK_FILE, {})
    log("Reset: Marked all as in-stock, cleared snoozed - will notify out-of-stock on next check")
    return jsonify({"status": "reset", "message": "Will send fresh stock notifications on next check"})


def verify_signature(req):
    """Verify Discord request signature"""
    signature = req.headers.get('X-Signature-Ed25519')
    timestamp = req.headers.get('X-Signature-Timestamp')
    body = req.data.decode('utf-8')

    log(f"Verifying signature - Key set: {bool(DISCORD_PUBLIC_KEY)}, Sig: {bool(signature)}, TS: {bool(timestamp)}")

    if not signature or not timestamp or not DISCORD_PUBLIC_KEY:
        log("Missing signature, timestamp, or public key")
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))
        log("Signature verified OK")
        return True
    except BadSignatureError as e:
        log(f"Bad signature: {e}")
        return False
    except Exception as e:
        log(f"Signature error: {e}")
        return False


@app.route('/interactions', methods=['POST'])
def discord_interactions():
    """Handle Discord button interactions"""
    log("Received interaction request")

    # Get raw body for signature verification
    raw_body = request.data

    # Verify signature
    signature = request.headers.get('X-Signature-Ed25519')
    timestamp = request.headers.get('X-Signature-Timestamp')

    log(f"Sig: {signature[:20] if signature else 'None'}... TS: {timestamp}")

    if DISCORD_PUBLIC_KEY and signature and timestamp:
        try:
            verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
            verify_key.verify(f'{timestamp}{raw_body.decode("utf-8")}'.encode(), bytes.fromhex(signature))
            log("Signature OK")
        except Exception as e:
            log(f"Sig verify failed: {e}")
            return 'Invalid signature', 401
    else:
        log(f"Missing: key={bool(DISCORD_PUBLIC_KEY)} sig={bool(signature)} ts={bool(timestamp)}")
        return 'Missing signature data', 401

    data = request.json
    log(f"Interaction type: {data.get('type')}")

    if data.get('type') == 1:  # PING
        log("Responding to PING")
        return jsonify({"type": 1})

    if data.get('type') == 3:  # MESSAGE_COMPONENT (button click)
        custom_id = data.get('data', {}).get('custom_id', '')

        if custom_id.startswith('approve_'):
            approval_id = custom_id.replace('approve_', '')
            return handle_approve(approval_id, data)

        elif custom_id.startswith('decline_'):
            approval_id = custom_id.replace('decline_', '')
            return handle_decline(approval_id, data)

        elif custom_id.startswith('bundle_approve_'):
            approval_id = custom_id.replace('bundle_approve_', '')
            return handle_bundle_approve(approval_id, data)

        elif custom_id.startswith('bundle_decline_'):
            approval_id = custom_id.replace('bundle_decline_', '')
            return handle_bundle_decline(approval_id, data)

        elif custom_id.startswith('bundle_update_'):
            approval_id = custom_id.replace('bundle_update_', '')
            return handle_bundle_update(approval_id, data)

        elif custom_id.startswith('bundle_ignore_'):
            approval_id = custom_id.replace('bundle_ignore_', '')
            return handle_bundle_ignore(approval_id, data)

        elif custom_id.startswith('stock_snooze_'):
            variant_id = custom_id.replace('stock_snooze_', '')
            return handle_stock_snooze(variant_id, data)

    return jsonify({"type": 4, "data": {"content": "Unknown interaction"}})


# ============ BUNDLE INTERACTION HANDLERS ============

def handle_bundle_approve(approval_id, interaction_data):
    """Approve detected bundle items"""
    pending = get_pending_bundle(approval_id)
    user = interaction_data.get('member', {}).get('user', {})
    username = user.get('username', 'Unknown')
    message_id = interaction_data.get('message', {}).get('id')
    channel_id = interaction_data.get('channel_id')

    if not pending:
        return jsonify({"type": 4, "data": {"content": "This bundle confirmation has expired.", "flags": 64}})

    # Save the bundle configuration
    item_ids = [item['variant_id'] for item in pending['detected_items']]
    save_bundle(pending['bundle_product_id'], pending['bundle_name'], item_ids)
    remove_pending_bundle(approval_id)

    log(f"Bundle confirmed: {pending['bundle_name']} with {len(item_ids)} items by {username}")

    # Delete original and send confirmation
    if message_id and channel_id and DISCORD_BOT_TOKEN:
        try:
            delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
            headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            requests.delete(delete_url, headers=headers, timeout=10)
        except:
            pass

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
        items_text = ", ".join([item['title'] for item in pending['detected_items']])
        payload = {
            "embeds": [{
                "title": f"Bundle Confirmed: {pending['bundle_name']}",
                "color": 0x57F287,
                "description": f"Items: {items_text}",
                "footer": {"text": f"Confirmed by {username}"}
            }]
        }
        try:
            requests.post(url, headers=headers, json=payload, timeout=10)
        except:
            pass

    return jsonify({"type": 6})


def handle_bundle_decline(approval_id, interaction_data):
    """Decline detected items - ask for manual input"""
    pending = get_pending_bundle(approval_id)
    user = interaction_data.get('member', {}).get('user', {})
    username = user.get('username', 'Unknown')
    message_id = interaction_data.get('message', {}).get('id')
    channel_id = interaction_data.get('channel_id')

    if not pending:
        return jsonify({"type": 4, "data": {"content": "This bundle confirmation has expired.", "flags": 64}})

    # Delete original message
    if message_id and channel_id and DISCORD_BOT_TOKEN:
        try:
            delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
            headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            requests.delete(delete_url, headers=headers, timeout=10)
        except:
            pass

        # Send message asking for variant IDs
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "embeds": [{
                "title": f"Enter Items for: {pending['bundle_name']}",
                "color": 0xFEE75C,
                "description": f"Reply with variant IDs separated by commas.\nExample: `12345, 67890, 11111`\n\nBundle ID: `{pending['bundle_product_id']}`",
                "footer": {"text": f"Use /setbundle {pending['bundle_product_id']} id1,id2,id3"}
            }]
        }
        try:
            requests.post(url, headers=headers, json=payload, timeout=10)
        except:
            pass

    # Keep in pending but mark as awaiting manual input
    pending['awaiting_manual'] = True
    add_pending_bundle(approval_id, pending)

    return jsonify({"type": 6})


def handle_bundle_update(approval_id, interaction_data):
    """Update bundle price to match items total"""
    pending = get_pending(approval_id)
    user = interaction_data.get('member', {}).get('user', {})
    username = user.get('username', 'Unknown')
    message_id = interaction_data.get('message', {}).get('id')
    channel_id = interaction_data.get('channel_id')

    if not pending or pending.get('type') != 'bundle_price':
        return jsonify({"type": 4, "data": {"content": "This has expired.", "flags": 64}})

    # Update Shopify price
    success = update_shopify_price(pending['variant_id'], pending['new_price'])

    if success:
        remove_pending(approval_id)
        log(f"Bundle price updated: {pending['name']} -> ${pending['new_price']:.2f} by {username}")

        # Delete and confirm
        if message_id and channel_id and DISCORD_BOT_TOKEN:
            try:
                delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
                headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
                requests.delete(delete_url, headers=headers, timeout=10)
            except:
                pass

            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
            payload = {
                "embeds": [{
                    "title": f"Bundle Price Updated: {pending['name']}",
                    "color": 0x57F287,
                    "fields": [
                        {"name": "Old", "value": f"${pending['old_price']:.2f}", "inline": True},
                        {"name": "New", "value": f"${pending['new_price']:.2f}", "inline": True},
                    ],
                    "footer": {"text": f"Updated by {username}"}
                }]
            }
            try:
                requests.post(url, headers=headers, json=payload, timeout=10)
            except:
                pass

    return jsonify({"type": 6})


def handle_bundle_ignore(approval_id, interaction_data):
    """Ignore bundle price mismatch"""
    pending = get_pending(approval_id)
    message_id = interaction_data.get('message', {}).get('id')
    channel_id = interaction_data.get('channel_id')

    if pending:
        remove_pending(approval_id)

    # Just delete the message
    if message_id and channel_id and DISCORD_BOT_TOKEN:
        try:
            delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
            headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            requests.delete(delete_url, headers=headers, timeout=10)
        except:
            pass

    return jsonify({"type": 6})


def handle_stock_snooze(variant_id, interaction_data):
    """Snooze stock alert for 24 hours"""
    user = interaction_data.get('member', {}).get('user', {})
    username = user.get('username', 'Unknown')
    message_id = interaction_data.get('message', {}).get('id')
    channel_id = interaction_data.get('channel_id')

    # Snooze the item
    snooze_stock_item(variant_id, hours=24)
    log(f"Stock snoozed: {variant_id} by {username}")

    # Delete the message
    if message_id and channel_id and DISCORD_BOT_TOKEN:
        try:
            delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
            headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            requests.delete(delete_url, headers=headers, timeout=10)
        except:
            pass

    return jsonify({"type": 6})


def check_permission(interaction_data):
    """Check if user has permission to approve/decline"""
    member = interaction_data.get('member', {})
    roles = member.get('roles', [])

    # If no allowed roles configured, allow everyone
    if not ALLOWED_ROLE_IDS or ALLOWED_ROLE_IDS == ['']:
        return True

    # Check if user has any allowed role
    for role_id in roles:
        if role_id in ALLOWED_ROLE_IDS:
            return True
    return False


def handle_approve(approval_id, interaction_data):
    """Handle approve button click"""
    # Check permission
    if not check_permission(interaction_data):
        return jsonify({
            "type": 4,
            "data": {"content": "You don't have permission to approve prices.", "flags": 64}
        })

    pending = get_pending(approval_id)
    user = interaction_data.get('member', {}).get('user', {})
    username = user.get('username', 'Unknown')
    message_id = interaction_data.get('message', {}).get('id')
    channel_id = interaction_data.get('channel_id')

    if not pending:
        # Still delete the message even if expired
        if message_id and channel_id and DISCORD_BOT_TOKEN:
            try:
                delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
                headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
                requests.delete(delete_url, headers=headers, timeout=10)
            except:
                pass
        return jsonify({"type": 6})

    # Update Shopify price
    success = update_shopify_price(pending['variant_id'], pending['new_price'])

    if success:
        remove_pending(approval_id)
        log_action("APPROVE", pending['name'], username, pending['old_price'], pending['new_price'])

        # Delete original message
        if message_id and channel_id and DISCORD_BOT_TOKEN:
            try:
                delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
                headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
                requests.delete(delete_url, headers=headers, timeout=10)
            except Exception as e:
                log(f"Failed to delete message: {e}")

        # Send new confirmation message (no ping)
        if DISCORD_BOT_TOKEN and channel_id:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
            payload = {
                "embeds": [{
                    "title": f"Price Updated: {pending['name']}",
                    "color": 0x57F287,
                    "fields": [
                        {"name": "Old Price", "value": f"${pending['old_price']:.2f}", "inline": True},
                        {"name": "New Price", "value": f"${pending['new_price']:.2f}", "inline": True},
                    ],
                    "footer": {"text": f"Approved by {username}"}
                }]
            }
            try:
                requests.post(url, headers=headers, json=payload, timeout=10)
            except Exception as e:
                log(f"Failed to send confirmation: {e}")

        return jsonify({"type": 6})  # DEFERRED_UPDATE_MESSAGE (acknowledge)
    else:
        return jsonify({
            "type": 4,
            "data": {"content": "Failed to update price. Check Shopify API.", "flags": 64}
        })


def handle_decline(approval_id, interaction_data):
    """Handle decline button click"""
    # Check permission
    if not check_permission(interaction_data):
        return jsonify({
            "type": 4,
            "data": {"content": "You don't have permission to decline prices.", "flags": 64}
        })

    pending = get_pending(approval_id)
    user = interaction_data.get('member', {}).get('user', {})
    username = user.get('username', 'Unknown')
    message_id = interaction_data.get('message', {}).get('id')
    channel_id = interaction_data.get('channel_id')

    if not pending:
        # Still delete the message even if expired
        if message_id and channel_id and DISCORD_BOT_TOKEN:
            try:
                delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
                headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
                requests.delete(delete_url, headers=headers, timeout=10)
            except:
                pass
        return jsonify({"type": 6})

    # Snooze item for 24 hours
    snooze_item(pending['item_key'], hours=24)
    remove_pending(approval_id)
    log_action("DECLINE", pending['name'], username)

    # Delete original message
    if message_id and channel_id and DISCORD_BOT_TOKEN:
        try:
            delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
            headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            requests.delete(delete_url, headers=headers, timeout=10)
        except Exception as e:
            log(f"Failed to delete message: {e}")

    # Send new confirmation message (no ping)
    if DISCORD_BOT_TOKEN and channel_id:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "embeds": [{
                "title": f"Declined: {pending['name']}",
                "color": 0xED4245,
                "description": "Snoozed for 24 hours",
                "footer": {"text": f"Declined by {username}"}
            }]
        }
        try:
            requests.post(url, headers=headers, json=payload, timeout=10)
        except Exception as e:
            log(f"Failed to send confirmation: {e}")

    return jsonify({"type": 6})  # DEFERRED_UPDATE_MESSAGE (acknowledge)


# ============ PRICE CHECKER ============

def check_prices():
    """Main price checking function"""
    log("Checking prices...")

    saved_prices = load_json(PRICE_FILE)
    current_sp = get_starpets_prices()
    current_bb = get_buyblox_prices()

    log(f"StarPets: {len(current_sp)} | BuyBlox: {len(current_bb)}")

    # First run - just save prices without notifications
    if not saved_prices:
        log("First run - saving prices without notifications")
        save_json(PRICE_FILE, current_sp)
        return

    changes_found = 0

    for key, sp_data in current_sp.items():
        # Skip if snoozed
        if is_snoozed(key):
            continue

        # Skip if already has pending approval
        if has_pending_for_item(key):
            continue

        # Find matching BuyBlox item
        bb_data = current_bb.get(key)
        if not bb_data:
            continue

        sp_price = sp_data['price']
        bb_price = bb_data['price']

        # Check if StarPets is cheaper (we should lower our price)
        if sp_price < bb_price - 0.01:
            # Skip if price difference is too big AND significant $ amount (likely wrong match)
            # Never skip items under 50 cents
            price_diff_percent = (bb_price - sp_price) / bb_price
            price_diff_abs = bb_price - sp_price
            if bb_price >= 0.50 and price_diff_percent > 0.70 and price_diff_abs > 1.00:
                log(f"Skipping {bb_data['name']}: {price_diff_percent*100:.0f}% diff, ${price_diff_abs:.2f} (likely wrong match)")
                continue

            new_price = round(sp_price * (1 - UNDERCUT_PERCENT), 2)

            # Only notify if this is a significant change or new
            old_sp_price = saved_prices.get(key, {}).get('price', 0)
            if abs(sp_price - old_sp_price) > 0.01 or key not in saved_prices:

                approval_id = f"{int(time.time())}_{hash(key) % 10000}"

                # Send Discord notification (red - lower price)
                message_id = send_approval_request(sp_data, bb_data, sp_price, approval_id, "lower")

                # Save pending approval with message ID
                add_pending(approval_id, {
                    'item_key': key,
                    'name': bb_data['name'],
                    'variant_id': bb_data['variant_id'],
                    'old_price': bb_price,
                    'new_price': new_price,
                    'sp_price': sp_price,
                    'is_chroma': sp_data.get('is_chroma', False),
                    'channel_id': DISCORD_CHANNEL_ID,
                    'message_id': message_id
                })
                changes_found += 1
                time.sleep(1)  # Rate limit - 1 message per second

        # Check if StarPets is 20%+ higher (we can raise our price)
        elif sp_price > bb_price * 1.20:
            # Skip if price difference is too big AND significant $ amount (likely wrong match)
            # Never skip items under 50 cents
            price_diff_percent = (sp_price - bb_price) / bb_price
            price_diff_abs = sp_price - bb_price
            if bb_price >= 0.50 and price_diff_percent > 1.0 and price_diff_abs > 1.00:
                log(f"Skipping {bb_data['name']}: {price_diff_percent*100:.0f}% higher, ${price_diff_abs:.2f} (likely wrong match)")
                continue

            new_price = round(sp_price * (1 - UNDERCUT_PERCENT), 2)

            # Only notify if this is a significant change or new
            old_sp_price = saved_prices.get(key, {}).get('price', 0)
            if abs(sp_price - old_sp_price) > 0.01 or key not in saved_prices:

                approval_id = f"{int(time.time())}_{hash(key) % 10000}"

                # Send Discord notification (green - raise price)
                message_id = send_approval_request(sp_data, bb_data, sp_price, approval_id, "higher")

                # Save pending approval with message ID
                add_pending(approval_id, {
                    'item_key': key,
                    'name': bb_data['name'],
                    'variant_id': bb_data['variant_id'],
                    'old_price': bb_price,
                    'new_price': new_price,
                    'sp_price': sp_price,
                    'is_chroma': sp_data.get('is_chroma', False),
                    'channel_id': DISCORD_CHANNEL_ID,
                    'message_id': message_id
                })
                changes_found += 1
                time.sleep(1)  # Rate limit - 1 message per second

    log(f"Found {changes_found} items needing approval")
    save_json(PRICE_FILE, current_sp)


def price_checker_loop():
    """Background loop for price checking every 10 mins"""
    time.sleep(10)  # Initial delay
    while True:
        try:
            check_prices()
            detect_new_bundles()
            check_bundles()
        except Exception as e:
            log(f"Error in price check: {e}")
        time.sleep(CHECK_INTERVAL)


def stock_checker_loop():
    """Background loop for stock checking every 10 mins, offset by 5 mins"""
    time.sleep(310)  # Initial delay + 5 min offset
    while True:
        try:
            check_stock()
        except Exception as e:
            log(f"Error in stock check: {e}")
        time.sleep(CHECK_INTERVAL)


# ============ DISCORD GATEWAY (for online status) ============

def approve_all_in_channel(channel_id, user_id, username):
    """Approve all pending items in a channel - process each individually"""
    pending = load_json(PENDING_FILE)
    approved = 0

    for approval_id, data in list(pending.items()):
        if data.get('channel_id') == channel_id:
            # Update Shopify price
            success = update_shopify_price(data['variant_id'], data['new_price'])
            if success:
                log_action("APPROVE", data['name'], username, data['old_price'], data['new_price'])

                # Delete original message
                msg_id = data.get('message_id')
                if msg_id and DISCORD_BOT_TOKEN:
                    try:
                        delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}"
                        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
                        requests.delete(delete_url, headers=headers, timeout=10)
                    except:
                        pass

                # Send confirmation
                send_individual_confirmation(channel_id, "Price Updated", data['name'],
                    data['old_price'], data['new_price'], username, 0x57F287)

                approved += 1
                time.sleep(0.5)  # Rate limit

    # Clear all pending for this channel
    pending = {k: v for k, v in pending.items() if v.get('channel_id') != channel_id}
    save_json(PENDING_FILE, pending)

    return approved


def decline_all_in_channel(channel_id, user_id, username):
    """Decline all pending items in a channel - process each individually"""
    pending = load_json(PENDING_FILE)
    declined = 0

    for approval_id, data in list(pending.items()):
        if data.get('channel_id') == channel_id:
            snooze_item(data['item_key'], hours=24)
            log_action("DECLINE", data['name'], username)

            # Delete original message
            msg_id = data.get('message_id')
            if msg_id and DISCORD_BOT_TOKEN:
                try:
                    delete_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}"
                    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
                    requests.delete(delete_url, headers=headers, timeout=10)
                except:
                    pass

            # Send confirmation
            send_decline_confirmation(channel_id, data['name'], username)

            declined += 1
            time.sleep(0.5)  # Rate limit

    # Clear all pending for this channel
    pending = {k: v for k, v in pending.items() if v.get('channel_id') != channel_id}
    save_json(PENDING_FILE, pending)

    return declined


def send_individual_confirmation(channel_id, title, item_name, old_price, new_price, username, color):
    """Send individual confirmation for bulk action"""
    if not DISCORD_BOT_TOKEN:
        return

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    payload = {
        "embeds": [{
            "title": f"{title}: {item_name}",
            "color": color,
            "fields": [
                {"name": "Old Price", "value": f"${old_price:.2f}", "inline": True},
                {"name": "New Price", "value": f"${new_price:.2f}", "inline": True},
            ],
            "footer": {"text": f"Approved by {username}"}
        }]
    }

    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except:
        pass


def send_decline_confirmation(channel_id, item_name, username):
    """Send decline confirmation for bulk action"""
    if not DISCORD_BOT_TOKEN:
        return

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    payload = {
        "embeds": [{
            "title": f"Declined: {item_name}",
            "color": 0xED4245,
            "description": "Snoozed for 24 hours",
            "footer": {"text": f"Declined by {username}"}
        }]
    }

    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except:
        pass


def send_bulk_confirmation(channel_id, action, count, user_id):
    """Send confirmation message for bulk action"""
    if not DISCORD_BOT_TOKEN:
        return

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    color = 0x57F287 if action == "approved" else 0xED4245

    payload = {
        "embeds": [{
            "title": f"Bulk {action.title()}",
            "color": color,
            "description": f"{count} items {action}",
            "footer": {"text": f"By user {user_id}"}
        }]
    }

    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except:
        pass


def send_command_confirmation(channel_id, title, message):
    """Send confirmation for admin commands"""
    if not DISCORD_BOT_TOKEN:
        return

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    payload = {
        "embeds": [{
            "title": title,
            "color": 0x5865F2,
            "description": message
        }]
    }

    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except:
        pass


def send_help_message(channel_id):
    """Send help message with all commands"""
    if not DISCORD_BOT_TOKEN:
        return

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}

    payload = {
        "embeds": [{
            "title": "Commands",
            "color": 0x5865F2,
            "description": """**$approveall** - Approve all pending in this channel
**$declineall** - Decline all pending in this channel
**$reset** - Reset price alerts
**$resetstock** - Reset stock alerts
**$resetbundles** - Reset bundle detection
**$help** - Show this message"""
        }]
    }

    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except:
        pass


_processed_messages = set()  # Track processed message IDs to avoid duplicates

def discord_gateway():
    """Connect to Discord gateway to show bot as online"""
    if not DISCORD_BOT_TOKEN:
        log("No bot token, skipping gateway connection")
        return

    gateway_url = "wss://gateway.discord.gg/?v=10&encoding=json"
    heartbeat_interval = 41250  # Default, updated from HELLO

    def on_message(ws, message):
        nonlocal heartbeat_interval
        data = json.loads(message)
        op = data.get('op')
        t = data.get('t')  # Event type

        if op == 10:  # HELLO
            heartbeat_interval = data['d']['heartbeat_interval']
            log(f"Gateway connected, heartbeat: {heartbeat_interval}ms")

            # Send IDENTIFY with intents for messages
            # GUILDS (1) + GUILD_MESSAGES (512) + MESSAGE_CONTENT (32768) = 33281
            identify = {
                "op": 2,
                "d": {
                    "token": DISCORD_BOT_TOKEN,
                    "intents": 33281,
                    "properties": {
                        "os": "linux",
                        "browser": "mm2-monitor",
                        "device": "mm2-monitor"
                    },
                    "presence": {
                        "status": "online",
                        "activities": []
                    }
                }
            }
            ws.send(json.dumps(identify))

            # Start heartbeat thread
            def heartbeat():
                while True:
                    time.sleep(heartbeat_interval / 1000)
                    try:
                        ws.send(json.dumps({"op": 1, "d": None}))
                    except:
                        break
            threading.Thread(target=heartbeat, daemon=True).start()

        elif op == 11:  # HEARTBEAT ACK
            pass  # All good

        elif op == 0 and t == 'MESSAGE_CREATE':
            # Handle messages for $approveall and $declineall
            msg_data = data.get('d', {})
            msg_id = msg_data.get('id', '')

            # Deduplicate messages
            if msg_id in _processed_messages:
                return
            _processed_messages.add(msg_id)
            # Keep set small
            if len(_processed_messages) > 100:
                _processed_messages.clear()

            content = msg_data.get('content', '').strip().lower()
            author_id = msg_data.get('author', {}).get('id', '')
            channel_id = msg_data.get('channel_id', '')

            # Only allow admin user
            if author_id != ADMIN_USER_ID:
                return

            if content == '$approveall':
                username = msg_data.get('author', {}).get('username', 'Unknown')
                count = approve_all_in_channel(channel_id, author_id, username)
                log(f"$approveall: {count} items approved by {username} in {channel_id}")

            elif content == '$declineall':
                username = msg_data.get('author', {}).get('username', 'Unknown')
                count = decline_all_in_channel(channel_id, author_id, username)
                log(f"$declineall: {count} items declined by {username} in {channel_id}")

            elif content == '$reset':
                save_json(PENDING_FILE, {})
                save_json(PRICE_FILE, {})
                log(f"$reset: Price data cleared by {author_id}")
                send_command_confirmation(channel_id, "Price Reset", "Price data cleared. Fresh notifications on next check.")

            elif content == '$resetstock':
                current = load_json(STOCK_FILE)
                for key in current:
                    current[key]['inventory'] = 1
                save_json(STOCK_FILE, current)
                save_json(SNOOZED_STOCK_FILE, {})
                log(f"$resetstock: Stock data reset by {author_id}")
                send_command_confirmation(channel_id, "Stock Reset", "Stock data reset. Fresh notifications on next check.")

            elif content == '$resetbundles':
                save_json(BUNDLES_FILE, {})
                save_json(PENDING_BUNDLES_FILE, {})
                log(f"$resetbundles: Bundle data cleared by {author_id}")
                send_command_confirmation(channel_id, "Bundles Reset", "Bundle data cleared. Will re-detect on next check.")

            elif content == '$help':
                send_help_message(channel_id)

    def on_error(ws, error):
        log(f"Gateway error: {error}")

    def on_close(ws, close_status, close_msg):
        log(f"Gateway closed: {close_status} {close_msg}")
        time.sleep(5)
        discord_gateway()  # Reconnect

    def on_open(ws):
        log("Gateway connection opened")

    ws = websocket.WebSocketApp(
        gateway_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    ws.run_forever()


# ============ STARTUP ============

def startup():
    """Initialize and start background tasks"""
    log("=" * 50)
    log("MM2 PRICE MONITOR WITH APPROVAL SYSTEM")
    log("=" * 50)
    log(f"Discord Bot Token: {'Set' if DISCORD_BOT_TOKEN else 'NOT SET'}")
    log(f"Discord Channel: {DISCORD_CHANNEL_ID or 'NOT SET'}")
    log(f"Shopify Store: {SHOPIFY_STORE or 'NOT SET'}")
    log(f"Check Interval: {CHECK_INTERVAL}s")
    log(f"Undercut: {UNDERCUT_PERCENT * 100}%")
    log("=" * 50)

    # Start price checker in background
    checker_thread = threading.Thread(target=price_checker_loop, daemon=True)
    checker_thread.start()

    # Start stock checker in background (5 min offset)
    stock_thread = threading.Thread(target=stock_checker_loop, daemon=True)
    stock_thread.start()

    # Start Discord gateway for online status
    gateway_thread = threading.Thread(target=discord_gateway, daemon=True)
    gateway_thread.start()


# Auto-start on module load (only once)
_started = False
if not _started:
    _started = True
    startup()


if __name__ == "__main__":
    log(f"Starting web server on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT)
