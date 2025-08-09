import os
import logging
import asyncio
import aiohttp
import base58
import base64
import random
import math
import json
import websockets
import time
from datetime import datetime, timedelta
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from nacl.signing import SigningKey
from solders.signature import Signature
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts, TokenAccountOpts
from spl.token._layouts import ACCOUNT_LAYOUT
from spl.token.instructions import create_associated_token_account, get_associated_token_address
from prometheus_client import start_http_server, Gauge, Counter, Histogram

# === UPDATED CONFIGURATION ===
SOL_MINT = "So11111111111111111111111111111111111111112"
TARGET_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
TOKEN_DECIMALS = 9
TOKEN_SYMBOL = "POPCAT"
MIN_ATR_VALUE = 0.00005
MIN_PRICE_CHANGE = 0.001
MAX_REASONABLE_PRICE = 0.005
MIN_TOKEN_RECEIVED = 100_000_000
MIN_SOL_BALANCE = 0.05
HELIUS_WS_URL = "wss://mainnet.helius-rpc.com/?api-key=a4f26d1d-0de0-4879-bede-d7818f4a919a"
JUPITER_PRICE_API = "https://price.jup.ag/v4/price?ids=7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr&vsToken=SOL"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v4/quote"
JUPITER_SWAP_API = "https://swap-api.jup.ag/v4/swap"

class HealthMonitor:
    def __init__(self):
        self.last_success = datetime.now()
        self.consecutive_errors = 0
        self.daily_pnl = 0.0
        self.MAX_DAILY_LOSS = -0.1
        self.last_daily_reset = datetime.now()
    
    async def check_health(self):
        if (datetime.now() - self.last_success).seconds > 600:
            logging.critical("🆘 No successful trades in 10 minutes!")
        self.last_success = datetime.now()
    
    def record_error(self):
        self.consecutive_errors += 1
        if self.consecutive_errors > 5:
            logging.error("🚨 Consecutive error threshold exceeded!")
    
    def reset_daily_pnl(self):
        if datetime.now().date() != self.last_daily_reset.date():
            self.daily_pnl = 0.0
            self.last_daily_reset = datetime.now()

