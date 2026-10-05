# XMR SimpleX Bot

A SimpleX Chat bot that monitors the Monero (XMR) price in one or more fiat currencies and sends you alerts. Prices come from the CoinGecko public API; no account or API key is required.

## Features

- Current XMR price with 24h change, in every configured currency
- One-time alerts when the price goes above or below a value
- Repeating alerts on a percentage move
- Several currencies (e.g. USD, EUR, GBP) fetched in a single API request per poll
- Alerts saved to disk, so they survive restarts
- Automatic reconnect to SimpleX and back-off on CoinGecko rate limits

## Commands

Send these to the bot in any SimpleX app. The currency is optional and defaults to the first one configured.

| Command | What it does | Example |
| --- | --- | --- |
| `/price [cur]` | Price in all currencies, or just one | `/price eur` |
| `/above <price> [cur]` | Alert once when price reaches or passes a value | `/above 600 usd` |
| `/below <price> [cur]` | Alert once when price drops to or under a value | `/below 450 eur` |
| `/move <percent> [cur]` | Alert on every move of that size | `/move 5` |
| `/alerts` | List your active alerts | `/alerts` |
| `/clear` | Delete all your alerts | `/clear` |
| `/help` | Show the command list | `/help` |

## Requirements

- Linux (tested in an Ubuntu 24.04 Distrobox on Bazzite)
- Python 3.10+
- [SimpleX Chat CLI](https://github.com/simplex-chat/simplex-chat/blob/stable/docs/CLI.md) (tested with v7.0.0.12)

## Setup

1. Install the dependencies:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Install the SimpleX CLI using the [official instructions](https://github.com/simplex-chat/simplex-chat/blob/stable/docs/CLI.md).

3. Create the bot profile:

   ```bash
   simplex-chat -d db/bot --create-bot-display-name 'XMR Price Bot'
   ```

   At the `>` prompt, run `/address` (save the link it prints), then `/auto_accept on`, then exit with Ctrl+C.

## Running

Start the CLI as a local API server in one terminal and leave it running:

```bash
simplex-chat -d db/bot -p 5225
```

Start the bot in a second terminal:

```bash
source .venv/bin/activate
VS_CURRENCIES=usd,eur,gbp python xmr_bot.py
```

Then connect to the bot's address from the SimpleX app on your phone and send `/price`.

## Configuration

All settings are optional environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `VS_CURRENCIES` | `usd` | Comma-separated currencies; the first is the default |
| `POLL_SECONDS` | `120` | Seconds between price checks (minimum 60) |
| `CG_DEMO_KEY` | none | Optional free CoinGecko Demo API key |
| `PRICE_PROXY` | none | Proxy for price requests, e.g. `socks5://127.0.0.1:9050` for Tor |
| `SIMPLEX_WS` | `ws://127.0.0.1:5225` | SimpleX CLI WebSocket address |
| `STATE_FILE` | `xmr_bot_state.json` | Where alerts are saved |

## Security and privacy

- The SimpleX CLI WebSocket API has no authentication or encryption. Keep it on `127.0.0.1` and never expose port 5225 to a network ([SimpleX bot API docs](https://github.com/simplex-chat/simplex-chat/blob/stable/bots/README.md)).
- Never commit the `db/` folder: it holds the bot's SimpleX identity and keys. It is excluded in `.gitignore`.
- Anyone with the bot's address can use it. Replace the address with `/da` then `/address` in the interactive CLI if it leaks.
- By default the CLI connects to SimpleX relays directly. Add `-x` to the CLI command to route through Tor at `127.0.0.1:9050`.
- Price requests go to `api.coingecko.com`. Set `PRICE_PROXY` to send them through Tor.

## Disclaimer

Price data comes from CoinGecko and may be delayed or wrong. This is not financial advice; don't rely on it for trading decisions.

## License

MIT, see [LICENSE](LICENSE).
