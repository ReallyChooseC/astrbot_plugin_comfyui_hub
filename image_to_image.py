import json
import random
import time
from typing import Optional

from astrbot.api import logger

from .comfyui_api import ComfyUIAPI


class ImageToImage:
    def __init__(self, api: ComfyUIAPI, workflow_path: str,
                 positive_node: str = "20", negative_node: str = "21",
                 input_node: str = "15"):
        self.api = api
        self.workflow = self._load_workflow(workflow_path)
        self.positive_node = positive_node
        self.negative_node = negative_node
        self.input_node = input_node

    @staticmethod
    def _load_workflow(path: str) -> dict:
        """加载工作流文件"""
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    @staticmethod
    def _set_prompt(node: dict, prompt: str) -> bool:
        """设置提示词到节点的第一个输入字段"""
        if not node or "inputs" not in node:
            return False

        inputs = node["inputs"]
        if not inputs:
            return False

        first_key = next(iter(inputs))
        inputs[first_key] = prompt
        return True

    async def generate(self, image_data: bytes, prompt: str, negative: str = "") -> Optional[bytes]:
        """生成图片"""
        workflow = json.loads(json.dumps(self.workflow))

        # 上传图片到 ComfyUI（使用时间戳避免缓存）
        filename = f"img2img_input_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.png"
        try:
            await self.api.upload_image(filename, image_data)
        except Exception as e:
            logger.error(f"[ComfyUI] 上传图片失败: {e}")
            return None

        # 更新工作流中的图片引用
        # 方式1：使用配置的输入节点 ID
        load_image_found = False
        if self.input_node and self.input_node in workflow:
            node_data = workflow[self.input_node]
            if isinstance(node_data, dict) and node_data.get("class_type") == "LoadImage":
                node_data["inputs"]["image"] = filename
                load_image_found = True

        # 方式2：如果未指定输入节点，则查找 LoadImage 节点并更新其 image 字段
        if not load_image_found:
            for node_id, node_data in workflow.items():
                if isinstance(node_data, dict) and node_data.get("class_type") == "LoadImage":
                    node_data["inputs"]["image"] = filename
                    load_image_found = True
                    break

        if not load_image_found:
            logger.error("[ComfyUI] 工作流中未找到 LoadImage 节点")
            return None

        # 设置正面提示词
        pos_node = workflow.get(self.positive_node)
        if not pos_node:
            logger.error(f"[ComfyUI] 找不到正面提示词节点 {self.positive_node}")
            return None

        if not self._set_prompt(pos_node, prompt):
            logger.error(f"[ComfyUI] 节点 {self.positive_node} 没有输入字段")
            return None

        # 设置负面提示词
        if self.negative_node and negative:
            neg_node = workflow.get(self.negative_node)
            if neg_node:
                self._set_prompt(neg_node, negative)

        # 设置随机种子
        base_seed = random.randint(1, 999999999999999)
        offset = 0
        for node_data in workflow.values():
            if isinstance(node_data, dict):
                inputs = node_data.get("inputs", {})
                if "seed" in inputs:
                    inputs["seed"] = base_seed + offset
                    offset += 1
                if "noise_seed" in inputs:
                    inputs["noise_seed"] = base_seed + offset
                    offset += 1

        # 提交任务
        prompt_id = await self.api.queue_prompt(workflow)
        if not prompt_id:
            logger.error("[ComfyUI] 提交图生图任务失败")
            return None

        # 等待结果
        result = await self.api.wait_result(prompt_id)

        if not result:
            logger.error("[ComfyUI] 图生图等待结果超时或失败")

        return result
