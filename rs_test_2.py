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
from prometheus_client import start_http_server, Gauge, Counter, Histogram

load_dotenv()

# --- Configuration ---
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

SOL_MINT = os.getenv("SOL_MINT", "So11111111111111111111111111111111111111112")
TARGET_MINT = os.getenv("TARGET_MINT", "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")
TOKEN_DECIMALS = int(os.getenv("TOKEN_DECIMALS", "9"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "7"))
RSI_OVERBOUGHT = int(os.getenv("RSI_OVERBOUGHT", "75"))
RSI_OVERSOLD = int(os.getenv("RSI_OVERSOLD", "29"))
MAX_REASONABLE_PRICE = float(os.getenv("MAX_REASONABLE_PRICE", "0.1"))
MIN_SOL_BALANCE = float(os.getenv("MIN_SOL_BALANCE", "0.1"))
TRADE_COOLDOWN = int(os.getenv("TRADE_COOLDOWN", "120"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.1"))
SELL_PERCENTAGE = float(os.getenv("SELL_PERCENTAGE", "0.5"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "5000"))
TX_FEE_BUFFER = float(os.getenv("TX_FEE_BUFFER", "0.00001"))
MAX_CONSECUTIVE_ERRORS = int(os.getenv("MAX_CONSECUTIVE_ERRORS", "3"))
ERROR_PAUSE_MINUTES = int(os.getenv("ERROR_PAUSE_MINUTES", "2"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "-0.2"))

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"

# --- Enhanced Logging ---
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'professional_trading.log')),
        logging.StreamHandler()
    ]
)

# --- Professional Metrics System ---
class PnLCalculator:
    def __init__(self):
        self.trade_history = []
        self.positions = {'sol': 0.0, 'token': 0.0}
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.starting_balance = None
        self.consecutive_wins = 0
        self.consecutive_losses = 0

    def record_trade(self, direction, quantity, entry_price, exit_price, fees):
        trade = {
            'timestamp': datetime.now(),
            'direction': direction,
            'quantity': quantity,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'fees': fees
        }
        self.trade_history.append(trade)
        
        if direction == 'buy':
            self.positions['token'] += quantity
            self.positions['sol'] -= (quantity * entry_price) + fees
        else:
            self.positions['token'] -= quantity
            self.positions['sol'] += (quantity * exit_price) - fees
        
        profit = (exit_price - entry_price) * quantity - fees
        if profit > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        
        self.realized_pnl += profit
        logging.info(f"Trade recorded: {direction} {quantity:.4f} @ {entry_price:.8f}→{exit_price:.8f}, Profit: {profit:.6f}")

    def update_unrealized(self, current_price):
        if current_price and self.positions['token'] > 0:
            self.unrealized_pnl = self.positions['token'] * current_price
        return self.unrealized_pnl

# --- Enhanced Health Monitor ---
class HealthMonitor:
    def __init__(self):
        self.metrics = PnLCalculator()
        self.consecutive_errors = 0
        self.trading_paused_until = datetime.min
        self.max_daily_loss = MAX_DAILY_LOSS
        self.last_trade_time = datetime.min

    def record_error(self):
        self.consecutive_errors += 1
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            self.trading_paused_until = datetime.now() + timedelta(minutes=ERROR_PAUSE_MINUTES)
            logging.error(f"🛑 Trading paused until {self.trading_paused_until.strftime('%H:%M:%S')}")

    def reset_errors(self):
        self.consecutive_errors = 0
        self.trading_paused_until = datetime.min

    def check_daily_loss(self, current_balance):
        if self.metrics.starting_balance is None:
            self.metrics.starting_balance = current_balance
        current_value = current_balance + self.metrics.update_unrealized(self.metrics.positions['token'])
        daily_pnl = (current_value - self.metrics.starting_balance) / self.metrics.starting_balance
        if daily_pnl <= self.max_daily_loss:
            self.trading_paused_until = datetime.now() + timedelta(hours=24)
            logging.error(f"🛑 Daily loss limit reached. Trading paused until {self.trading_paused_until.strftime('%Y-%m-%d %H:%M:%S')}")
            return True
        return False

