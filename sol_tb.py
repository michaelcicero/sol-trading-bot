#!/usr/bin/env python3

import os
import logging
import asyncio
import aiohttp
import base64
import numpy as np
from datetime import datetime, timedelta
from collections import deque, defaultdict
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import create_associated_token_account, get_associated_token_address
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from prometheus_client import start_http_server, Gauge, Counter, Histogram
import time

load_dotenv()

# Configuration - SOL ONLY TRADING
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# SOL Trading Configuration
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TARGET_MINT = USDC_MINT  # Trading SOL <-> USDC

# ⚡ OPTIMIZED PARAMETERS FOR MORE TRADES ⚡
RSI_BASE_PERIOD = 10                    # Much shorter = more signals
RSI_THRESHOLD_SENSITIVITY = 1.2         # Higher sensitivity = easier triggers  
TRADE_COOLDOWN = 600                    # 10 minutes instead of 2 hours
MIN_TIMEFRAME_AGREEMENT = 1             # Only need 1 of 3 timeframes
MIN_VOLUME_RATIO = 1.0                  # No volume filtering
TOKEN_DECIMALS = 6                      # USDC decimals
ATR_PERIOD = 10
TIMEFRAMES = ['5m', '15m', '1h']
TF_MAPPING = {'5m': 300, '15m': 900, '1h': 3600}

MAX_RISK_PCT = 0.05                     # 5% max per trade
MIN_RISK_PCT = 0.02                     # 2% min per trade
MAX_REASONABLE_PRICE = 1000             # SOL can go to $1000
MIN_SOL_BALANCE = 0.1
SELL_PERCENTAGE = 0.7                   # Sell 70% when signal triggers
SLIPPAGE_BPS = 100                      # 1% slippage
TX_FEE_BUFFER = 0.00001

MAX_CONSECUTIVE_ERRORS = 5
ERROR_PAUSE_MINUTES = 1                 # Only 1 minute pause
MAX_DAILY_LOSS = -0.1                   # 10% daily loss limit

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"

DIVERGENCE_WINDOW = 5
MIN_PRICE_CHANGE = 0.02                 # 2% price change
MIN_RSI_CHANGE = 5.0                    # 5 RSI change  
CONFIRMATION_COUNT = 1                  # Only need 1 confirmation

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'sol_trading_bot.log')),
        logging.StreamHandler()
    ]
)

logging.info("🎯 SOL-ONLY Trading Bot - Optimized for frequent trades")

