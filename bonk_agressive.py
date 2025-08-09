import os
import logging
import asyncio
import aiohttp
from datetime import datetime
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

# === AGGRESSIVE TRADING CONFIG ===
SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
load_dotenv()
TARGET_MINT = Pubkey.from_string(os.getenv("TARGET_MINT", "GPW7cF8dbvpJGgnFEhtMgf1fbA8A3ZTJYiR74CsSiKNV"))
TOKEN_DECIMALS = 6
JUPITER_API_URL = "https://lite-api.jup.ag/swap/v1"
MAX_RETRIES = 5
BASE_TRADE_SIZE = 0.1  # 0.1 SOL per trade
SLIPPAGE_BPS = 800  # 8% slippage
# === END CONFIG ===

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("solana_bot.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ],
    force=True
)

class AggressiveTrader:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.keypair = Keypair.from_base58_string(os.getenv("PRIVATE_KEY"))
        self.wallet = self.keypair.pubkey()  # Pubkey object
        self.base_url = JUPITER_API_URL
        self.last_trade = datetime.min
        self.consecutive_fails = 0

    async def get_aggressive_quote(self):
        """Get quote with aggressive parameters"""
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "inputMint": str(SOL_MINT),
                    "outputMint": str(TARGET_MINT),
                    "amount": str(int(BASE_TRADE_SIZE * 1e9)),
                    "slippageBps": str(SLIPPAGE_BPS),
                    "restrictIntermediateTokens": "false",
                    "swapMode": "ExactIn",
                    "maxAccounts": "64"
                }
                async with session.get(
                    f"{self.base_url}/quote",
                    params=params
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()
        except Exception as e:
            logging.error(f"Quote failed: {str(e)}")
            return None

    async def execute_aggressive_swap(self):
        """Execute trade with aggressive retry logic"""
        for attempt in range(MAX_RETRIES):
            try:
                quote = await self.get_aggressive_quote()
                if not quote or "data" not in quote:
                    continue

                async with aiohttp.ClientSession() as session:
                    swap_payload = {
                        "route": quote["data"][0],
                        "userPublicKey": str(self.wallet),  # Convert to string
                        "wrapUnwrapSOL": "true",
                        "asLegacyTransaction": "true",
                        "priorityFee": {
                            "autoMultiplier": min(attempt + 2, 5)
                        }
                    }
                    async with session.post(
                        f"{self.base_url}/swap",
                        json=swap_payload
                    ) as resp:
                        if resp.status != 200:
                            continue
                        swap_data = await resp.json()
                        tx = swap_data["swapTransaction"]
                        txid = await self.client.send_raw_transaction(bytes.fromhex(tx))
                        await self.client.confirm_transaction(txid.value)
                        logging.info(f"🔥 Aggressive entry: https://solscan.io/tx/{txid.value}")
                        self.consecutive_fails = 0
                        return True
            except Exception as e:
                logging.error(f"Attempt {attempt+1} failed: {str(e)}")
                await asyncio.sleep(0.5 * (attempt + 1))
        
        self.consecutive_fails += 1
        if self.consecutive_fails >= 3:
            logging.critical("Critical failure - check configuration")
            raise SystemExit
        return False

    async def monitor_and_trade(self):
        """Aggressive trading strategy"""
        while True:
            try:
                balance = await self.client.get_balance(self.wallet)
                sol_balance = balance.value / 1e9
                
                if sol_balance > BASE_TRADE_SIZE + 0.05:
                    logging.info("🚀 Launching aggressive trade...")
                    if await self.execute_aggressive_swap():
                        self.last_trade = datetime.now()
                elif sol_balance < 0.1:
                    logging.warning("⚠️ Low SOL balance - refill required")
                    
                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(5)

async def main():
    trader = AggressiveTrader()
    await trader.monitor_and_trade()

if __name__ == "__main__":
    logging.info("=== AGGRESSIVE TRADER ACTIVATED ===")
    asyncio.run(main())
