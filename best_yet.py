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
        """Get BONK price via swap quotes (your working method)"""
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "inputMint": SOL_MINT,
                    "outputMint": BONK_MINT,
                    "amount": 1_000_000,  # 0.001 SOL
                    "slippageBps": 50
                }
                
                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200:
                        return None
                    quote = await resp.json()
                
                # Price calculation that worked for your buys
                return (float(quote["outAmount"]) / 1e5) / 0.001  # BONK per SOL
                
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        """Your working balance check with proper token handling"""
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return resp.value / 1e9
            else:
                # Working BONK balance check from your successful runs
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
        """Your proven transaction execution code"""
        try:
            async with aiohttp.ClientSession() as session:
                # Successful blockhash retrieval
                latest_blockhash = await self.client.get_latest_blockhash()

                # Working swap parameters from your successful buys
                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(amount * 1e9) if input_mint == SOL_MINT else int(amount),
                    "slippageBps": 200,
                    "config": json.dumps({"priorityFee": {"autoMultiplier": 5}})
                }

                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    quote = await resp.json()

                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "dynamicComputeUnitLimit": True
                }
                
                async with session.post(f"{self.base_url}/swap", json=payload) as resp:
                    swap_data = await resp.json()

                # Your working transaction processing
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)

                # Your successful signing flow
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                secret_key_bytes = base58.b58decode(self.base58_private_key)[:32]
                signing_key = SigningKey(secret_key_bytes)
                signature_bytes = signing_key.sign(message_bytes).signature
                signature = Signature(signature_bytes)
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])

                # Your working transaction submission
                tx_opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=5)
                txid = await self.client.send_raw_transaction(bytes(signed_tx), opts=tx_opts)
                
                # Your confirmation logic that worked
                await self.client.confirm_transaction(txid.value, latest_blockhash.last_valid_block_height)
                logging.info(f"✅ Trade successful! TX: https://solscan.io/tx/{txid.value}")
                return True

        except Exception as e:
            logging.error(f"❌ Trade failed: {str(e)}")
            return False

    async def trading_strategy(self):
        """Your trading logic with working parameters"""
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

                # Maintain your successful price tracking
                self.price_history = (self.price_history + [price])[-30:]
                avg_price = sum(self.price_history) / len(self.price_history) if self.price_history else 0

                # Use your working balance checks
                sol_balance = await self.get_balance(SOL_MINT)
                bonk_balance = await self.get_balance(BONK_MINT)

                logging.info(f"BONK Price: {price:,.2f} per SOL | Avg: {avg_price:,.2f}")

                # Your successful trading signals
                if sol_balance >= self.trade_size and price < avg_price * 0.99:
                    logging.info("🚀 Buying BONK...")
                    if await self.execute_trade(SOL_MINT, BONK_MINT, self.trade_size):
                        self.last_trade = current_time

                elif bonk_balance > 0 and price > avg_price * 1.01:
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
