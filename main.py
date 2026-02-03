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
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests

# Environment Variables
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # For buttons to work
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # Channel to send to
ROLE_ID = os.getenv("DISCORD_ROLE_ID", "1468305257757933853")
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
UNDERCUT_PERCENT = float(os.getenv("UNDERCUT_PERCENT", "0.01"))
PORT = int(os.getenv("PORT", "3000"))

# Files
PRICE_FILE = "starpets_prices.json"
SNOOZED_FILE = "snoozed_items.json"
PENDING_FILE = "pending_approvals.json"

app = Flask(__name__)


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


# ============ FILE HELPERS ============

def load_json(filename, default=None):
    if default is None:
        default = {}
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except:
            return default
    return default


def save_json(filename, data):
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


# ============ PENDING APPROVALS ============

def add_pending(approval_id, data):
    pending = load_json(PENDING_FILE)
    pending[approval_id] = data
    save_json(PENDING_FILE, pending)


def get_pending(approval_id):
    pending = load_json(PENDING_FILE)
    return pending.get(approval_id)


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

                if price is None or rarity not in ['godly', 'ancient', 'vintage', 'legendary', 'chroma']:
                    continue

                key = f"{name.lower()}|{'chroma' if is_chroma else 'regular'}"
                if key not in items or float(price) < items[key]['price']:
                    items[key] = {'name': name, 'price': float(price), 'rarity': rarity, 'is_chroma': is_chroma}

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


# ============ DISCORD ============

