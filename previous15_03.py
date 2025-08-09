import time
import logging
import requests
import solana
from solana.rpc.api import Client
from solana.publickey import PublicKey
from solana.system_program import SYS_PROGRAM_ID
from decimal import Decimal
from solana.transaction import Transaction
from solana.account import Account

# Set up logging to both console and a file
log_filename = "trading_bot.log"
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(), logging.FileHandler(log_filename)])

# Predefined list of top tokens with their respective addresses
top_tokens_addresses = {
    "paperclip": "GLeWRGNtMZibkf9zTSEjfssnaN2WCnKiBcZDiYe3pump",
    "trencher": "8ncucXv6U6epZKHPbgaEBcEK399TpHGKCquSt4RnmX4f",
    "house": "DitHyRMQiSDhn5cnKMJV2CDDt6sVct96YrECiM49pump",
    "letsBONK": "CDBdbNqmrLu1PcgjrFG52yxg71QnFhBZcUE6PSFdbonk",
    "LINK": "0x514910771af9ca656af840dff83e8264ecf986ca",
    "BONK": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
}

# Solana Client (for interacting with Solana Blockchain)
solana_client = Client("https://api.mainnet-beta.solana.com")  # Solana mainnet endpoint

# Account (replace with your actual account information)
account = Account()  # Your private key here, for example: Account.from_secret_key(bytes)

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

# Function to get token balance using its address
def get_token_balance(account, token_address):
    try:
        token_pubkey = PublicKey(token_address)
        token_balance = solana_client.get_token_account_balance(token_pubkey)
        if token_balance.get('result'):
            return token_balance['result']['value']['uiAmount']
        return 0
    except Exception as e:
        logging.error(f"Error getting token balance for {token_address}: {e}")
        return 0

# Function to get token price (placeholder)
def get_token_price(token_address):
    try:
        # Placeholder for real price data (You would use a price API here like Jupiter or another market data provider)
        token_price = 0.1  # Example token price (in SOL or USDC depending on the pair)
        logging.info(f"Price for token {token_address}: {token_price}")
        return token_price
    except Exception as e:
        logging.error(f"Error fetching token price for {token_address}: {e}")
        return None

# Function to check if the price should trigger a buy decision
def should_buy(current_price, avg_price):
    decision = current_price < avg_price
    logging.info(f"Should buy decision: {decision} (Current: {current_price} < Average: {avg_price})")
    return decision

# Function to check trailing stop loss
def trailing_stop_loss(current_price, highest_price, stop_loss_percent=0.05):
    stop_loss_price = highest_price * (1 - stop_loss_percent)
    decision = current_price <= stop_loss_price
    if decision:
        logging.info(f"Trailing stop loss triggered. Price dropped below {stop_loss_price}")
    return decision

# Function to create a transaction (buy/sell token)
def create_transaction(from_token, to_token, amount_in_sol):
    try:
        # Placeholder logic to create and sign a transaction
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

# Function to filter tokens based on addresses in the predefined list
def filter_tokens(tokens, top_tokens_addresses):
    filtered_tokens = []
    for token in tokens:
        token_address = token.get('address', '').lower()  # Ensure address is in lowercase for comparison
        if token_address in top_tokens_addresses.values():
            filtered_tokens.append(token)
        else:
            logging.warning(f"Skipping invalid token with address: {token_address}")
    return filtered_tokens

# Run trading bot function
def run_trading_bot():
    highest_price = 0  # To track the highest price during the trade
    position_open = False  # Flag to track whether the bot has an open position

    while True:
        sol_balance = get_sol_balance(account)

        # Ensure we have enough SOL to trade
        if sol_balance < 0.005:
            logging.warning("Insufficient balance to trade. Retrying after cooldown.")
            time.sleep(10)
            continue

        tokens = get_token_list()

        if not tokens:
            logging.error("Error: No tokens found. Retrying after cooldown.")
            time.sleep(10)
            continue

        # Filter tokens based on the predefined list of token addresses
        filtered_tokens = filter_tokens(tokens, top_tokens_addresses)

        if len(filtered_tokens) == 0:
            logging.warning("No relevant tokens found in the filtered list. Retrying...")
            time.sleep(10)
            continue

        for token in filtered_tokens:
            if 'symbol' in token and 'address' in token:
                token_symbol = token['symbol']
                logging.info(f"Processing token: {token_symbol}")

                amount_in_sol = 0.005  # Amount of SOL to trade
                lamports = int(amount_in_sol * 1e9)

                token_price = get_token_price(token["address"])

                if token_price is None:
                    continue  # Skip if we can't fetch price data

                avg_price = update_price_history(token['symbol'], token_price)

                if position_open:
                    highest_price = max(highest_price, token_price)
                    logging.info(f"Highest price so far: {highest_price}")

                    # Check trailing stop loss
                    if trailing_stop_loss(token_price, highest_price):
                        logging.info(f"[SELL] Converting {token_symbol} to SOL at price {token_price}")
                        transaction = create_transaction(token["address"], "SOL", lamports)
                        if transaction:
                            send_transaction(transaction)
                        position_open = False

                if not position_open:
                    if should_buy(token_price, avg_price):
                        logging.info(f"[BUY] Converting SOL to {token_symbol} at price {token_price}")
                        transaction = create_transaction("SOL", token["address"], lamports)
                        if transaction:
                            send_transaction(transaction)
                        position_open = True
                        highest_price = token_price

                    time.sleep(10)  # Cooldown between trades

        time.sleep(10)  # Cooldown before the next cycle

# Start the trading bot
if __name__ == "__main__":
    logging.info("Starting the trading bot...")
    run_trading_bot()
