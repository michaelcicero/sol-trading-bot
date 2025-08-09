import time
import logging
from solana.rpc.api import Client
from solana.publickey import PublicKey
from solana.account import Account
from collections import deque

# Set up logging to both console and a file
log_filename = "trading_bot.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(), logging.FileHandler(log_filename)])

# BONK Token Address
bonk_address = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

# Solana Client (for interacting with Solana Blockchain)
solana_client = Client("https://api.mainnet-beta.solana.com")  # Solana mainnet endpoint

# Account (replace with your actual account information)
account = Account()  # Your private key here, for example: Account.from_secret_key(bytes)

# Initialize a simple moving average deque for BONK
sma_length = 5  # Number of past price points to average
price_history = {bonk_address: deque(maxlen=sma_length)}

# Function to get SOL balance from wallet
def get_sol_balance(account):
    try:
        balance = solana_client.get_balance(account.public_key)
        sol_balance = balance['result']['value'] / 1e9  # Convert lamports to SOL
        logging.info(f"Current SOL balance: {sol_balance:.4f} SOL")
        return sol_balance
    except Exception as e:
        logging.error(f"Error getting SOL balance: {e}")
        return 0

# Function to get token price (for now, using a static price for BONK as placeholder)
def get_token_price(token_address):
    try:
        # Static price for BONK (this should be replaced with actual price fetching logic later)
        if token_address == bonk_address:
            token_price = 0.0005  # Example BONK price (replace with real API in future)
            logging.info(f"Price for BONK token: {token_price} SOL")
            return token_price
        return None
    except Exception as e:
        logging.error(f"Error fetching token price for {token_address}: {e}")
        return None

# Function to calculate simple moving average (SMA)
def calculate_sma(token_symbol):
    prices = price_history[token_symbol]
    if len(prices) == sma_length:
        return sum(prices) / len(prices)
    return None  # Not enough data to calculate SMA

# Function to check if the price should trigger a buy decision
def should_buy(current_price, sma_price):
    return current_price < sma_price

# Function to trailing stop loss
def trailing_stop_loss(current_price, highest_price, stop_loss_percent=0.05):
    stop_loss_price = highest_price * (1 - stop_loss_percent)
    return current_price <= stop_loss_price

# Function to create a transaction (buy/sell token)
def create_transaction(from_token, to_token, amount_in_sol):
    try:
        lamports = int(amount_in_sol * 1e9)  # Convert SOL to lamports
        transaction = Transaction()

        # Example logic for creating transaction (buying a token)
        # Replace with actual Solana token swap logic using token swap program
        transaction.add(
            # Add real logic here for token transfer or swap using token swap program
            )
        
        return transaction
    except Exception as e:
        logging.error(f"Error creating transaction: {e}")
        return None

# Function to send a transaction
def send_transaction(transaction):
    try:
        response = solana_client.send_transaction(transaction, account)
        logging.info(f"Transaction sent. Signature: {response['result']}")
        return response['result']
    except Exception as e:
        logging.error(f"Error sending transaction: {e}")
        return None

# Function to run trading bot
def run_trading_bot():
    highest_price = {bonk_address: 0}  # Track highest price for BONK
    position_open = {bonk_address: False}  # Track open position for BONK

    while True:
        sol_balance = get_sol_balance(account)

        # Ensure we have enough SOL to trade
        if sol_balance < 0.005:
            logging.warning("Insufficient balance to trade. Retrying after cooldown.")
            time.sleep(10)
            continue

        # Fetch and process BONK token
        logging.info(f"Processing token: BONK at address {bonk_address}")

        token_price = get_token_price(bonk_address)
        if token_price is None:
            logging.error(f"Error fetching price for BONK. Retrying after cooldown.")
            time.sleep(10)
            continue

        # Update price history for moving average
        price_history[bonk_address].append(token_price)
        sma_price = calculate_sma(bonk_address)

        if sma_price is None:
            logging.warning(f"Not enough data to calculate SMA for BONK. Skipping.")
            continue

        logging.info(f"SMA for BONK: {sma_price:.4f}")

        # If position is open, check stop loss and sell conditions
        if position_open[bonk_address]:
            highest_price[bonk_address] = max(highest_price[bonk_address], token_price)
            if trailing_stop_loss(token_price, highest_price[bonk_address]):
                logging.info(f"[SELL] Converting BONK to SOL at price {token_price}")
                transaction = create_transaction(bonk_address, "SOL", 0.005)
                if transaction:
                    send_transaction(transaction)
                position_open[bonk_address] = False

        # If no position is open, check for buy conditions
        elif not position_open[bonk_address]:
            if should_buy(token_price, sma_price):
                logging.info(f"[BUY] Converting SOL to BONK at price {token_price}")
                transaction = create_transaction("SOL", bonk_address, 0.005)
                if transaction:
                    send_transaction(transaction)
                position_open[bonk_address] = True
                highest_price[bonk_address] = token_price

        time.sleep(10)  # Cooldown before the next cycle

# Start the trading bot
if __name__ == "__main__":
    logging.info("Starting the trading bot...")
    run_trading_bot()
