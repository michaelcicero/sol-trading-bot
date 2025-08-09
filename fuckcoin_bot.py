import os
import logging
import asyncio
import aiohttp
import json
import base58
import base64
from datetime import datetime, timedelta
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
TOKEN_SYMBOL = os.getenv("TOKEN_SYMBOL", "POPCOIN")
# === END CONFIGURATION ===

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("solana_bot.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

class TokenTrader:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.base58_private_key = os.getenv("PRIVATE_KEY")
        self.keypair = Keypair.from_base58_string(self.base58_private_key)
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"
        self.price_history = []
        self.last_trade = datetime.min
        self.base_trade_size = float(os.getenv("BASE_TRADE_SIZE", 0.1))  # Increased default to 0.1 SOL
        self.cooldown = int(os.getenv("COOLDOWN", 300))  # Reduced cooldown to 5 minutes
        self.min_sell_amount = float(os.getenv("MIN_SELL_AMOUNT", 15))
        self.sell_percentage = float(os.getenv("SELL_PERCENTAGE", 0.9))  # Sell 90% by default
        self.sell_threshold = float(os.getenv("SELL_THRESHOLD", 1.025))  # 1% above average
        self.trailing_stop_pct = float(os.getenv("TRAILING_STOP_PCT", 0.95))  # 5% trailing stop
        self.trailing_peak = 0.0

    async def get_token_price(self):
        """Get token price via Jupiter API swap quote"""
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "inputMint": SOL_MINT,
                    "outputMint": TARGET_MINT,
                    "amount": int(self.base_trade_size * 1e9),
                    "slippageBps": 200
                }
                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200: return None
                    quote = await resp.json()
                    return (float(quote["outAmount"]) / (10 ** TOKEN_DECIMALS)) / self.base_trade_size
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        """Get balances with optimized fee buffer"""
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return (resp.value / 1e9) * 0.97  # 3% fee buffer
            else:
                opts = TokenAccountOpts(mint=Pubkey.from_string(mint))
                resp = await self.client.get_token_accounts_by_owner(
                    self.keypair.pubkey(),
                    opts,
                    commitment=Confirmed
                )
                return sum(account.account.lamports / (10 ** TOKEN_DECIMALS) for account in resp.value)
        except Exception as e:
            logging.error(f"Balance check failed: {str(e)}")
            return 0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        """Enhanced transaction logic with retry mechanism"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    blockhash_resp = await self.client.get_latest_blockhash()
                    latest_blockhash = blockhash_resp.value

                    current_balance = await self.get_balance(input_mint)
                    actual_amount = min(amount, current_balance)

                    params = {
                        "inputMint": input_mint,
                        "outputMint": output_mint,
                        "amount": int(actual_amount * 1e9) if input_mint == SOL_MINT else int(actual_amount * (10 ** TOKEN_DECIMALS)),
                        "slippageBps": 200,
                        "config": json.dumps({"priorityFee": {"autoMultiplier": 5}})
                    }

                    async with session.get(f"{self.base_url}/quote", params=params) as resp:
                        if resp.status != 200: continue
                        quote = await resp.json()

                    payload = {
                        "quoteResponse": quote,
                        "userPublicKey": self.wallet,
                        "wrapUnwrapSOL": True,
                        "dynamicComputeUnitLimit": True
                    }
                    async with session.post(f"{self.base_url}/swap", json=payload) as resp:
                        if resp.status != 200: continue
                        swap_data = await resp.json()

                    tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                    unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)

                    message_bytes = to_bytes_versioned(unsigned_tx.message)
                    secret_key_bytes = base58.b58decode(self.base58_private_key)[:32]
                    signing_key = SigningKey(secret_key_bytes)
                    signature_bytes = signing_key.sign(message_bytes).signature
                    signature = Signature(signature_bytes)
                    signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])

                    tx_opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                    txid = await self.client.send_raw_transaction(bytes(signed_tx), opts=tx_opts)
                    await self.client.confirm_transaction(
                        txid.value,
                        latest_blockhash.blockhash,
                        commitment=Confirmed
                    )

                    logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value}")
                    return True
            except Exception as e:
                logging.error(f"❌ Trade failed (attempt {attempt+1}/{max_retries}): {str(e)}")
                await asyncio.sleep(1)
        return False

    async def trading_strategy(self):
        """Advanced trading strategy with position scaling"""
        while True:
            try:
                current_time = datetime.now()
                time_since_last = (current_time - self.last_trade).seconds

                if time_since_last < self.cooldown:
                    await asyncio.sleep(30)
                    continue

                price = await self.get_token_price()
                if price is None:
                    await asyncio.sleep(10)
                    continue

                # Update price history and indicators
                self.price_history = (self.price_history + [price])[-30:]
                avg_price = sum(self.price_history)/len(self.price_history) if self.price_history else 0

                # Update trailing peak
                if price > self.trailing_peak:
                    self.trailing_peak = price
                elif price < self.trailing_peak * self.trailing_stop_pct:
                    logging.info(f"🚨 Trailing stop triggered at {price:.2f}")
                    self.trailing_peak = 0.0  # Reset trailing peak

                # Get balances
                sol_balance = await self.get_balance(SOL_MINT)
                token_balance = await self.get_balance(TARGET_MINT)

                logging.info(
                    f"{TOKEN_SYMBOL}: {price:,.2f} | Avg: {avg_price:,.2f} | "
                    f"SOL: {sol_balance:.4f} | {TOKEN_SYMBOL}: {token_balance:,.0f}"
                )

                # Dynamic position scaling
                position_size = min(self.base_trade_size * (1 + (avg_price - price)/avg_price), sol_balance)

                # Buy logic (2% below average with volatility scaling)
                if sol_balance >= position_size and price < avg_price * 0.98:
                    logging.info(f"🚀 Buying {TOKEN_SYMBOL} ({position_size:.4f} SOL)...")
                    if await self.execute_trade(SOL_MINT, TARGET_MINT, position_size):
                        self.last_trade = current_time
                        self.trailing_peak = price  # Reset trailing stop on new position

                # Enhanced sell logic
                sell_conditions = [
                    price > avg_price * self.sell_threshold,
                    price < self.trailing_peak * self.trailing_stop_pct
                ]
                
                if token_balance > self.min_sell_amount and any(sell_conditions):
                    # Calculate sell amount
                    sell_amount = token_balance * self.sell_percentage
                    remaining_balance = token_balance - sell_amount
                    
                    # Ensure minimum kept balance
                    if remaining_balance < (self.min_sell_amount * 0.1):  # Keep at least 10% of min sell amount
                        sell_amount = token_balance - (self.min_sell_amount * 0.1)
                    
                    if sell_amount >= self.min_sell_amount:
                        logging.info(f"💰 Selling {sell_amount:,.0f} {TOKEN_SYMBOL} ({(sell_amount/token_balance)*100:.0f}%)...")
                        if await self.execute_trade(TARGET_MINT, SOL_MINT, sell_amount):
                            self.last_trade = current_time
                            self.trailing_peak = 0.0  # Reset trailing stop after sale
                    else:
                        logging.info(f"🟡 Potential sale too small: {sell_amount:,.0f} {TOKEN_SYMBOL}")

                await asyncio.sleep(15)  # Faster polling for better responsiveness

            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(60)

async def main():
    trader = TokenTrader()
    await trader.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
