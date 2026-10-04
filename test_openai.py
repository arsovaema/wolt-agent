import asyncio
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

async def main():
    try:
        models = await client.models.list()
        print("SUCCESS")
        print(models.data[0].id)

    except Exception as e:
        print("ERROR TYPE:", type(e).__name__)
        print("ERROR:", repr(e))
        print("CAUSE:", repr(e.__cause__))

asyncio.run(main())