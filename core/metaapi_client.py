import os
import asyncio
import logging
from typing import Optional
from metaapi_cloud_sdk import MetaApi
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class MetaApiClient:
    def __init__(self):
        self._api: Optional[MetaApi] = None
        self._connection = None
        self._lock = asyncio.Lock()

    async def get_connection(self, retries: int = 5):
        async with self._lock:
            if self._connection is not None:
                return self._connection
            return await self._connect(retries)

    async def _connect(self, retries: int):
        token      = os.environ["META_API_TOKEN"]
        account_id = os.environ["HFM_ACCOUNT_ID"]

        if self._api is None:
            self._api = MetaApi(token)

        for attempt in range(retries):
            try:
                account    = await self._api.metatrader_account_api.get_account(account_id)
                connection = account.get_rpc_connection()
                await connection.connect()
                await connection.wait_synchronized()
                self._connection = connection
                logger.info("MetaApi connected and synchronized")
                return self._connection
            except Exception as exc:
                logger.warning(
                    "MetaApi connect attempt %d/%d failed: %s", attempt + 1, retries, exc
                )
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)   # 1 → 2 → 4 → 8 → 16 s

    async def close(self):
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("MetaApi connection closed")


# Module-level singleton — each agent process gets its own instance
_client = MetaApiClient()


async def get_connection(retries: int = 5):
    return await _client.get_connection(retries)


async def close_connection():
    await _client.close()
