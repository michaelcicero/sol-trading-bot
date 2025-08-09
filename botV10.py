import os
import logging
import asyncio
import aiohttp
import json
import base58
import base64
from datetime import datetime
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
        self.price_history = deque(maxlen=30)
        self.session = aiohttp.ClientSession()
        self.last_trade = datetime.min
        self.base_trade_size = float(os.getenv("BASE_TRADE_SIZE", 0.1))
        self.cooldown = int(os.getenv("COOLDOWN", 120))
        self.min_sell_amount = float(os.getenv("MIN_SELL_AMOUNT", 15))
        self.sell_percentage = float(os.getenv("SELL_PERCENTAGE", 0.95))
        self.sell_threshold = float(os.getenv("SELL_THRESHOLD", 1.025))
        self.trailing_stop_pct = float(os.getenv("TRAILING_STOP_PCT", 0.97))
        self.trailing_peak = 0.0

    async def cleanup(self):
        if not self.session.closed:
            await self.session.close()
        await self.client.close()

    async def get_token_price(self):
        try:
            params = {
                "inputMint": SOL_MINT,
                "outputMint": TARGET_MINT,
                "amount": int(self.base_trade_size * 1e9),
                "slippageBps": 200
            }
            async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                if resp.status != 200: return None
                quote = await resp.json()
                return (float(quote["outAmount"]) / (10 ** TOKEN_DECIMALS)) / self.base_trade_size
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return (resp.value / 1e9) * 0.97
            else:
                opts = TokenAccountOpts(mint=Pubkey.from_string(mint))
                resp = await self.client.get_token_accounts_by_owner(
                    self.keypair.pubkey(), opts, commitment=Confirmed)
                return sum(account.account.lamports / (10 ** TOKEN_DECIMALS) for account in resp.value)
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
                    if resp.status != 200: continue
                    quote = await resp.json()
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True
                }
                async with self.session.post(f"{self.base_url}/swap", json=payload) as resp:
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
                
                self.price_history.append(price)
                avg_price = sum(self.price_history)/len(self.price_history) if self.price_history else 0
                self.trailing_peak = max(self.trailing_peak, price)
                
                sol_balance, token_balance = await asyncio.gather(
                    self.get_balance(SOL_MINT),
                    self.get_balance(TARGET_MINT)
                )
                
                logging.info(f"{TOKEN_SYMBOL}: {price:.2f} | Avg: {avg_price:.2f} | SOL: {sol_balance:.2f} | Tokens: {token_balance:.0f}")
                
                if sol_balance >= self.base_trade_size and price < avg_price * 0.98:
                    logging.info(f"🚀 Buying {TOKEN_SYMBOL}...")
                    if await self.execute_trade(SOL_MINT, TARGET_MINT, self.base_trade_size):
                        self.last_trade = current_time
                        self.trailing_peak = price
                
                if token_balance > self.min_sell_amount and (price > avg_price * self.sell_threshold or price < self.trailing_peak * self.trailing_stop_pct):
                    sell_amount = token_balance * self.sell_percentage
                    logging.info(f"💰 Selling {sell_amount:.0f} {TOKEN_SYMBOL}...")
                    if await self.execute_trade(TARGET_MINT, SOL_MINT, sell_amount):
                        self.last_trade = current_time
                        self.trailing_peak = 0.0
                
                await asyncio.sleep(15)
            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(60)

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
