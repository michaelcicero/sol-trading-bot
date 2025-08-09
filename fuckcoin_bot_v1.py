import os
import logging
import asyncio
import aiohttp
from datetime import datetime
from dotenv import load_dotenv
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient

# === CONFIGURATION ===
SOL_MINT = "So11111111111111111111111111111111111111112"
load_dotenv()
TARGET_MINT = os.getenv("TARGET_MINT", "GPW7cF8dbvpJGgnFEhtMgf1fbA8A3ZTJYiR74CsSiKNV")
TOKEN_DECIMALS = 6
JUPITER_API_URL = "https://lite-api.jup.ag/swap/v1"
# === END CONFIGURATION ===

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("solana_bot.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ],
    force=True
)

class TokenTrader:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.keypair = Keypair.from_base58_string(os.getenv("PRIVATE_KEY"))
        self.wallet = str(self.keypair.pubkey())
        self.base_url = JUPITER_API_URL
        self.base_trade_size = 0.02  # 0.02 SOL base size

    async def get_token_price(self):
        """Get token price via Jupiter API swap quote"""
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "inputMint": SOL_MINT,
                    "outputMint": TARGET_MINT,
                    "amount": str(int(self.base_trade_size * 1e9)),  # Must be string
                    "slippageBps": "500",  # String per API spec
                    "restrictIntermediateTokens": "true"
                }
                async with session.get(
                    f"{self.base_url}/quote",
                    params=params
                ) as resp:
                    if resp.status != 200:
                        logging.error(f"Price API error: {resp.status}")
                        return None
                    quote = await resp.json()
                    if "data" not in quote or len(quote["data"]) == 0:
                        logging.warning("No swap routes available")
                        return None
                    return (float(quote["data"][0]["outAmount"]) / (10 ** TOKEN_DECIMALS)) / self.base_trade_size
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def execute_trade(self, amount: float):
        """Execute trade with proper parameter formatting"""
        try:
            async with aiohttp.ClientSession() as session:
                # Get quote
                quote_params = {
                    "inputMint": SOL_MINT,
                    "outputMint": TARGET_MINT,
                    "amount": str(int(amount * 1e9)),
                    "slippageBps": "500",
                    "restrictIntermediateTokens": "true"
                }
                async with session.get(f"{self.base_url}/quote", params=quote_params) as resp:
                    if resp.status != 200:
                        logging.error("Failed to get quote")
                        return False
                    quote = await resp.json()
                
                # Execute swap
                swap_payload = {
                    "route": quote["data"][0],
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "asLegacyTransaction": True
                }
                async with session.post(f"{self.base_url}/swap", json=swap_payload) as resp:
                    if resp.status != 200:
                        logging.error("Swap failed")
                        return False
                    swap_data = await resp.json()
                    logging.info(f"✅ Trade successful: {swap_data['swapTransaction']}")
                    return True
        except Exception as e:
            logging.error(f"Trade execution failed: {str(e)}")
            return False

    async def trading_strategy(self):
        """Trading strategy loop"""
        while True:
            try:
                price = await self.get_token_price()
                if price:
                    logging.info(f"FUCK Price: {price:.2f}")
                    # Example trade execution (customize your strategy here)
                    if price < 100000:  # Replace with your buy condition
                        await self.execute_trade(self.base_trade_size)
                await asyncio.sleep(15)
            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(30)

async def main():
    trader = TokenTrader()
    await trader.trading_strategy()

if __name__ == "__main__":
    logging.info("=== BOT STARTED ===")
    asyncio.run(main())
