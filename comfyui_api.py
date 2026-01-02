import aiohttp
import asyncio
import random
from typing import Optional


class ComfyUIAPI:
    def __init__(self, server_url: str = "http://127.0.0.1:8188", timeout: int = 300):
        self.server_url = server_url
        self.timeout = timeout
        self.client_id = str(random.randint(100000, 999999))

    async def queue_prompt(self, workflow: dict) -> Optional[str]:
        """提交任务，返回 prompt_id"""
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.server_url}/prompt",
                                    json={"prompt": workflow, "client_id": self.client_id}) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result.get("prompt_id")
                else:
                    try:
                        error_detail = await resp.text()
                        from astrbot.api import logger
                        logger.error(f"[ComfyUI] 提交任务失败，状态码: {resp.status}, 详情: {error_detail}")
                    except:
                        from astrbot.api import logger
                        logger.error(f"[ComfyUI] 提交任务失败，状态码: {resp.status}")
        return None

    async def wait_result(self, prompt_id: str) -> Optional[bytes]:
        """等待并下载结果"""
        async with aiohttp.ClientSession() as session:
            for _ in range(self.timeout):
                await asyncio.sleep(1)
                try:
                    async with session.get(f"{self.server_url}/history/{prompt_id}") as resp:
                        if resp.status == 200:
                            history = await resp.json()
                            if prompt_id in history:
                                outputs = history[prompt_id].get("outputs", {})
                                for node_output in outputs.values():
                                    if "images" in node_output and node_output["images"]:
                                        img = node_output["images"][0]
                                        img_url = f"{self.server_url}/view?filename={img['filename']}&subfolder={img['subfolder']}&type={img['type']}"
                                        async with session.get(img_url) as img_resp:
                                            if img_resp.status == 200:
                                                return await img_resp.read()
                except (aiohttp.ClientError, asyncio.TimeoutError, KeyError):
                    continue
        return None
