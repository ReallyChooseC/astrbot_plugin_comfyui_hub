import json
import random
import time
from typing import Optional

from astrbot.api import logger

from .comfyui_api import ComfyUIAPI


class ImageToText:
    def __init__(self, api: ComfyUIAPI, workflow_path: str, output_node: str = "", input_node: str = ""):
        self.api = api
        self.workflow = self._load_workflow(workflow_path)
        self.output_node = output_node
        self.input_node = input_node

    @staticmethod
    def _load_workflow(path: str) -> dict:
        """加载工作流文件"""
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    async def generate(self, image_data: bytes) -> Optional[str]:
        """生成图片标签文本"""
        workflow = json.loads(json.dumps(self.workflow))

        # 上传图片到 ComfyUI（使用时间戳避免缓存）
        filename = f"tagger_input_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.png"
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
            if isinstance(node_data, dict):
                node_data["inputs"]["image"] = filename
                load_image_found = True

        # 方式2：如果未指定输入节点，则查找 LoadImage 节点并更新其 image 字段
        if not load_image_found:
            for node_id, node_data in workflow.items():
                if isinstance(node_data, dict) and node_data.get("class_type") == "LoadImage":
                    node_data["inputs"]["image"] = filename
                    load_image_found = True
                    break

        # 提交任务
        prompt_id = await self.api.queue_prompt(workflow)
        if not prompt_id:
            logger.error("[ComfyUI] 提交tagger任务失败")
            return None

        # 等待结果
        result = await self.api.wait_text_result(prompt_id, self.output_node)

        if not result:
            logger.error("[ComfyUI] tagger等待结果超时或失败")

        return result
