import asyncio
import random
from typing import Optional

import aiohttp


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
        """等待并下载图片结果"""
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

    async def upload_image(self, filename: str, image_data: bytes) -> bool:
        """上传图片到ComfyUI服务器"""
        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field('image', image_data, filename=filename)
            async with session.post(f"{self.server_url}/upload/image", data=data) as resp:
                if resp.status == 200:
                    return True
                else:
                    from astrbot.api import logger
                    try:
                        error_detail = await resp.text()
                        logger.error(f"[ComfyUI] 上传图片失败，状态码: {resp.status}, 详情: {error_detail}")
                    except:
                        logger.error(f"[ComfyUI] 上传图片失败，状态码: {resp.status}")
        return False

    async def wait_text_result(self, prompt_id: str, output_node: str = "") -> Optional[str]:
        """等待并获取文本结果"""
        async with aiohttp.ClientSession() as session:
            for _ in range(self.timeout):
                await asyncio.sleep(1)
                try:
                    async with session.get(f"{self.server_url}/history/{prompt_id}") as resp:
                        if resp.status == 200:
                            history = await resp.json()
                            if prompt_id in history:
                                outputs = history[prompt_id].get("outputs", {})

                                # 如果指定了输出节点，只查找该节点的输出
                                if output_node and output_node in outputs:
                                    node_output = outputs[output_node]

                                    # 检查 string 字段（常见输出）
                                    if "string" in node_output and node_output["string"]:
                                        # 如果是数组，返回第一个元素；如果是字符串，直接返回
                                        return node_output["string"][0] if isinstance(node_output["string"], list) and node_output["string"] else node_output["string"]

                                    # 检查 tags 字段（WD14Tagger 输出）
                                    if "tags" in node_output and node_output["tags"]:
                                        # 如果是数组，返回第一个元素；如果是字符串，直接返回
                                        return node_output["tags"][0] if isinstance(node_output["tags"], list) and node_output["tags"] else node_output["tags"]

                                # 否则查找所有节点的文本输出
                                for node_output in outputs.values():
                                    # 检查 string 字段（常见输出）
                                    if "string" in node_output and node_output["string"]:
                                        # 如果是数组，返回第一个元素；如果是字符串，直接返回
                                        return node_output["string"][0] if isinstance(node_output["string"], list) and node_output["string"] else node_output["string"]

                                    # 检查 tags 字段（WD14Tagger 输出）
                                    if "tags" in node_output and node_output["tags"]:
                                        # 如果是数组，返回第一个元素；如果是字符串，直接返回
                                        return node_output["tags"][0] if isinstance(node_output["tags"], list) and node_output["tags"] else node_output["tags"]
                except (aiohttp.ClientError, asyncio.TimeoutError, KeyError):
                    continue
        return None
