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

load_dotenv()

SOL_MINT = "So11111111111111111111111111111111111111112"
TARGET_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
TOKEN_DECIMALS = 9
TOKEN_SYMBOL = "POPCAT"
RSI_PERIOD = 14
RSI_OVERBOUGHT = 59
RSI_OVERSOLD = 30
MAX_REASONABLE_PRICE = 0.1
MIN_SOL_BALANCE = 0.2
MIN_TOKEN_RECEIVED = 100_000_000
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

class RSITrader:
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
        self.current_price = None
        self.balance_cache = {'sol': 0.0, 'token': 0.0}
        self.last_balance_update = datetime.min
        self.rsi = None
        self.cooldown = 60
        self.min_sell_amount = 30
        self.sell_percentage = 0.90
        self.risk_per_trade = 0.3
        self.fifo_queue = deque()
        self.total_invested_sol = 0.0
        self.total_popcat_bought = 0.0
        self.last_trade = datetime.min
        self.price_update_event = asyncio.Event()
        self.last_ws_update = datetime.now()
        self.price_lock = asyncio.Lock()
        self.balance_lock = asyncio.Lock()
        self.health_monitor = HealthMonitor()

    def _setup_prometheus(self):
        self.sol_balance_gauge = Gauge('sol_balance', 'Current SOL balance')
        self.popcat_balance_gauge = Gauge('popcat_balance', 'Current POPCAT balance')
        self.popcat_price_gauge = Gauge('popcat_price', 'Current POPCAT price')
        self.rsi_gauge = Gauge('rsi', 'Current RSI value')
        self.trade_count = Counter('trade_count_total', 'Total number of trades', ['direction'])
        self.realized_pnl = Counter('realized_pnl_total', 'Total realized PnL in SOL')
        self.unrealized_pnl = Gauge('unrealized_pnl', 'Current unrealized PnL')
        start_http_server(8000)

    async def update_metrics(self):
        self.sol_balance_gauge.set(self.balance_cache['sol'])
        self.popcat_balance_gauge.set(self.balance_cache['token'])
        if self.current_price:
            self.popcat_price_gauge.set(self.current_price)
        if self.rsi is not None:
            self.rsi_gauge.set(self.rsi)
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
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass
            except (websockets.exceptions.ConnectionClosed, 
                    websockets.exceptions.WebSocketException,
                    asyncio.TimeoutError) as e:
                logging.warning(f"WebSocket disconnected: {e}. Reconnecting...")
                await asyncio.sleep(reconnect_delay)
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
                    await asyncio.wait_for(pong_waiter, timeout=10)
            except asyncio.TimeoutError:
                logging.warning("WebSocket ping timeout, forcing reconnection")
                await ws.close(code=1001, reason="Ping timeout")
                break
            except Exception as e:
                logging.error(f"Heartbeat error: {e}")
                await asyncio.sleep(5)

    async def _process_websocket_message(self, message):
        try:
            data = json.loads(message)
            if data.get('method') != 'transactionNotification':
                return
            result = data.get('params', {}).get('result')
            if not result:
                return
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
                        if in_amount == 0 or out_amount == 0:
                            continue
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
                    if price:
                        await self._update_price_data(price, 'api_poll')
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
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
                if raw_token_amount <= 0:
                    return None
                token_amount = raw_token_amount / (10 ** TOKEN_DECIMALS)
                price = 0.01 / token_amount
                if price > MAX_REASONABLE_PRICE or price < 1e-8:
                    return None
                return price
        except aiohttp.ClientError as e:
            logging.warning(f"Jupiter price API error: {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error fetching price: {e}")
            return None

    async def _update_price_data(self, price, source):
        async with self.price_lock:
            if self.current_price is None:
                self.current_price = price
                logging.info(f"💰 Initial price set: {price:.6f}")
            if price <= 0 or price > MAX_REASONABLE_PRICE:
                return
            if self.current_price and abs(price - self.current_price)/self.current_price < 0.0005:
                return
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
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Balance update failed: {e}")
                await asyncio.sleep(30)

    async def _get_token_balance(self):
        try:
            ata = get_associated_token_address(
                Pubkey.from_string(self.wallet),
                Pubkey.from_string(TARGET_MINT)
            )
            payload = {
                "jsonrpc": "2.0", 
                "id": random.randint(1, 10000),
                "method": "getTokenAccountBalance",
                "params": [str(ata), {"commitment": "confirmed"}]
            }
            async with self.session.post(
                self.client._provider.endpoint_uri,
                json=payload
            ) as resp:
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
                except Exception:
                    continue
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
            
        current_balance = await self.get_balance(input_mint)
        required = amount + (MIN_SOL_BALANCE if input_mint == SOL_MINT else 0)
        if current_balance < required:
            logging.error(f"🚨 Insufficient funds: {current_balance:.4f} < {required:.4f}")
            return False

        for attempt in range(3):
            try:
                await self._ensure_associated_token_account()
                blockhash_resp = await asyncio.wait_for(
                    self.client.get_latest_blockhash(commitment=Confirmed),
                    timeout=10
                )
                latest_blockhash = blockhash_resp.value
                
                async with self.session.get(
                    f"{JUPITER_API_BASE}/quote",
                    params={
                        "inputMint": input_mint,
                        "outputMint": output_mint,
                        "amount": int(amount * (1e9 if input_mint == SOL_MINT else 10**TOKEN_DECIMALS)),
                        "slippageBps": 1000,
                        "swapMode": "ExactIn" if input_mint == SOL_MINT else "ExactOut"
                    },
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    quote = await resp.json()
                
                async with self.session.post(
                    f"{JUPITER_API_BASE}/swap",
                    json={
                        "quoteResponse": quote,
                        "userPublicKey": self.wallet,
                        "wrapUnwrapSOL": True,
                        "computeUnitPriceMicroLamports": 50000
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
                
                if output_mint == TARGET_MINT:
                    bought_amount = amount / self.current_price
                    self.fifo_queue.append({
                        'amount': bought_amount,
                        'cost': amount,
                        'price': self.current_price,
                        'time': datetime.now()
                    })
                    self.total_invested_sol += amount
                    self.total_popcat_bought += bought_amount
                return True
                
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                delay = (attempt + 1) * 1.5
                logging.error(f"Network error (retry {attempt+1}/3): {e} - waiting {delay}s")
                await asyncio.sleep(delay)
            except Exception as e:
                logging.error(f"Trade execution error: {e}")
                self.health_monitor.record_error()
                await asyncio.sleep(1)
                
        logging.error(f"❌ Trade failed after all attempts")
        return False
        
    async def _ensure_associated_token_account(self):
        try:
            ata = get_associated_token_address(
                Pubkey.from_string(self.wallet),
                Pubkey.from_string(TARGET_MINT)
            )
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

    def calculate_rsi(self):
        closes = [p['close'] for p in self.price_history]
        if len(closes) < RSI_PERIOD + 1:
            return None
            
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[:RSI_PERIOD])
        avg_loss = np.mean(losses[:RSI_PERIOD])
        
        for i in range(RSI_PERIOD, len(deltas)):
            avg_gain = (avg_gain * (RSI_PERIOD - 1) + gains[i]) / RSI_PERIOD
            avg_loss = (avg_loss * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD
        
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    async def trading_strategy(self):
        initial_price = await self._get_jupiter_price()
        if initial_price:
            await self._update_price_data(initial_price, 'startup')
        
        while len(self.price_history) < RSI_PERIOD + 1:
            logging.info(f"📈 Collecting price data ({len(self.price_history)}/{RSI_PERIOD + 1})")
            try:
                await asyncio.wait_for(self.price_update_event.wait(), timeout=10)
                self.price_update_event.clear()
            except asyncio.TimeoutError:
                price = await self._get_jupiter_price()
                if price:
                    await self._update_price_data(price, 'startup_poll')
            await asyncio.sleep(5)
        
        logging.info("✅ Price history collected, starting trading strategy")
        
        while True:
            try:
                if self.health_monitor.reset_daily_pnl():
                    logging.info("🔄 Daily metrics reset")
                
                if self.health_monitor.daily_pnl < self.health_monitor.MAX_DAILY_LOSS:
                    logging.critical("🛑 Daily loss limit reached!")
                    await asyncio.sleep(600)
                    continue
                
                self.rsi = self.calculate_rsi()
                logging.info(f"📊 RSI: {self.rsi:.2f}")
                
                sol_balance = await self.get_balance(SOL_MINT)
                token_balance = await self.get_balance(TARGET_MINT)
                
                if self.rsi < RSI_OVERSOLD and sol_balance > MIN_SOL_BALANCE:
                    position_size = sol_balance * self.risk_per_trade
                    logging.info(f"🚀 Buy signal: {position_size:.4f} SOL at {self.current_price:.6f}")
                    if await self.execute_trade(SOL_MINT, TARGET_MINT, position_size):
                        pass
                
                elif self.rsi > RSI_OVERBOUGHT and token_balance >= self.min_sell_amount:
                    sell_amount = token_balance * self.sell_percentage
                    logging.info(f"💰 Sell signal: {sell_amount:.2f} tokens at {self.current_price:.6f}")
                    realized_pnl = await self._calculate_realized_pnl(sell_amount)
                    if await self.execute_trade(TARGET_MINT, SOL_MINT, sell_amount):
                        self.realized_pnl.inc(realized_pnl)
                        self.health_monitor.daily_pnl += realized_pnl
                        self.total_popcat_bought -= sell_amount
                        self.total_invested_sol -= (self.current_price * sell_amount - realized_pnl)
                
                await asyncio.sleep(15)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Trading strategy error: {e}")
                self.health_monitor.record_error()
                await asyncio.sleep(60)

    async def _calculate_realized_pnl(self, sell_amount):
        if not self.fifo_queue:
            return 0.0
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
        handlers=[
            logging.FileHandler("trading_bot.log"),
            logging.StreamHandler()
        ]
    )
    
    trader = None
    try:
        trader = RSITrader()
        tasks = [
            asyncio.create_task(trader.listen_for_price_updates(), name="websocket_listener"),
            asyncio.create_task(trader.update_balances(), name="balance_updater"),
            asyncio.create_task(trader.backup_price_poller(), name="price_poller"),
            asyncio.create_task(trader.trading_strategy(), name="trading_strategy")
        ]
        
        done, pending = await asyncio.wait(
            tasks, 
            return_when=asyncio.FIRST_EXCEPTION
        )
        
        for task in done:
            if task.exception():
                logging.critical(f"Task {task.get_name()} failed: {task.exception()}")
                
        for task in pending:
            task.cancel()
        
        if pending:
            await asyncio.wait(pending, timeout=5)
    
    except KeyboardInterrupt:
        logging.info("Shutting down gracefully...")
    except Exception as e:
        logging.critical(f"Critical error: {e}")
    finally:
        if trader:
            await trader.cleanup()
        logging.info("Bot shutdown complete")

if __name__ == "__main__":
    asyncio.run(main())
