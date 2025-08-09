import os
import logging
import asyncio
import aiohttp
import json
import base64
import base58
from dotenv import load_dotenv

from nacl.signing import SigningKey
from solders.signature import Signature
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
import time

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class JupiterTrader:
    def __init__(self):
        self.client = AsyncClient(os.getenv("HELIUS_RPC_URL"))
        self.base58_private_key = os.getenv("PRIVATE_KEY")
        self.keypair = Keypair.from_base58_string(self.base58_private_key)
        self.wallet = str(self.keypair.pubkey())
        self.base_url = "https://quote-api.jup.ag/v6"

    async def force_buy(self, amount: float = 0.001):
        """Perform a SOL -> USDC swap using Jupiter Aggregator"""
        try:
            async with aiohttp.ClientSession() as session:
                logging.info("Fetching latest blockhash...")
                latest_blockhash_resp = await self.client.get_latest_blockhash()
                latest_blockhash = latest_blockhash_resp.value

                # Get Jupiter swap quote
                params = {
                    "inputMint": "So11111111111111111111111111111111111111112",  # SOL
                    "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                    "amount": int(amount * 1e9),  # in lamports
                    "slippageBps": 200,
                    "config": json.dumps({"priorityFee": {"autoMultiplier": 5}})
                }

                async with session.get(f"{self.base_url}/quote", params=params) as resp:
                    if resp.status != 200:
                        raise Exception(f"Quote API error: {await resp.text()}")
                    quote = await resp.json()

                # Get swap transaction from Jupiter
                payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapUnwrapSOL": True,
                    "dynamicComputeUnitLimit": True
                }
                headers = {"Content-Type": "application/json"}
                async with session.post(f"{self.base_url}/swap", json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        raise Exception(f"Swap API error: {await resp.text()}")
                    swap_data = await resp.json()

                # Debug: Log the raw swap transaction for debugging
                logging.info(f"Swap Transaction Raw: {swap_data['swapTransaction']}")

                # Deserialize and prepare transaction for signing
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)

                # Sign the transaction manually with correct signature flow
                message_bytes = to_bytes_versioned(unsigned_tx.message)
                secret_key_bytes = base58.b58decode(self.base58_private_key)[:32]
                signing_key = SigningKey(secret_key_bytes)
                signature_bytes = signing_key.sign(message_bytes).signature
                signature = Signature(signature_bytes)

                # Populate transaction with signature
                signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])

                # Send transaction to Solana network
                tx_opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed, max_retries=5)
                txid = await self.client.send_raw_transaction(bytes(signed_tx), opts=tx_opts)

                # Log the full response from sending the transaction
                logging.info(f"Transaction Response: {txid}")

                # Confirm the transaction in a loop with a retry mechanism
                retries = 5
                while retries > 0:
                    logging.info(f"Attempting to confirm transaction: {txid.value}")
                    tx_status = await self.client.get_transaction(txid.value)

                    if tx_status is not None:
                        logging.info(f"Transaction Status: {tx_status}")
                        break
                    else:
                        retries -= 1
                        logging.info(f"Retrying... {retries} attempts left.")
                        time.sleep(3)  # Wait for 3 seconds before retrying

                if retries == 0:
                    logging.error(f"Transaction failed to confirm after multiple attempts.")
                    return False

                # If the transaction is confirmed, check for final confirmation
                await self.client.confirm_transaction(txid.value, latest_blockhash.last_valid_block_height)
                logging.info(f"✅ Swap successful! TXID: {txid.value}")
                return True

        except Exception as e:
            logging.error(f"❌ Swap failed: {str(e)}")
            return False

        finally:
            await self.client.close()

async def main():
    trader = JupiterTrader()
    await trader.force_buy(0.001)  # Example: swap 0.001 SOL to USDC

if __name__ == "__main__":
    asyncio.run(main())
