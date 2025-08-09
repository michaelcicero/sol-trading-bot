import os
import logging
import asyncio
import aiohttp
import json
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from logging.handlers import RotatingFileHandler

# Load environment variables
load_dotenv()

# Configure structured JSON logging
log_formatter = logging.Formatter(
    '{"time": "%(asctime)s", "level": "%(levelname)s", "message": %(message)s}'
)

log_file = "trading_bot.json.log"
file_handler = RotatingFileHandler(
    log_file, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)

class TradingBotLogger:
    @staticmethod
    def log_event(event_type, data):
        log_data = {
            "event": event_type,
            "data": data,
            "bot_version": "3.2.0"
        }
        logging.info(json.dumps(log_data))

TOKEN_LIST = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
}

class SolanaTradingBot:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.keypair = self._load_keypair()
        self.wallet_address = str(self.keypair.pubkey())
        self.session = None
        self.price_history = {}

    def _load_keypair(self):
        private_key = os.getenv("PRIVATE_KEY")
        if not private_key:
            raise ValueError("Missing PRIVATE_KEY in .env file")
        return Keypair.from_base58_string(private_key)

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        await self.session.close()
        await self.client.close()

    async def get_balance(self):
        try:
            response = await self.client.get_balance(self.keypair.pubkey())
            return response.value / 1e9
        except Exception as e:
            TradingBotLogger.log_event("balance_error", {"error": str(e)})
            return 0

    async def get_jupiter_price(self, mint_address: str):
        """Get price using Jupiter's Lite API v2 endpoint"""
        try:
            url = f"https://lite-api.jup.ag/price/v2?ids={mint_address}"
            async with self.session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    TradingBotLogger.log_event("price_error", {
                        "mint": mint_address,
                        "error": f"HTTP {resp.status}"
                    })
                    return None
                data = await resp.json()
                price = data["data"].get(mint_address, {}).get("price")
                if price is None:
                    TradingBotLogger.log_event("price_error", {
                        "mint": mint_address,
                        "error": "No price in response"
                    })
                    return None
                return float(price)
        except Exception as e:
            TradingBotLogger.log_event("price_error", {
                "mint": mint_address,
                "error": str(e)
            })
            return None

    async def trading_strategy(self):
        while True:
            try:
                sol_balance = await self.get_balance()
                TradingBotLogger.log_event("balance_update", {"sol_balance": sol_balance})

                for symbol, mint in TOKEN_LIST.items():
                    price = await self.get_jupiter_price(mint)
                    if price:
                        TradingBotLogger.log_event("price_update", {
                            "symbol": symbol,
                            "price": price
                        })
                        # Example: you can add your trading logic here

                await asyncio.sleep(60)

            except Exception as e:
                TradingBotLogger.log_event("strategy_error", {"error": str(e)})
                await asyncio.sleep(60)

async def main():
    async with SolanaTradingBot() as bot:
        await bot.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
