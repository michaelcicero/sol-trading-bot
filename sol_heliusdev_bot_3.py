import os
import logging
import asyncio
import aiohttp
import base58
import base64
import random
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
from spl.token._layouts import ACCOUNT_LAYOUT
from spl.token.instructions import create_associated_token_account, get_associated_token_address

# === CONFIGURATION ===
SOL_MINT = "So11111111111111111111111111111111111111112"
TARGET_MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
TOKEN_DECIMALS = 9  # Verified from Helius API response
TOKEN_SYMBOL = "POPCAT"
MIN_PRICE_CHANGE = 0.001
MIN_ATR_VALUE = 0.0000001  # 100x smaller
MAX_REASONABLE_PRICE = 0.005
MIN_TOKEN_RECEIVED = 100_000_000  # 0.1 POPCAT (100,000,000/10^9)

class HealthMonitor:
    def __init__(self):
        self.last_success = datetime.now()
        
    async def check_health(self):
        if (datetime.now() - self.last_success).seconds > 600:
            logging.critical("🆘 No successful trades in 10 minutes!")
        self.last_success = datetime.now()

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
        self.base_url = "https://quote-api.jup.ag/v6"
        
        self.price_history = deque(maxlen=100)
        self.short_atr_period = 14
        self.long_atr_period = 28
        self.atr_multiplier = 2.0
        self.short_atr_values = deque(maxlen=14)
        self.long_atr_values = deque(maxlen=28)
        
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=30),
            headers={'X-Helius-Client': 'TradingBot/1.0'},
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self.last_trade = datetime.min
        self.base_trade_size = 0.15
        self.cooldown = 180
        self.min_sell_amount = 50
        self.sell_percentage = 0.85
        self.sell_threshold = 1.012
        self.trailing_stop_pct = 0.98
        self.trailing_peak = 0.0
        self.min_profit_sol = 0.03
        self.max_trade_duration = timedelta(minutes=90)  # Time-based exit
        self.risk_per_trade = 0.02  # 2% of portfolio per trade

        # Risk management parameters
        self.consecutive_buys = 0
        self.max_consecutive_buys = 2
        self.last_trade_was_loss = False
        self.loss_cooldown_until = datetime.min
        self.loss_cooldown_period = timedelta(minutes=30)
        self.ma_period = 21
        
        self.trade_start_time = {}  # Track trade durations
        self.health_monitor = HealthMonitor()
        logging.info(f"🔑 Trading Wallet: {self.wallet}")
        logging.info(f"🔗 Helius Endpoint: {self.client._provider.endpoint_uri}")

    def is_in_downtrend(self):
        if len(self.price_history) < self.ma_period:
            return False
        closes = [p['close'] for p in list(self.price_history)[-self.ma_period:]]
        ma = sum(closes) / len(closes)
        return self.price_history[-1]['close'] < ma

    async def calculate_position_size(self, sol_balance, atr, current_price):
        """Dynamic position sizing based on volatility and portfolio risk"""
        risk_amount = sol_balance * self.risk_per_trade
        atr_adjusted = max(atr, current_price * 0.005)  # Minimum 0.5% volatility
        position_size = risk_amount / atr_adjusted
        return min(position_size, sol_balance * 0.25)  # Max 25% of balance

    async def get_dynamic_slippage(self, short_atr, current_price):
        base_slippage = 300
        if current_price == 0:
            return base_slippage
        volatility_factor = (short_atr / current_price) * 150
        return min(1000, max(200, int(base_slippage + volatility_factor)))

    async def get_token_price(self):
        """Fetch accurate SOL/token price from Jupiter API"""
        max_retries = 5
        base_delay = 1.5
        
        for attempt in range(max_retries):
            try:
                params = {
                    "inputMint": SOL_MINT,
                    "outputMint": TARGET_MINT,
                    "amount": int(0.01 * 1e9),
                    "slippageBps": 1000,
                    "swapMode": "ExactIn",
                    "excludeDexes": "Mango,Mercurial"
                }
                
                async with self.session.get(
                    f"{self.base_url}/quote",
                    params=params,
                    headers={'Accept': 'application/json'}
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        logging.error(f"Jupiter API Error {resp.status}: {error}")
                        continue
                    
                    quote = await resp.json()
                    if "error" in quote:
                        logging.error(f"Jupiter Error: {quote['error']}")
                        continue
                    
                    raw_token_amount = float(quote.get("outAmount", 0))
                    if raw_token_amount <= 0:
                        logging.warning("Invalid token amount received")
                        continue
                    
                    token_amount = raw_token_amount / (10 ** TOKEN_DECIMALS)
                    if token_amount < (MIN_TOKEN_RECEIVED / (10 ** TOKEN_DECIMALS)):
                        logging.warning(f"Insufficient liquidity: {token_amount:.9f} tokens")
                        continue
                    
                    new_price = 0.01 / token_amount
                    
                    if new_price > MAX_REASONABLE_PRICE or new_price < 1e-8:
                        logging.warning(f"Price out of range: {new_price:.8f} SOL")
                        continue
                    
                    self.price_history.append({
                        'high': new_price,
                        'low': new_price,
                        'close': new_price,
                        'timestamp': datetime.now()
                    })
                    return new_price
                    
            except Exception as e:
                logging.error(f"Price check failed (attempt {attempt+1}): {str(e)}")
                await asyncio.sleep(base_delay ** attempt)
        
        logging.error("Max price check retries exceeded")
        return None

    async def get_balance(self, mint: str):
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey(), commitment=Confirmed)
                return resp.value / 1e9
            else:
                # Method 1: Direct token account balance check
                try:
                    ata = get_associated_token_address(
                        Pubkey.from_string(self.wallet),
                        Pubkey.from_string(mint)
                    )
                    payload = {
                        "jsonrpc": "2.0",
                        "id": f"{random.randint(1, 10000)}",
                        "method": "getTokenAccountBalance",
                        "params": [
                            str(ata),
                            {"commitment": "finalized"}
                        ]
                    }
                    
                    async with self.session.post(
                        self.client._provider.endpoint_uri,
                        json=payload,
                        headers={"Content-Type": "application/json"}
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            if "result" in result and "value" in result["result"]:
                                raw_balance = int(result["result"]["value"]["amount"])
                                return raw_balance / (10 ** TOKEN_DECIMALS)
                except Exception as e:
                    logging.error(f"Direct balance check failed: {str(e)}")

                # Method 2: JSON parsed accounts
                try:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": f"{random.randint(1, 10000)}",
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            self.wallet,
                            {"mint": mint},
                            {"encoding": "jsonParsed", "commitment": "finalized"}
                        ]
                    }
                    
                    async with self.session.post(
                        self.client._provider.endpoint_uri,
                        json=payload,
                        headers={"Content-Type": "application/json"}
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            total_balance = 0.0
                            if "result" in result and "value" in result["result"]:
                                for account in result["result"]["value"]:
                                    try:
                                        token_amount = float(account["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"])
                                        total_balance += token_amount
                                    except Exception as e:
                                        logging.error(f"JSON parsing error: {str(e)}")
                                return round(total_balance, 4)
                except Exception as e:
                    logging.error(f"JSON parsed token accounts failed: {str(e)}")

                # Method 3: Base64 fallback
                try:
                    opts = TokenAccountOpts(
                        mint=Pubkey.from_string(mint),
                        encoding="base64",
                        commitment=Confirmed
                    )
                    resp = await self.client.get_token_accounts_by_owner(
                        self.keypair.pubkey(), 
                        opts
                    )
                    
                    total_balance = 0.0
                    for account in resp.value:
                        try:
                            data_str = account.account.data[0] if isinstance(account.account.data, list) else account.account.data
                            account_data = base64.b64decode(data_str)
                            if len(account_data) >= 72:
                                amount_bytes = account_data[64:72]
                                amount = int.from_bytes(amount_bytes, byteorder='little')
                                total_balance += amount / (10 ** TOKEN_DECIMALS)
                        except Exception as e:
                            logging.error(f"Account parsing error: {str(e)}")
                    return round(total_balance, 4)
                except Exception as e:
                    logging.error(f"Base64 fallback failed: {str(e)}")
                
                return 0.0
        except Exception as e:
            logging.error(f"Balance check failed: {str(e)}")
            return 0.0

    async def create_ata_if_needed(self):
        """Create associated token account if it doesn't exist"""
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
        except Exception as e:
            logging.error(f"ATA check failed: {str(e)}")

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        max_retries = 5
        base_delay = 1.5
        
        for attempt in range(max_retries):
            try:
                await self.create_ata_if_needed()
                current_price = await self.get_token_price()
                if not current_price or current_price < 0.000001:
                    logging.error("Price validation failed")
                    return False

                blockhash_resp = await self.client.get_latest_blockhash(commitment=Confirmed)
                if not blockhash_resp.value:
                    logging.error("Blockhash retrieval failed")
                    continue
                latest_blockhash = blockhash_resp.value

                current_balance = await self.get_balance(input_mint)
                if current_balance < amount * 0.9:
                    logging.error("Insufficient balance")
                    return False

                short_atr = await self.calculate_atr(self.short_atr_period, self.short_atr_values)
                slippage_bps = await self.get_dynamic_slippage(short_atr, current_price)

                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(amount * (1e9 if input_mint == SOL_MINT else 10**TOKEN_DECIMALS)),
                    "slippageBps": slippage_bps,
                    "swapMode": "ExactIn" if input_mint == SOL_MINT else "ExactOut"
                }

                async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200:
                        continue
                    quote = await resp.json()

                # Dynamic priority fees based on volatility
                priority_fee = int(50000 * (1 + (short_atr/current_price)))
                
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "computeUnitPriceMicroLamports": priority_fee
                }

                async with self.session.post(f"{self.base_url}/swap", json=payload) as resp:
                    if resp.status != 200:
                        continue
                    swap_data = await resp.json()

                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                secret_key_bytes = base58.b58decode(self.base58_private_key)[:32]
                signing_key = SigningKey(secret_key_bytes)
                signature_bytes = signing_key.sign(message_bytes).signature
                signature = Signature(signature_bytes)
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])
                
                txid = await self.client.send_raw_transaction(
                    bytes(signed_tx), 
                    opts=TxOpts(
                        skip_preflight=False,
                        preflight_commitment=Confirmed
                    )
                )
                
                # Verify transaction confirmation
                confirmed = await self.client.confirm_transaction(
                    txid.value,
                    latest_blockhash.blockhash,
                    sleep_seconds=0.5,
                    last_valid_block_height=latest_blockhash.last_valid_block_height
                )
                if not confirmed:
                    logging.error("❌ Transaction confirmation failed")
                    return False
                
                logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value}")
                self.health_monitor.check_health()
                self.trade_start_time[txid.value] = datetime.now()
                return True

            except aiohttp.ClientError as e:
                delay = base_delay ** attempt + random.uniform(0, 0.5)
                logging.error(f"Network error (retry {attempt+1}): Sleeping {delay:.1f}s")
                await asyncio.sleep(delay)
            except Exception as e:
                logging.error(f"Trade error (retry {attempt+1}): {str(e)}")
                await asyncio.sleep(1)
        
        logging.error("Max retries exceeded")
        return False

    async def calculate_tr(self, prev, curr):
        tr1 = curr['high'] - curr['low']
        tr2 = abs(curr['high'] - prev['close'])
        tr3 = abs(curr['low'] - prev['close'])
        return max(tr1, tr2, tr3)

    async def calculate_atr(self, period, atr_values):
        if len(self.price_history) < period * 2:
            return MIN_ATR_VALUE
        
        if not atr_values:
            tr_values = []
            for i in range(period):
                prev = self.price_history[i]
                curr = self.price_history[i+1]
                tr = await self.calculate_tr(prev, curr)
                tr_values.append(tr)
            initial_atr = sum(tr_values) / period
            atr_values.append(initial_atr)
            return initial_atr
        
        prev = self.price_history[-2]
        curr = self.price_history[-1]
        tr = await self.calculate_tr(prev, curr)
        new_atr = (atr_values[-1] * (period - 1) + tr) / period
        atr_values.append(new_atr)
        return max(new_atr, MIN_ATR_VALUE)

    async def get_dynamic_multiplier(self, short_atr, long_atr):
        if short_atr > long_atr * 1.2:
            return max(0.8, self.atr_multiplier * 0.8)
        elif short_atr < long_atr * 0.8:
            return min(2.5, self.atr_multiplier * 1.2)
        else:
            return self.atr_multiplier

    async def monitor_balances(self):
        last_token_balance = 0
        last_sol_balance = 0
        
        while True:
            try:
                current_sol_balance = await self.get_balance(SOL_MINT)
                current_token_balance = await self.get_balance(TARGET_MINT)
                
                sol_change = current_sol_balance - last_sol_balance if last_sol_balance > 0 else 0
                token_change = current_token_balance - last_token_balance if last_token_balance > 0 else 0
                
                if abs(sol_change) > 0.001 or abs(token_change) > 1 or last_token_balance == 0:
                    sol_change_pct = (sol_change / last_sol_balance * 100) if last_sol_balance > 0 else 0
                    token_change_pct = (token_change / last_token_balance * 100) if last_token_balance > 0 else 0
                    
                    logging.info(f"📊 Balance Update: {current_sol_balance:.4f} SOL ({sol_change:+.4f}, {sol_change_pct:+.2f}%) | "
                                f"{current_token_balance:.2f} {TOKEN_SYMBOL} ({token_change:+.2f}, {token_change_pct:+.2f}%)")
                
                last_sol_balance = current_sol_balance
                last_token_balance = current_token_balance
                await asyncio.sleep(60)
            except Exception as e:
                logging.error(f"Balance monitoring error: {str(e)}")
                await asyncio.sleep(60)

    async def trading_strategy(self):
        while True:
            try:
                current_time = datetime.now()
                if (current_time - self.last_trade).seconds < self.cooldown:
                    await asyncio.sleep(30)
                    continue

                price = await self.get_token_price()
                if not price:
                    await asyncio.sleep(10)
                    continue

                required_history = max(self.short_atr_period, self.long_atr_period) * 2
                if len(self.price_history) < required_history:
                    logging.info(f"📈 Collecting data ({len(self.price_history)}/{required_history})")
                    await asyncio.sleep(5)
                    continue

                short_atr = await self.calculate_atr(self.short_atr_period, self.short_atr_values)
                long_atr = await self.calculate_atr(self.long_atr_period, self.long_atr_values)
                avg_price = sum(p['close'] for p in self.price_history) / len(self.price_history)
                
                if avg_price <= 0 or (short_atr <= 0 and long_atr <= 0):
                    logging.warning("⚠️ Low volatility - extending data collection")
                    await asyncio.sleep(300)
                    continue

                multiplier = await self.get_dynamic_multiplier(short_atr, long_atr)
                dynamic_threshold = avg_price - (short_atr * multiplier)
                self.trailing_peak = max(self.trailing_peak, price)
                
                sol_balance, token_balance = await asyncio.gather(
                    self.get_balance(SOL_MINT),
                    self.get_balance(TARGET_MINT)
                )

                logging.info(f"{TOKEN_SYMBOL}: {price:.6f} | Avg: {avg_price:.6f} | S-ATR: {short_atr:.6f} | L-ATR: {long_atr:.6f} | Mult: {multiplier:.2f} | Thresh: {dynamic_threshold:.6f}")
                logging.info(f"Balances: {sol_balance:.4f} SOL | {token_balance:.2f} {TOKEN_SYMBOL}")

                # Risk management checks
                if self.is_in_downtrend():
                    logging.info("⏸ Skipping buy: Trend filter active (downtrend detected)")
                    await asyncio.sleep(30)
                    continue

                if self.consecutive_buys >= self.max_consecutive_buys:
                    logging.info("⏸ Skipping buy: Max consecutive buys reached")
                    await asyncio.sleep(30)
                    continue

                if current_time < self.loss_cooldown_until:
                    logging.info(f"⏸ Skipping buy: In loss cooldown until {self.loss_cooldown_until}")
                    await asyncio.sleep(30)
                    continue

                # Dynamic position sizing
                position_size = await self.calculate_position_size(sol_balance, short_atr, price)
                
                # Buy logic
                if position_size >= 0.01 and price < dynamic_threshold:
                    logging.info(f"🚀 Buying {TOKEN_SYMBOL}... (Size: {position_size:.4f} SOL, Short ATR: {short_atr:.6f}, Mult: {multiplier:.2f})")
                    buy_success = await self.execute_trade(SOL_MINT, TARGET_MINT, position_size)
                    if buy_success:
                        self.last_trade = current_time
                        self.trailing_peak = price
                        self.consecutive_buys += 1
                        self.last_trade_was_loss = False

                # Sell logic
                dynamic_sell_threshold = 1 + (long_atr / avg_price) * (long_atr/short_atr) if short_atr > 0 else self.sell_threshold
                sell_reasons = []

                # Time-based exit check
                if token_balance > 0:
                    for txid, start_time in list(self.trade_start_time.items()):
                        if current_time - start_time > self.max_trade_duration:
                            sell_reasons.append(f"time-based exit ({self.max_trade_duration})")
                            break

                # Traditional exit conditions
                if token_balance > self.min_sell_amount and (
                    price > avg_price * max(self.sell_threshold, dynamic_sell_threshold) or 
                    price < self.trailing_peak * self.trailing_stop_pct or
                    len(sell_reasons) > 0
                ):
                    sell_amount = token_balance * self.sell_percentage
                    trade_value_sol = price * sell_amount
                    
                    if trade_value_sol >= self.min_profit_sol:
                        reason = "profit target" if price > avg_price * max(self.sell_threshold, dynamic_sell_threshold) else "trailing stop"
                        if len(sell_reasons) > 0:
                            reason = ", ".join(sell_reasons)
                        
                        logging.info(f"💰 Selling {sell_amount:.2f} {TOKEN_SYMBOL} (Value: {trade_value_sol:.6f} SOL) - Reason: {reason}")
                        sell_success = await self.execute_trade(TARGET_MINT, SOL_MINT, sell_amount)
                        if sell_success:
                            self.last_trade = current_time
                            self.trailing_peak = 0.0
                            self.consecutive_buys = 0
                            self.trade_start_time.pop(txid, None)
                            if reason == "trailing stop":
                                self.last_trade_was_loss = True
                                self.loss_cooldown_until = current_time + self.loss_cooldown_period
                            else:
                                self.last_trade_was_loss = False

                await asyncio.sleep(15)
            
            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(60)

    async def cleanup(self):
        if not self.session.closed:
            await self.session.close()
        await self.client.close()

async def main():
    trader = TokenTrader()
    try:
        await asyncio.gather(
            trader.trading_strategy(),
            trader.monitor_balances()
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
