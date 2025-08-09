import os
import logging
import asyncio
import aiohttp
import json
import base58
import base64
import gzip
import zstandard
import brotli
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
        logging.FileHandler("solana_bot.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

SOL_MINT = "So11111111111111111111111111111111111111112"

class TokenTrader:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.keypair = Keypair.from_base58_string(os.getenv("PRIVATE_KEY"))
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"
        
        # Configuration from .env
        self.target_mint = os.getenv("TARGET_MINT")
        self.token_decimals = int(os.getenv("TOKEN_DECIMALS", 5))
        self.token_symbol = os.getenv("TOKEN_SYMBOL", "POPCAT")
        self.base_trade_size = float(os.getenv("BASE_TRADE_SIZE", 0.05))
        self.cooldown = int(os.getenv("COOLDOWN", 120))
        self.min_sell_amount = float(os.getenv("MIN_SELL_AMOUNT", 15))
        self.sell_percentage = float(os.getenv("SELL_PERCENTAGE", 0.95))
        self.sell_threshold = float(os.getenv("SELL_THRESHOLD", 1.025))
        self.trailing_stop_pct = float(os.getenv("TRAILING_STOP_PCT", 0.97))
        self.min_sol_balance = float(os.getenv("MIN_SOL_BALANCE", 0.1))
        
        self.price_history = []
        self.last_trade = datetime.min
        self.trailing_peak = 0.0
        self.session = aiohttp.ClientSession()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    async def get_token_price(self):
        """Fetch current token price from Jupiter API"""
        try:
            params = {
                "inputMint": SOL_MINT,
                "outputMint": self.target_mint,
                "amount": int(self.base_trade_size * 1e9),
                "slippageBps": 50
            }
            async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                if resp.status != 200:
                    return None
                quote = await resp.json()
                return (float(quote["outAmount"]) / (10 ** self.token_decimals)) / self.base_trade_size
        except Exception as e:
            logging.error(f"Price check failed: {str(e)}")
            return None

    async def get_balance(self, mint: str):
        """Get token balances with safety buffer"""
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return (resp.value / 1e9) * 0.95  # 5% buffer
            else:
                opts = TokenAccountOpts(mint=Pubkey.from_string(mint))
                resp = await self.client.get_token_accounts_by_owner(
                    self.keypair.pubkey(),
                    opts,
                    commitment=Confirmed
                )
                return sum(account.account.lamports / (10 ** self.token_decimals)
                          for account in resp.value)
        except Exception as e:
            logging.error(f"Balance check failed: {str(e)}")
            return 0

    def correct_base64_padding(self, data: str) -> str:
        """Dynamically correct base64 padding"""
        missing_padding = len(data) % 4
        if missing_padding:
            data += '=' * (4 - missing_padding)
        return data

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        """Execute trade with enhanced compression handling"""
        for attempt in range(3):
            try:
                # Get fresh blockhash
                blockhash_resp = await self.client.get_latest_blockhash(commitment=Confirmed)
                blockhash = blockhash_resp.value.blockhash
                last_valid_height = blockhash_resp.value.last_valid_block_height

                # Validate balance
                current_balance = await self.get_balance(input_mint)
                trade_amount = min(amount, current_balance * 0.98)
                
                if trade_amount <= 0:
                    logging.warning("Insufficient balance for trade")
                    return False

                # Build transaction parameters
                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(trade_amount * (1e9 if input_mint == SOL_MINT else 10 ** self.token_decimals)),
                    "slippageBps": 50,
                    "config": json.dumps({"priorityFee": {"autoMultiplier": 10}})
                }

                # Get swap quote
                async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200:
                        continue
                    quote = await resp.json()

                # Build transaction payload
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "dynamicComputeUnitLimit": True
                }
                
                # Process swap transaction
                async with self.session.post(f"{self.base_url}/swap", json=payload) as resp:
                    if resp.status != 200:
                        continue
                    swap_data = await resp.json()

                # Handle base64 decoding with dynamic padding correction
                tx_b64 = self.correct_base64_padding(swap_data["swapTransaction"])
                tx_bytes = base64.urlsafe_b64decode(tx_b64)

                # Handle compression formats
                if tx_bytes.startswith(b'\x28\xb5\x2f\xfd'):  # Zstandard
                    dctx = zstandard.ZstdDecompressor()
                    tx_bytes = dctx.decompress(tx_bytes)
                elif tx_bytes.startswith(b'\x1f\x8b'):  # Gzip
                    tx_bytes = gzip.decompress(tx_bytes)
                elif tx_bytes.startswith(b'\xce\x2c\x4a\x2d'):  # Brotli
                    tx_bytes = brotli.decompress(tx_bytes)
                else:
                    logging.info("Processing uncompressed transaction")

                # Deserialize transaction
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
                
                # Sign transaction
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                secret_key = SigningKey(base58.b58decode(self.keypair.__bytes__())[:32])
                signature = secret_key.sign(message_bytes).signature
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [Signature(signature)])

                # Send and confirm transaction
                tx_opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                txid = await self.client.send_raw_transaction(
                    bytes(signed_tx),
                    opts=tx_opts
                )
                
                await self.client.confirm_transaction(
                    txid.value,
                    last_valid_height,
                    commitment=Confirmed
                )
                
                logging.info(f"✅ Trade success! TX: https://solscan.io/tx/{txid.value}")
                return True

            except Exception as e:
                logging.error(f"🔥 Trade failed (attempt {attempt+1}): {str(e)}")
                await asyncio.sleep(0.5 * (attempt + 1))
        return False

    async def trading_strategy(self):
        """Core trading logic"""
        while True:
            try:
                current_time = datetime.now()
                
                # Check cooldown period
                if (current_time - self.last_trade).total_seconds() < self.cooldown:
                    await asyncio.sleep(10)
                    continue

                # Get market data
                price = await self.get_token_price()
                if price is None:
                    await asyncio.sleep(5)
                    continue

                # Update price history
                self.price_history = (self.price_history + [price])[-5:]
                avg_price = sum(self.price_history)/len(self.price_history) if self.price_history else 0

                # Update trailing peak
                if price > self.trailing_peak:
                    self.trailing_peak = price
                elif price < self.trailing_peak * self.trailing_stop_pct:
                    logging.info(f"🚨 Trailing stop triggered at {price:.2f}")
                    self.trailing_peak = 0.0

                # Get balances
                sol_balance = await self.get_balance(SOL_MINT)
                token_balance = await self.get_balance(self.target_mint)

                logging.info(
                    f"{self.token_symbol}: {price:,.2f} | Avg: {avg_price:,.2f} | "
                    f"SOL: {sol_balance:.4f} | Balance: {token_balance:,.0f}"
                )

                # Buy logic (1% below average)
                if sol_balance >= self.base_trade_size and price < avg_price * 0.9999:
                    logging.info(f"🚀 Buying {self.base_trade_size:.4f} SOL")
                    if await self.execute_trade(SOL_MINT, self.target_mint, self.base_trade_size):
                        self.last_trade = current_time

                # Sell logic
                sell_conditions = [
                    price > avg_price * self.sell_threshold,
                    price < self.trailing_peak * self.trailing_stop_pct
                ]
                
                if token_balance > self.min_sell_amount and any(sell_conditions):
                    sell_amount = token_balance * self.sell_percentage
                    remaining = token_balance - sell_amount
                    
                    if remaining < self.min_sell_amount * 0.1:
                        sell_amount = token_balance - self.min_sell_amount * 0.1
                    
                    if sell_amount >= self.min_sell_amount:
                        logging.info(f"💰 Selling {sell_amount:,.0f}")
                        if await self.execute_trade(self.target_mint, SOL_MINT, sell_amount):
                            self.last_trade = current_time

                await asyncio.sleep(15)

            except Exception as e:
                logging.error(f"💣 Critical error: {str(e)}")
                await asyncio.sleep(30)

async def main():
    async with TokenTrader() as trader:
        await trader.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
