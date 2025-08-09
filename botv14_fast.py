import os
import logging
import asyncio
import aiohttp
import base58
import base64
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

# === CONFIGURATION ===
SOL_MINT = "So11111111111111111111111111111111111111112"
TARGET_MINT = os.getenv("TARGET_MINT", "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")
TOKEN_DECIMALS = int(os.getenv("TOKEN_DECIMALS", 5))
TOKEN_SYMBOL = os.getenv("TOKEN_SYMBOL", "POPCAT")

# Risk parameters
MIN_TRADE_SIZE_SOL = 0.03
MIN_SOL_BALANCE = 0.05
CANDLE_DURATION = timedelta(seconds=30)
ATR_PERIOD = 2

class TokenTrader:
    def __init__(self):
        load_dotenv()
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.base58_private_key = os.getenv("PRIVATE_KEY")
        self.keypair = Keypair.from_base58_string(self.base58_private_key)
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"

        # Price tracking
        self.price_history = deque(maxlen=10)
        self.current_candle = None
        self.last_candle_update = datetime.min

        # Risk management
        self.risk_percentage = 0.005
        self.max_position_pct = 0.1
        self.atr_period = ATR_PERIOD
        self.atr_multiplier = 2.0
        self.atr_values = deque(maxlen=self.atr_period)

        # Trading parameters
        self.session = aiohttp.ClientSession()
        self.last_trade = datetime.min
        self.base_trade_size = 0.1  # 0.1 SOL for price checks
        self.cooldown = 60
        self.min_sell_amount = 25
        self.sell_threshold = 1.01
        self.trailing_stop_pct = 0.98
        self.trailing_peak = 0.0
        self.min_profit_sol = 0.3

    async def calculate_position_size(self, price, atr):
        sol_balance = await self.get_balance(SOL_MINT)
        if sol_balance < MIN_SOL_BALANCE or atr == 0 or price == 0:
            return 0.0
        risk_amount = sol_balance * self.risk_percentage
        volatility_units = risk_amount / (atr * self.atr_multiplier)
        max_position = sol_balance * self.max_position_pct
        return max(min(volatility_units, max_position), MIN_TRADE_SIZE_SOL)

    async def calculate_tr(self, prev, curr):
        tr1 = curr['high'] - curr['low']
        tr2 = abs(curr['high'] - prev['close'])
        tr3 = abs(curr['low'] - prev['close'])
        return max(tr1, tr2, tr3)

    async def calculate_atr(self):
        if len(self.price_history) < self.atr_period + 1:
            return 0.0
        if not self.atr_values:
            tr_sum = sum(await self.calculate_tr(self.price_history[i], self.price_history[i+1]) 
                        for i in range(self.atr_period))
            initial_atr = tr_sum / self.atr_period
            self.atr_values.append(initial_atr)
        latest_tr = await self.calculate_tr(self.price_history[-2], self.price_history[-1])
        new_atr = (self.atr_values[-1] * (self.atr_period - 1) + latest_tr) / self.atr_period
        self.atr_values.append(new_atr)
        return self.atr_values[-1]

    async def get_token_price(self):
        try:
            params = {
                "inputMint": SOL_MINT,
                "outputMint": TARGET_MINT,
                "amount": int(self.base_trade_size * 1e9),
                "slippageBps": 500  # Increased from 200
            }
            async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                if resp.status != 200:
                    return None
                quote = await resp.json()
                current_price = float(quote["outAmount"]) / (10 ** TOKEN_DECIMALS) / self.base_trade_size
            
            now = datetime.now()
            if not self.current_candle or (now - self.last_candle_update) > CANDLE_DURATION:
                self.current_candle = {
                    'open': current_price,
                    'high': current_price,
                    'low': current_price,
                    'close': current_price,
                    'start_time': now
                }
                self.price_history.append(self.current_candle)
                self.last_candle_update = now
            else:
                self.current_candle['high'] = max(self.current_candle['high'], current_price)
                self.current_candle['low'] = min(self.current_candle['low'], current_price)
                self.current_candle['close'] = current_price
            return current_price
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return resp.value / 1e9  # No rent subtraction
            else:
                opts = TokenAccountOpts(mint=Pubkey.from_string(mint))
                resp = await self.client.get_token_accounts_by_owner_json_parsed(
                    self.keypair.pubkey(), opts, commitment=Confirmed
                )
                total = 0.0
                for account in resp.value:
                    try:
                        token_amount = account.account.data.parsed['info']['tokenAmount']['uiAmount']
                        total += float(token_amount)
                    except Exception as e:
                        logging.error(f"Token parse error: {str(e)}")
                return total
        except Exception as e:
            logging.error(f"Balance error: {str(e)}")
            return 0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Get fresh blockhash for each attempt
                blockhash_resp = await self.client.get_latest_blockhash(commitment=Confirmed)
                latest_blockhash = blockhash_resp.value.blockhash
                
                current_balance = await self.get_balance(input_mint)
                actual_amount = min(amount, current_balance)
                
                if input_mint == SOL_MINT and actual_amount < MIN_TRADE_SIZE_SOL:
                    logging.error("🚨 Insufficient SOL for minimum trade")
                    return False
                    
                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(actual_amount * 1e9) if input_mint == SOL_MINT else int(actual_amount * (10 ** TOKEN_DECIMALS)),
                    "slippageBps": 500  # Increased from 200
                }
                async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2**attempt)
                        continue
                    quote = await resp.json()
                
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "computeUnitPriceMicroLamports": 100000  # Priority fee
                }
                async with self.session.post(f"{self.base_url}/swap", json=payload) as resp:
                    swap_data = await resp.json()
                
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                secret_key = SigningKey(base58.b58decode(self.base58_private_key)[:32])
                signature = secret_key.sign(message_bytes).signature
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [Signature(signature)])
                
                txid = await self.client.send_raw_transaction(bytes(signed_tx), opts=TxOpts(
                    skip_preflight=False,
                    preflight_commitment=Confirmed
                ))
                await self.client.confirm_transaction(txid.value, latest_blockhash)
                logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value}")
                return True
            except Exception as e:
                logging.error(f"Trade error (retry {attempt+1}): {str(e)}")
                await asyncio.sleep(5 * (attempt + 1))  # 5s, 10s, 15s delays
        return False

    async def trading_strategy(self):
        while True:
            try:
                current_time = datetime.now()
                if (current_time - self.last_trade).seconds < self.cooldown:
                    await asyncio.sleep(15)
                    continue

                price = await self.get_token_price()
                if price is None:
                    await asyncio.sleep(10)
                    continue

                avg_price = np.mean([p['close'] for p in self.price_history]) if self.price_history else price
                atr = await self.calculate_atr() if len(self.price_history) >= self.atr_period else 0.0
                dynamic_threshold = avg_price - (atr * self.atr_multiplier)
                self.trailing_peak = max(self.trailing_peak, price)

                # Sequential balance checks
                sol_balance = await self.get_balance(SOL_MINT)
                token_balance = await self.get_balance(TARGET_MINT)

                # Critical SOL check
                if sol_balance < MIN_SOL_BALANCE:
                    logging.critical(f"🔴 SOL balance {sol_balance:.6f} below minimum {MIN_SOL_BALANCE}")
                    await self.cleanup()
                    return

                logging.info(f"""
{TOKEN_SYMBOL} Price: {price:.5f}
Candles Collected: {len(self.price_history)}
ATR ({self.atr_period}): {atr:.5f}
SOL Balance: {sol_balance:.5f}
Token Balance: {token_balance:.2f}
Trailing Peak: {self.trailing_peak:.5f}
                """.strip())

                position_size = await self.calculate_position_size(price, atr)
                if (position_size > 0 
                    and sol_balance >= position_size 
                    and price < dynamic_threshold
                    and price < avg_price * 0.99):
                    logging.info(f"🚀 Buying {position_size:.4f} SOL of {TOKEN_SYMBOL}...")
                    if await self.execute_trade(SOL_MINT, TARGET_MINT, position_size):
                        self.last_trade = current_time
                        self.trailing_peak = price

                # Prevent selling if SOL balance is critical
                if token_balance > self.min_sell_amount and sol_balance > 0.02:
                    sell_conditions = [
                        price > avg_price * self.sell_threshold,
                        price < self.trailing_peak * self.trailing_stop_pct,
                        price > self.trailing_peak * 1.02,
                    ]
                    if any(sell_conditions):
                        sell_amount = token_balance
                        trade_value = price * sell_amount / (10 ** TOKEN_DECIMALS)
                        if trade_value >= self.min_profit_sol:
                            logging.info(f"💰 Selling {sell_amount:.2f} {TOKEN_SYMBOL}...")
                            if await self.execute_trade(TARGET_MINT, SOL_MINT, sell_amount):
                                self.last_trade = current_time
                                self.trailing_peak = 0.0
                await asyncio.sleep(10)
            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(30)

    async def cleanup(self):
        await self.session.close()
        await self.client.close()

async def main():
    trader = TokenTrader()
    try:
        await trader.trading_strategy()
    finally:
        await trader.cleanup()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
    )
    asyncio.run(main())