def send_approval_request(item_data, bb_data, sp_price, approval_id):
    """Send Discord embed with approve/decline buttons"""

    new_price = round(sp_price * (1 - UNDERCUT_PERCENT), 2)
    price_diff = bb_data['price'] - sp_price

    chroma_tag = " [CHROMA]" if item_data.get('is_chroma') else ""

    embed = {
        "title": f"Price Change: {bb_data['name']}{chroma_tag}",
        "color": 0xFF6B6B if price_diff > 0 else 0x4ECB71,  # Red if SP cheaper, green if BB cheaper
        "fields": [
            {"name": "BuyBlox Price", "value": f"**${bb_data['price']:.2f}**", "inline": True},
            {"name": "StarPets Price", "value": f"**${sp_price:.2f}**", "inline": True},
            {"name": "Difference", "value": f"${abs(price_diff):.2f}", "inline": True},
            {"name": "New Price if Approved", "value": f"**${new_price:.2f}** ({UNDERCUT_PERCENT*100:.0f}% under SP)", "inline": False},
        ],
        "footer": {"text": f"ID: {approval_id}"}
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
                "label": "APPROVE",
                "custom_id": f"approve_{approval_id}"
            },
            {
                "type": 2,  # Button
                "style": 4,  # Red
                "label": "DECLINE",
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
                log(f"Sent approval request for {bb_data['name']}")
                return True
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

    return False


# ============ FLASK ENDPOINTS ============

@app.route('/')
def home():
    return jsonify({"status": "running", "service": "MM2 Price Monitor"})


@app.route('/interactions', methods=['POST'])
def discord_interactions():
    """Handle Discord button interactions"""
    data = request.json

    # Verify interaction (in production, verify signature)
    if data.get('type') == 1:  # PING
        return jsonify({"type": 1})

    if data.get('type') == 3:  # MESSAGE_COMPONENT (button click)
        custom_id = data.get('data', {}).get('custom_id', '')

        if custom_id.startswith('approve_'):
            approval_id = custom_id.replace('approve_', '')
            return handle_approve(approval_id, data)

        elif custom_id.startswith('decline_'):
            approval_id = custom_id.replace('decline_', '')
            return handle_decline(approval_id, data)

    return jsonify({"type": 4, "data": {"content": "Unknown interaction"}})


def handle_approve(approval_id, interaction_data):
    """Handle approve button click"""
    pending = get_pending(approval_id)

    if not pending:
        return jsonify({
            "type": 4,
            "data": {"content": "This approval has expired or was already handled.", "flags": 64}
        })

    # Update Shopify price
    success = update_shopify_price(pending['variant_id'], pending['new_price'])

    if success:
        remove_pending(approval_id)
        log(f"APPROVED: {pending['name']} -> ${pending['new_price']:.2f}")

        # Update the message
        return jsonify({
            "type": 7,  # UPDATE_MESSAGE
            "data": {
                "content": "",
                "embeds": [{
                    "title": f"Price Updated: {pending['name']}",
                    "color": 0x4ECB71,
                    "fields": [
                        {"name": "Old Price", "value": f"${pending['old_price']:.2f}", "inline": True},
                        {"name": "New Price", "value": f"**${pending['new_price']:.2f}**", "inline": True},
                    ]
                }],
                "components": []  # Remove buttons
            }
        })
    else:
        return jsonify({
            "type": 4,
            "data": {"content": "Failed to update price. Check Shopify API.", "flags": 64}
        })


def handle_decline(approval_id, interaction_data):
    """Handle decline button click"""
    pending = get_pending(approval_id)

    if not pending:
        return jsonify({
            "type": 4,
            "data": {"content": "This approval has expired or was already handled.", "flags": 64}
        })

    # Snooze item for 24 hours
    snooze_item(pending['item_key'], hours=24)
    remove_pending(approval_id)
    log(f"DECLINED: {pending['name']} - snoozed 24h")

    return jsonify({
        "type": 7,  # UPDATE_MESSAGE
        "data": {
            "content": "",
            "embeds": [{
                "title": f"Declined: {pending['name']}",
                "color": 0xFF6B6B,
                "description": "Snoozed"
            }],
            "components": []  # Remove buttons
        }
    })


# ============ PRICE CHECKER ============

def check_prices():
    """Main price checking function"""
    log("Checking prices...")

    saved_prices = load_json(PRICE_FILE)
    current_sp = get_starpets_prices()
    current_bb = get_buyblox_prices()

    log(f"StarPets: {len(current_sp)} | BuyBlox: {len(current_bb)}")

    changes_found = 0

    for key, sp_data in current_sp.items():
        # Skip if snoozed
        if is_snoozed(key):
            continue

        # Find matching BuyBlox item
        bb_data = current_bb.get(key)
        if not bb_data:
            continue

        sp_price = sp_data['price']
        bb_price = bb_data['price']

        # Check if StarPets is cheaper (we should lower our price)
        if sp_price < bb_price - 0.01:
            new_price = round(sp_price * (1 - UNDERCUT_PERCENT), 2)

            # Only notify if this is a significant change or new
            old_sp_price = saved_prices.get(key, {}).get('price', 0)
            if abs(sp_price - old_sp_price) > 0.01 or key not in saved_prices:

                approval_id = f"{int(time.time())}_{hash(key) % 10000}"

                # Save pending approval
                add_pending(approval_id, {
                    'item_key': key,
                    'name': bb_data['name'],
                    'variant_id': bb_data['variant_id'],
                    'old_price': bb_price,
                    'new_price': new_price,
                    'sp_price': sp_price,
                    'is_chroma': sp_data.get('is_chroma', False)
                })

                # Send Discord notification
                send_approval_request(sp_data, bb_data, sp_price, approval_id)
                changes_found += 1

    log(f"Found {changes_found} items needing approval")
    save_json(PRICE_FILE, current_sp)


def price_checker_loop():
    """Background loop for price checking"""
    time.sleep(10)  # Initial delay
    while True:
        try:
            check_prices()
        except Exception as e:
            log(f"Error in price check: {e}")
        time.sleep(CHECK_INTERVAL)


# ============ MAIN ============

def main():
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

    # Start Flask server for Discord interactions
    log(f"Starting web server on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT)


if __name__ == "__main__":
    main()
