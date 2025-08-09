import os
import logging
import asyncio
import aiohttp
import json
import base58
import base64  # Critical missing import added
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

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bonk_trader.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
SOL_MINT = "So11111111111111111111111111111111111111112"

class BonkTrader:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.base58_private_key = os.getenv("PRIVATE_KEY")
        self.keypair = Keypair.from_base58_string(self.base58_private_key)
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"
        self.price_history = []
        self.last_trade = datetime.min
        self.base_trade_size = 0.001  # SOL
        self.cooldown = 600  # 10 minutes

    async def get_bonk_price(self):
        """Get BONK price via Jupiter API swap quote"""
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "inputMint": SOL_MINT,
                    "outputMint": BONK_MINT,
                    "amount": 1000000,  # 0.001 SOL
                    "slippageBps": 200
                }
                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200: return None
                    quote = await resp.json()
                    return (float(quote["outAmount"]) / 1e5) / 0.001  # BONK per SOL
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        """Get balances with fee buffer"""
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return (resp.value / 1e9) * 0.999  # Reserve 1% for fees
            else:
                opts = TokenAccountOpts(mint=Pubkey.from_string(BONK_MINT))
                resp = await self.client.get_token_accounts_by_owner(
                    self.keypair.pubkey(),
                    opts,
                    commitment=Confirmed
                )
                return sum(account.account.lamports / 1e5 for account in resp.value)
        except Exception as e:
            logging.error(f"Balance check failed: {str(e)}")
            return 0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        """Fixed transaction logic with proper error handling"""
        try:
            async with aiohttp.ClientSession() as session:
                # Get fresh blockhash
                latest_blockhash_resp = await self.client.get_latest_blockhash()
                latest_blockhash = latest_blockhash_resp.value

                # Dynamic trade sizing
                current_balance = await self.get_balance(input_mint)
                actual_amount = min(amount, current_balance)

                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(actual_amount * 1e9) if input_mint == SOL_MINT else int(actual_amount),
                    "slippageBps": 200,
                    "config": json.dumps({"priorityFee": {"autoMultiplier": 5}})
                }

                # Get swap quote
                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200: return False
                    quote = await resp.json()

                # Get swap transaction
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "dynamicComputeUnitLimit": True
                }
                async with session.post(f"{self.base_url}/swap", json=payload) as resp:
                    if resp.status != 200: return False
                    swap_data = await resp.json()

                # Process transaction
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)

                # Sign transaction
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                secret_key_bytes = base58.b58decode(self.base58_private_key)[:32]
                signing_key = SigningKey(secret_key_bytes)
                signature_bytes = signing_key.sign(message_bytes).signature
                signature = Signature(signature_bytes)
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])

                # Send transaction (CRITICAL FIX)
                tx_opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=5)
                txid = await self.client.send_raw_transaction(bytes(signed_tx), opts=tx_opts)

                # Confirm with correct blockhash reference
                await self.client.confirm_transaction(
                    txid.value,
                    latest_blockhash.blockhash,  # FIXED HERE
                    commitment=Confirmed
                )
                
                logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value}")
                return True
        except Exception as e:
            logging.error(f"❌ Trade failed: {str(e)}")
            return False

    async def trading_strategy(self):
        """Optimized trading strategy"""
        while True:
            try:
                current_time = datetime.now()
                time_since_last = (current_time - self.last_trade).seconds

                if time_since_last < self.cooldown:
                    await asyncio.sleep(30)
                    continue

                price = await self.get_bonk_price()
                if price is None:
                    await asyncio.sleep(10)
                    continue

                # Extended price history window
                self.price_history = (self.price_history + [price])[-60:]
                avg_price = sum(self.price_history)/len(self.price_history) if self.price_history else 0

                sol_balance = await self.get_balance(SOL_MINT)
                bonk_balance = await self.get_balance(BONK_MINT)

                logging.info(
                    f"BONK: {price:,.2f} | Avg: {avg_price:,.2f} | "
                    f"SOL: {sol_balance:.4f} | BONK: {bonk_balance:,.0f}"
                )

                # Buy signal (1% below average)
                if sol_balance >= self.base_trade_size and price < avg_price * 0.99:
                    logging.info(f"🚀 Buying BONK ({self.base_trade_size} SOL)...")
                    if await self.execute_trade(SOL_MINT, BONK_MINT, self.base_trade_size):
                        self.last_trade = current_time

                # Sell signal (2% above average)
                elif bonk_balance > 1000 and price > avg_price * 1.02:
                    logging.info(f"💰 Selling {bonk_balance:,.0f} BONK...")
                    if await self.execute_trade(BONK_MINT, SOL_MINT, bonk_balance):
                        self.last_trade = current_time

                await asyncio.sleep(45)

            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(60)

async def main():
    trader = BonkTrader()
    await trader.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
