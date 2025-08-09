import os
import logging
import asyncio
import aiohttp
import json
import base64
import base58
from datetime import datetime, timedelta
from dotenv import load_dotenv
from nacl.signing import SigningKey
from solders.signature import Signature
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
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
        self.trade_size = 0.001  # SOL amount per trade
        self.cooldown = 300  # 5 minutes between trades

    async def get_bonk_price(self):
        """Get BONK price via swap quotes"""
        try:
            async with aiohttp.ClientSession() as session:
                # Get quote for 0.001 SOL -> BONK
                params = {
                    "inputMint": SOL_MINT,
                    "outputMint": BONK_MINT,
                    "amount": 1_000_000,  # 0.001 SOL in lamports
                    "slippageBps": 50
                }
                
                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200:
                        return None
                    quote = await resp.json()
                    
                # Calculate price: BONK per 1 SOL
                bonk_amount = float(quote["outAmount"]) / 1e5  # BONK has 5 decimals
                return bonk_amount / 0.001  # Normalize to 1 SOL
                
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        """Get token balance in wallet"""
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return resp.value / 1e9
            else:
                # BONK balance check
                resp = await self.client.get_token_accounts_by_owner(
                    self.keypair.pubkey(),
                    mint=Pubkey.from_string(BONK_MINT)
                )
                return sum(account.account.lamports / 1e5 for account in resp.value)
        except Exception as e:
            logging.error(f"Balance check failed: {str(e)}")
            return 0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        """Your proven swap logic"""
        try:
            async with aiohttp.ClientSession() as session:
                logging.info("Fetching latest blockhash...")
                latest_blockhash_resp = await self.client.get_latest_blockhash()
                latest_blockhash = latest_blockhash_resp.value

                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(amount * 1e9) if input_mint == SOL_MINT else int(amount),
                    "slippageBps": 200,
                    "config": json.dumps({"priorityFee": {"autoMultiplier": 5}})
                }

                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200:
                        raise Exception(f"Quote API error: {await resp.text()}")
                    quote = await resp.json()

                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "dynamicComputeUnitLimit": True
                }
                
                async with session.post(f"{self.base_url}/swap", json=payload) as resp:
                    if resp.status != 200:
                        raise Exception(f"Swap API error: {await resp.text()}")
                    swap_data = await resp.json()

                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)

                # Your working signing flow
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                secret_key_bytes = base58.b58decode(self.base58_private_key)[:32]
                signing_key = SigningKey(secret_key_bytes)
                signature_bytes = signing_key.sign(message_bytes).signature
                signature = Signature(signature_bytes)
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])

                tx_opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=5)
                txid = await self.client.send_raw_transaction(bytes(signed_tx), opts=tx_opts)

                # Confirmation logic
                retries = 5
                while retries > 0:
                    tx_status = await self.client.get_transaction(txid.value)
                    if tx_status is not None:
                        break
                    retries -= 1
                    await asyncio.sleep(3)

                await self.client.confirm_transaction(txid.value, latest_blockhash.last_valid_block_height)
                logging.info(f"✅ Trade successful! TX: https://solscan.io/tx/{txid.value}")
                return True

        except Exception as e:
            logging.error(f"❌ Trade failed: {str(e)}")
            return False

    async def trading_strategy(self):
        """Trading strategy using reliable price data"""
        while True:
            try:
                current_time = datetime.now()
                if (current_time - self.last_trade).seconds < self.cooldown:
                    await asyncio.sleep(30)
                    continue

                price = await self.get_bonk_price()
                if price is None:
                    await asyncio.sleep(10)
                    continue

                # Update price history (30-period average)
                self.price_history = (self.price_history + [price])[-30:]
                avg_price = sum(self.price_history) / len(self.price_history) if self.price_history else 0

                sol_balance = await self.get_balance(SOL_MINT)
                bonk_balance = await self.get_balance(BONK_MINT)

                logging.info(f"BONK Price: {price:.2f} per SOL | Avg: {avg_price:.2f}")

                # Buy signal: Price 2% below average
                if sol_balance >= self.trade_size and price < avg_price * 0.98:
                    logging.info("🚀 Buying BONK...")
                    if await self.execute_trade(SOL_MINT, BONK_MINT, self.trade_size):
                        self.last_trade = current_time

                # Sell signal: Price 3% above average
                elif bonk_balance > 0 and price > avg_price * 1.03:
                    logging.info("💰 Selling BONK...")
                    if await self.execute_trade(BONK_MINT, SOL_MINT, bonk_balance):
                        self.last_trade = current_time

                await asyncio.sleep(30)

            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(60)

async def main():
    trader = BonkTrader()
    await trader.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