class PnLCalculator:
    def __init__(self):
        self.trade_history = []
        self.positions = {'sol': 0.0, 'usdc': 0.0}
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.starting_balance = None
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.peak_prices = deque(maxlen=DIVERGENCE_WINDOW)
        self.trough_prices = deque(maxlen=DIVERGENCE_WINDOW)
        self.peak_rsi = deque(maxlen=DIVERGENCE_WINDOW)
        self.trough_rsi = deque(maxlen=DIVERGENCE_WINDOW)
        self.consecutive_bullish = 0
        self.consecutive_bearish = 0
        self.last_peak_price = None
        self.last_trough_price = None
        self.last_peak_rsi = None
        self.last_trough_rsi = None

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

        if direction == 'sell_sol':
            self.positions['sol'] -= quantity
            self.positions['usdc'] += (quantity * exit_price) - fees
        else:
            self.positions['sol'] += quantity
            self.positions['usdc'] -= (quantity * entry_price) + fees

        profit = (exit_price - entry_price) * quantity - fees
        if profit > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0

        self.realized_pnl += profit
        logging.info(f"✅ Trade: {direction} {quantity:.3f} SOL @ ${entry_price:.2f} → ${exit_price:.2f}, P&L: ${profit:.2f}")

    def update_unrealized(self, current_sol_price):
        if current_sol_price and self.positions['usdc'] > 0:
            potential_sol = self.positions['usdc'] / current_sol_price
            self.unrealized_pnl = potential_sol * current_sol_price
        return self.unrealized_pnl

    def detect_divergence(self, current_price, current_rsi):
        if current_price > (np.mean(self.peak_prices) if self.peak_prices else 0):
            if self.last_peak_price is None or abs(current_price - self.last_peak_price)/self.last_peak_price >= MIN_PRICE_CHANGE:
                self.peak_prices.append(current_price)
                self.last_peak_price = current_price
        elif current_price < (np.mean(self.trough_prices) if self.trough_prices else float('inf')):
            if self.last_trough_price is None or abs(current_price - self.last_trough_price)/self.last_trough_price >= MIN_PRICE_CHANGE:
                self.trough_prices.append(current_price)
                self.last_trough_price = current_price

        if current_rsi > (np.mean(self.peak_rsi) if self.peak_rsi else 0):
            if self.last_peak_rsi is None or abs(current_rsi - self.last_peak_rsi) >= MIN_RSI_CHANGE:
                self.peak_rsi.append(current_rsi)
                self.last_peak_rsi = current_rsi
        elif current_rsi < (np.mean(self.trough_rsi) if self.trough_rsi else 100):
            if self.last_trough_rsi is None or abs(current_rsi - self.last_trough_rsi) >= MIN_RSI_CHANGE:
                self.trough_rsi.append(current_rsi)
                self.last_trough_rsi = current_rsi

        bullish_div = False
        bearish_div = False

        if len(self.peak_prices) >= 2 and len(self.peak_rsi) >= 2:
            price_trend = self.peak_prices[-1] > self.peak_prices[-2]
            rsi_trend = self.peak_rsi[-1] < self.peak_rsi[-2]
            if price_trend and not rsi_trend:
                bearish_div = True

        if len(self.trough_prices) >= 2 and len(self.trough_rsi) >= 2:
            price_trend = self.trough_prices[-1] < self.trough_prices[-2]
            rsi_trend = self.trough_rsi[-1] > self.trough_rsi[-2]
            if price_trend and not rsi_trend:
                bullish_div = True

        if bullish_div:
            self.consecutive_bullish += 1
            self.consecutive_bearish = 0
        elif bearish_div:
            self.consecutive_bearish += 1
            self.consecutive_bullish = 0
        else:
            self.consecutive_bullish = 0
            self.consecutive_bearish = 0

        if self.consecutive_bullish >= CONFIRMATION_COUNT:
            return 'bullish'
        elif self.consecutive_bearish >= CONFIRMATION_COUNT:
            return 'bearish'
        return None

