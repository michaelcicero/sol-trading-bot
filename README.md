# SOL-Only Trading Bot

A Prometheus-instrumented Solana trading bot that swaps SOL ↔ USDC based on multi-timeframe RSI and divergence signals.

## Installation

1. Clone:
   ```bash
   git clone https://github.com/michaelcicero/sol-trading-bot.git
   cd sol-trading-bot
   ```
2. Python venv:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy env example:
   ```bash
   cp env.example .env
   # Fill in PRIVATE_KEY and HELIUS_API_KEY
   ```

## Usage

Run the bot:
```bash
python sol_tb.py
```

## Metrics

Prometheus server on port 8000 exposes the  endpoint for scraping.

