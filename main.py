"""
MM2 Price Monitor for Railway
Monitors StarPets prices, notifies Discord, auto-updates BuyBlox
"""

import requests
import json
import time
import os
from datetime import datetime

# Environment Variables (set these in Railway)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
ROLE_ID = os.getenv("DISCORD_ROLE_ID", "1468305257757933853")
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")  # e.g., "yourstore.myshopify.com"
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")  # Admin API access token
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # Default 5 minutes
UNDERCUT_PERCENT = float(os.getenv("UNDERCUT_PERCENT", "0.01"))  # Default 1%

# File to store prices (Railway has ephemeral storage, but works for runtime)
PRICE_FILE = "starpets_prices.json"


def log(msg):
    """Print with timestamp"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def load_saved_prices():
    """Load previously saved StarPets prices"""
    if os.path.exists(PRICE_FILE):
        with open(PRICE_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_prices(prices):
    """Save current StarPets prices"""
    with open(PRICE_FILE, 'w') as f:
        json.dump(prices, f)


def get_starpets_prices():
    """Fetch current StarPets prices (godly/ancient/vintage/legendary/chroma only)"""
    api_url = "https://mm2-market.apineural.com/api/store/items/all"
    headers = {
        'content-type': 'application/json',
        'origin': 'https://starpets.gg',
        'referer': 'https://starpets.gg/'
    }

    items = {}
    for page in range(1, 25):
        try:
            payload = {
                'filter': {'types': [{'type': 'weapon'}, {'type': 'pet'}, {'type': 'misc'}]},
                'page': page,
                'amount': 72,
                'currency': 'usd',
                'sort': {'popularity': 'desc'}
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

                if price is None:
                    continue
                if rarity not in ['godly', 'ancient', 'vintage', 'legendary', 'chroma']:
                    continue

                key = f"{name.lower()}|{'chroma' if is_chroma else 'regular'}"

                # Keep lowest price if duplicates
                if key not in items or float(price) < items[key]['price']:
                    items[key] = {
                        'name': name,
                        'price': float(price),
                        'rarity': rarity,
                        'is_chroma': is_chroma
                    }

            if len(data) < 72:
                break
            time.sleep(0.3)
        except Exception as e:
            log(f"Error fetching StarPets page {page}: {e}")
            break

    return items


def get_buyblox_prices():
    """Fetch current BuyBlox prices with variant IDs for updating"""
    items = {}
    for page in [1, 2, 3, 4]:
        try:
            resp = requests.get(
                f'https://buyblox.gg/collections/mm2/products.json?page={page}&limit=250',
                timeout=60
            )
            products = resp.json().get('products', [])
            if not products:
                break

            for p in products:
                title = p['title'].strip()
                price = float(p['variants'][0]['price'])
                variant_id = p['variants'][0]['id']
                product_id = p['id']

                is_chroma = 'chroma' in title.lower()
                base_name = title.lower().replace('chroma ', '') if is_chroma else title.lower()

                key = f"{base_name}|{'chroma' if is_chroma else 'regular'}"
                items[key] = {
                    'name': title,
                    'price': price,
                    'variant_id': variant_id,
                    'product_id': product_id,
                    'is_chroma': is_chroma
                }
            time.sleep(0.3)
        except Exception as e:
            log(f"Error fetching BuyBlox page {page}: {e}")
            break

    return items


def calculate_new_price(starpets_price):
    """Calculate BuyBlox price (X% under StarPets)"""
    return round(starpets_price * (1 - UNDERCUT_PERCENT), 2)


def update_shopify_price(variant_id, new_price):
    """Update price via Shopify Admin API"""
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        return False

    url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/variants/{variant_id}.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "variant": {
            "id": variant_id,
            "price": str(new_price)
        }
    }

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=30)
        return resp.status_code == 200
    except Exception as e:
        log(f"Error updating Shopify: {e}")
        return False


def send_discord_notification(changes, buyblox_prices):
    """Send Discord webhook with price change notifications"""
    if not changes or not DISCORD_WEBHOOK:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Header with role ping
    header = f"<@&{ROLE_ID}> **StarPets Price Changes!** - {timestamp}\n"
    header += f"**{len(changes)} items changed:**\n"

    try:
        requests.post(DISCORD_WEBHOOK, json={"content": header}, timeout=10)
        time.sleep(0.5)
    except Exception as e:
        log(f"Discord error: {e}")

    # Send changes in chunks
    lines = []
    for change in changes:
        item_name = change['name']
        old_price = change['old_price']
        new_price = change['new_price']
        change_type = change['type']
        is_chroma = change.get('is_chroma', False)

        # Get BuyBlox info
        key = f"{item_name.lower()}|{'chroma' if is_chroma else 'regular'}"
        bb_data = buyblox_prices.get(key)
        bb_price = bb_data['price'] if bb_data else None
        recommended = calculate_new_price(new_price) if new_price else None

        chroma_tag = " [CHROMA]" if is_chroma else ""

        if change_type == 'new':
            line = f"🆕 **{item_name}{chroma_tag}**: NEW @ ${new_price:.2f}"
            if recommended:
                line += f" | Recommended BB: **${recommended:.2f}**"
        elif change_type == 'removed':
            line = f"❌ **{item_name}{chroma_tag}**: REMOVED (was ${old_price:.2f})"
        else:
            direction = "📈" if new_price > old_price else "📉"
            line = f"{direction} **{item_name}{chroma_tag}**\n"
            line += f"   SP: ~~${old_price:.2f}~~ → **${new_price:.2f}**"
            if bb_price:
                line += f" | BB: ${bb_price:.2f}"
            if recommended:
                line += f"\n   → Set BB to: **${recommended:.2f}**"

        lines.append(line)

    # Send in chunks of 5
    for i in range(0, len(lines), 5):
        chunk = lines[i:i+5]
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": "\n\n".join(chunk)}, timeout=10)
            time.sleep(0.5)
        except Exception as e:
            log(f"Discord chunk error: {e}")


def check_and_update():
    """Main check function"""
    log("Checking prices...")

    saved_prices = load_saved_prices()
    current_sp = get_starpets_prices()
    current_bb = get_buyblox_prices()

    log(f"StarPets: {len(current_sp)} items | BuyBlox: {len(current_bb)} items")

    # Detect changes
    changes = []
    updates_made = []

    for key, sp_data in current_sp.items():
        if key in saved_prices:
            old_price = saved_prices[key]['price']
            new_price = sp_data['price']

            if abs(old_price - new_price) > 0.01:
                changes.append({
                    'name': sp_data['name'],
                    'old_price': old_price,
                    'new_price': new_price,
                    'is_chroma': sp_data['is_chroma'],
                    'type': 'changed'
                })

                # Auto-update BuyBlox if configured
                if SHOPIFY_STORE and SHOPIFY_TOKEN:
                    bb_data = current_bb.get(key)
                    if bb_data:
                        recommended = calculate_new_price(new_price)
                        if update_shopify_price(bb_data['variant_id'], recommended):
                            updates_made.append(f"{sp_data['name']}: ${recommended:.2f}")
        else:
            changes.append({
                'name': sp_data['name'],
                'old_price': None,
                'new_price': sp_data['price'],
                'is_chroma': sp_data['is_chroma'],
                'type': 'new'
            })

    # Check removed items
    for key, old_data in saved_prices.items():
        if key not in current_sp:
            changes.append({
                'name': old_data['name'],
                'old_price': old_data['price'],
                'new_price': None,
                'is_chroma': old_data.get('is_chroma', False),
                'type': 'removed'
            })

    if changes:
        log(f"Found {len(changes)} changes!")
        send_discord_notification(changes, current_bb)

        if updates_made:
            log(f"Auto-updated {len(updates_made)} BuyBlox prices")
    else:
        log("No changes detected.")

    save_prices(current_sp)


def main():
    log("=" * 50)
    log("MM2 PRICE MONITOR STARTING")
    log("=" * 50)
    log(f"Discord Webhook: {'Set' if DISCORD_WEBHOOK else 'NOT SET!'}")
    log(f"Role to ping: {ROLE_ID}")
    log(f"Shopify Store: {SHOPIFY_STORE or 'NOT SET (no auto-update)'}")
    log(f"Shopify Token: {'Set' if SHOPIFY_TOKEN else 'NOT SET'}")
    log(f"Check Interval: {CHECK_INTERVAL}s")
    log(f"Undercut: {UNDERCUT_PERCENT * 100}%")
    log("=" * 50)

    if not DISCORD_WEBHOOK:
        log("WARNING: DISCORD_WEBHOOK not set! Set it in Railway environment variables.")

    # Initial check
    check_and_update()

    # Continuous monitoring
    while True:
        log(f"Sleeping {CHECK_INTERVAL}s until next check...")
        time.sleep(CHECK_INTERVAL)
        try:
            check_and_update()
        except Exception as e:
            log(f"Error during check: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
