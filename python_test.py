import asyncio
import aiohttp

async def test_jupiter_api():
    # USDC and JUP
    url = "https://lite-api.jup.ag/price/v2?ids=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v,JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                print(f"Status: {resp.status}")
                data = await resp.json()
                print("Sample response:", data)
    except Exception as e:
        print("Network/API error:", e)

if __name__ == "__main__":
    asyncio.run(test_jupiter_api())
