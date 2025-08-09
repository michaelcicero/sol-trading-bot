#!/usr/bin/env python3
import os
import logging
import asyncio
import aiohttp
import base58
import base64
import random
import json
import websockets
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import deque
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
from spl.token.instructions import create_associated_token_account, get_associated_token_address
from prometheus_client import start_http_server, Gauge, Counter
from backtesting import Backtest, Strategy
from backtesting.lib import crossover

# Load environment variables first
load_dotenv()

# Configuration
SOL_MINT = "So11111111111111111111111111111111111111112"
TARGET_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
TOKEN_DECIMALS = 9
TOKEN_SYMBOL = "POPCAT"
MIN_ATR_VALUE = 0.00005
MIN_PRICE_CHANGE_PCT = 0.005  # 0.5% minimum volatility
MAX_REASONABLE_PRICE = 0.005
MIN_TOKEN_RECEIVED = 100_000_000
MIN_SOL_BALANCE = 0.05

# API endpoints
HELIUS_WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY')}"
JUPITER_API_BASE = "https://quote-api.jup.ag/v6"

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
        
    def reset_error_count(self):
        self.consecutive_errors = 0
        
    def reset_daily_pnl(self):
        if datetime.now().date() != self.last_daily_reset.date():
            self.daily_pnl = 0.0
            self.last_daily_reset = datetime.now()
            return True
        return False

