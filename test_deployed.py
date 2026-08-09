import asyncio, os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from databricks.sdk.core import Config

URL = "https://weather-mcp-7474654713254531.aws.databricksapps.com/mcp"
TOKEN = Config(profile="weather").oauth_token().access_token

async def main():
    transport = StreamableHttpTransport(URL, headers={"Authorization": f"Bearer {TOKEN}"})
    async with Client(transport) as client:
        tools = await client.list_tools()
        print("tools:", [t.name for t in tools])
        result = await client.call_tool("get_current_weather", {"location": "Pullman, Washington"})
        print(result)

asyncio.run(main())