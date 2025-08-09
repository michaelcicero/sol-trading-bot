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

# === CONFIGURATION ===
SOL_MINT = "So11111111111111111111111111111111111111112"
TARGET_MINT = os.getenv("TARGET_MINT", "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")
TOKEN_DECIMALS = int(os.getenv("TOKEN_DECIMALS", 5))
TOKEN_SYMBOL = os.getenv("TOKEN_SYMBOL", "POPCAT")
MIN_PRICE_CHANGE = 0.001  # 0.1% minimum price variation
MIN_ATR_VALUE = 0.0001    # Minimum volatility threshold
MAX_REASONABLE_PRICE = .1 # Sanity check (SOL per token)
MIN_TOKEN_RECEIVED = 300 # Minimum tokens expected per 1 SOL

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
            os.getenv("HELIUS_RPC_URL"),
            timeout=30,
            commitment=Confirmed
        )
        self.base58_private_key = os.getenv("PRIVATE_KEY")
        self.keypair = Keypair.from_base58_string(self.base58_private_key)
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"
        
        # Volatility configuration
        self.price_history = deque(maxlen=100)
        self.short_atr_period = int(os.getenv("SHORT_ATR_PERIOD", 14))
        self.long_atr_period = int(os.getenv("LONG_ATR_PERIOD", 28))
        self.atr_multiplier = float(os.getenv("ATR_MULTIPLIER", 2.0))
        self.short_atr_values = deque(maxlen=self.short_atr_period)
        self.long_atr_values = deque(maxlen=self.long_atr_period)
        self.dynamic_multiplier_enabled = bool(int(os.getenv("DYNAMIC_MULTIPLIER", 1)))
        
        # Trading parameters
        self.session = aiohttp.ClientSession(
            headers={'X-Helius-Client': 'TradingBot/1.0'}
        )
        self.last_trade = datetime.min
        self.base_trade_size = float(os.getenv("BASE_TRADE_SIZE", 0.25))
        self.cooldown = int(os.getenv("COOLDOWN", 180))
        self.min_sell_amount = float(os.getenv("MIN_SELL_AMOUNT", 50))
        self.sell_percentage = float(os.getenv("SELL_PERCENTAGE", 0.85))
        self.sell_threshold = float(os.getenv("SELL_THRESHOLD", 1.035))
        self.trailing_stop_pct = float(os.getenv("TRAILING_STOP_PCT", 0.96))
        self.trailing_peak = 0.0
        self.min_profit_sol = float(os.getenv("MIN_PROFIT_SOL", 0.15))
        
        # Monitoring
        self.health_monitor = HealthMonitor()

    async def get_dynamic_slippage(self, short_atr, current_price):
        base_slippage = 200  # 2%
        if current_price == 0:
            return base_slippage
        volatility_factor = (short_atr / current_price) * 100
        return min(500, max(100, int(base_slippage + volatility_factor)))

    async def get_token_price(self):
        """Fetch accurate SOL/token price from Jupiter API"""
        try:
            params = {
                "inputMint": SOL_MINT,
                "outputMint": TARGET_MINT,
                "amount": int(0.1 * 1e9),  # 0.1 SOL to prevent large token amounts
                "slippageBps": 500,
                "swapMode": "ExactIn",  # Corrected to ExactIn
                "excludeDexes": "Meteora,Mercurial"
            }
            
            async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                if resp.status != 200:
                    logging.error(f"Price API error: {resp.status}")
                    return None
                
                quote = await resp.json()
                if "error" in quote:
                    logging.error(f"Jupiter error: {quote['error']}")
                    return None
                
                raw_token_amount = float(quote.get("outAmount", 0))
                if raw_token_amount <= 0:
                    logging.error("Invalid token amount received")
                    return None
                
                token_amount = raw_token_amount / (10 ** TOKEN_DECIMALS)
                if token_amount < MIN_TOKEN_RECEIVED:
                    logging.warning(f"Low token amount: {token_amount:.0f} < {MIN_TOKEN_RECEIVED}")
                    return None
                
                new_price = (0.1 * 1e9) / raw_token_amount  # Correct price calculation
                
                if new_price <= 1e-6 or new_price > MAX_REASONABLE_PRICE:
                    logging.error(f"Price sanity check failed: {new_price:.6f} SOL")
                    return None
                
                self.price_history.append({
                    'high': new_price,
                    'low': new_price,
                    'close': new_price,
                    'timestamp': datetime.now()
                })
                return new_price
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey(), commitment=Confirmed)
                return resp.value / 1e9
            else:
                opts = TokenAccountOpts(
                    mint=Pubkey.from_string(mint),
                    encoding="base64"
                )
                resp = await self.client.get_token_accounts_by_owner(
                    self.keypair.pubkey(), 
                    opts
                )
                
                total_balance = 0
                for account in resp.value:
                    try:
                        account_data = base64.b64decode(account.account.data)
                        if len(account_data) < 165:
                            continue
                        parsed_account = ACCOUNT_LAYOUT.parse(account_data)
                        if str(parsed_account.mint) != mint:
                            continue
                        total_balance += parsed_account.amount / (10 ** TOKEN_DECIMALS)
                    except Exception as e:
                        continue
                return total_balance
        except Exception as e:
            logging.error(f"Balance check failed: {str(e)}")
            return 0.0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        max_retries = 5
        base_delay = 1.5
        
        for attempt in range(max_retries):
            try:
                current_price = await self.get_token_price()
                if not current_price or current_price < 0.000001:
                    logging.error("Price sanity check failed")
                    return False

                blockhash_resp = await self.client.get_latest_blockhash(commitment=Confirmed)
                if not blockhash_resp.value:
                    logging.error("Blockhash retrieval failed")
                    continue
                latest_blockhash = blockhash_resp.value

                current_balance = await self.get_balance(input_mint)
                if current_balance < amount * 0.9:
                    logging.error("Insufficient balance for trade")
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

                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "computeUnitPriceMicroLamports": 100000
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
                
                await self.client.confirm_transaction(
                    txid.value,
                    latest_blockhash.blockhash,
                    sleep_seconds=0.5,
                    last_valid_block_height=latest_blockhash.last_valid_block_height
                )
                
                logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value}")
                self.health_monitor.check_health()
                return True

            except aiohttp.ClientError as e:
                delay = base_delay ** attempt + random.uniform(0, 0.5)
                logging.error(f"Network error (retry {attempt+1}): Sleeping {delay:.1f}s")
                await asyncio.sleep(delay)
            except Exception as e:
                logging.error(f"Trade error (retry {attempt+1}): {str(e)}")
                await asyncio.sleep(1)
        
        logging.error("Max retries exceeded for trade")
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
                
                if abs(sol_change) > 0.001 or abs(token_change) > 1:
                    sol_change_pct = (sol_change / last_sol_balance * 100) if last_sol_balance > 0 else 0
                    token_change_pct = (token_change / last_token_balance * 100) if last_token_balance > 0 else 0
                    
                    logging.info(f"📊 Balance Update: {current_sol_balance:.4f} SOL ({sol_change:+.4f}, {sol_change_pct:+.2f}%) | "
                                f"{current_token_balance:.2f} {TOKEN_SYMBOL} ({token_change:+.2f}, {token_change_pct:+.2f}%)")
                
                last_sol_balance = current_sol_balance
                last_token_balance = current_token_balance
                await asyncio.sleep(300)
            except Exception as e:
                logging.error(f"Balance monitoring error: {str(e)}")
                await asyncio.sleep(60)

    async def trading_strategy(self):
        emergency_activation = False
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
                    await asyncio.sleep(15)
                    continue

                short_atr = await self.calculate_atr(self.short_atr_period, self.short_atr_values)
                long_atr = await self.calculate_atr(self.long_atr_period, self.long_atr_values)
                avg_price = sum(p['close'] for p in self.price_history) / len(self.price_history)
                
                if avg_price <= 0 or (short_atr <= 0 and long_atr <= 0):
                    logging.warning("⚠️ Insufficient volatility - extending data collection")
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

                if sol_balance >= self.base_trade_size and price < dynamic_threshold:
                    logging.info(f"🚀 Buying {TOKEN_SYMBOL}... (Short ATR: {short_atr:.6f}, Mult: {multiplier:.2f})")
                    if await self.execute_trade(SOL_MINT, TARGET_MINT, self.base_trade_size):
                        self.last_trade = current_time
                        self.trailing_peak = price
                        new_token_balance = await self.get_balance(TARGET_MINT)
                        tokens_bought = new_token_balance - token_balance
                        logging.info(f"✅ Bought {tokens_bought:.2f} {TOKEN_SYMBOL} | New Balance: {new_token_balance:.2f} {TOKEN_SYMBOL}")

                dynamic_sell_threshold = 1 + (long_atr / avg_price) * (long_atr/short_atr) if short_atr > 0 else self.sell_threshold
                if token_balance > self.min_sell_amount and (
                    price > avg_price * max(self.sell_threshold, dynamic_sell_threshold) or 
                    price < self.trailing_peak * self.trailing_stop_pct
                ):
                    sell_amount = token_balance * self.sell_percentage
                    trade_value_sol = price * sell_amount / (10 ** TOKEN_DECIMALS)
                    
                    if trade_value_sol >= self.min_profit_sol:
                        reason = "profit target" if price > avg_price * max(self.sell_threshold, dynamic_sell_threshold) else "trailing stop"
                        logging.info(f"💰 Selling {sell_amount:.2f} {TOKEN_SYMBOL} (Value: {trade_value_sol:.6f} SOL) - Reason: {reason}")
                        
                        if await self.execute_trade(TARGET_MINT, SOL_MINT, sell_amount):
                            self.last_trade = current_time
                            self.trailing_peak = 0.0
                            new_token_balance = await self.get_balance(TARGET_MINT)
                            tokens_sold = token_balance - new_token_balance
                            new_sol_balance = await self.get_balance(SOL_MINT)
                            sol_gained = new_sol_balance - sol_balance
                            logging.info(f"✅ Sold {tokens_sold:.2f} {TOKEN_SYMBOL} for {sol_gained:.6f} SOL | Remaining: {new_token_balance:.2f} {TOKEN_SYMBOL}")
                    else:
                        logging.info(f"⏩ Skipping sell: trade value {trade_value_sol:.6f} SOL < min profit {self.min_profit_sol} SOL")
                
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
        handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
    )
    asyncio.run(main())
