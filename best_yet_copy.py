import os
import logging
import asyncio
import aiohttp
import json
import base64
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAccountOpts, TxOpts
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
            "bot_version": "7.6.1"
        }
        logging.info(json.dumps(log_data))

TOKEN_LIST = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "NOM": "2MDr15dTn6km3NWusFcnZyhq3vWpYDg7vWprghpzbonk",
    "House": "DitHyRMQiSDhn5cnKMJV2CDDt6sVct96YrECiM49pump"
}

class SolanaTradingBot:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.keypair = self._load_keypair()
        self.wallet_address = str(self.keypair.pubkey())
        self.session = None
        self.price_history = {}
        self.positions = {}
        self.last_trade = datetime.min
        self.trade_size = 0.01
        self.cooldown = 300

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
            balance = response.value / 1e9
            TradingBotLogger.log_event("balance_check", {"sol_balance": balance})
            return balance
        except Exception as e:
            TradingBotLogger.log_event("balance_error", {"error": str(e)})
            return 0

    async def get_token_balance(self, mint_address: str):
        try:
            opts = TokenAccountOpts(
                mint=Pubkey.from_string(mint_address),
                encoding="base64",
                program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            )
            response = await self.client.get_token_accounts_by_owner(
                self.keypair.pubkey(),
                opts,
                commitment=Confirmed
            )
            
            if not response.value:
                return 0.0

            total = 0.0
            for account in response.value:
                try:
                    base64_str = account.account.data
                    if isinstance(base64_str, bytes):
                        base64_str = base64_str.decode('utf-8')
                    missing_padding = len(base64_str) % 4
                    if missing_padding:
                        base64_str += '=' * (4 - missing_padding)
                    decoded_data = json.loads(base64.b64decode(base64_str))
                    token_amount = decoded_data['parsed']['info']['tokenAmount']
                    total += float(token_amount['uiAmount'])
                except Exception as e:
                    TradingBotLogger.log_event("token_parse_error", {
                        "mint": mint_address,
                        "error": str(e),
                        "raw_data": str(base64_str)[:100] + "..."
                    })
            return total
        except Exception as e:
            TradingBotLogger.log_event("token_balance_error", {
                "mint": mint_address,
                "error": str(e)
            })
            return 0.0

    async def get_jupiter_price(self, mint_address: str):
        try:
            url = f"https://lite-api.jup.ag/price/v2?ids={mint_address}"
            async with self.session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return float(data["data"][mint_address]["price"])
        except Exception as e:
            TradingBotLogger.log_event("price_error", {
                "mint": mint_address,
                "error": str(e)
            })
            return None

    async def execute_swap(self, input_mint: str, output_mint: str, amount: float):
        try:
            quote_url = "https://quote-api.jup.ag/v6/quote"
            swap_url = "https://quote-api.jup.ag/v6/swap"

            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": int(amount * 1e9),
                "slippageBps": 100
            }
            async with self.session.get(quote_url, params=params) as resp:
                quote = await resp.json()

            payload = {
                "quoteResponse": quote,
                "userPublicKey": self.wallet_address,
                "wrapUnwrapSOL": True
            }
            async with self.session.post(swap_url, json=payload) as resp:
                swap_data = await resp.json()

            # Decode base64 transaction
            transaction_bytes = base64.b64decode(swap_data['swapTransaction'])
            unsigned_tx = VersionedTransaction.from_bytes(transaction_bytes)

            # === CRITICAL FIX START ===
            message_bytes = to_bytes_versioned(unsigned_tx.message)
            message_hash = hashlib.sha256(message_bytes).digest()
            signature = self.keypair.sign_message(message_hash)
            signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])
            # === CRITICAL FIX END ===

            # Send and confirm (no extra keyword args!)
            txid = await self.client.send_raw_transaction(bytes(signed_tx))
            await self.client.confirm_transaction(txid.value, commitment=Confirmed)

            TradingBotLogger.log_event("swap_executed", {
                "input": input_mint,
                "output": output_mint,
                "amount": amount,
                "txid": str(txid.value)
            })
            return True
        except Exception as e:
            TradingBotLogger.log_event("swap_failed", {"error": str(e)})
            return False

    async def trading_strategy(self):
        while True:
            try:
                current_time = datetime.now()
                sol_balance = await self.get_balance()
                TradingBotLogger.log_event("balance_update", {
                    "sol_balance": sol_balance,
                    "timestamp": current_time.isoformat()
                })

                for symbol, mint in TOKEN_LIST.items():
                    try:
                        price = await self.get_jupiter_price(mint)
                        balance = await self.get_token_balance(mint)
                        TradingBotLogger.log_event("token_status", {
                            "symbol": symbol,
                            "balance": balance,
                            "price": price,
                            "value": balance * price if price else 0
                        })

                        if symbol not in self.price_history:
                            self.price_history[symbol] = []
                        self.price_history[symbol].append(price or 0)
                        self.price_history[symbol] = self.price_history[symbol][-30:]

                        if price and len(self.price_history[symbol]) >= 30:
                            avg_price = sum(self.price_history[symbol]) / 30

                            # Buy signal
                            if (price < avg_price * 0.995 and 
                                sol_balance >= self.trade_size and
                                (current_time - self.last_trade).seconds > self.cooldown):
                                
                                if await self.execute_swap(
                                    "So11111111111111111111111111111111111111112",
                                    mint,
                                    self.trade_size
                                ):
                                    self.last_trade = current_time
                                    TradingBotLogger.log_event("position_opened", {
                                        "symbol": symbol,
                                        "amount": self.trade_size,
                                        "price": price
                                    })

                            # Sell signal
                            elif balance > 0 and price > avg_price * 1.02:
                                if await self.execute_swap(
                                    mint,
                                    "So11111111111111111111111111111111111111112",
                                    balance
                                ):
                                    TradingBotLogger.log_event("position_closed", {
                                        "symbol": symbol,
                                        "amount": balance,
                                        "price": price
                                    })

                    except Exception as e:
                        TradingBotLogger.log_event("token_error", {
                            "symbol": symbol,
                            "error": str(e)
                        })

                await asyncio.sleep(30)

            except Exception as e:
                TradingBotLogger.log_event("strategy_error", {"error": str(e)})
                await asyncio.sleep(60)

async def main():
    async with SolanaTradingBot() as bot:
        await bot.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
