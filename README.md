text
# SOL-Only Trading Bot

A Prometheus-instrumented Solana trading bot that continuously swaps SOL ↔ USDC based on multi-timeframe RSI (Relative Strength Index) and divergence signals.  
This bot is optimized for frequent trades using short RSI periods and dynamic thresholds to capitalize on market movements. It also exposes trading and performance metrics for monitoring via Prometheus and Grafana.

## Features

- Trading between SOL and USDC on the Solana blockchain
- Uses multi-timeframe RSI and divergence signals for trade execution
- Optimized parameters for more frequent trading opportunities
- Built-in risk management with max risk per trade and daily loss limits
- Metrics exposure via a Prometheus server on port 8000 for real-time monitoring
- Logs all trades with detailed P&L and performance calculation

## Installation

1. **Clone the repository:**

git clone https://github.com/michaelcicero/sol-trading-bot.git
cd sol-trading-bot


2. **Create and activate a Python virtual environment:**

python3 -m venv venv
source venv/bin/activate


3. **Install required Python dependencies:**

pip install aiohttp numpy python-dotenv solders solana spl-token prometheus-client


4. **Set up environment variables:**

cp env.example .env

Edit `.env` and fill in:
- `PRIVATE_KEY`: Your Solana wallet private key in base58 format
- `HELIUS_API_KEY`: Your Helius RPC API key for Solana network access

## Usage

Run the trading bot:

python sol_tb.py

The bot will start collecting prices, calculating RSI, and executing trades based on signals.

## Metrics and Monitoring

- The bot runs a Prometheus metrics server on port `8000`.
- Metrics include SOL and USDC balances, RSI values, trade execution durations, P&L, trade errors, and more.
- To visually monitor these metrics, import the provided Grafana dashboard (see `grafana_dashboard.json`) into your Grafana instance:
  1. Open Grafana UI.
  2. Click “+” → Import.
  3. Upload the `grafana_dashboard.json` file.
  4. Select your Prometheus data source.
  5. Click Import.

This dashboard provides real-time insights into your trading bot’s health and performance.

## Logging

- All logs are saved in the `logs/` directory inside the repo.
- Includes detailed trade execution info and error tracking.

## Notes

- This bot is designed for SOL-only trading vs USDC and uses specific optimized parameters for frequent trades.
- Maximum risk per trade and daily loss limits are built-in for risk management.
- Adjust your `.env` keys and parameters in `sol_tb.py` as needed for your preferences and risk tolerance.

---

For more information or support, please open an issue on the GitHub repo or contact the maintainer.
