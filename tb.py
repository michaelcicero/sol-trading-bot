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

load_dotenv()

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

SOL_MINT = os.getenv("SOL_MINT", "So11111111111111111111111111111111111111112")
TARGET_MINT = os.getenv("TARGET_MINT", "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")
TOKEN_DECIMALS = int(os.getenv("TOKEN_DECIMALS", "9"))
RSI_BASE_PERIOD = int(os.getenv("RSI_BASE_PERIOD", "7"))
RSI_THRESHOLD_SENSITIVITY = float(os.getenv("RSI_THRESHOLD_SENSITIVITY", "1.0"))  # Reduced sensitivity
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
TIMEFRAMES = ['5m', '15m', '1h']
TF_MAPPING = {'5m': 300, '15m': 900, '1h': 3600}
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "0.1"))
MIN_RISK_PCT = float(os.getenv("MIN_RISK_PCT", "0.03"))
RISK_ADJUSTMENT_FACTOR = float(os.getenv("RISK_ADJUSTMENT_FACTOR", "0.5"))
MAX_REASONABLE_PRICE = float(os.getenv("MAX_REASONABLE_PRICE", "0.1"))
MIN_SOL_BALANCE = float(os.getenv("MIN_SOL_BALANCE", "0.1"))
TRADE_COOLDOWN = int(os.getenv("TRADE_COOLDOWN", "1800"))  # 30 minutes instead of 2
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
DIVERGENCE_WINDOW = int(os.getenv("DIVERGENCE_WINDOW", "7"))
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "0.05"))
MIN_RSI_CHANGE = float(os.getenv("MIN_RSI_CHANGE", "10.0"))
CONFIRMATION_COUNT = int(os.getenv("CONFIRMATION_COUNT", "2"))

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'professional_trading.log')),
        logging.StreamHandler()
    ]
)

