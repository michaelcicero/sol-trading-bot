#!/usr/bin/env python3

import os
import logging
import asyncio
import aiohttp
import base64
import numpy as np
from datetime import datetime, timedelta
from collections import deque
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import create_associated_token_account, get_associated_token_address
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from prometheus_client import start_http_server, Gauge, Counter

load_dotenv()

# --- Configuration ---

SOL_MINT = os.getenv("SOL_MINT", "So11111111111111111111111111111111111111112")
TARGET_MINT = os.getenv("TARGET_MINT", "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")
TOKEN_DECIMALS = int(os.getenv("TOKEN_DECIMALS", "9"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "7"))
RSI_OVERBOUGHT = int(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD = int(os.getenv("RSI_OVERSOLD", "30"))
MAX_REASONABLE_PRICE = float(os.getenv("MAX_REASONABLE_PRICE", "0.1"))
MIN_SOL_BALANCE = float(os.getenv("MIN_SOL_BALANCE", "0.1"))
TRADE_COOLDOWN = int(os.getenv("TRADE_COOLDOWN", "120"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.1"))
SELL_PERCENTAGE = float(os.getenv("SELL_PERCENTAGE", "0.5"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "5000"))
TX_FEE_BUFFER = float(os.getenv("TX_FEE_BUFFER", "0.00001"))
MAX_CONSECUTIVE_ERRORS = int(os.getenv("MAX_CONSECUTIVE_ERRORS", "3"))
ERROR_PAUSE_MINUTES = int(os.getenv("ERROR_PAUSE_MINUTES", "2"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "-0.1"))  # -10% daily loss cap

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"

# --- Logging ---
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)

# --- Health Monitor ---

class HealthMonitor:
    def __init__(self):
        self.consecutive_errors = 0
        self.trading_paused_until = datetime.min
        self.daily_pnl = 0.0
        self.starting_balance = None
        self.max_daily_loss = MAX_DAILY_LOSS

    def record_error(self):
        self.consecutive_errors += 1
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            self.trading_paused_until = datetime.now() + timedelta(minutes=ERROR_PAUSE_MINUTES)
            logging.error(f"🛑 Trading paused until {self.trading_paused_until.strftime('%H:%M:%S')}")

    def reset_errors(self):
        self.consecutive_errors = 0
        self.trading_paused_until = datetime.min

    def update_pnl(self, current_balance):
        if self.starting_balance is None:
            self.starting_balance = current_balance
        self.daily_pnl = (current_balance - self.starting_balance) / self.starting_balance
        if self.daily_pnl <= self.max_daily_loss:
            self.trading_paused_until = datetime.now() + timedelta(hours=24)
            logging.error(f"🛑 Daily loss limit reached. Trading paused until {self.trading_paused_until.strftime('%Y-%m-%d %H:%M:%S')}")

# --- RSI Trader ---

class RSITrader:
    def __init__(self):
        self._validate_env()
        self.keypair = Keypair.from_base58_string(PRIVATE_KEY)
        self.wallet = str(self.keypair.pubkey())
        self.client = AsyncClient(HELIUS_RPC_URL, timeout=30, commitment=Confirmed)
        self.session = None  # Will be set in async context
        self.price_history = deque(maxlen=100)
        self.ema_history = []
        self.current_price = None
        self.balances = {'sol': 0.0, 'token': 0.0}
        self.last_balance_update = datetime.min
        self.last_trade = datetime.min
        self.health = HealthMonitor()
        self._setup_prometheus()

    def _validate_env(self):
        required = ['PRIVATE_KEY', 'HELIUS_API_KEY']
        missing = [var for var in required if not os.getenv(var)]
        if missing:
            raise ValueError(f"Missing: {', '.join(missing)}")
        if len(PRIVATE_KEY) < 40:
            raise ValueError("Invalid PRIVATE_KEY format")

    def _setup_prometheus(self):
        self.sol_balance_gauge = Gauge('sol_balance', 'Current SOL balance')
        self.token_balance_gauge = Gauge('popcat_balance', 'Current POPCAT balance')
        self.price_gauge = Gauge('popcat_price', 'Current POPCAT price')
        self.rsi_gauge = Gauge('rsi', 'Current RSI value')
        self.trade_count = Counter('trade_count_total', 'Total number of trades', ['direction'])
        self.error_count = Counter('trade_error_total', 'Total trade errors')
        start_http_server(8000)

    async def _get_fresh_balance(self, mint):
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                if not resp.value:
                    raise ValueError("No SOL balance found")
                return resp.value / 1e9
            else:
                ata = get_associated_token_address(Pubkey.from_string(self.wallet), Pubkey.from_string(TARGET_MINT))
                resp = await self.client.get_token_account_balance(ata)
                return float(resp.value.amount) / 10**TOKEN_DECIMALS if resp.value else 0.0
        except Exception as e:
            logging.error("Balance fetch failed", exc_info=True)
            return 0.0

    async def _ensure_ata(self):
        ata = get_associated_token_address(Pubkey.from_string(self.wallet), Pubkey.from_string(TARGET_MINT))
        info = await self.client.get_account_info(ata)
        if not info.value:
            logging.info("🛠 Creating token account...")
            ix = create_associated_token_account(self.keypair.pubkey(), self.keypair.pubkey(), Pubkey.from_string(TARGET_MINT))
            await self.client.send_transaction(ix, self.keypair)
            await asyncio.sleep(2)

    async def _get_jupiter_price(self):
        try:
            async with self.session.get(
                f"{JUPITER_API_BASE}/quote",
                params={
                    "inputMint": SOL_MINT,
                    "outputMint": TARGET_MINT,
                    "amount": int(0.01 * 1e9),
                    "slippageBps": SLIPPAGE_BPS
                }
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"Jupiter API error: {resp.status}")
                data = await resp.json()
                if "outAmount" not in data or float(data["outAmount"]) == 0:
                    raise Exception("Invalid Jupiter price data")
                return 0.01 / (float(data["outAmount"]) / 10**TOKEN_DECIMALS)
        except Exception as e:
            logging.error("Price check failed", exc_info=True)
            return None

    async def _update_price(self, price):
        if price and 0 < price < MAX_REASONABLE_PRICE:
            self.current_price = price
            self.price_history.append({'close': price, 'timestamp': datetime.now()})
            self.price_gauge.set(price)
            if len(self.price_history) >= RSI_PERIOD:
                return await self._calculate_rsi()
        return None

    async def _calculate_rsi(self):
        closes = [p['close'] for p in self.price_history]
        if len(closes) < RSI_PERIOD + 1:
            return None
        # EMA RSI calculation
        deltas = np.diff(closes)
        seed = deltas[:RSI_PERIOD]
        up = seed[seed >= 0].sum() / RSI_PERIOD
        down = -seed[seed < 0].sum() / RSI_PERIOD
        rs = up / down if down != 0 else 0
        rsi = 100. - 100. / (1. + rs)
        for delta in deltas[RSI_PERIOD:]:
            upval = max(delta, 0)
            downval = -min(delta, 0)
            up = (up * (RSI_PERIOD - 1) + upval) / RSI_PERIOD
            down = (down * (RSI_PERIOD - 1) + downval) / RSI_PERIOD
            rs = up / down if down != 0 else 0
            rsi = 100. - 100. / (1. + rs)
        self.rsi_gauge.set(rsi)
        return rsi

    async def execute_trade(self, direction, amount):
        if datetime.now() < self.health.trading_paused_until:
            logging.warning("⏸️ Trading paused")
            return False

        current_balance = await self._get_fresh_balance(SOL_MINT if direction == 'buy' else TARGET_MINT)
        required = amount + (TX_FEE_BUFFER + MIN_SOL_BALANCE) if direction == 'buy' else amount

        if current_balance < required:
            logging.warning(f"⛔ Insufficient balance: {current_balance:.6f} < {required:.6f}")
            return False

        try:
            await self._ensure_ata()
            # Get quote
            async with self.session.get(
                f"{JUPITER_API_BASE}/quote",
                params={
                    "inputMint": SOL_MINT if direction == 'buy' else TARGET_MINT,
                    "outputMint": TARGET_MINT if direction == 'buy' else SOL_MINT,
                    "amount": int(amount * (1e9 if direction == 'buy' else 10**TOKEN_DECIMALS)),
                    "slippageBps": SLIPPAGE_BPS
                }
            ) as quote_resp:
                if quote_resp.status != 200:
                    raise Exception(f"Jupiter quote error: {quote_resp.status}")
                quote_data = await quote_resp.json()

            # Get swap transaction
            async with self.session.post(
                f"{JUPITER_API_BASE}/swap",
                json={
                    "quoteResponse": quote_data,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True
                }
            ) as swap_resp:
                if swap_resp.status != 200:
                    raise Exception(f"Jupiter swap error: {swap_resp.status}")
                swap_data = await swap_resp.json()

            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)
            # Correct signing: reconstruct a new VersionedTransaction with the message and signers
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            txid = await self.client.send_raw_transaction(bytes(signed_tx))
            await self.client.confirm_transaction(txid.value)
            logging.info(f"✅ Trade success: https://solscan.io/tx/{txid.value}")
            self.trade_count.labels(direction=direction).inc()
            self.health.reset_errors()
            return True
        except Exception as e:
            logging.error("Trade failed", exc_info=True)
            self.error_count.inc()
            self.health.record_error()
            return False

    async def trading_loop(self):
        async with aiohttp.ClientSession(
            headers={'User-Agent': 'RSITrader/2.0'},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            self.session = session
            # Initial price collection
            while len(self.price_history) < RSI_PERIOD + 1:
                price = await self._get_jupiter_price()
                await self._update_price(price)
                logging.info(f"📈 Collecting data ({len(self.price_history)}/{RSI_PERIOD+1})")
                await asyncio.sleep(5)

            while True:
                try:
                    if datetime.now() < self.health.trading_paused_until:
                        pause_remaining = (self.health.trading_paused_until - datetime.now()).total_seconds()
                        logging.info(f"⏸️ Paused for {int(pause_remaining//60)}m {int(pause_remaining%60)}s")
                        await asyncio.sleep(10)
                        continue

                    sol_balance = await self._get_fresh_balance(SOL_MINT)
                    self.sol_balance_gauge.set(sol_balance)
                    self.health.update_pnl(sol_balance)
                    if self.health.daily_pnl <= self.health.max_daily_loss:
                        logging.warning("💔 Daily loss cap reached, pausing trading.")
                        await asyncio.sleep(60)
                        continue

                    if sol_balance < MIN_SOL_BALANCE + TX_FEE_BUFFER:
                        logging.warning("💸 Below minimum SOL balance")
                        await asyncio.sleep(300)
                        continue

                    token_balance = await self._get_fresh_balance(TARGET_MINT)
                    self.token_balance_gauge.set(token_balance)
                    price = await self._get_jupiter_price()
                    rsi = await self._update_price(price)
                    if not rsi:
                        await asyncio.sleep(10)
                        continue

                    now = datetime.now()
                    if rsi < RSI_OVERSOLD and (now - self.last_trade).total_seconds() > TRADE_COOLDOWN:
                        amount = min(sol_balance - MIN_SOL_BALANCE - TX_FEE_BUFFER, sol_balance * RISK_PER_TRADE)
                        if amount >= 0.001:
                            logging.info(f"🚀 Buying {amount:.4f} SOL worth")
                            if await self.execute_trade('buy', amount):
                                self.last_trade = now

                    elif rsi > RSI_OVERBOUGHT and (now - self.last_trade).total_seconds() > TRADE_COOLDOWN:
                        if token_balance >= 1.0:
                            amount = token_balance * SELL_PERCENTAGE
                            logging.info(f"💰 Selling {amount:.2f} tokens")
                            if await self.execute_trade('sell', amount):
                                self.last_trade = now

                    await asyncio.sleep(15)
                except Exception as e:
                    logging.error("Strategy error", exc_info=True)
                    await asyncio.sleep(30)

    async def close(self):
        await self.client.close()

# --- Entrypoint ---

async def main():
    trader = RSITrader()
    try:
        await trader.trading_loop()
    except KeyboardInterrupt:
        logging.info("🛑 Stopped by user")
    finally:
        await trader.close()

if __name__ == "__main__":
    asyncio.run(main())