class TokenTrader:
    def __init__(self):
        self._validate_environment()
        self._setup_solana_client()
        self._setup_state_management()
        self._setup_prometheus()
        
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=True, limit=50),
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            headers={'User-Agent': 'TradingBot/1.0'},
            raise_for_status=True
        )
        
        logging.info(f"🔑 Trading Wallet: {self.wallet}")
        logging.info(f"🔗 Helius Endpoint: {self.client._provider.endpoint_uri}")

    def _validate_environment(self):
        required_vars = ['PRIVATE_KEY', 'HELIUS_API_KEY']
        missing = [var for var in required_vars if not os.getenv(var)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
            
        self.base58_private_key = os.getenv("PRIVATE_KEY")
        if len(self.base58_private_key) < 40:
            raise ValueError("Invalid PRIVATE_KEY format - must be base58 encoded")

    def _setup_solana_client(self):
        try:
            self.keypair = Keypair.from_base58_string(self.base58_private_key)
            self.wallet = str(self.keypair.pubkey())
            self.client = AsyncClient(
                f"https://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY')}",
                timeout=30,
                commitment=Confirmed
            )
        except Exception as e:
            logging.critical(f"Failed to initialize Solana client: {e}")
            raise

    def _setup_state_management(self):
        self.price_history = deque(maxlen=100)
        self.short_atr_values = deque(maxlen=14)
        self.long_atr_values = deque(maxlen=28)
        self.current_price = None
        self.balance_cache = {'sol': 0.0, 'token': 0.0}
        self.last_balance_update = datetime.min
        self.MIN_BALANCE_BUFFER = 0.005
        self.MIN_TOKEN_BUFFER = 0.0001
        self.short_atr_period = 14
        self.long_atr_period = 28
        self.atr_multiplier = 3.0
        self.cooldown = 60
        self.min_sell_amount = 30
        self.sell_percentage = 0.90
        self.sell_threshold = 1.005
        self.trailing_stop_pct = 0.985
        self.min_profit_sol = 0.005
        self.max_trade_duration = timedelta(minutes=30)
        self.risk_per_trade = 0.03
        self.fifo_queue = deque()
        self.total_invested_sol = 0.0
        self.total_popcat_bought = 0.0
        self.last_trade = datetime.min
        self.consecutive_buys = 0
        self.max_consecutive_buys = 3
        self.price_update_event = asyncio.Event()
        self.last_ws_update = datetime.now()
        self.price_lock = asyncio.Lock()
        self.atr_lock = asyncio.Lock()
        self.balance_lock = asyncio.Lock()
        self.health_monitor = HealthMonitor()

    def _setup_prometheus(self):
        self.sol_balance_gauge = Gauge('sol_balance', 'Current SOL balance')
        self.popcat_balance_gauge = Gauge('popcat_balance', 'Current POPCAT balance')
        self.popcat_price_gauge = Gauge('popcat_price', 'Current POPCAT price')
        self.short_atr_gauge = Gauge('short_atr', 'Short-term ATR value')
        self.long_atr_gauge = Gauge('long_atr', 'Long-term ATR value')
        self.trade_count = Counter('trade_count_total', 'Total number of trades', ['direction'])
        self.realized_pnl = Counter('realized_pnl_total', 'Total realized PnL in SOL')
        self.unrealized_pnl = Gauge('unrealized_pnl', 'Current unrealized PnL')
        self.api_errors = Counter('api_errors_total', 'API errors encountered', ['endpoint'])
        self.websocket_reconnects = Counter('websocket_reconnects_total', 'WebSocket reconnection attempts')
        start_http_server(8000)

    async def update_metrics(self):
        self.sol_balance_gauge.set(self.balance_cache['sol'])
        self.popcat_balance_gauge.set(self.balance_cache['token'])
        if self.current_price:
            self.popcat_price_gauge.set(self.current_price)
        if self.short_atr_values:
            self.short_atr_gauge.set(self.short_atr_values[-1])
        if self.long_atr_values:
            self.long_atr_gauge.set(self.long_atr_values[-1])
        if self.total_popcat_bought > 0 and self.current_price:
            unrealized = (self.current_price * self.total_popcat_bought) - self.total_invested_sol
            self.unrealized_pnl.set(unrealized)

    async def listen_for_price_updates(self):
        reconnect_delay = 1
        max_reconnect_delay = 60
        while True:
            try:
                async with websockets.connect(
                    HELIUS_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    extra_headers={"User-Agent": "Mozilla/5.0"}
                ) as ws:
                    logging.info("✅ WebSocket connected")
                    reconnect_delay = 1
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "transactionSubscribe",
                        "params": [
                            {"mentions": [f"token:{TARGET_MINT}"]},
                            {"commitment": "confirmed", "encoding": "jsonParsed"}
                        ]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    await ws.recv()
                    heartbeat_task = asyncio.create_task(self._websocket_heartbeat(ws))
                    try:
                        async for message in ws:
                            await self._process_websocket_message(message)
                    finally:
                        heartbeat_task.cancel()
                        try: await heartbeat_task
                        except asyncio.CancelledError: pass
            except (websockets.exceptions.ConnectionClosed, 
                    websockets.exceptions.WebSocketException,
                    asyncio.TimeoutError) as e:
                self.websocket_reconnects.inc()
                jitter = random.uniform(0.8, 1.2)
                delay = reconnect_delay * jitter
                logging.warning(f"WebSocket disconnected: {e}. Reconnecting in {delay:.1f}s")
                await asyncio.sleep(delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
            except Exception as e:
                logging.error(f"Unhandled WebSocket error: {e}")
                await asyncio.sleep(5)

    async def _websocket_heartbeat(self, ws):
        while True:
            try:
                await asyncio.sleep(30)
                if (datetime.now() - self.last_ws_update).total_seconds() > 60:
                    logging.info("Sending websocket ping...")
                    pong_waiter = await ws.ping()
                    try: await asyncio.wait_for(pong_waiter, timeout=10)
                    except asyncio.TimeoutError:
                        logging.warning("WebSocket ping timeout, forcing reconnection")
                        await ws.close(code=1001, reason="Ping timeout")
                        break
            except asyncio.CancelledError: break
            except Exception as e:
                logging.error(f"Heartbeat error: {e}")
                await asyncio.sleep(5)

    async def _process_websocket_message(self, message):
        try:
            data = json.loads(message)
            if data.get('method') != 'transactionNotification': return
            result = data.get('params', {}).get('result')
            if not result: return
            price = await self._extract_price_from_transaction(result)
            if price and 1e-8 < price < MAX_REASONABLE_PRICE:
                await self._update_price_data(price, 'websocket')
                self.last_ws_update = datetime.now()
                self.price_update_event.set()
        except json.JSONDecodeError:
            logging.warning(f"Invalid JSON in websocket message")
        except Exception as e:
            logging.error(f"Error processing websocket message: {e}")

    async def _extract_price_from_transaction(self, transaction_data):
        try:
            message = transaction_data['transaction']['message']
            account_keys = message['accountKeys']
            for ix in message['instructions']:
                if ix['programId'] in ['srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX', 
                                      '9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP']:
                    data = base64.b64decode(ix['data'])
                    if len(data) >= 17 and data[0] in [9, 1]:
                        in_amount = int.from_bytes(data[1:9], 'little')
                        out_amount = int.from_bytes(data[9:17], 'little')
                        if in_amount == 0 or out_amount == 0: continue
                        try:
                            sol_index = account_keys.index(SOL_MINT)
                            popcat_index = next(i for i, key in enumerate(account_keys) 
                                               if key == TARGET_MINT)
                            if sol_index < popcat_index:
                                return in_amount / (out_amount / 10**TOKEN_DECIMALS) / 1e9
                            else:
                                return out_amount / (in_amount / 10**TOKEN_DECIMALS) / 1e9
                        except (ValueError, IndexError):
                            continue
            return None
        except Exception as e:
            logging.error(f"Error extracting price: {e}")
            return None

    async def backup_price_poller(self):
        while True:
            try:
                if (datetime.now() - self.last_ws_update).total_seconds() > 60:
                    price = await self._get_jupiter_price()
                    if price: await self._update_price_data(price, 'api_poll')
                await asyncio.sleep(30)
            except asyncio.CancelledError: break
            except Exception as e:
                logging.error(f"Price polling error: {e}")
                await asyncio.sleep(30)

    async def _get_jupiter_price(self):
        try:
            async with self.session.get(
                f"{JUPITER_API_BASE}/quote",
                params={
                    "inputMint": SOL_MINT,
                    "outputMint": TARGET_MINT,
                    "amount": int(0.01 * 1e9),
                    "slippageBps": 1000
                }
            ) as response:
                data = await response.json()
                raw_token_amount = float(data.get("outAmount", 0))
                if raw_token_amount <= 0: return None
                token_amount = raw_token_amount / (10 ** TOKEN_DECIMALS)
                price = 0.01 / token_amount
                if price > MAX_REASONABLE_PRICE or price < 1e-8: return None
                return price
        except aiohttp.ClientError as e:
            self.api_errors.labels(endpoint="jupiter_price").inc()
            logging.warning(f"Jupiter price API error: {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error fetching price: {e}")
            return None

    async def _update_price_data(self, price, source):
        async with self.price_lock:
            logging.info(f"Updating price data: price={price}, source={source}")
            if price <= 0 or price > MAX_REASONABLE_PRICE: return
            if self.current_price and abs(price - self.current_price)/self.current_price < 0.0005: return
            
            high = price
            low = price
            if self.price_history:
                prev_close = self.price_history[-1]['close']
                high = max(price, prev_close * 1.001)
                low = min(price, prev_close * 0.999)
            
            self.current_price = price
            self.price_history.append({
                'high': high,
                'low': low,
                'close': price,
                'timestamp': datetime.now()
            })
            
            if len(self.price_history) >= 2:
                prev_price = self.price_history[-2]['close']
                change_pct = ((price - prev_price) / prev_price) * 100
                if abs(change_pct) > 3:
                    logging.info(f"📊 Significant price change: {prev_price:.6f} -> {price:.6f} ({change_pct:+.2f}%)")
            
            logging.debug(f"Price update ({source}): {price:.6f} SOL")
            self.popcat_price_gauge.set(price)
            await self.update_metrics()

    async def update_balances(self):
        while True:
            try:
                async with self.balance_lock:
                    sol_resp = await self.client.get_balance(self.keypair.pubkey())
                    sol_balance = sol_resp.value / 1e9
                    token_balance = await self._get_token_balance()
                    self.balance_cache = {'sol': sol_balance, 'token': token_balance}
                    self.last_balance_update = datetime.now()
                    logging.info(f"📊 Balance: {sol_balance:.4f} SOL | {token_balance:.2f} {TOKEN_SYMBOL}")
                    await self.update_metrics()
                await asyncio.sleep(60)
            except asyncio.CancelledError: break
            except Exception as e:
                self.health_monitor.record_error()
                logging.error(f"Balance update failed: {e}")
                await asyncio.sleep(30)

    async def _get_token_balance(self):
        try:
            ata = get_associated_token_address(Pubkey.from_string(self.wallet), Pubkey.from_string(TARGET_MINT))
            payload = {
                "jsonrpc": "2.0", 
                "id": random.randint(1, 10000),
                "method": "getTokenAccountBalance",
                "params": [str(ata), {"commitment": "confirmed"}]
            }
            async with self.session.post(self.client._provider.endpoint_uri, json=payload) as resp:
                result = await resp.json()
                if "result" in result and "value" in result["result"]:
                    return int(result["result"]["value"]["amount"]) / 10**TOKEN_DECIMALS
        except Exception as e:
            logging.debug(f"Direct token balance check failed: {e}")
        
        try:
            opts = TokenAccountOpts(mint=Pubkey.from_string(TARGET_MINT), encoding="jsonParsed")
            resp = await self.client.get_token_accounts_by_owner(self.keypair.pubkey(), opts)
            total_balance = 0.0
            for account in resp.value:
                try:
                    info = account.account.data.parsed["info"]
                    if "tokenAmount" in info:
                        total_balance += int(info["tokenAmount"]["amount"]) / 10**TOKEN_DECIMALS
                except Exception: continue
            return total_balance
        except Exception as e:
            logging.warning(f"Token accounts fallback failed: {e}")
            return 0.0

    async def get_balance(self, mint: str):
        if (datetime.now() - self.last_balance_update).total_seconds() > 60:
            async with self.balance_lock:
                try:
                    if mint == SOL_MINT:
                        resp = await self.client.get_balance(self.keypair.pubkey())
                        self.balance_cache['sol'] = resp.value / 1e9
                    else:
                        self.balance_cache['token'] = await self._get_token_balance()
                    self.last_balance_update = datetime.now()
                except Exception as e:
                    logging.error(f"Balance update failed: {e}")
        return self.balance_cache['sol' if mint == SOL_MINT else 'token']

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        start_time = time.time()
        if amount <= 0:
            logging.error("Invalid trade amount")
            return False
            
        for attempt in range(3):
            try:
                await self._ensure_associated_token_account()
                blockhash_resp = await asyncio.wait_for(
                    self.client.get_latest_blockhash(commitment=Confirmed),
                    timeout=10
                )
                latest_blockhash = blockhash_resp.value
                
                current_balance = await self.get_balance(input_mint)
                if input_mint == SOL_MINT and current_balance < amount + self.MIN_BALANCE_BUFFER:
                    raise ValueError(f"Insufficient SOL balance: {current_balance} (needed {amount + self.MIN_BALANCE_BUFFER})")
                elif input_mint != SOL_MINT and current_balance < amount:
                    raise ValueError(f"Insufficient token balance: {current_balance} (needed {amount})")
                    
                slippage_bps = await self._calculate_dynamic_slippage()
                
                async with self.session.get(
                    f"{JUPITER_API_BASE}/quote",
                    params={
                        "inputMint": input_mint,
                        "outputMint": output_mint,
                        "amount": int(amount * (1e9 if input_mint == SOL_MINT else 10**TOKEN_DECIMALS)),
                        "slippageBps": slippage_bps,
                        "swapMode": "ExactIn" if input_mint == SOL_MINT else "ExactOut"
                    },
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    quote = await resp.json()
                
                priority_fee = 50000
                if self.short_atr_values and self.current_price:
                    priority_fee = int(50000 * (1 + (self.short_atr_values[-1]/self.current_price)))
                    
                async with self.session.post(
                    f"{JUPITER_API_BASE}/swap",
                    json={
                        "quoteResponse": quote,
                        "userPublicKey": self.wallet,
                        "wrapUnwrapSOL": True,
                        "computeUnitPriceMicroLamports": priority_fee
                    },
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    swap_data = await resp.json()
                    
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                signing_key = SigningKey(base58.b58decode(self.base58_private_key)[:32])
                signature = Signature(signing_key.sign(message_bytes).signature)
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])
                
                txid = await asyncio.wait_for(
                    self.client.send_raw_transaction(
                        bytes(signed_tx),
                        opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                    ),
                    timeout=15
                )
                
                confirmed = await asyncio.wait_for(
                    self.client.confirm_transaction(
                        txid.value,
                        latest_blockhash.blockhash,
                        last_valid_block_height=latest_blockhash.last_valid_block_height
                    ),
                    timeout=30
                )
                
                if not confirmed:
                    logging.error("❌ Transaction confirmation failed")
                    await asyncio.sleep(1)
                    continue
                    
                duration = time.time() - start_time
                logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value} ({duration:.2f}s)")
                direction = "buy" if output_mint == TARGET_MINT else "sell"
                self.trade_count.labels(direction=direction).inc()
                self.health_monitor.reset_error_count()
                await self.health_monitor.check_health()
                self.last_balance_update = datetime.min
                return True
                
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                delay = (attempt + 1) * 1.5
                logging.error(f"Network error (retry {attempt+1}/{3}): {e} - waiting {delay}s")
                self.api_errors.labels(endpoint="jupiter_swap").inc()
                await asyncio.sleep(delay)
            except Exception as e:
                logging.error(f"Trade execution error: {e}")
                self.api_errors.labels(endpoint="execution").inc()
                self.health_monitor.record_error()
                await asyncio.sleep(1)
                
        logging.error(f"❌ Trade failed after all attempts")
        return False
        
    async def _ensure_associated_token_account(self):
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
                await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"ATA check failed: {e}")
            raise

    async def trading_strategy(self):
        initial_price = await self._get_jupiter_price()
        if initial_price: await self._update_price_data(initial_price, 'startup')
        
        min_history_required = max(self.short_atr_period, self.long_atr_period) * 2
        while len(self.price_history) < min_history_required:
            logging.info(f"📈 Collecting price data ({len(self.price_history)}/{min_history_required})")
            try:
                await asyncio.wait_for(self.price_update_event.wait(), timeout=10)
                self.price_update_event.clear()
            except asyncio.TimeoutError:
                price = await self._get_jupiter_price()
                if price: await self._update_price_data(price, 'startup_poll')
            await asyncio.sleep(5)
        
        logging.info("✅ Price history collected, starting trading strategy")
        await self._process_market_data()  # Force initial ATR calculation
        
        while True:
            try:
                if self.health_monitor.reset_daily_pnl():
                    logging.info("🔄 Daily metrics reset")
                
                if self.health_monitor.daily_pnl < self.health_monitor.MAX_DAILY_LOSS:
                    logging.critical("🛑 Daily loss limit reached!")
                    await asyncio.sleep(600)
                    continue
                
                next_evaluation = self.last_trade + timedelta(seconds=self.cooldown)
                if datetime.now() < next_evaluation:
                    wait_seconds = (next_evaluation - datetime.now()).total_seconds()
                    await asyncio.sleep(min(wait_seconds, 5))  # Reduced cooldown check
                    continue
                
                await self._process_market_data()
                await self._execute_trading_logic()
                await asyncio.sleep(5)  # Increased frequency
                
            except asyncio.CancelledError: break
            except Exception as e:
                logging.error(f"Trading strategy error: {e}")
                self.health_monitor.record_error()
                await asyncio.sleep(60)

    async def _process_market_data(self):
        async with self.atr_lock:
            logging.info("🔄 Processing market data...")
            try:
                self.short_atr = await self.calculate_atr(self.short_atr_period, self.short_atr_values)
                self.long_atr = await self.calculate_atr(self.long_atr_period, self.long_atr_values)
                self.sma20 = await self.calculate_sma(20)
                self.sma50 = await self.calculate_sma(50)
                self.avg_price = sum(p['close'] for p in self.price_history) / len(self.price_history)
                volatility_ratio = self.short_atr / (self.current_price + 1e-8)
                self.multiplier = min(max(self.atr_multiplier * (1 + volatility_ratio), 2.0), 4.0)
                self.dynamic_threshold = self.avg_price - (self.short_atr * self.multiplier)
                logging.info(
                    f"📈 Strategy Update || Price: {self.current_price:.6f} | "
                    f"Threshold: {self.dynamic_threshold:.6f} | "
                    f"ATR: {self.short_atr:.6f} | "
                    f"Multiplier: {self.multiplier:.2f}"
                )
            except Exception as e:
                logging.error(f"Market data processing error: {e}")
                raise

    async def _execute_trading_logic(self):
        sol_balance = await self.get_balance(SOL_MINT)
        token_balance = await self.get_balance(TARGET_MINT)
        
        if await self._should_buy(sol_balance):
            position_size = await self._calculate_position_size(sol_balance)
            logging.info(f"🚀 Buy signal: {position_size:.4f} SOL at {self.current_price:.6f}")
            if await self.execute_trade(SOL_MINT, TARGET_MINT, position_size):
                self._update_after_buy(position_size)
        
        if await self._should_sell(token_balance):
            sell_amount = token_balance * self.sell_percentage
            logging.info(f"💰 Sell signal: {sell_amount:.2f} tokens at {self.current_price:.6f}")
            realized_pnl = await self._calculate_realized_pnl(sell_amount)
            if await self.execute_trade(TARGET_MINT, SOL_MINT, sell_amount):
                self._update_after_sell(sell_amount, realized_pnl)

    async def _should_buy(self, sol_balance):
        position_size = await self._calculate_position_size(sol_balance)
        return (
            self.current_price < self.dynamic_threshold and 
            position_size >= 0.02 and
            sol_balance - position_size > MIN_SOL_BALANCE and
            self.consecutive_buys < self.max_consecutive_buys and
            (self.sma20 is None or self.current_price > self.sma20 * 0.98)
        )

    async def _should_sell(self, token_balance):
        if token_balance < self.min_sell_amount: return False
        target_price = self.avg_price * max(
            self.sell_threshold,
            1 + (self.long_atr / self.current_price)
        )
        trailing_stop_hit = False
        if hasattr(self, 'trailing_peak') and self.trailing_peak > 0:
            trailing_stop_hit = self.current_price < self.trailing_peak * self.trailing_stop_pct
        time_exit = hasattr(self, 'last_buy_time') and (
            datetime.now() - self.last_buy_time > self.max_trade_duration
        )
        return (
            self.current_price >= target_price or
            trailing_stop_hit or
            time_exit
        )

    def _update_after_buy(self, position_size):
        bought_amount = position_size / self.current_price if self.current_price > 0 else 0
        self.fifo_queue.append({
            'amount': bought_amount,
            'cost': position_size,
            'price': self.current_price,
            'time': datetime.now()
        })
        self.total_invested_sol += position_size
        self.total_popcat_bought += bought_amount
        self.last_trade = datetime.now()
        self.last_buy_time = datetime.now()
        self.trailing_peak = self.current_price
        self.consecutive_buys += 1
        self.last_balance_update = datetime.min

    def _update_after_sell(self, sell_amount, realized_pnl):
        self.realized_pnl.inc(realized_pnl)
        self.health_monitor.daily_pnl += realized_pnl
        self.total_popcat_bought -= sell_amount
        self.total_invested_sol -= (self.current_price * sell_amount - realized_pnl)
        self.last_trade = datetime.now()
        self.last_sell_time = datetime.now()
        self.consecutive_buys = 0
        self.trailing_peak = 0
        self.last_balance_update = datetime.min

    async def calculate_tr(self, prev, curr):
        min_volatility = max(MIN_ATR_VALUE, curr['close'] * MIN_PRICE_CHANGE_PCT)
        tr1 = max(curr['high'] - curr['low'], min_volatility)
        tr2 = abs(curr['high'] - prev['close'])
        tr3 = abs(curr['low'] - prev['close'])
        logging.debug(f"TR Calculation: {tr1:.6f}, MinVol: {min_volatility:.6f}")
        return max(tr1, tr2, tr3)

    async def calculate_atr(self, period, atr_values):
        async with self.atr_lock:
            logging.info(f"🔧 ATR Calculation || Period: {period} | History: {len(self.price_history)}")
            
            if len(self.price_history) < period + 1:
                logging.warning("⚠️ Insufficient history for ATR calculation")
                return MIN_ATR_VALUE
            
            if not atr_values:
                tr_values = []
                for i in range(min(period, len(self.price_history)-1)):
                    prev_idx = len(self.price_history) - 2 - i
                    curr_idx = len(self.price_history) - 1 - i
                    if prev_idx < 0: break
                    prev = self.price_history[prev_idx]
                    curr = self.price_history[curr_idx]
                    tr = await self.calculate_tr(prev, curr)
                    tr_values.append(tr)
                
                if not tr_values:
                    return MIN_ATR_VALUE
                
                initial_atr = sum(tr_values) / len(tr_values)
                capped_initial = max(initial_atr, MIN_ATR_VALUE)
                atr_values.append(capped_initial)
                logging.info(f"🎯 Initial ATR Set || Value: {capped_initial:.6f}")
                return capped_initial
            
            if len(self.price_history) < 2:
                return atr_values[-1]
                
            prev = self.price_history[-2]
            curr = self.price_history[-1]
            tr = await self.calculate_tr(prev, curr)
            
            new_atr = (atr_values[-1] * (period - 1) + tr) / period
            capped_atr = max(new_atr, MIN_ATR_VALUE)
            atr_values.append(capped_atr)
            
            logging.info(f"🔄 ATR Updated || New: {capped_atr:.6f} | Raw: {new_atr:.6f} | TR: {tr:.6f}")
            return capped_atr

    async def calculate_sma(self, period):
        if len(self.price_history) < period: return None
        closes = [p['close'] for p in list(self.price_history)[-period:]]
        return sum(closes) / len(closes)

    async def _calculate_dynamic_slippage(self):
        if not self.short_atr_values or not self.current_price: return 500
        volatility = self.short_atr_values[-1] / self.current_price
        return min(int(volatility * 20000), 2000)

    async def _calculate_position_size(self, available_balance):
        risk_amount = available_balance * self.risk_per_trade
        if not self.short_atr_values: return min(risk_amount * 2, available_balance - self.MIN_BALANCE_BUFFER)
        position_size = risk_amount / (self.short_atr_values[-1] / self.current_price)
        return min(position_size, available_balance - self.MIN_BALANCE_BUFFER)

    async def _calculate_realized_pnl(self, sell_amount):
        if not self.fifo_queue: return 0.0
        realized = 0.0
        remaining = sell_amount
        while remaining > 0 and self.fifo_queue:
            oldest = self.fifo_queue[0]
            if oldest['amount'] <= remaining:
                realized += (self.current_price - oldest['price']) * oldest['amount']
                remaining -= oldest['amount']
                self.fifo_queue.popleft()
            else:
                realized += (self.current_price - oldest['price']) * remaining
                oldest['amount'] -= remaining
                remaining = 0
        return realized

    async def cleanup(self):
        try:
            if hasattr(self, 'session') and self.session and not self.session.closed:
                await self.session.close()
            if hasattr(self, 'client'):
                await self.client.close()
            logging.info("Resources cleaned up successfully")
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler("trading_bot.log"), logging.StreamHandler()]
    )
    
    trader = None
    try:
        trader = TokenTrader()
        tasks = [
            asyncio.create_task(trader.listen_for_price_updates(), name="websocket_listener"),
            asyncio.create_task(trader.update_balances(), name="balance_updater"),
            asyncio.create_task(trader.backup_price_poller(), name="price_poller"),
            asyncio.create_task(trader.trading_strategy(), name="trading_strategy")
        ]
        
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if task.exception(): logging.critical(f"Task {task.get_name()} failed: {task.exception()}")
        for task in pending: task.cancel()
        if pending: await asyncio.wait(pending, timeout=5)
    
    except KeyboardInterrupt: logging.info("Shutting down gracefully...")
    except Exception as e: logging.critical(f"Critical error: {e}")
    finally:
        if trader: await trader.cleanup()
        logging.info("Bot shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())
