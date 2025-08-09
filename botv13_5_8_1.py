import os
import logging
import asyncio
import aiohttp
import base58
import base64
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

# === CONFIGURATION ===
SOL_MINT = "So11111111111111111111111111111111111111112"
TARGET_MINT = os.getenv("TARGET_MINT", "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")
TOKEN_DECIMALS = int(os.getenv("TOKEN_DECIMALS", 5))
TOKEN_SYMBOL = os.getenv("TOKEN_SYMBOL", "POPCAT")

class TokenTrader:
    def __init__(self):
        load_dotenv()
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.base58_private_key = os.getenv("PRIVATE_KEY")
        self.keypair = Keypair.from_base58_string(self.base58_private_key)
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"
        
        self.price_history = deque(maxlen=100)
        self.short_atr_period = int(os.getenv("SHORT_ATR_PERIOD", 7))
        self.long_atr_period = int(os.getenv("LONG_ATR_PERIOD", 14))
        self.atr_multiplier = float(os.getenv("ATR_MULTIPLIER", 1.5))
        self.short_atr_values = deque(maxlen=self.short_atr_period)
        self.long_atr_values = deque(maxlen=self.long_atr_period)
        self.dynamic_multiplier_enabled = True
        
        self.session = aiohttp.ClientSession()
        self.last_trade = datetime.min
        self.base_trade_size = float(os.getenv("BASE_TRADE_SIZE", 0.28))
        self.cooldown = int(os.getenv("COOLDOWN", 120))
        self.min_sell_amount = float(os.getenv("MIN_SELL_AMOUNT", 50))
        self.sell_percentage = float(os.getenv("SELL_PERCENTAGE", 0.95))
        self.sell_threshold = float(os.getenv("SELL_THRESHOLD", 1.023))
        self.trailing_stop_pct = float(os.getenv("TRAILING_STOP_PCT", 0.97))
        self.trailing_peak = 0.0
        self.min_profit_sol = float(os.getenv("MIN_PROFIT_SOL", 0.5))

    async def calculate_tr(self, prev, curr):
        tr1 = curr['high'] - curr['low']
        tr2 = abs(curr['high'] - prev['close'])
        tr3 = abs(curr['low'] - prev['close'])
        return max(tr1, tr2, tr3)

    async def calculate_atr(self, period, atr_values):
        if len(self.price_history) < period + 1:
            return 0.0
        
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
        return new_atr

    async def get_dynamic_multiplier(self, short_atr, long_atr):
        if short_atr > long_atr * 1.2:
            return max(0.8, self.atr_multiplier * 0.8)
        elif short_atr < long_atr * 0.8:
            return min(2.5, self.atr_multiplier * 1.2)
        else:
            return self.atr_multiplier

    async def get_token_price(self):
        try:
            params = {
                "inputMint": SOL_MINT,
                "outputMint": TARGET_MINT,
                "amount": int(self.base_trade_size * 1e9),
                "slippageBps": 200
            }
            async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                if resp.status != 200:
                    return None
                quote = await resp.json()
                price = (float(quote["outAmount"]) / (10 ** TOKEN_DECIMALS)) / self.base_trade_size
                self.price_history.append({
                    'high': price,
                    'low': price,
                    'close': price,
                    'timestamp': datetime.now()
                })
                return price
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return (resp.value / 1e9) * 0.97  # 3% fee buffer
            else:
                opts = TokenAccountOpts(mint=Pubkey.from_string(mint))
                resp = await self.client.get_token_accounts_by_owner(
                    self.keypair.pubkey(), opts, commitment=Confirmed)
                
                if not resp.value:
                    return 0
                
                total_balance = 0
                for account in resp.value:
                    token_balance_resp = await self.client.get_token_account_balance(account.pubkey)
                    if token_balance_resp and hasattr(token_balance_resp.value, 'ui_amount'):
                        total_balance += token_balance_resp.value.ui_amount
                return total_balance
        except Exception as e:
            logging.error(f"Balance check failed: {str(e)}")
            return 0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                blockhash_resp = await self.client.get_latest_blockhash()
                latest_blockhash = blockhash_resp.value
                current_balance = await self.get_balance(input_mint)
                actual_amount = min(amount, current_balance)

                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(actual_amount * 1e9) if input_mint == SOL_MINT else int(actual_amount * (10 ** TOKEN_DECIMALS)),
                    "slippageBps": 200
                }

                async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200:
                        continue
                    quote = await resp.json()

                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True
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
                
                txid = await self.client.send_raw_transaction(bytes(signed_tx), opts=TxOpts(skip_preflight=False))
                await self.client.confirm_transaction(txid.value, latest_blockhash.blockhash)
                logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value}")
                return True

            except aiohttp.ClientError as e:
                backoff = 2 ** attempt
                logging.error(f"Network error (retry {attempt+1}): Sleeping {backoff}s")
                await asyncio.sleep(backoff)
            except Exception as e:
                logging.error(f"Trade error (retry {attempt+1}): {str(e)}")
                await asyncio.sleep(1)
        return False

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

                short_atr = await self.calculate_atr(self.short_atr_period, self.short_atr_values)
                long_atr = await self.calculate_atr(self.long_atr_period, self.long_atr_values)
                avg_price = sum(p['close'] for p in self.price_history) / len(self.price_history) if self.price_history else 0
                multiplier = await self.get_dynamic_multiplier(short_atr, long_atr) if self.dynamic_multiplier_enabled else self.atr_multiplier
                dynamic_threshold = avg_price - (short_atr * multiplier)
                self.trailing_peak = max(self.trailing_peak, price)
                
                sol_balance, token_balance = await asyncio.gather(
                    self.get_balance(SOL_MINT),
                    self.get_balance(TARGET_MINT)
                )

                logging.info(f"{TOKEN_SYMBOL}: {price:.4f} | Avg: {avg_price:.4f} | S-ATR: {short_atr:.4f} | L-ATR: {long_atr:.4f} | Mult: {multiplier:.2f} | Thresh: {dynamic_threshold:.4f}")
                logging.info(f"Balances: {sol_balance:.4f} SOL | {token_balance:.2f} {TOKEN_SYMBOL}")

                if sol_balance >= self.base_trade_size and price < dynamic_threshold:
                    logging.info(f"🚀 Buying {TOKEN_SYMBOL}... (Short ATR: {short_atr:.4f}, Mult: {multiplier:.2f})")
                    if await self.execute_trade(SOL_MINT, TARGET_MINT, self.base_trade_size):
                        self.last_trade = current_time
                        self.trailing_peak = price
                        new_token_balance = await self.get_balance(TARGET_MINT)
                        tokens_bought = new_token_balance - token_balance
                        logging.info(f"✅ Bought {tokens_bought:.2f} {TOKEN_SYMBOL} | New Balance: {new_token_balance:.2f} {TOKEN_SYMBOL}")

                dynamic_sell_threshold = 1 + (long_atr / avg_price) * 2
                if token_balance > self.min_sell_amount and (
                    price > avg_price * max(self.sell_threshold, dynamic_sell_threshold) or 
                    price < self.trailing_peak * self.trailing_stop_pct
                ):
                    sell_amount = token_balance * self.sell_percentage
                    trade_value_sol = price * sell_amount / (10 ** TOKEN_DECIMALS)
                    
                    if trade_value_sol >= self.min_profit_sol:
                        reason = "profit target" if price > avg_price * max(self.sell_threshold, dynamic_sell_threshold) else "trailing stop"
                        logging.info(f"💰 Selling {sell_amount:.2f} {TOKEN_SYMBOL} (Value: {trade_value_sol:.4f} SOL) - Reason: {reason}")
                        
                        if await self.execute_trade(TARGET_MINT, SOL_MINT, sell_amount):
                            self.last_trade = current_time
                            self.trailing_peak = 0.0
                            new_token_balance = await self.get_balance(TARGET_MINT)
                            tokens_sold = token_balance - new_token_balance
                            new_sol_balance = await self.get_balance(SOL_MINT)
                            sol_gained = new_sol_balance - sol_balance
                            logging.info(f"✅ Sold {tokens_sold:.2f} {TOKEN_SYMBOL} for {sol_gained:.4f} SOL | Remaining: {new_token_balance:.2f} {TOKEN_SYMBOL}")
                    else:
                        logging.info(f"⏩ Skipping sell: trade value {trade_value_sol:.4f} SOL < min profit {self.min_profit_sol} SOL")
                
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