# --- Professional Trading Bot ---
class ProfessionalRSITrader:
    def __init__(self):
        self._validate_env()
        self.keypair = Keypair.from_base58_string(PRIVATE_KEY)
        self.wallet = str(self.keypair.pubkey())
        self.client = AsyncClient(HELIUS_RPC_URL, timeout=30, commitment=Confirmed)
        self.session = None
        self.price_history = deque(maxlen=100)
        self.current_price = None
        self.balances = {'sol': 0.0, 'token': 0.0}
        self.health = HealthMonitor()
        self._setup_prometheus()

    def _validate_env(self):
        required = ['PRIVATE_KEY', 'HELIUS_API_KEY']
        if missing := [var for var in required if not os.getenv(var)]:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")
        if len(PRIVATE_KEY) < 40:
            raise ValueError("Invalid PRIVATE_KEY format")

    def _setup_prometheus(self):
        self.sol_balance = Gauge('sol_balance', 'Current SOL balance')
        self.token_balance = Gauge('token_balance', 'Current token balance')
        self.asset_price = Gauge('asset_price', 'Current trading pair price')
        self.rsi_value = Gauge('rsi', 'Current RSI value')
        self.realized_pnl = Gauge('realized_pnl', 'Realized profit/loss')
        self.unrealized_pnl = Gauge('unrealized_pnl', 'Unrealized profit/loss')
        self.trade_duration = Histogram('trade_duration_seconds', 'Trade processing duration', buckets=[0.1, 0.5, 1, 2, 5])
        self.consecutive_wins = Gauge('consecutive_wins', 'Current winning streak')
        self.consecutive_losses = Gauge('consecutive_losses', 'Current losing streak')
        self.trade_count = Counter('trade_count_total', 'Total trades executed', ['direction'])
        self.error_count = Counter('trade_errors_total', 'Total trade errors')
        start_http_server(8000)

    async def _get_fresh_balance(self, mint):
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return resp.value / 1e9 if resp.value else 0.0
            else:
                ata = get_associated_token_address(Pubkey.from_string(self.wallet), Pubkey.from_string(TARGET_MINT))
                resp = await self.client.get_token_account_balance(ata)
                return float(resp.value.amount) / 10**TOKEN_DECIMALS if resp.value else 0.0
        except Exception as e:
            logging.error("Balance check failed", exc_info=True)
            return 0.0

    async def _ensure_ata(self):
        ata = get_associated_token_address(Pubkey.from_string(self.wallet), Pubkey.from_string(TARGET_MINT))
        if not (await self.client.get_account_info(ata)).value:
            logging.info("🔧 Creating associated token account...")
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
                    raise ValueError(f"Jupiter API error: {resp.status}")
                data = await resp.json()
                return 0.01 / (float(data["outAmount"]) / 10**TOKEN_DECIMALS)
        except Exception as e:
            logging.error("Price check failed", exc_info=True)
            return None

    async def _update_price(self, price):
        if price and 0 < price < MAX_REASONABLE_PRICE:
            self.current_price = price
            self.price_history.append({'close': price, 'timestamp': datetime.now()})
            if len(self.price_history) >= RSI_PERIOD:
                return await self._calculate_rsi()
        return None

    async def _calculate_rsi(self):
        closes = [p['close'] for p in self.price_history]
        if len(closes) < RSI_PERIOD + 1:
            return None
        
        deltas = np.diff(closes)
        gains = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]
        
        avg_gain = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
        avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD or 0.0001
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        for delta in deltas[RSI_PERIOD:]:
            avg_gain = (avg_gain * (RSI_PERIOD - 1) + max(delta, 0)) / RSI_PERIOD
            avg_loss = (avg_loss * (RSI_PERIOD - 1) + abs(min(delta, 0))) / RSI_PERIOD
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        
        self.rsi_value.set(rsi)
        return rsi

    async def execute_trade(self, direction, amount):
        start_time = datetime.now()
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
                    raise ValueError(f"Quote error: {quote_resp.status}")
                quote_data = await quote_resp.json()

            # Extract actual prices
            input_amount = float(quote_data["inAmount"])
            output_amount = float(quote_data["outAmount"])
            
            if direction == 'buy':
                entry_price = (input_amount / 1e9) / (output_amount / 10**TOKEN_DECIMALS)
                quantity = output_amount / 10**TOKEN_DECIMALS
                exit_price = self.current_price  # Will update next tick
            else:
                exit_price = (output_amount / 1e9) / (input_amount / 10**TOKEN_DECIMALS)
                quantity = input_amount / 10**TOKEN_DECIMALS
                entry_price = self.current_price  # Use last known entry

            # Execute swap
            async with self.session.post(
                f"{JUPITER_API_BASE}/swap",
                json={
                    "quoteResponse": quote_data,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True
                }
            ) as swap_resp:
                if swap_resp.status != 200:
                    raise ValueError(f"Swap error: {swap_resp.status}")
                swap_data = await swap_resp.json()

            # Process transaction
            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            txid = await self.client.send_raw_transaction(bytes(signed_tx))
            await self.client.confirm_transaction(txid.value)
            
            # Record metrics with actual prices
            fees = float(swap_data.get('fee', 0)) / 1e9
            self.health.metrics.record_trade(
                direction=direction,
                quantity=quantity,
                entry_price=entry_price,
                exit_price=exit_price,
                fees=fees
            )
            
            # Update Prometheus metrics
            trade_duration = (datetime.now() - start_time).total_seconds()
            self.trade_duration.observe(trade_duration)
            self.realized_pnl.set(self.health.metrics.realized_pnl)
            self.unrealized_pnl.set(self.health.metrics.update_unrealized(self.current_price))
            self.consecutive_wins.set(self.health.metrics.consecutive_wins)
            self.consecutive_losses.set(self.health.metrics.consecutive_losses)
            self.trade_count.labels(direction=direction).inc()
            
            logging.info(f"✅ {direction.upper()} {quantity:.4f} tokens @ {entry_price:.8f}→{exit_price:.8f}")
            self.health.last_trade_time = datetime.now()
            self.health.reset_errors()
            return True
            
        except Exception as e:
            logging.error("Trade execution failed", exc_info=True)
            self.error_count.inc()
            self.health.record_error()
            return False

    async def trading_loop(self):
        async with aiohttp.ClientSession(
            headers={'User-Agent': 'ProfessionalTrader/3.0'},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            self.session = session
            
            # Initial data collection
            while len(self.price_history) < RSI_PERIOD + 1:
                price = await self._get_jupiter_price()
                await self._update_price(price)
                logging.info(f"📊 Initializing data ({len(self.price_history)}/{RSI_PERIOD+1})")
                await asyncio.sleep(5)

            # Main trading loop
            while True:
                try:
                    if datetime.now() < self.health.trading_paused_until:
                        remaining = (self.health.trading_paused_until - datetime.now()).total_seconds()
                        logging.info(f"⏳ Paused for {int(remaining//60)}m {int(remaining%60)}s")
                        await asyncio.sleep(10)
                        continue

                    # Update balances
                    sol_balance = await self._get_fresh_balance(SOL_MINT)
                    self.sol_balance.set(sol_balance)
                    
                    if self.health.check_daily_loss(sol_balance):
                        await asyncio.sleep(60)
                        continue

                    if sol_balance < MIN_SOL_BALANCE + TX_FEE_BUFFER:
                        logging.warning("💸 Low SOL balance")
                        await asyncio.sleep(300)
                        continue

                    # Update market data
                    token_balance = await self._get_fresh_balance(TARGET_MINT)
                    self.token_balance.set(token_balance)
                    price = await self._get_jupiter_price()
                    rsi = await self._update_price(price)
                    
                    if not rsi:
                        await asyncio.sleep(10)
                        continue

                    # Update unrealized PnL
                    self.asset_price.set(price)
                    self.unrealized_pnl.set(self.health.metrics.update_unrealized(price))

                    # Trading logic
                    now = datetime.now()
                    if rsi < RSI_OVERSOLD and (now - self.health.last_trade_time).total_seconds() > TRADE_COOLDOWN:
                        amount = min(sol_balance - MIN_SOL_BALANCE - TX_FEE_BUFFER, sol_balance * RISK_PER_TRADE)
                        if amount >= 0.001:
                            logging.info(f"🚀 Buying {amount:.4f} SOL worth")
                            if await self.execute_trade('buy', amount):
                                self.health.last_trade_time = now

                    elif rsi > RSI_OVERBOUGHT and (now - self.health.last_trade_time).total_seconds() > TRADE_COOLDOWN:
                        if token_balance >= 1.0:
                            amount = token_balance * SELL_PERCENTAGE
                            logging.info(f"💰 Selling {amount:.2f} tokens")
                            if await self.execute_trade('sell', amount):
                                self.health.last_trade_time = now

                    await asyncio.sleep(15)
                    
                except Exception as e:
                    logging.error("Main loop error", exc_info=True)
                    await asyncio.sleep(30)

    async def close(self):
        await self.client.close()

# --- Application Entry Point ---
async def main():
    trader = ProfessionalRSITrader()
    try:
        await trader.trading_loop()
    except KeyboardInterrupt:
        logging.info("🛑 User requested shutdown")
    finally:
        await trader.close()

if __name__ == "__main__":
    asyncio.run(main())
