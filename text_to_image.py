import json
import math
import random
from typing import Optional

from astrbot.api import logger

from .comfyui_api import ComfyUIAPI


class TextToImage:
    MAX_OUTPUT_PIXELS = 2048 * 2048
    MIN_DIMENSION = 64
    MAX_DIMENSION = 4096
    ALIGN = 8

    def __init__(self, api: ComfyUIAPI, workflow_path: str,
                 positive_node: str = "6", negative_node: str = "7",
                 resolution_node: str = "", width_field: str = "width", height_field: str = "height",
                 upscale_node: str = "", scale_field: str = "resize_scale"):
        self.api = api
        self.workflow = self._load_workflow(workflow_path)
        self.positive_node = positive_node
        self.negative_node = negative_node
        self.resolution_node = resolution_node
        self.width_field = width_field
        self.height_field = height_field
        self.upscale_node = upscale_node
        self.scale_field = scale_field

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

    @classmethod
    def _clamp_dimensions(cls, width: int, height: int) -> tuple:
        """将用户传入的 width/height 钳制到合法范围内并按 ALIGN 对齐"""
        w = max(cls.MIN_DIMENSION, min(int(width), cls.MAX_DIMENSION))
        h = max(cls.MIN_DIMENSION, min(int(height), cls.MAX_DIMENSION))

        pixels = w * h
        if pixels > cls.MAX_OUTPUT_PIXELS:
            scale = math.sqrt(cls.MAX_OUTPUT_PIXELS / pixels)
            w = int(w * scale)
            h = int(h * scale)

        align = cls.ALIGN
        w = max(align, (w // align) * align)
        h = max(align, (h // align) * align)
        return w, h

    async def generate(self, prompt: str, negative: str = "bad hands", width: int = None, height: int = None,
                       scale: float = None, max_wait: float = 300.0, on_wait_callback=None, on_submitted_callback=None) -> Optional[bytes]:
        """生成图片"""
        workflow = json.loads(json.dumps(self.workflow))

        pos_node = workflow.get(self.positive_node)
        if not pos_node:
            logger.error(f"[ComfyUI] 找不到正面提示词节点 {self.positive_node}")
            return None

        if not self._set_prompt(pos_node, prompt):
            logger.error(f"[ComfyUI] 节点 {self.positive_node} 没有输入字段")
            return None

        neg_node = workflow.get(self.negative_node)
        if neg_node:
            self._set_prompt(neg_node, negative)

        if width is not None and height is not None:
            try:
                width, height = self._clamp_dimensions(width, height)
            except (TypeError, ValueError) as e:
                logger.warning(f"[ComfyUI] 分辨率参数无效，已忽略: {e}")
                width = height = None

        if width is not None and height is not None:
            if self.resolution_node:
                if self.resolution_node in workflow:
                    workflow[self.resolution_node]["inputs"][self.width_field] = width
                    workflow[self.resolution_node]["inputs"][self.height_field] = height
            else:
                for node_id, node_data in workflow.items():
                    if isinstance(node_data, dict) and node_data.get("class_type") == "EmptyLatentImage":
                        node_data["inputs"]["width"] = width
                        node_data["inputs"]["height"] = height
                        break

        if scale is not None and self.upscale_node:
            if self.upscale_node in workflow:
                workflow[self.upscale_node]["inputs"][self.scale_field] = scale

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

        result = await self.api.queue_and_wait_image(workflow, max_wait=max_wait, on_wait_callback=on_wait_callback, on_submitted_callback=on_submitted_callback)

        if not result:
            logger.error("[ComfyUI] 生成失败或等待结果超时")

        return result
