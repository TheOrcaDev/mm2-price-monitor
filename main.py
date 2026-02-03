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
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
import websocket

# Environment Variables
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # For buttons to work
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # Channel to send to
DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY")  # For signature verification
ROLE_ID = os.getenv("DISCORD_ROLE_ID", "1468305257757933853")
ALLOWED_ROLE_IDS = os.getenv("ALLOWED_ROLE_IDS", "").split(",")  # Roles that can approve/decline
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.50"))  # Minimum price to set
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
UNDERCUT_PERCENT = float(os.getenv("UNDERCUT_PERCENT", "0.01"))
PORT = int(os.getenv("PORT", "3000"))

# Files
PRICE_FILE = "starpets_prices.json"
SNOOZED_FILE = "snoozed_items.json"
PENDING_FILE = "pending_approvals.json"
ACTION_LOG_FILE = "actions.log"

app = Flask(__name__)


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
    starpets_url = "https://starpets.gg/mm2"

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

    return jsonify({"type": 4, "data": {"content": "Unknown interaction"}})


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
        return jsonify({
            "type": 4,
            "data": {"content": "This approval has expired or was already handled.", "flags": 64}
        })

    # Check minimum price
    if pending['new_price'] < MIN_PRICE:
        return jsonify({
            "type": 4,
            "data": {"content": f"Price ${pending['new_price']:.2f} is below minimum (${MIN_PRICE:.2f}). Declined.", "flags": 64}
        })

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
        return jsonify({
            "type": 4,
            "data": {"content": "This approval has expired or was already handled.", "flags": 64}
        })

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
            # Skip if price difference is too big (likely wrong match)
            price_diff_percent = (bb_price - sp_price) / bb_price
            if price_diff_percent > 0.70:
                log(f"Skipping {bb_data['name']}: {price_diff_percent*100:.0f}% diff (likely wrong match)")
                continue

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

                # Send Discord notification (red - lower price)
                send_approval_request(sp_data, bb_data, sp_price, approval_id, "lower")
                changes_found += 1
                time.sleep(1)  # Rate limit - 1 message per second

        # Check if StarPets is 15%+ higher (we can raise our price)
        elif sp_price > bb_price * 1.15:
            # Skip if price difference is too big (likely wrong match)
            price_diff_percent = (sp_price - bb_price) / bb_price
            if price_diff_percent > 1.0:  # More than 100% higher is suspicious
                log(f"Skipping {bb_data['name']}: {price_diff_percent*100:.0f}% higher (likely wrong match)")
                continue

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

                # Send Discord notification (green - raise price)
                send_approval_request(sp_data, bb_data, sp_price, approval_id, "higher")
                changes_found += 1
                time.sleep(1)  # Rate limit - 1 message per second

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


# ============ DISCORD GATEWAY (for online status) ============

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

        if op == 10:  # HELLO
            heartbeat_interval = data['d']['heartbeat_interval']
            log(f"Gateway connected, heartbeat: {heartbeat_interval}ms")

            # Send IDENTIFY
            identify = {
                "op": 2,
                "d": {
                    "token": DISCORD_BOT_TOKEN,
                    "intents": 0,
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

    # Start Discord gateway for online status
    gateway_thread = threading.Thread(target=discord_gateway, daemon=True)
    gateway_thread.start()


# Track if startup has run
_started = False

def ensure_startup():
    global _started
    if not _started:
        _started = True
        startup()

# For gunicorn - start on first request
@app.before_request
def before_request():
    ensure_startup()


if __name__ == "__main__":
    # For local development
    startup()
    log(f"Starting web server on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT)
