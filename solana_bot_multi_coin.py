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
        
        self.tokens = json.loads(os.getenv("TOKENS", "[]"))
        self.token_data = {t['mint']: t for t in self.tokens}
        
        self.cooldown = int(os.getenv("COOLDOWN", 180))
        self.last_trade = {t['mint']: datetime.min for t in self.tokens}
        self.price_history = {t['mint']: [] for t in self.tokens}
        self.trailing_peaks = {t['mint']: 0.0 for t in self.tokens}

    async def get_token_price(self, token_mint: str):
        """Get token price via Jupiter API with enhanced error handling"""
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
                    return (float(quote["outAmount"]) / (10 ** token_config['decimals'])) / token_config['base_size']
        except Exception as e:
            symbol = self.token_data.get(token_mint, {}).get('symbol', 'UNKNOWN')
            logging.error(f"Price check error: {str(e)}", extra={'symbol': symbol})
            return None

    async def get_balance(self, mint: str):
        """Get balances with dynamic fee buffer"""
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(self.keypair.pubkey())
                return (resp.value / 1e9) * 0.97  # 3% fee buffer
            
            token_config = self.token_data[mint]
            opts = TokenAccountOpts(mint=Pubkey.from_string(mint))
            resp = await self.client.get_token_accounts_by_owner(
                self.keypair.pubkey(),
                opts,
                commitment=Confirmed
            )
            return sum(account.account.lamports / (10 ** token_config['decimals']) for account in resp.value)
        except Exception as e:
            symbol = self.token_data.get(mint, {}).get('symbol', 'UNKNOWN')
            logging.error(f"Balance check failed: {str(e)}", extra={'symbol': symbol})
            return 0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        """Execute trade with enhanced error handling and retries"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                token_config = self.token_data[output_mint if input_mint == SOL_MINT else input_mint]
                symbol = token_config['symbol']
                
                async with aiohttp.ClientSession() as session:
                    blockhash_resp = await self.client.get_latest_blockhash()
                    blockhash = blockhash_resp.value.blockhash

                    # Calculate trade amount with safety checks
                    current_balance = await self.get_balance(input_mint)
                    trade_amount = min(amount, current_balance)
                    trade_amount = max(trade_amount, token_config.get('min_trade', 0.01))

                    params = {
                        "inputMint": input_mint,
                        "outputMint": output_mint,
                        "amount": int(trade_amount * (1e9 if input_mint == SOL_MINT else 10 ** token_config['decimals'])),
                        "slippageBps": 200,
                        "config": json.dumps({"priorityFee": {"autoMultiplier": 5}})
                    }

                    # Get swap quote
                    async with session.get(f"{self.base_url}/quote", params=params) as resp:
                        if resp.status != 200: continue
                        quote = await resp.json()

                    # Build transaction
                    payload = {
                        "quoteResponse": quote,
                        "userPublicKey": self.wallet,
                        "wrapUnwrapSOL": True,
                        "dynamicComputeUnitLimit": True
                    }
                    async with session.post(f"{self.base_url}/swap", json=payload) as resp:
                        if resp.status != 200: continue
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
                    await self.client.confirm_transaction(txid.value, blockhash, commitment=Confirmed)
                    
                    logging.info(f"Trade success! TX: https://solscan.io/tx/{txid.value}", extra={'symbol': symbol})
                    return True
                
            except Exception as e:
                logging.error(f"Trade failed (attempt {attempt+1}/{max_retries}): {str(e)}", extra={'symbol': symbol})
                await asyncio.sleep(1)
        return False

    async def trading_strategy(self):
        """Advanced trading strategy with aggressive selling"""
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

                    # Update trailing peak
                    if price > self.trailing_peaks[mint]:
                        self.trailing_peaks[mint] = price
                    elif price < self.trailing_peaks[mint] * token.get('trailing_stop_pct', 0.97):
                        logging.info(f"🚨 Trailing stop triggered at {price:.2f}", extra={'symbol': symbol})
                        self.trailing_peaks[mint] = 0.0

                    # Get balances
                    sol_balance = await self.get_balance(SOL_MINT)
                    token_balance = await self.get_balance(mint)

                    logging.info(
                        f"Price: {price:,.2f} | Avg: {avg_price:,.2f} | "
                        f"SOL: {sol_balance:.4f} | Balance: {token_balance:,.2f}",
                        extra={'symbol': symbol}
                    )

                    # Buy logic
                    if sol_balance >= token['base_size'] and price < avg_price * 0.98:
                        trade_size = max(token['base_size'], sol_balance * 0.25)  # Use up to 25% of SOL balance
                        logging.info(f"🚀 Buying {trade_size:.4f} SOL...", extra={'symbol': symbol})
                        if await self.execute_trade(SOL_MINT, mint, trade_size):
                            self.last_trade[mint] = current_time
                            self.trailing_peaks[mint] = price

                    # Aggressive sell logic
                    sell_conditions = [
                        price > avg_price * token.get('sell_threshold', 1.02),
                        price < self.trailing_peaks[mint] * token.get('trailing_stop_pct', 0.97)
                    ]
                    
                    if token_balance > token['min_sell_amount'] and any(sell_conditions):
                        sell_percentage = token.get('sell_percentage', 1.0)
                        sell_amount = token_balance * sell_percentage
                        
                        # Ensure minimum kept balance
                        min_keep = token.get('min_keep_amount', 0)
                        if (token_balance - sell_amount) < min_keep:
                            sell_amount = token_balance - min_keep
                        
                        if sell_amount >= token['min_sell_amount']:
                            logging.info(f"💰 Selling {sell_amount:,.2f} ({(sell_amount/token_balance)*100:.0f}%)...", 
                                        extra={'symbol': symbol})
                            if await self.execute_trade(mint, SOL_MINT, sell_amount):
                                self.last_trade[mint] = current_time
                                self.trailing_peaks[mint] = 0.0
                        else:
                            logging.info(f"🟡 Sale amount below minimum: {sell_amount:,.2f}", 
                                        extra={'symbol': symbol})

                await asyncio.sleep(15)

            except Exception as e:
                logging.error(f"Strategy error: {str(e)}")
                await asyncio.sleep(60)

async def main():
    trader = MultiCoinTrader()
    await trader.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
