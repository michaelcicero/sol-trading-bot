import os
import time
import logging
import base58
import requests
from dotenv import load_dotenv
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction, TransactionMessage
from solders.instructions import Instruction, AccountMeta
from solders.message import MessageV0
from solders.pubkey import Pubkey
from spl.token.instructions import transfer_checked, TransferCheckedParams
from spl.token.constants import TOKEN_PROGRAM_ID

# Load environment variables
load_dotenv()
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")

if not PRIVATE_KEY:
    raise ValueError("PRIVATE_KEY not set in .env")

# Decode private key and create Keypair
private_key_bytes = base58.b58decode(PRIVATE_KEY)
keypair = Keypair.from_bytes(private_key_bytes)

# Set up Solana client
client = Client(RPC_URL)

# Fetch wallet info
pubkey = keypair.pubkey()
logging.basicConfig(level=logging.INFO)

def get_sol_balance():
    try:
        balance_response = client.get_balance(pubkey)
        sol_balance = balance_response.value / 1e9  # Convert lamports to SOL
        logging.info(f"Current SOL balance: {sol_balance:.6f} SOL")
        return sol_balance
    except Exception as e:
        logging.error(f"Error getting SOL balance: {e}")
        return 0

def get_token_list():
    try:
        logging.info("Fetching token list from Jupiter API...")
        response = requests.get("https://quote-api.jup.ag/v6/tokens", timeout=10)
        if response.status_code == 429:
            logging.error("Rate limit exceeded. Retrying after 30 seconds...")
            time.sleep(30)
            return get_token_list()
        response.raise_for_status()
        tokens = response.json()
        logging.info(f"Fetched {len(tokens)} tokens.")
        return tokens
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching token list: {e}")
        return []

def get_associated_token_account(mint_address: str):
    try:
        return Pubkey.find_program_address(
            [bytes(pubkey), bytes(TOKEN_PROGRAM_ID), bytes(Pubkey.from_string(mint_address))],
            Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
        )[0]
    except Exception as e:
        logging.error(f"Error getting associated token account: {e}")
        return None

def sell_token(token_address: str, amount: float, destination_pubkey: str, decimals: int = 6):
    try:
        mint_pubkey = Pubkey.from_string(token_address)
        dest_pubkey = Pubkey.from_string(destination_pubkey)
        
        # Get token accounts
        source_token_account = get_associated_token_account(token_address)
        dest_token_account = get_associated_token_account(token_address)  # Need proper destination account
        
        # Create transfer instruction
        transfer_params = TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=source_token_account,
            mint=mint_pubkey,
            dest=dest_pubkey,  # Should be destination's token account
            owner=pubkey,
            amount=int(amount * (10 ** decimals)),
            decimals=decimals
        )
        
        instruction = transfer_checked(transfer_params)
        
        # Get recent blockhash
        recent_blockhash = client.get_latest_blockhash().value.blockhash
        
        # Build message
        message = MessageV0.try_compile(
            payer=pubkey,
            instructions=[instruction],
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash
        )
        
        # Create transaction
        transaction = VersionedTransaction(message, [keypair])
        
        # Send transaction
        response = client.send_transaction(transaction)
        logging.info(f"Sell transaction sent. Signature: {response.value}")
        return response
    except Exception as e:
        logging.error(f"Error during sell transaction: {e}")
        return None

def run_trading_bot():
    logging.info("Starting the trading bot...")
    while True:
        try:
            sol_balance = get_sol_balance()
            if sol_balance < 0.005:
                logging.info("Insufficient balance. Retrying in 10 seconds...")
                time.sleep(10)
                continue

            tokens = get_token_list()
            if not tokens:
                logging.error("No tokens found. Retrying...")
                time.sleep(10)
                continue

            logging.info("Top 10 tokens:")
            for token in tokens[:10]:
                logging.info(f"- {token.get('symbol', 'Unknown')} (Address: {token['address']})")

            # Example usage - replace with actual trading logic
            if tokens and input("Test sell first token? (y/n): ").lower() == 'y':
                sell_token(
                    token_address=tokens[0]['address'],
                    amount=1.0,
                    destination_pubkey="RECIPIENT_PUBKEY_HERE",  # Replace with actual
                    decimals=tokens[0].get('decimals', 6)
                )

            time.sleep(10)
        
        except Exception as e:
            logging.error(f"Error in trading loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run_trading_bot()