class HealthMonitor:
    def __init__(self):
        self.metrics = PnLCalculator()
        self.consecutive_errors = 0
        self.trading_paused_until = datetime.min
        self.max_daily_loss = MAX_DAILY_LOSS
        self.last_trade_time = datetime.min
        self.volatility = 0.0
        self.timeframe_data = defaultdict(lambda: deque(maxlen=50))
        self.volume_history = deque(maxlen=20)

    def record_error(self):
        self.consecutive_errors += 1
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            self.trading_paused_until = datetime.now() + timedelta(minutes=ERROR_PAUSE_MINUTES)
            logging.error(f"🛑 Trading paused until {self.trading_paused_until.strftime('%H:%M:%S')}")

    def reset_errors(self):
        self.consecutive_errors = 0
        self.trading_paused_until = datetime.min

    def check_daily_loss(self, current_sol_balance, current_sol_price):
        if self.metrics.starting_balance is None:
            self.metrics.starting_balance = current_sol_balance * current_sol_price
        
        current_usd_value = (current_sol_balance * current_sol_price) + self.metrics.update_unrealized(current_sol_price)
        daily_pnl = (current_usd_value - self.metrics.starting_balance) / self.metrics.starting_balance
        
        if daily_pnl <= self.max_daily_loss:
            self.trading_paused_until = datetime.now() + timedelta(hours=24)
            logging.error(f"🛑 Daily loss limit reached ({daily_pnl:.1%}). Trading paused.")
            return True
        return False

    def calculate_atr(self, high, low, close):
        tr = max(high - low, abs(high - close), abs(low - close))
        self.volatility = (self.volatility * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        return self.volatility

    def check_volume_condition(self):
        # Always return True for simplified volume checking
        return True

class SOLOnlyTrader:
    def __init__(self):
        self._validate_env()
        self.keypair = Keypair.from_base58_string(PRIVATE_KEY)
        self.wallet = str(self.keypair.pubkey())
        self.client = AsyncClient(HELIUS_RPC_URL, timeout=30, commitment=Confirmed)
        self.session = None
        self.price_history = deque(maxlen=100)
        self.current_sol_price = None
        self.health = HealthMonitor()
        self._setup_prometheus()
        self.rsi_history = deque(maxlen=30)
        logging.info(f"🔑 Wallet: {self.wallet}")

    def _validate_env(self):
        required = ['PRIVATE_KEY', 'HELIUS_API_KEY']
        if missing := [var for var in required if not os.getenv(var)]:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")
        if len(PRIVATE_KEY) < 40:
            raise ValueError("Invalid PRIVATE_KEY format")

    def _setup_prometheus(self):
        self.sol_balance = Gauge('sol_balance', 'SOL balance')
        self.usdc_balance = Gauge('usdc_balance', 'USDC balance')
        self.sol_price = Gauge('sol_price', 'SOL price USD')
        self.rsi_value = Gauge('rsi_value', 'RSI value')
        self.rsi_upper_threshold = Gauge('rsi_upper_threshold', 'RSI upper threshold')
        self.rsi_lower_threshold = Gauge('rsi_lower_threshold', 'RSI lower threshold')
        self.atr_value = Gauge('atr_value', 'ATR value')
        self.realized_pnl = Gauge('realized_pnl', 'Realized PnL')
        self.unrealized_pnl = Gauge('unrealized_pnl', 'Unrealized PnL')
        self.position_size = Gauge('position_size', 'Position size USD')
        self.trades_executed_total = Counter('trades_executed_total', 'Trades executed', ['direction', 'pair'])
        self.trade_execution_duration_seconds = Histogram('trade_execution_duration_seconds', 'Trade duration', buckets=[0.1, 0.5, 1, 2, 5, 10])
        self.trade_errors_total = Counter('trade_errors_total', 'Trade errors', ['error_type'])
        self.consecutive_wins = Gauge('consecutive_wins', 'Consecutive wins')
        self.consecutive_losses = Gauge('consecutive_losses', 'Consecutive losses')
        self.trading_paused = Gauge('trading_paused', 'Trading paused')
        self.buy_sol_signal = Gauge('buy_sol_signal', 'Buy SOL signal')
        self.sell_sol_signal = Gauge('sell_sol_signal', 'Sell SOL signal')
        self.volume_ratio = Gauge('volume_ratio', 'Volume ratio')
        self.divergence_status = Gauge('divergence_status', 'Divergence status', ['type'])
        self.risk_allocation = Gauge('risk_allocation', 'Risk allocation %')
        self.daily_pnl = Gauge('daily_pnl', 'Daily PnL %')
        
        try:
            start_http_server(8000)
            logging.info("🔧 Prometheus server started on port 8000")
        except Exception as e:
            logging.error(f"❌ Prometheus server failed: {e}")

    def _calculate_proper_atr(self):
        if len(self.price_history) < ATR_PERIOD:
            return 0.0
        
        recent_prices = list(self.price_history)[-ATR_PERIOD:]
        closes = [p['close'] for p in recent_prices]
        highs = [close * 1.002 for close in closes]  # 0.2% spread for SOL
        lows = [close * 0.998 for close in closes]
        
        true_ranges = []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            tr = max(hl, hc, lc)
            true_ranges.append(tr)
        
        if true_ranges:
            atr_value = np.mean(true_ranges)
            self.health.volatility = atr_value
            return atr_value
        
        return 0.0

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
            logging.debug("Balance check failed", exc_info=True)
            self.trade_errors_total.labels(error_type='balance_check').inc()
            return 0.0

    async def _ensure_ata(self):
        try:
            ata = get_associated_token_address(Pubkey.from_string(self.wallet), Pubkey.from_string(TARGET_MINT))
            if not (await self.client.get_account_info(ata)).value:
                logging.info("🔧 Creating USDC token account...")
                ix = create_associated_token_account(self.keypair.pubkey(), self.keypair.pubkey(), Pubkey.from_string(TARGET_MINT))
                await self.client.send_transaction(ix, self.keypair)
                await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"ATA creation failed: {e}")

    async def _get_sol_price(self):
        try:
            async with self.session.get(
                f"{JUPITER_API_BASE}/quote",
                params={
                    "inputMint": SOL_MINT,
                    "outputMint": USDC_MINT,
                    "amount": int(1 * 1e9),  # 1 SOL
                    "slippageBps": SLIPPAGE_BPS
                }
            ) as resp:
                if resp.status != 200:
                    self.trade_errors_total.labels(error_type='price_api').inc()
                    raise ValueError(f"Jupiter API error: {resp.status}")
                data = await resp.json()
                
                sol_price = float(data["outAmount"]) / 10**TOKEN_DECIMALS
                simulated_volume = sol_price * 100000  # Simulate high SOL volume
                self.health.volume_history.append(simulated_volume)
                
                return sol_price
        except Exception as e:
            logging.debug(f"Price fetch failed: {e}")
            self.trade_errors_total.labels(error_type='price_fetch').inc()
            return None

    async def _update_price(self, price):
        if price and 10 < price < MAX_REASONABLE_PRICE:  # SOL reasonable range
            self.current_sol_price = price
            self.price_history.append({'close': price, 'timestamp': datetime.now()})
            self.sol_price.set(price)
            return await self._calculate_rsi()
        return None

    async def _calculate_rsi(self):
        closes = [p['close'] for p in self.price_history]
        if len(closes) < RSI_BASE_PERIOD + 1:
            return None

        deltas = np.diff(closes)
        gains = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]

        avg_gain = sum(gains[:RSI_BASE_PERIOD]) / RSI_BASE_PERIOD
        avg_loss = sum(losses[:RSI_BASE_PERIOD]) / RSI_BASE_PERIOD or 0.0001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        for delta in deltas[RSI_BASE_PERIOD:]:
            avg_gain = (avg_gain * (RSI_BASE_PERIOD - 1) + max(delta, 0)) / RSI_BASE_PERIOD
            avg_loss = (avg_loss * (RSI_BASE_PERIOD - 1) + abs(min(delta, 0))) / RSI_BASE_PERIOD
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

        self.rsi_value.set(rsi)
        self.rsi_history.append(rsi)
        return rsi

    def _calculate_dynamic_thresholds(self):
        if len(self.rsi_history) < 5:
            return (75, 25)  # More extreme thresholds for SOL
        
        avg_rsi = np.mean(self.rsi_history)
        rsi_std = np.std(self.rsi_history)
        
        upper_threshold = avg_rsi + (rsi_std * RSI_THRESHOLD_SENSITIVITY)
        lower_threshold = avg_rsi - (rsi_std * RSI_THRESHOLD_SENSITIVITY)
        
        # SOL-specific bounds
        final_upper = min(85, max(65, upper_threshold))
        final_lower = max(15, min(35, lower_threshold))
        
        self.rsi_upper_threshold.set(final_upper)
        self.rsi_lower_threshold.set(final_lower)
        return (final_upper, final_lower)

    async def _get_multi_timeframe_rsi(self):
        timeframe_rsis = {}
        for tf in TIMEFRAMES:
            try:
                if len(self.health.timeframe_data[tf]) >= RSI_BASE_PERIOD:
                    closes = list(self.health.timeframe_data[tf])[-RSI_BASE_PERIOD:]
                    deltas = np.diff(closes)
                    gains = [max(d, 0) for d in deltas]
                    losses = [abs(min(d, 0)) for d in deltas]
                    avg_gain = sum(gains) / RSI_BASE_PERIOD
                    avg_loss = sum(losses) / RSI_BASE_PERIOD or 0.0001
                    rs = avg_gain / avg_loss
                    timeframe_rsis[tf] = 100 - (100 / (1 + rs))
                
                if self.current_sol_price:
                    self.health.timeframe_data[tf].append(self.current_sol_price)
                    
            except Exception as e:
                logging.debug(f"Timeframe RSI {tf} failed: {e}")
        return timeframe_rsis

    async def execute_trade(self, direction, amount):
        start_time = time.time()
        
        if datetime.now() < self.health.trading_paused_until:
            logging.warning("⏸️ Trading paused")
            return False

        try:
            await self._ensure_ata()
            
            if direction == 'sell_sol':
                input_mint = SOL_MINT
                output_mint = USDC_MINT
                trade_amount = int(amount * 1e9)
            else:  # buy_sol
                input_mint = USDC_MINT
                output_mint = SOL_MINT
                trade_amount = int(amount * 10**TOKEN_DECIMALS)
            
            async with self.session.get(
                f"{JUPITER_API_BASE}/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": trade_amount,
                    "slippageBps": SLIPPAGE_BPS
                }
            ) as quote_resp:
                if quote_resp.status != 200:
                    self.trade_errors_total.labels(error_type='quote_api').inc()
                    raise ValueError(f"Quote error: {quote_resp.status}")
                quote_data = await quote_resp.json()

                input_amount = float(quote_data["inAmount"])
                output_amount = float(quote_data["outAmount"])
                
                if direction == 'sell_sol':
                    sol_quantity = input_amount / 1e9
                    usdc_received = output_amount / 10**TOKEN_DECIMALS
                    entry_price = self.current_sol_price
                    exit_price = usdc_received / sol_quantity
                else:  # buy_sol
                    usdc_spent = input_amount / 10**TOKEN_DECIMALS
                    sol_quantity = output_amount / 1e9
                    entry_price = usdc_spent / sol_quantity
                    exit_price = self.current_sol_price

                async with self.session.post(
                    f"{JUPITER_API_BASE}/swap",
                    json={
                        "quoteResponse": quote_data,
                        "userPublicKey": self.wallet,
                        "wrapUnwrapSOL": True
                    }
                ) as swap_resp:
                    if swap_resp.status != 200:
                        self.trade_errors_total.labels(error_type='swap_api').inc()
                        raise ValueError(f"Swap error: {swap_resp.status}")
                    swap_data = await swap_resp.json()

                    tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                    tx = VersionedTransaction.from_bytes(tx_bytes)
                    signed_tx = VersionedTransaction(tx.message, [self.keypair])
                    txid = await self.client.send_raw_transaction(bytes(signed_tx))
                    await self.client.confirm_transaction(txid.value)

                    fees = 0.000005  # Estimated SOL fee
                    self.health.metrics.record_trade(
                        direction=direction,
                        quantity=sol_quantity,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        fees=fees
                    )

                    # Update metrics
                    trade_duration = time.time() - start_time
                    self.trade_execution_duration_seconds.observe(trade_duration)
                    self.trades_executed_total.labels(direction=direction, pair='SOL/USDC').inc()
                    self.realized_pnl.set(self.health.metrics.realized_pnl)
                    self.unrealized_pnl.set(self.health.metrics.update_unrealized(self.current_sol_price))
                    self.consecutive_wins.set(self.health.metrics.consecutive_wins)
                    self.consecutive_losses.set(self.health.metrics.consecutive_losses)
                    
                    if direction == 'sell_sol':
                        self.position_size.set(-usdc_received)
                    else:
                        self.position_size.set(usdc_spent)

                    self.health.last_trade_time = datetime.now()
                    self.health.reset_errors()
                    return True
                    
        except Exception as e:
            logging.error(f"Trade execution failed: {e}")
            self.trade_errors_total.labels(error_type='execution').inc()
            self.health.record_error()
            return False

    async def trading_loop(self):
        async with aiohttp.ClientSession(
            headers={'User-Agent': 'SOLTrader/1.0'},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            self.session = session
            
            # Initialize price history
            logging.info(f"📊 Initializing SOL price data...")
            while len(self.price_history) < RSI_BASE_PERIOD + 1:
                price = await self._get_sol_price()
                if price:
                    await self._update_price(price)
                    logging.info(f"📊 SOL Price: ${price:.2f} ({len(self.price_history)}/{RSI_BASE_PERIOD+1})")
                await asyncio.sleep(3)

            logging.info("🚀 Trading loop started!")

            while True:
                try:
                    # Check if trading is paused
                    is_paused = datetime.now() < self.health.trading_paused_until
                    self.trading_paused.set(1 if is_paused else 0)
                    
                    if is_paused:
                        remaining = (self.health.trading_paused_until - datetime.now()).total_seconds()
                        logging.info(f"⏳ Paused for {int(remaining//60)}m {int(remaining%60)}s")
                        await asyncio.sleep(10)
                        continue

                    # Update balances
                    sol_balance = await self._get_fresh_balance(SOL_MINT)
                    usdc_balance = await self._get_fresh_balance(USDC_MINT)
                    self.sol_balance.set(sol_balance)
                    self.usdc_balance.set(usdc_balance)
                    
                    # Get current price and RSI
                    price = await self._get_sol_price()
                    rsi = await self._update_price(price)

                    if not rsi or not price:
                        await asyncio.sleep(10)
                        continue

                    # Check daily loss limit
                    if self.health.check_daily_loss(sol_balance, price):
                        await asyncio.sleep(60)
                        continue

                    # Calculate daily PnL
                    if self.health.metrics.starting_balance:
                        current_usd_value = (sol_balance * price) + usdc_balance
                        daily_pnl_pct = ((current_usd_value - self.health.metrics.starting_balance) / self.health.metrics.starting_balance) * 100
                        self.daily_pnl.set(daily_pnl_pct)

                    # Calculate ATR
                    atr_value = self._calculate_proper_atr()
                    self.atr_value.set(atr_value)

                    # Calculate thresholds
                    upper_threshold, lower_threshold = self._calculate_dynamic_thresholds()
                    timeframe_rsis = await self._get_multi_timeframe_rsi()
                    divergence = self.health.metrics.detect_divergence(price, rsi)
                    
                    # Update divergence metrics
                    if divergence == 'bullish':
                        self.divergence_status.labels(type='bullish').set(1)
                        self.divergence_status.labels(type='bearish').set(0)
                    elif divergence == 'bearish':
                        self.divergence_status.labels(type='bearish').set(1)
                        self.divergence_status.labels(type='bullish').set(0)
                    else:
                        self.divergence_status.labels(type='bullish').set(0)
                        self.divergence_status.labels(type='bearish').set(0)
                    
                    # Check trading conditions
                    now = datetime.now()
                    time_since_last = (now - self.health.last_trade_time).total_seconds()
                    
                    timeframe_alignment_buy = sum(1 for rsi_val in timeframe_rsis.values() if rsi_val < lower_threshold)
                    timeframe_alignment_sell = sum(1 for rsi_val in timeframe_rsis.values() if rsi_val > upper_threshold)
                    
                    # SIMPLIFIED CONDITIONS FOR MORE TRADES
                    buy_sol_conditions = [
                        rsi < lower_threshold,
                        time_since_last > TRADE_COOLDOWN,
                        timeframe_alignment_buy >= MIN_TIMEFRAME_AGREEMENT,
                        usdc_balance > 20,  # At least $20 USDC
                        sol_balance > MIN_SOL_BALANCE + TX_FEE_BUFFER
                    ]
                    
                    sell_sol_conditions = [
                        rsi > upper_threshold,
                        time_since_last > TRADE_COOLDOWN,
                        timeframe_alignment_sell >= MIN_TIMEFRAME_AGREEMENT,
                        sol_balance > MIN_SOL_BALANCE + TX_FEE_BUFFER + 0.1  # Have SOL to sell
                    ]

                    # Status logging
                    logging.info(
                        f"🔍 SOL=${price:.2f} RSI={rsi:.1f} L={lower_threshold:.1f} U={upper_threshold:.1f} "
                        f"SOL={sol_balance:.3f} USDC=${usdc_balance:.0f} "
                        f"BuyOK={all(buy_sol_conditions)} SellOK={all(sell_sol_conditions)} "
                        f"LastTrade={int(time_since_last/60)}min"
                    )

                    self.buy_sol_signal.set(1 if all(buy_sol_conditions) else 0)
                    self.sell_sol_signal.set(1 if all(sell_sol_conditions) else 0)

                    # Execute trades
                    if all(buy_sol_conditions):
                        total_usd_value = (sol_balance * price) + usdc_balance
                        risk_pct = min(MAX_RISK_PCT, max(MIN_RISK_PCT, 0.03))  # 3% default
                        usdc_to_spend = min(usdc_balance * 0.8, total_usd_value * risk_pct)
                        
                        self.risk_allocation.set(risk_pct * 100)
                        
                        if usdc_to_spend >= 20:
                            logging.info(f"🚀 BUYING SOL with ${usdc_to_spend:.0f} USDC at ${price:.2f}")
                            if await self.execute_trade('buy_sol', usdc_to_spend):
                                self.health.last_trade_time = now

                    elif all(sell_sol_conditions):
                        sol_to_sell = (sol_balance - MIN_SOL_BALANCE - TX_FEE_BUFFER) * SELL_PERCENTAGE
                        
                        if sol_to_sell >= 0.05:  # At least 0.05 SOL
                            usd_value = sol_to_sell * price
                            logging.info(f"💰 SELLING {sol_to_sell:.2f} SOL (~${usd_value:.0f}) at ${price:.2f}")
                            if await self.execute_trade('sell_sol', sol_to_sell):
                                self.health.last_trade_time = now

                    await asyncio.sleep(30)  # Check every 30 seconds
                    
                except Exception as e:
                    logging.error(f"Main loop error: {e}")
                    self.trade_errors_total.labels(error_type='main_loop').inc()
                    await asyncio.sleep(30)

    async def close(self):
        await self.client.close()

async def main():
    trader = SOLOnlyTrader()
    try:
        await trader.trading_loop()
    except KeyboardInterrupt:
        logging.info("🛑 Shutdown requested")
    finally:
        await trader.close()

if __name__ == "__main__":
    asyncio.run(main())
