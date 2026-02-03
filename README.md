# MM2 Price Monitor

Monitors StarPets.gg prices and notifies via Discord webhook when prices change.
Optionally auto-updates BuyBlox.gg prices via Shopify API.

## Environment Variables (set in Railway)

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_WEBHOOK` | Yes | Your Discord webhook URL |
| `DISCORD_ROLE_ID` | No | Role ID to ping (default: 1468305257757933853) |
| `SHOPIFY_STORE` | No | Your store URL (e.g., `yourstore.myshopify.com`) |
| `SHOPIFY_TOKEN` | No | Shopify Admin API access token |
| `CHECK_INTERVAL` | No | Seconds between checks (default: 300 = 5 min) |
| `UNDERCUT_PERCENT` | No | How much to undercut StarPets (default: 0.01 = 1%) |

## Deploy to Railway

1. Push this repo to GitHub
2. Go to [Railway.app](https://railway.app)
3. New Project → Deploy from GitHub repo
4. Add environment variables in Railway dashboard
5. Deploy!

## Getting Shopify Admin API Token

1. Go to Shopify Admin → Settings → Apps → Develop apps
2. Create app → Name it "Price Updater"
3. Configure Admin API scopes: `read_products`, `write_products`
4. Install app
5. Copy the Admin API access token
