import os
from dotenv import load_dotenv
from solana.rpc.api import Client
from solders.keypair import Keypair
import base58

# Load environment variables
load_dotenv()

# Fetch and decode the private key
private_key_str = os.getenv("PRIVATE_KEY")
if not private_key_str:
    raise ValueError("PRIVATE_KEY not set in .env file")

try:
    private_key_bytes = base58.b58decode(private_key_str)
    keypair = Keypair.from_bytes(private_key_bytes)
except Exception as e:
    raise ValueError(f"Error decoding private key: {str(e)}")

# Set up Solana RPC
rpc_url = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
client = Client(rpc_url)

# Print wallet address
pubkey = keypair.pubkey()
print(f"Wallet Public Key: {pubkey}")

# Fetch and print balance
balance_response = client.get_balance(pubkey)

# Extract lamports from response object
lamports = balance_response.value
sol_balance = lamports / 1e9
print(f"Wallet balance: {lamports} lamports")
print(f"Wallet balance: {sol_balance} SOL")
