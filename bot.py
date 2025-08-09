import os
import logging
import asyncio
import aiohttp
import json
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction  # Correct import
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
            "bot_version": "4.1.0"
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
        self.positions = {}
        self.daily_pnl = 0.0
        self.max_position_size = 0.05  # 5% of portfolio per trade
        self.trailing_stop_loss = 0.05  # 5% trailing stop

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

    async def get_portfolio_value(self):
        sol_balance = await self.get_balance()
        token_values = []
        for symbol, mint in TOKEN_LIST.items():
            price = await self.get_jupiter_price(mint)
            if price is None:
                continue
            balance = await self.get_token_balance(mint)
            token_values.append(balance * price)
        return sol_balance + sum(token_values)

    async def get_balance(self):
        try:
            response = await self.client.get_balance(self.keypair.pubkey())
            return response.value / 1e9
        except Exception as e:
            TradingBotLogger.log_event("balance_error", {"error": str(e)})
            return 0

    async def get_token_balance(self, mint_address: str):
        try:
            token_accounts = await self.client.get_token_accounts_by_owner(
                self.keypair.pubkey(),
                mint=Pubkey.from_string(mint_address),
                commitment=Confirmed
            )
            total = 0.0
            for acc in token_accounts.value:
                token_amount = acc.account.data.parsed['info']['tokenAmount']
                total += int(token_amount['amount']) / (10 ** int(token_amount['decimals']))
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
            # Get quote
            quote_url = "https://lite-api.jup.ag/quote/v6/quote"
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": int(amount * 1e9),
                "slippageBps": 50
            }
            async with self.session.get(quote_url, params=params) as resp:
                quote = await resp.json()

            # Prepare swap
            swap_url = "https://lite-api.jup.ag/swap/v6/swap"
            payload = {
                "quoteResponse": quote,
                "userPublicKey": self.wallet_address,
                "wrapUnwrapSOL": True
            }
            async with self.session.post(swap_url, json=payload) as resp:
                swap_data = await resp.json()

            # Build and send transaction
            transaction_bytes = bytes.fromhex(swap_data['swapTransaction'])
            transaction = VersionedTransaction.deserialize(transaction_bytes)
            transaction.sign([self.keypair])
            txid = await self.client.send_raw_transaction(bytes(transaction))
            await self.client.confirm_transaction(txid.value)
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
                portfolio_value = await self.get_portfolio_value()
                if portfolio_value == 0:
                    await asyncio.sleep(60)
                    continue

                for symbol, mint in TOKEN_LIST.items():
                    current_price = await self.get_jupiter_price(mint)
                    if current_price is None:
                        continue

                    # Update price history (30-period moving average)
                    if symbol not in self.price_history:
                        self.price_history[symbol] = []
                    self.price_history[symbol].append(current_price)
                    self.price_history[symbol] = self.price_history[symbol][-30:]
                    avg_price = sum(self.price_history[symbol])/len(self.price_history[symbol])

                    position = self.positions.get(symbol)
                    
                    if position and position['type'] == 'long':
                        highest_price = max(position['highest_price'], current_price)
                        stop_price = highest_price * (1 - self.trailing_stop_loss)
                        if current_price <= stop_price:
                            balance = await self.get_token_balance(mint)
                            if balance > 0:
                                success = await self.execute_swap(
                                    mint,
                                    "So11111111111111111111111111111111111111112",
                                    balance
                                )
                                if success:
                                    self.positions.pop(symbol)
                                    TradingBotLogger.log_event("position_closed", {
                                        "symbol": symbol,
                                        "price": current_price,
                                        "profit": (current_price - position['entry_price']) * balance
                                    })
                    else:
                        if current_price < avg_price * 0.98:
                            trade_size = min(
                                portfolio_value * self.max_position_size,
                                await self.get_balance()
                            )
                            if trade_size >= 0.01:
                                success = await self.execute_swap(
                                    "So11111111111111111111111111111111111111112",
                                    mint,
                                    trade_size
                                )
                                if success:
                                    self.positions[symbol] = {
                                        'type': 'long',
                                        'entry_price': current_price,
                                        'highest_price': current_price,
                                        'quantity': trade_size / current_price
                                    }
                                    TradingBotLogger.log_event("position_opened", {
                                        "symbol": symbol,
                                        "price": current_price,
                                        "size": trade_size
                                    })

                await asyncio.sleep(300)

            except Exception as e:
                TradingBotLogger.log_event("strategy_error", {"error": str(e)})
                await asyncio.sleep(60)

async def main():
    async with SolanaTradingBot() as bot:
        await bot.trading_strategy()

if __name__ == "__main__":
    asyncio.run(main())
