import os
import logging
import asyncio
import aiohttp
import json
import base58
import base64
from datetime import datetime
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

load_dotenv()

# Fixed logging configuration with proper handler closure
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(symbol)s] %(message)s',
    handlers=[
        logging.FileHandler("multi_coin_trader.log"),
        logging.StreamHandler()
    ]
)

SOL_MINT = "So11111111111111111111111111111111111111112"

class MultiCoinTrader:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.keypair = Keypair.from_base58_string(os.getenv("PRIVATE_KEY"))
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"
        
        # Load token configurations with SOL explicitly included
        self.tokens = json.loads(os.getenv("TOKENS"))
        self.token_data = {t['mint']: t for t in self.tokens}
        
        if SOL_MINT not in self.token_data:  # Ensure SOL configuration exists
            self.token_data[SOL_MINT] = {
                'symbol': 'SOL',
                'decimals': 9,
                'base_size': 0.01,
                'min_sell_amount': 0.01
            }
            
        self.cooldown = int(os.getenv("COOLDOWN", 300))
        self.last_trade = {t['mint']: datetime.min for t in self.tokens}
        self.price_history = {t['mint']: [] for t in self.tokens}

    async def get_token_price(self, token_mint: str):
        """Get token price via Jupiter API"""
        try:
            token_config = self.token_data[token_mint]
            async with aiohttp.ClientSession() as session:
                params = {
                    "inputMint": SOL_MINT,
                    "outputMint": token_mint,
                    "amount": int(token_config['base_size'] * 1e9),
                    "slippageBps": 200
                }
                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200: return None
                    quote = await resp.json()
                    price = (float(quote["outAmount"]) / (10 ** token_config['decimals'])) / token_config['base_size']
                    return price
        except Exception as e:
            symbol = self.token_data.get(token_mint, {}).get('symbol', 'UNKNOWN')
            logging.error(f"Price check error: {str(e)}", extra={'symbol': symbol})
            return None

    async def get_balance(self, mint: str):
        """Get balances with improved error handling"""
        try:
            token_config = self.token_data[mint]
            symbol = token_config['symbol']
            
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                balance = (resp.value / 1e9) * 0.95  # 5% fee buffer
            else:
                opts = TokenAccountOpts(mint=Pubkey.from_string(mint))
                resp = await self.client.get_token_accounts_by_owner(
                    self.keypair.pubkey(),
                    opts,
                    commitment=Confirmed
                )
                balance = sum(account.account.lamports / (10 ** token_config['decimals']) 
                            for account in resp.value)
            
            return balance
            
        except Exception as e:
            # Handle SOL balance check separately
            symbol = 'SOL' if mint == SOL_MINT else self.token_data.get(mint, {}).get('symbol', 'UNKNOWN')
            logging.error(f"Balance check failed: {str(e)}", extra={'symbol': symbol})
            return 0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        """Execute trade with proper blockhash handling"""
        try:
            token_config = self.token_data[output_mint if input_mint == SOL_MINT else input_mint]
            symbol = token_config['symbol']
            
            async with aiohttp.ClientSession() as session:
                # Get fresh blockhash
                blockhash_resp = await self.client.get_latest_blockhash()
                latest_blockhash = blockhash_resp.value

                # Enforce 0.01 SOL minimum for buys
                trade_amount = max(amount, 0.01) if input_mint == SOL_MINT else amount
                
                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(trade_amount * (1e9 if input_mint == SOL_MINT else 10 ** token_config['decimals'])),
                    "slippageBps": 200,
                    "config": json.dumps({"priorityFee": {"autoMultiplier": 5}})
                }

                # Get swap quote
                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200: return False
                    quote = await resp.json()

                # Build transaction
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "dynamicComputeUnitLimit": True
                }
                
                async with session.post(f"{self.base_url}/swap", json=payload) as resp:
                    if resp.status != 200: return False
                    swap_data = await resp.json()

                # Process and sign transaction
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                secret_key = SigningKey(base58.b58decode(self.keypair.__bytes__())[:32])
                signature = Signature(secret_key.sign(message_bytes).signature)
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])

                # Send transaction
                tx_opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                txid = await self.client.send_raw_transaction(bytes(signed_tx), opts=tx_opts)
                await self.client.confirm_transaction(txid.value, latest_blockhash.blockhash, commitment=Confirmed)
                
                logging.info(f"Trade success! TX: https://solscan.io/tx/{txid.value}", extra={'symbol': symbol})
                return True
                
        except Exception as e:
            logging.error(f"Trade failed: {str(e)}", extra={'symbol': symbol})
            return False

    async def trading_strategy(self):
        """Dual-coin trading strategy"""
        while True:
            try:
                for token in self.tokens:
                    mint = token['mint']
                    symbol = token['symbol']
                    current_time = datetime.now()
                    
                    if (current_time - self.last_trade[mint]).seconds < self.cooldown:
                        continue

                    # Market data
                    price = await self.get_token_price(mint)
                    if price is None: continue
                    
                    # Update price history
                    self.price_history[mint] = (self.price_history[mint] + [price])[-token['window_size']:]
                    avg_price = sum(self.price_history[mint])/len(self.price_history[mint]) if self.price_history[mint] else 0

                    # Balances
                    sol_balance = await self.get_balance(SOL_MINT)
                    token_balance = await self.get_balance(mint)

                    # Logging with symbol context
                    logging.info(f"Price: {price:,.2f} | Avg: {avg_price:,.2f} | SOL: {sol_balance:.4f} | Balance: {token_balance:,.2f}", extra={'symbol': symbol})

                    # Buy logic (1% below average with 0.01 SOL minimum)
                    if sol_balance >= 0.01 and price < avg_price * 0.99:
                        trade_size = max(token['base_size'], 0.01)
                        logging.info(f"Buying {trade_size:.4f} SOL...", extra={'symbol': symbol})
                        if await self.execute_trade(SOL_MINT, mint, trade_size):
                            self.last_trade[mint] = current_time

                    # Sell logic (2% above average)
                    elif token_balance > token['min_sell_amount'] and price > avg_price * 1.02:
                        logging.info(f"Selling {token_balance:,.2f}...", extra={'symbol': symbol})
                        if await self.execute_trade(mint, SOL_MINT, token_balance):
                            self.last_trade[mint] = current_time

                await asyncio.sleep(30)

            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(60)

async def main():
    trader = MultiCoinTrader()
    await trader.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