class PnLCalculator:
    def __init__(self):
        self.trade_history = []
        self.positions = {'sol': 0.0, 'token': 0.0}
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
        self.timeframe_data = defaultdict(lambda: deque(maxlen=100))

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

    def calculate_atr(self, high, low, close):
        tr = max(high - low, abs(high - close), abs(low - close))
        self.volatility = (self.volatility * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        return self.volatility

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
        self.rsi_history = deque(maxlen=50)
        self.atr_values = deque(maxlen=ATR_PERIOD)

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
        self.rsi_upper_threshold = Gauge('rsi_upper_threshold', 'Dynamic upper RSI threshold')
        self.rsi_lower_threshold = Gauge('rsi_lower_threshold', 'Dynamic lower RSI threshold')
        self.divergence_status = Gauge('divergence_status', 'Market divergence status', ['type'])
        self.atr = Gauge('atr', 'Average True Range')
        self.risk_allocation = Gauge('risk_allocation', 'Current risk allocation percentage')
        self.position_size = Gauge('position_size', 'Current position size')
        self.buy_signal = Gauge('buy_signal', 'Active buy signal')
        self.sell_signal = Gauge('sell_signal', 'Active sell signal')
        start_http_server(8000)

    def _calculate_proper_atr(self):
        """Calculate ATR using price history with realistic crypto spreads"""
        if len(self.price_history) < ATR_PERIOD:
            return 0.0
        
        recent_prices = list(self.price_history)[-ATR_PERIOD:]
        closes = [p['close'] for p in recent_prices]
        
        # Use realistic crypto spreads (1.5% instead of 0.1%)
        highs = [close * 1.015 for close in closes]  # 1.5% spread
        lows = [close * 0.985 for close in closes]
        
        true_ranges = []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i-1])
            lc = abs(lows[i] - closes[i-1])
            tr = max(hl, hc, lc)
            true_ranges.append(tr)
        
        if len(true_ranges) >= ATR_PERIOD - 1:
            atr_value = np.mean(true_ranges[-(ATR_PERIOD-1):])
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
            return await self._calculate_enhanced_rsi()
        return None

    async def _calculate_enhanced_rsi(self):
        closes = [p['close'] for p in self.price_history]
        if len(closes) < RSI_BASE_PERIOD + 1:
            return None

        price_list = list(self.price_history)
        volatility = np.std([p['close'] for p in price_list[-ATR_PERIOD:]])
        dynamic_period = max(5, min(20, int(RSI_BASE_PERIOD * (1 + volatility * 2))))
        
        deltas = np.diff(closes)
        gains = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]

        avg_gain = sum(gains[:dynamic_period]) / dynamic_period
        avg_loss = sum(losses[:dynamic_period]) / dynamic_period or 0.0001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        for delta in deltas[dynamic_period:]:
            avg_gain = (avg_gain * (dynamic_period - 1) + max(delta, 0)) / dynamic_period
            avg_loss = (avg_loss * (dynamic_period - 1) + abs(min(delta, 0))) / dynamic_period
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

        self.rsi_value.set(rsi)
        self.rsi_history.append(rsi)
        return rsi

    def _calculate_dynamic_thresholds(self):
        if len(self.rsi_history) < ATR_PERIOD:
            return (70, 30)  # Less extreme thresholds
        
        avg_rsi = np.mean(self.rsi_history)
        rsi_std = np.std(self.rsi_history)
        
        # Wider thresholds for fewer, better trades
        upper_threshold = avg_rsi + (rsi_std * RSI_THRESHOLD_SENSITIVITY)
        lower_threshold = avg_rsi - (rsi_std * RSI_THRESHOLD_SENSITIVITY)
        
        final_upper = min(75, max(60, upper_threshold))  # Tighter range
        final_lower = max(25, min(40, lower_threshold))
        
        self.rsi_upper_threshold.set(final_upper)
        self.rsi_lower_threshold.set(final_lower)
        return (final_upper, final_lower)

    async def _get_multi_timeframe_rsi(self):
        timeframe_rsis = {}
        for tf in TIMEFRAMES:
            try:
                async with self.session.get(
                    f"{JUPITER_API_BASE}/quote",
                    params={
                        "inputMint": SOL_MINT,
                        "outputMint": TARGET_MINT,
                        "amount": int(0.01 * 1e9),
                        "slippageBps": SLIPPAGE_BPS,
                        "interval": TF_MAPPING[tf]
                    }
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = 0.01 / (float(data["outAmount"]) / 10**TOKEN_DECIMALS)
                        self.health.timeframe_data[tf].append(price)
                        if len(self.health.timeframe_data[tf]) >= RSI_BASE_PERIOD:
                            closes = list(self.health.timeframe_data[tf])[-RSI_BASE_PERIOD:]
                            deltas = np.diff(closes)
                            gains = [max(d, 0) for d in deltas]
                            losses = [abs(min(d, 0)) for d in deltas]
                            avg_gain = sum(gains) / RSI_BASE_PERIOD
                            avg_loss = sum(losses) / RSI_BASE_PERIOD or 0.0001
                            rs = avg_gain / avg_loss
                            timeframe_rsis[tf] = 100 - (100 / (1 + rs))
            except Exception as e:
                logging.error(f"Failed to get {tf} timeframe RSI", exc_info=True)
        return timeframe_rsis

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

                input_amount = float(quote_data["inAmount"])
                output_amount = float(quote_data["outAmount"])
                
                if direction == 'buy':
                    entry_price = (input_amount / 1e9) / (output_amount / 10**TOKEN_DECIMALS)
                    quantity = output_amount / 10**TOKEN_DECIMALS
                    exit_price = self.current_price
                else:
                    exit_price = (output_amount / 1e9) / (input_amount / 10**TOKEN_DECIMALS)
                    quantity = input_amount / 10**TOKEN_DECIMALS
                    entry_price = self.current_price

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

                    tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                    tx = VersionedTransaction.from_bytes(tx_bytes)
                    signed_tx = VersionedTransaction(tx.message, [self.keypair])
                    txid = await self.client.send_raw_transaction(bytes(signed_tx))
                    await self.client.confirm_transaction(txid.value)

                    fees = float(swap_data.get('fee', 0)) / 1e9
                    self.health.metrics.record_trade(
                        direction=direction,
                        quantity=quantity,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        fees=fees
                    )

                    trade_duration = (datetime.now() - start_time).total_seconds()
                    self.trade_duration.observe(trade_duration)
                    self.realized_pnl.set(self.health.metrics.realized_pnl)
                    self.unrealized_pnl.set(self.health.metrics.update_unrealized(self.current_price))
                    self.consecutive_wins.set(self.health.metrics.consecutive_wins)
                    self.consecutive_losses.set(self.health.metrics.consecutive_losses)
                    self.trade_count.labels(direction=direction).inc()
                    self.position_size.set(quantity if direction == 'buy' else -quantity)

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
            headers={'User-Agent': 'ProfessionalTrader/4.0'},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            self.session = session
            while len(self.price_history) < RSI_BASE_PERIOD + 1:
                price = await self._get_jupiter_price()
                await self._update_price(price)
                logging.info(f"📊 Initializing data ({len(self.price_history)}/{RSI_BASE_PERIOD+1})")
                await asyncio.sleep(5)

            while True:
                try:
                    if datetime.now() < self.health.trading_paused_until:
                        remaining = (self.health.trading_paused_until - datetime.now()).total_seconds()
                        logging.info(f"⏳ Paused for {int(remaining//60)}m {int(remaining%60)}s")
                        await asyncio.sleep(10)
                        continue

                    sol_balance = await self._get_fresh_balance(SOL_MINT)
                    self.sol_balance.set(sol_balance)
                    if self.health.check_daily_loss(sol_balance):
                        await asyncio.sleep(60)
                        continue

                    if sol_balance < MIN_SOL_BALANCE + TX_FEE_BUFFER:
                        logging.warning("💸 Low SOL balance")
                        await asyncio.sleep(300)
                        continue

                    token_balance = await self._get_fresh_balance(TARGET_MINT)
                    self.token_balance.set(token_balance)
                    price = await self._get_jupiter_price()
                    rsi = await self._update_price(price)

                    if not rsi:
                        await asyncio.sleep(10)
                        continue

                    # Calculate ATR after price update
                    atr_value = self._calculate_proper_atr()
                    self.atr.set(atr_value)

                    self.asset_price.set(price)
                    self.unrealized_pnl.set(self.health.metrics.update_unrealized(price))
                    
                    upper_threshold, lower_threshold = self._calculate_dynamic_thresholds()
                    timeframe_rsis = await self._get_multi_timeframe_rsi()
                    divergence = self.health.metrics.detect_divergence(price, rsi)
                    
                    if divergence == 'bullish':
                        self.divergence_status.labels(type='bullish').set(1)
                        self.divergence_status.labels(type='bearish').set(0)
                    elif divergence == 'bearish':
                        self.divergence_status.labels(type='bearish').set(1)
                        self.divergence_status.labels(type='bullish').set(0)
                    else:
                        self.divergence_status.labels(type='bullish').set(0)
                        self.divergence_status.labels(type='bearish').set(0)
                    
                    now = datetime.now()
                    time_since_last = (now - self.health.last_trade_time).total_seconds()
                    
                    # Improved multi-timeframe alignment (majority consensus)
                    timeframe_alignment_buy = sum(1 for rsi_val in timeframe_rsis.values() if rsi_val < lower_threshold)
                    timeframe_alignment_sell = sum(1 for rsi_val in timeframe_rsis.values() if rsi_val > upper_threshold)
                    
                    buy_conditions = [
                        rsi < lower_threshold,
                        time_since_last > TRADE_COOLDOWN,
                        timeframe_alignment_buy >= 2,  # At least 2 of 3 timeframes agree
                    ]
                    
                    sell_conditions = [
                        rsi > upper_threshold,
                        time_since_last > TRADE_COOLDOWN,
                        timeframe_alignment_sell >= 2,  # At least 2 of 3 timeframes agree
                    ]

                    self.buy_signal.set(1 if all(buy_conditions) else 0)
                    self.sell_signal.set(1 if all(sell_conditions) else 0)

                    if all(buy_conditions):
                        # Improved position sizing
                        base_risk = 0.05  # 5% base risk
                        volatility_multiplier = min(2.0, max(0.5, atr_value * 1000))  # Scale ATR
                        risk_pct = base_risk * volatility_multiplier
                        risk_pct = max(MIN_RISK_PCT, min(MAX_RISK_PCT, risk_pct))
                        
                        amount = min(sol_balance - MIN_SOL_BALANCE - TX_FEE_BUFFER, sol_balance * risk_pct)
                        
                        self.risk_allocation.set(risk_pct * 100)
                        
                        if amount >= 0.001:
                            logging.info(f"🚀 Buying {amount:.4f} SOL worth (Risk: {risk_pct*100:.1f}%, ATR: {atr_value:.6f}, Alignment: {timeframe_alignment_buy}/3)")
                            if await self.execute_trade('buy', amount):
                                self.health.last_trade_time = now

                    elif all(sell_conditions) and token_balance >= 1.0:
                        amount = token_balance * SELL_PERCENTAGE
                        logging.info(f"💰 Selling {amount:.2f} tokens (ATR: {atr_value:.6f}, Alignment: {timeframe_alignment_sell}/3)")
                        if await self.execute_trade('sell', amount):
                            self.health.last_trade_time = now

                    await asyncio.sleep(60)  # 1 minute instead of 15 seconds
                except Exception as e:
                    logging.error("Main loop error", exc_info=True)
                    await asyncio.sleep(30)

    async def close(self):
        await self.client.close()

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
