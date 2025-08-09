import os
import logging
import asyncio
import aiohttp
import base58
import base64
import numpy as np
from datetime import datetime
from collections import deque
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

SOL_MINT = "So11111111111111111111111111111111111111112"

class TokenTrader:
    def __init__(self):
        load_dotenv()
        self._init_logging()
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.keypair = Keypair.from_base58_string(os.getenv("PRIVATE_KEY"))
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"
        
        # Load configuration
        self.target_mint = os.getenv("TARGET_MINT")
        self.token_decimals = int(os.getenv("TOKEN_DECIMALS"))
        self.token_symbol = os.getenv("TOKEN_SYMBOL")
        
        # Trading parameters with economic thresholds
        self.base_trade_size = max(float(os.getenv("BASE_TRADE_SIZE", 0.1)), 0.1)
        self.cooldown = int(os.getenv("COOLDOWN", 120))
        self.min_sell_amount = max(float(os.getenv("MIN_SELL_AMOUNT", 15)), 0.1)
        self.sell_percentage = float(os.getenv("SELL_PERCENTAGE", 0.95))
        self.sell_threshold = float(os.getenv("SELL_THRESHOLD", 1.025))
        self.trailing_stop_pct = float(os.getenv("TRAILING_STOP_PCT", 0.97))
        self.atr_period = int(os.getenv("ATR_PERIOD", 14))
        self.atr_multiplier = float(os.getenv("ATR_MULTIPLIER", 1.5))
        self.min_trade_value = 0.1  # Hardcoded minimum SOL value
        
        # State management
        self.ohlc_data = deque(maxlen=self.atr_period*2)
        self.atr_values = deque(maxlen=self.atr_period)
        self.trailing_peak = 0.0
        self.session = aiohttp.ClientSession()
        self.last_trade = datetime.min

    def _init_logging(self):
        """Enhanced logging configuration with rotation"""
        self.logger = logging.getLogger("JupiterTrader")
        self.logger.setLevel(logging.DEBUG)
        
        if self.logger.hasHandlers():
            self.logger.handlers.clear()
            
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # File handler with rotation
        file_handler = logging.FileHandler('trading.log', encoding='utf-8')
        file_handler.setFormatter(formatter)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    async def get_token_price(self):
        """Fetch price and update OHLC data through Jupiter API"""
        try:
            params = {
                "inputMint": SOL_MINT,
                "outputMint": self.target_mint,
                "amount": int(self.base_trade_size * 1e9),
                "slippageBps": 200
            }
            
            async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                if resp.status != 200:
                    self.logger.warning("Price check failed: HTTP %s", resp.status)
                    return None
                
                quote = await resp.json()
                price = (float(quote["outAmount"]) / (10 ** self.token_decimals)) / self.base_trade_size
                
                # Update OHLC data
                self.ohlc_data.append({
                    'open': price,
                    'high': price,
                    'low': price,
                    'close': price,
                    'timestamp': datetime.now()
                })
                
                return price
                
        except Exception as e:
            self.logger.error("Price check error: %s", str(e), exc_info=True)
            return None

    async def get_balance(self, mint: str):
        """Get balance with fee buffer"""
        try:
            if mint == SOL_MINT:
                resp = await self.client.get_balance(Pubkey.from_string(self.wallet))
                balance = resp.value / 1e9
                return max(balance - 0.001, 0)  # 0.001 SOL fee buffer
            else:
                opts = TokenAccountOpts(mint=Pubkey.from_string(self.target_mint))
                resp = await self.client.get_token_accounts_by_owner(
                    Pubkey.from_string(self.wallet), opts
                )
                return sum(account.account.lamports / (10 ** self.token_decimals) 
                         for account in resp.value)
        except Exception as e:
            self.logger.error("Balance error: %s", str(e), exc_info=True)
            return 0

    async def execute_trade(self, input_mint: str, output_mint: str, amount: float):
        """Execute trade through Jupiter API with retries"""
        for attempt in range(3):
            try:
                # Get latest blockhash
                blockhash = (await self.client.get_latest_blockhash()).value
                
                # Get quote
                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": int(amount * (1e9 if input_mint == SOL_MINT else 10 ** self.token_decimals)),
                    "slippageBps": 200
                }
                
                async with self.session.get(f"{self.base_url}/quote", params=params) as resp:
                    quote = await resp.json()
                
                # Build swap payload
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "prioritizationFeeLamports": "auto"
                }
                
                # Get swap transaction
                async with self.session.post(f"{self.base_url}/swap", json=payload) as resp:
                    swap_data = await resp.json()
                
                # Deserialize and sign transaction
                tx = VersionedTransaction.from_bytes(base64.b64decode(swap_data["swapTransaction"]))
                signed_tx = tx.copy(
                    signatures=[Signature(SigningKey(base58.b58decode(os.getenv("PRIVATE_KEY"))[:32])
                    .sign(to_bytes_versioned(tx.message)).signature)]
                )
                
                # Send and confirm transaction
                txid = await self.client.send_raw_transaction(bytes(signed_tx))
                await self.client.confirm_transaction(txid.value, blockhash.blockhash)
                
                self.logger.info("Trade executed: %s", txid.value)
                return True
                
            except Exception as e:
                self.logger.error("Trade attempt %d failed: %s", attempt+1, str(e))
                await asyncio.sleep(2 ** attempt)
        return False

    async def calculate_tr(self):
        """Calculate True Range for volatility measurement"""
        if len(self.ohlc_data) < 2:
            return 0.0
            
        previous = self.ohlc_data[-2]
        current = self.ohlc_data[-1]
        
        return max(
            current['high'] - current['low'],
            abs(current['high'] - previous['close']),
            abs(current['low'] - previous['close'])
        )

    async def calculate_atr(self):
        """Calculate Average True Range with Wilder's smoothing"""
        if len(self.ohlc_data) < self.atr_period:
            return 0.0
            
        if not self.atr_values:
            tr_values = [await self.calculate_tr() for _ in range(self.atr_period)]
            self.atr_values.append(sum(tr_values) / self.atr_period)
            
        tr = await self.calculate_tr()
        new_atr = (self.atr_values[-1] * (self.atr_period - 1) + tr) / self.atr_period
        self.atr_values.append(new_atr)
        return self.atr_values[-1]

    async def trading_strategy(self):
        """ATR-based trading strategy with economic thresholds"""
        self.logger.info("🚀 Starting trading strategy with %.2f SOL", await self.get_balance(SOL_MINT))
        while True:
            try:
                current_time = datetime.now()
                if (current_time - self.last_trade).seconds < self.cooldown:
                    await asyncio.sleep(30)
                    continue

                price = await self.get_token_price()
                if not price:
                    await asyncio.sleep(10)
                    continue

                # Calculate indicators
                atr = await self.calculate_atr()
                avg_price = np.mean([p['close'] for p in self.ohlc_data]) if self.ohlc_data else 0
                dynamic_buy_threshold = avg_price - (atr * self.atr_multiplier)
                self.trailing_peak = max(self.trailing_peak, price)
                
                # Get balances
                sol_balance, token_balance = await asyncio.gather(
                    self.get_balance(SOL_MINT),
                    self.get_balance(self.target_mint)
                )
                
                # Economic checks
                token_value = (price * token_balance) / (10 ** self.token_decimals)
                
                # Buy logic
                if sol_balance >= self.base_trade_size and price < dynamic_buy_threshold:
                    trade_amount = min(self.base_trade_size, sol_balance)
                    if trade_amount >= self.min_trade_value:
                        self.logger.info("🔵 Buying %.2f SOL of %s (Price: %.6f)", 
                                      trade_amount, self.token_symbol, price)
                        if await self.execute_trade(SOL_MINT, self.target_mint, trade_amount):
                            self.last_trade = current_time
                            self.trailing_peak = price

                # Sell logic
                if token_balance > self.min_sell_amount and token_value >= self.min_trade_value:
                    if price > self.trailing_peak * self.sell_threshold or price < self.trailing_peak * self.trailing_stop_pct:
                        sell_amount = token_balance * self.sell_percentage
                        self.logger.info("🔴 Selling %.0f %s (Value: %.2f SOL)", 
                                      sell_amount, self.token_symbol, token_value)
                        if await self.execute_trade(self.target_mint, SOL_MINT, sell_amount):
                            self.last_trade = current_time
                            self.trailing_peak = 0.0

                await asyncio.sleep(15)

            except Exception as e:
                self.logger.error("Strategy error: %s", str(e), exc_info=True)
                await asyncio.sleep(60)

    async def cleanup(self):
        """Proper resource cleanup"""
        self.logger.info("Cleaning up resources...")
        if not self.session.closed:
            await self.session.close()
        await self.client.close()

async def main():
    trader = TokenTrader()
    try:
        await trader.trading_strategy()
    finally:
        await trader.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