class TokenTrader:
    def __init__(self):
        load_dotenv()
        self.client = AsyncClient(
            "https://mainnet.helius-rpc.com/?api-key=a4f26d1d-0de0-4879-bede-d7818f4a919a",
            timeout=30,
            commitment=Confirmed
        )
        self.base58_private_key = os.getenv("PRIVATE_KEY")
        if not self.base58_private_key:
            raise ValueError("Missing PRIVATE_KEY in .env")
        self.keypair = Keypair.from_base58_string(self.base58_private_key)
        self.wallet = str(self.keypair.pubkey())
        
        self.price_history = deque(maxlen=100)
        self.short_atr_values = deque(maxlen=10)
        self.long_atr_values = deque(maxlen=20)
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.price_lock = asyncio.Lock()
        self.atr_lock = asyncio.Lock()
        
        self._setup_prometheus()
        self._setup_websocket()
        self._setup_balance_cache()
        self._setup_trading_parameters()
        self._setup_state_management()
        
        logging.info(f"🔑 Trading Wallet: {self.wallet}")
        logging.info(f"🔗 Helius Endpoint: {self.client._provider.endpoint_uri}")

    def _setup_prometheus(self):
        self.sol_balance_gauge = Gauge('sol_balance', 'Current SOL balance', ['wallet'])
        self.popcat_balance_gauge = Gauge('popcat_balance', 'Current POPCAT balance', ['wallet'])
        self.popcat_price_gauge = Gauge('popcat_price', 'Current POPCAT price')
        self.trade_size_gauge = Gauge('trade_size', 'Trade size in SOL', ['direction'])
        self.trade_count = Counter('trade_count_total', 'Total trades executed', ['direction'])
        self.trade_duration = Histogram('trade_duration_seconds', 'Trade execution time')
        self.realized_pnl = Counter('realized_pnl_total', 'Total realized PnL', ['currency'])
        self.unrealized_pnl = Gauge('unrealized_pnl', 'Current unrealized PnL')
        self.price_updates = Counter('price_updates_total', 'Price updates received', ['source'])
        self.api_errors = Counter('api_errors_total', 'API errors encountered', ['endpoint'])
        start_http_server(8000)

    def _setup_websocket(self):
        self.price_update_event = asyncio.Event()
        self.current_price = None
        self.last_ws_update = datetime.now()

    def _setup_balance_cache(self):
        self.balance_cache = {'sol': 0.0, 'token': 0.0}
        self.last_balance_update = datetime.min

    def _setup_trading_parameters(self):
        self.MIN_BALANCE_BUFFER = 0.005
        self.MIN_TOKEN_BUFFER = 0.0001
        self.short_atr_period = 10
        self.long_atr_period = 20
        self.atr_multiplier = 3.0
        self.cooldown = 60
        self.min_sell_amount = 30
        self.sell_percentage = 0.90
        self.sell_threshold = 1.0005
        self.trailing_stop_pct = 0.985
        self.min_profit_sol = 0.005
        self.max_trade_duration = timedelta(minutes=30)
        self.risk_per_trade = 0.03

    def _setup_state_management(self):
        self.fifo_queue = deque()
        self.total_invested_sol = 0.0
        self.total_popcat_bought = 0.0
        self.last_trade = datetime.min
        self.consecutive_buys = 0
        self.max_consecutive_buys = 4
        self.health_monitor = HealthMonitor()
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            headers={'X-Helius-Client': 'TradingBot/1.0'},
            timeout=aiohttp.ClientTimeout(total=30)
        )

    async def _log_metrics(self):
        self.sol_balance_gauge.labels(wallet=self.wallet).set(self.balance_cache['sol'])
        self.popcat_balance_gauge.labels(wallet=self.wallet).set(self.balance_cache['token'])
        self.popcat_price_gauge.set(self.current_price or 0)
        
        if self.total_popcat_bought > 0 and self.current_price:
            unrealized = (self.current_price * self.total_popcat_bought) - self.total_invested_sol
            self.unrealized_pnl.set(unrealized)

    async def fetch_initial_price(self):
        retries = 3
        backoff = 1
        for attempt in range(retries):
            try:
                async with self.session.get(
                    JUPITER_PRICE_API,
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as resp:
                    data = await resp.json()
                    price = float(data['data']['7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr']['price'])
                    for _ in range(10):
                        await self._update_price_data(price, source='Jupiter')
                    return
            except Exception as e:
                logging.warning(f"Price init attempt {attempt+1} failed: {str(e)}")
                await asyncio.sleep(backoff)
                backoff *= 2
        logging.error("Failed to initialize prices - check network connection")

    async def _update_price_data(self, price, source=None):
        async with self.price_lock:
            if self.current_price and abs(price - self.current_price)/self.current_price < 0.001:
                return
            
            high = price
            low = price
            
            if self.price_history:
                prev_close = self.price_history[-1]['close']
                high = max(price, prev_close * 1.005)
                low = min(price, prev_close * 0.995)
            
            self.current_price = price
            self.price_history.append({
                'high': high,
                'low': low,
                'close': price,
                'timestamp': datetime.now()
            })
            logging.debug(f"Price updated: {price:.6f}. History: {len(self.price_history)}")
            self.popcat_price_gauge.set(price)
            self.last_ws_update = datetime.now()
            if source:
                logging.info(f"📊 Price update ({source}): {price:.6f} SOL")
                self.price_updates.labels(source=source).inc()

    async def backup_price_poller(self):
        while True:
            try:
                async with self.session.get(
                    JUPITER_PRICE_API,
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as resp:
                    data = await resp.json()
                    price = float(data['data']['7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr']['price'])
                    await self._update_price_data(price, source='Jupiter')
            except Exception as e:
                logging.error(f"Price polling failed: {str(e)}")
            await asyncio.sleep(30)

    async def listen_for_price_updates(self):
        reconnect_delay = 1
        while True:
            try:
                async with websockets.connect(
                    HELIUS_WS_URL,
                    ping_interval=15,
                    ping_timeout=15,
                    close_timeout=10,
                    max_queue=2048,
                    extra_headers={"User-Agent": "Mozilla/5.0"}
                ) as ws:
                    logging.info("✅ WebSocket connected")
                    await self._setup_websocket_subscription(ws)
                    async for message in ws:
                        await self._process_websocket_message(message)
                        self.last_ws_update = datetime.now()
            except Exception as e:
                jitter = random.uniform(0.5, 1.5)
                delay = reconnect_delay * jitter
                logging.error(f"WS error: {str(e)}, retry in {delay:.1f}s")
                await asyncio.sleep(delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

    async def _setup_websocket_subscription(self, ws):
        subscribe_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "transactionSubscribe",
            "params": [
                {"mentions": [f"token:{TARGET_MINT}"], "failed": False},
                {"commitment": "confirmed", "encoding": "jsonParsed"}
            ]
        }
        await ws.send(json.dumps(subscribe_msg))
        await ws.recv()

    async def _process_websocket_message(self, message):
        try:
            data = json.loads(message)
            if data.get('method') != 'transactionNotification':
                return

            price = await self._parse_transaction_price(data)
            if price and 1e-8 < price < MAX_REASONABLE_PRICE:
                await self._update_price_data(price, source='WS')
                self.price_update_event.set()
        except Exception as e:
            logging.error(f"Price processing error: {str(e)}")
            try:
                async with self.session.get(
                    JUPITER_PRICE_API,
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as resp:
                    data = await resp.json()
                    price = float(data['data']['7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr']['price'])
                    await self._update_price_data(price, source='Jupiter')
            except Exception as fallback_error:
                logging.error(f"Fallback price fetch failed: {str(fallback_error)}")

    async def _parse_transaction_price(self, data):
        try:
            result = data['params']['result']
            message = result['transaction']['message']
            account_keys = message['accountKeys']
            
            for ix in message['instructions']:
                if ix['programId'] == 'srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX':
                    data = base64.b64decode(ix['data'])
                    if data[0] == 9:
                        in_amount = int.from_bytes(data[1:9], 'little')
                        out_amount = int.from_bytes(data[9:17], 'little')
                        sol_index = account_keys.index(SOL_MINT)
                        popcat_index = account_keys.index(TARGET_MINT)
                        return in_amount / (out_amount / 10**TOKEN_DECIMALS) / 1e9 if sol_index < popcat_index else out_amount / (in_amount / 10**TOKEN_DECIMALS) / 1e9
            return None
        except Exception as e:
            logging.error(f"Transaction parse error: {str(e)}")
            return None

    async def update_balances(self):
        while True:
            try:
                if (datetime.now() - self.last_balance_update).seconds >= 30:
                    sol_resp = await self.client.get_balance(self.keypair.pubkey())
                    current_sol = sol_resp.value / 1e9
                    
                    ata = get_associated_token_address(Pubkey.from_string(self.wallet), Pubkey.from_string(TARGET_MINT))
                    current_token = 0.0
                    try:
                        payload = {
                            "jsonrpc": "2.0",
                            "id": random.randint(1, 10000),
                            "method": "getTokenAccountBalance",
                            "params": [str(ata), {"commitment": "finalized"}]
                        }
                        async with self.session.post(self.client._provider.endpoint_uri, json=payload) as resp:
                            result = await resp.json()
                            current_token = int(result["result"]["value"]["amount"]) / 10**TOKEN_DECIMALS
                    except:
                        opts = TokenAccountOpts(mint=Pubkey.from_string(TARGET_MINT), encoding="base64")
                        resp = await self.client.get_token_accounts_by_owner(self.keypair.pubkey(), opts)
                        for account in resp.value:
                            data_str = account.account.data
                            if isinstance(data_str, list):
                                data_str = data_str[0]
                            
                            # Fix base64 padding
                            missing_padding = len(data_str) % 4
                            if missing_padding:
                                data_str += '=' * (4 - missing_padding)
                            
                            try:
                                account_data = base64.b64decode(data_str)
                                if len(account_data) >= 72:
                                    current_token += int.from_bytes(account_data[64:72], byteorder='little') / 10**TOKEN_DECIMALS
                            except Exception as e:
                                logging.error(f"Data decode error: {str(e)}")
                    
                    self.balance_cache = {'sol': current_sol, 'token': current_token}
                    self.last_balance_update = datetime.now()
                    logging.info(f"📊 Balance Update: {current_sol:.4f} SOL | {current_token:.2f} {TOKEN_SYMBOL}")
                    await self._log_metrics()
                
                await asyncio.sleep(60)
            except Exception as e:
                logging.error(f"Balance update failed: {str(e)}")
                await asyncio.sleep(60)

    async def get_balance(self, mint: str):
        if (datetime.now() - self.last_balance_update).seconds >= 30:
            await self.update_balances()
        return self.balance_cache['sol' if mint == SOL_MINT else 'token']

    async def create_ata_if_needed(self):
        try:
            ata = get_associated_token_address(Pubkey.from_string(self.wallet), Pubkey.from_string(TARGET_MINT))
            account_info = await self.client.get_account_info(ata)
            if not account_info.value:
                logging.info("🛠 Creating associated token account")
                ix = create_associated_token_account(
                    payer=self.keypair.pubkey(),
                    owner=self.keypair.pubkey(),
                    mint=Pubkey.from_string(TARGET_MINT)
                )
                await self.client.send_transaction(ix, self.keypair)
        except Exception as e:
            logging.error(f"ATA check failed: {str(e)}")

    async def _calculate_dynamic_slippage(self):
        short_atr = await self.calculate_atr(self.short_atr_period, self.short_atr_values)
        return min(1000, max(200, int(300 + (short_atr / self.current_price) * 150)))

    async def calculate_tr(self, prev, curr):
        tr1 = max(curr['high'] - curr['low'], MIN_PRICE_CHANGE * self.current_price)
        tr2 = abs(curr['high'] - prev['close'])
        tr3 = abs(curr['low'] - prev['close'])
        return max(tr1, tr2, tr3)

    async def calculate_atr(self, period, atr_values):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._sync_calculate_atr, period, atr_values)

    def _sync_calculate_atr(self, period, atr_values):
        if len(self.price_history) < period * 2:
            return MIN_ATR_VALUE
        
        if not atr_values:
            tr_sum = 0.0
            for i in range(period):
                prev = self.price_history[i]
                curr = self.price_history[i+1]
                tr = self._sync_calculate_tr(prev, curr)
                tr_sum += tr
            initial_atr = tr_sum / period
            atr_values.append(initial_atr)
            return initial_atr
        
        prev = self.price_history[-2]
        curr = self.price_history[-1]
        tr = self._sync_calculate_tr(prev, curr)
        new_atr = (atr_values[-1] * (period - 1) + tr) / period
        atr_values.append(new_atr)
        return max(new_atr, MIN_ATR_VALUE)

    def _sync_calculate_tr(self, prev, curr):
        tr1 = max(curr['high'] - curr['low'], MIN_PRICE_CHANGE * self.current_price)
        tr2 = abs(curr['high'] - prev['close'])
        tr3 = abs(curr['low'] - prev['close'])
        return max(tr1, tr2, tr3)

    async def get_dynamic_multiplier(self, short_atr, long_atr):
        volatility_ratio = short_atr / (self.current_price + 1e-8)
        base_multiplier = 2.0 + (volatility_ratio * 5)
        return min(max(base_multiplier, 1.5), 4.0)

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        start_time = datetime.now()
        for attempt in range(5):
            try:
                await self.create_ata_if_needed()
                blockhash_resp = await self.client.get_latest_blockhash()
                latest_blockhash = blockhash_resp.value
                
                current_balance = await self.get_balance(input_mint)
                if input_mint == SOL_MINT and current_balance < amount + self.MIN_BALANCE_BUFFER:
                    raise ValueError("Insufficient SOL balance")
                elif input_mint != SOL_MINT and current_balance < amount:
                    raise ValueError("Insufficient token balance")
                
                slippage_bps = await self._calculate_dynamic_slippage()
                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(amount * (1e9 if input_mint == SOL_MINT else 10**TOKEN_DECIMALS)),
                    "slippageBps": slippage_bps,
                    "swapMode": "ExactIn" if input_mint == SOL_MINT else "ExactOut"
                }
                async with self.session.get(JUPITER_QUOTE_API, params=params) as resp:
                    resp.raise_for_status()
                    quote = await resp.json()
                
                priority_fee = int(50000 * (1 + (self.short_atr_values[-1]/self.current_price if self.short_atr_values else 0)))
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "computeUnitPriceMicroLamports": priority_fee
                }
                async with self.session.post(JUPITER_SWAP_API, json=payload) as resp:
                    resp.raise_for_status()
                    swap_data = await resp.json()
                
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                signing_key = SigningKey(base58.b58decode(self.base58_private_key)[:32])
                signature = Signature(signing_key.sign(message_bytes).signature)
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])
                
                txid = await self.client.send_raw_transaction(
                    bytes(signed_tx),
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                )
                
                if await self.client.confirm_transaction(
                    txid.value,
                    latest_blockhash.blockhash,
                    sleep_seconds=0.5,
                    last_valid_block_height=latest_blockhash.last_valid_block_height
                ):
                    duration = (datetime.now() - start_time).total_seconds()
                    self.trade_duration.observe(duration)
                    logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value}")
                    self.health_monitor.check_health()
                    self.trade_count.labels(
                        direction="buy" if output_mint == TARGET_MINT else "sell"
                    ).inc()
                    self.trade_size_gauge.labels(
                        direction="buy" if output_mint == TARGET_MINT else "sell"
                    ).set(amount)
                    return True
                return False
                
            except Exception as e:
                logging.error(f"Trade error (attempt {attempt+1}): {str(e)}")
                self.api_errors.labels(endpoint="jupiter_swap").inc()
                self.health_monitor.record_error()
                await asyncio.sleep(1)
        return False

    async def trading_strategy(self):
        await self.fetch_initial_price()
        next_cycle = time.monotonic()
        
        while True:
            try:
                cycle_start = time.monotonic()
                logging.debug(f"Strategy cycle started at {cycle_start}")
                
                self.health_monitor.reset_daily_pnl()
                if self.health_monitor.daily_pnl < self.health_monitor.MAX_DAILY_LOSS:
                    logging.critical("🛑 Daily loss limit reached!")
                    await asyncio.sleep(600)
                    continue
                
                if await self.check_volatility_circuit_breaker():
                    await asyncio.sleep(300)
                    continue
                
                if len(self.price_history) >= 20:
                    last_update_age = (datetime.now() - self.price_history[-1]['timestamp']).total_seconds()
                    if last_update_age > 15:
                        logging.warning("♻️ Stale prices - resetting ATR")
                        self.short_atr_values.clear()
                        self.long_atr_values.clear()
                    
                    await self._process_market_data()
                    await self._execute_trading_logic()
                else:
                    logging.info(f"📈 Collecting data ({len(self.price_history)}/20)")
                
                next_cycle += 15
                now = time.monotonic()
                sleep_time = max(next_cycle - now, 0)
                logging.debug(f"Cycle completed in {now - cycle_start:.2f}s. Sleeping {sleep_time:.2f}s")
                await asyncio.sleep(sleep_time)
            except Exception as e:
                logging.error(f"Strategy loop error: {str(e)}", exc_info=True)
                await asyncio.sleep(60)

    async def check_volatility_circuit_breaker(self):
        if self.short_atr_values and (self.short_atr_values[-1]/self.current_price) > 0.15:
            logging.warning("⚡ High volatility - trading paused")
            return True
        return False

    async def _process_market_data(self):
        async with self.atr_lock:
            if not self.current_price:
                await self.fetch_initial_price()
            
            try:
                logging.debug(f"Price history length: {len(self.price_history)}")
                self.short_atr = await self.calculate_atr(self.short_atr_period, self.short_atr_values)
                self.long_atr = await self.calculate_atr(self.long_atr_period, self.long_atr_values)
                
                if not self.short_atr_values:
                    logging.warning("⚠️ ATR values not initialized - waiting for more data")
                    return
                
                logging.debug(f"Short ATR values: {list(self.short_atr_values)}")
                
                self.avg_price = sum(p['close'] for p in self.price_history) / len(self.price_history)
                self.multiplier = await self.get_dynamic_multiplier(self.short_atr, self.long_atr)
                dynamic_threshold = self.avg_price - (self.short_atr * self.multiplier)
                
                logging.info(
                    f"Strategy Check | Price: {self.current_price:.6f} vs Threshold: {dynamic_threshold:.6f} "
                    f"(Avg: {self.avg_price:.6f}, ATR: {self.short_atr:.6f})"
                )
                
                if self.current_price < dynamic_threshold:
                    logging.info("🚦 Buy signal ACTIVE")
                else:
                    logging.info("🚦 Buy signal INACTIVE")
                    
            except Exception as e:
                logging.error(f"Market data processing failed: {str(e)}", exc_info=True)

    async def _execute_trading_logic(self):
        sol_balance = await self.get_balance(SOL_MINT)
        token_balance = await self.get_balance(TARGET_MINT)
        
        if await self._should_buy(sol_balance):
            await self._execute_buy(sol_balance)
        
        if await self._should_sell(token_balance):
            await self._execute_sell(token_balance)

    async def _should_buy(self, sol_balance):
        return (
            (datetime.now() - self.last_trade).seconds >= self.cooldown and
            self.current_price < (self.avg_price - (self.short_atr * self.multiplier)) and
            (sol_balance - await self._calculate_position_size(sol_balance)) > MIN_SOL_BALANCE
        )

    async def _calculate_position_size(self, sol_balance):
        portfolio_value = sol_balance + (self.balance_cache['token'] * self.current_price)
        risk_amount = portfolio_value * self.risk_per_trade
        return max(
            min(
                risk_amount / max(self.short_atr, self.current_price * 0.005),
                portfolio_value * 0.25,
                sol_balance - self.MIN_BALANCE_BUFFER
            ),
            0.02  # Minimum 0.02 SOL per trade
        )

    async def _execute_buy(self, sol_balance):
        position_size = await self._calculate_position_size(sol_balance)
        if position_size >= 0.02:
            logging.info(f"🚀 Buying {TOKEN_SYMBOL} ({position_size:.4f} SOL)")
            if await self.execute_trade(SOL_MINT, TARGET_MINT, position_size):
                self._update_portfolio(position_size)

    def _update_portfolio(self, position_size):
        bought = position_size / self.current_price
        self.fifo_queue.append({'amount': bought, 'cost': position_size})
        self.total_invested_sol += position_size
        self.total_popcat_bought += bought
        self.last_trade = datetime.now()
        self.consecutive_buys += 1

    async def _should_sell(self, token_balance):
        return (
            token_balance > self.min_sell_amount and
            await self._meets_sell_criteria()
        )

    async def _meets_sell_criteria(self):
        target_price = self.avg_price * max(
            self.sell_threshold, 
            1 + (self.long_atr / self.avg_price) * (self.long_atr/self.short_atr)
        )
        return (
            self.current_price >= target_price or
            self.current_price < self.current_price * self.trailing_stop_pct or
            (datetime.now() - self.last_trade > self.max_trade_duration)
        )

    async def _execute_sell(self, token_balance):
        sell_amount = token_balance * self.sell_percentage
        safe_amount = min(sell_amount, await self.get_balance(TARGET_MINT) - self.MIN_TOKEN_BUFFER)
        
        if safe_amount > 0:
            realized_pnl = await self._calculate_realized_pnl(safe_amount)
            if realized_pnl >= self.min_profit_sol:
                logging.info(f"💰 Selling {safe_amount:.2f} {TOKEN_SYMBOL}")
                if await self.execute_trade(TARGET_MINT, SOL_MINT, safe_amount):
                    self._update_post_sell_state(safe_amount, realized_pnl)

    async def _calculate_realized_pnl(self, amount):
        remaining = amount
        cost_basis = 0.0
        
        while remaining > 0 and self.fifo_queue:
            oldest = self.fifo_queue[0]
            sell_qty = min(oldest['amount'], remaining)
            cost_basis += (sell_qty / oldest['amount']) * oldest['cost']
            remaining -= sell_qty
            
            if oldest['amount'] == sell_qty:
                self.fifo_queue.popleft()
            else:
                oldest['amount'] -= sell_qty
                oldest['cost'] -= (sell_qty / oldest['amount']) * oldest['cost']
        
        return (self.current_price * amount) - cost_basis

    def _update_post_sell_state(self, amount, pnl):
        self.total_popcat_bought -= amount
        self.total_invested_sol -= (pnl / self.current_price)
        self.realized_pnl.labels(currency='SOL').inc(pnl)
        self.health_monitor.daily_pnl += pnl
        self.last_trade = datetime.now()
        self.consecutive_buys = 0

    async def cleanup(self):
        if not self.session.closed:
            await self.session.close()
        await self.client.close()
        self.executor.shutdown(wait=False)

async def main():
    trader = TokenTrader()
    try:
        await asyncio.gather(
            trader.listen_for_price_updates(),
            trader.trading_strategy(),
            trader.update_balances(),
            trader.backup_price_poller()
        )
    finally:
        await trader.cleanup()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
        force=True
    )
    asyncio.run(main())
