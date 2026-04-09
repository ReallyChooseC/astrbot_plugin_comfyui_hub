import json
import random
import time
from io import BytesIO
from typing import Optional

from PIL import Image as PILImage
from astrbot.api import logger

from .comfyui_api import ComfyUIAPI


class ImageToImage:
    def __init__(self, api: ComfyUIAPI, workflow_path: str,
                 positive_node: str = "20", negative_node: str = "21",
                 input_nodes: list = None):
        self.api = api
        self.workflow = self._load_workflow(workflow_path)
        self.positive_node = positive_node
        self.negative_node = negative_node
        # 输入节点列表，按顺序分配图片
        self.input_nodes = input_nodes or []

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

    @staticmethod
    def _extract_first_frame_if_gif(image_data: bytes) -> Optional[bytes]:
        """
        如果输入是动图（GIF/WebP），提取第一帧
        如果是动图且处理失败，返回 None 拒绝使用原图

        Returns:
            处理后的图片数据（首帧），如果是动图且处理失败则返回 None
        """
        try:
            with PILImage.open(BytesIO(image_data)) as img:
                # 检查是否为动图
                is_animated = getattr(img, 'is_animated', False)

                if is_animated:
                    logger.info("[ComfyUI] 检测到动图，将使用首帧")
                    # 提取第一帧
                    img.seek(0)
                    # 转换为RGB模式（避免某些模式导致的错误）
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    # 保存为PNG格式
                    output = BytesIO()
                    img.save(output, format='PNG')
                    return output.getvalue()

                # 静态图片直接返回
                return image_data
        except Exception as e:
            logger.error(f"[ComfyUI] 动图检测/处理失败: {e}")
            return None

    async def generate(self, image_data_list: list, prompt: str, negative: str = "") -> Optional[bytes]:
        """生成图片
        
        Args:
            image_data_list: 图片数据列表，每个元素为 bytes。第一张图对应主输入节点，
                            后续图片对应额外输入节点。
            prompt: 正面提示词
            negative: 负面提示词
        """
        workflow = json.loads(json.dumps(self.workflow))

        # 处理所有图片（动图提取首帧）并上传
        uploaded_filenames = []
        for i, img_data in enumerate(image_data_list):
            processed = self._extract_first_frame_if_gif(img_data)
            if processed is None:
                logger.error(f"[ComfyUI] 第 {i+1} 张图片不支持动图输入，请使用静态图片")
                return None

            filename = f"img2img_input_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.png"
            try:
                await self.api.upload_image(filename, processed)
                uploaded_filenames.append(filename)
            except Exception as e:
                logger.error(f"[ComfyUI] 上传第 {i+1} 张图片失败: {e}")
                return None

        # 收集所有 LoadImage 节点（按节点ID排序）
        load_image_nodes = []
        for node_id, node_data in workflow.items():
            if isinstance(node_data, dict) and node_data.get("class_type") == "LoadImage":
                load_image_nodes.append((node_id, node_data))

        # 将上传的图片分配到对应的 LoadImage 节点
        assigned_count = 0
        # 优先按配置的输入节点顺序分配
        for input_node_id in self.input_nodes:
            if assigned_count >= len(uploaded_filenames):
                break
            if input_node_id in workflow:
                node_data = workflow[input_node_id]
                if isinstance(node_data, dict) and node_data.get("class_type") == "LoadImage":
                    node_data["inputs"]["image"] = uploaded_filenames[assigned_count]
                    assigned_count += 1

        # 如果还有未分配的图片，按 LoadImage 节点顺序分配剩余节点
        if assigned_count < len(uploaded_filenames):
            remaining_nodes = [(nid, nd) for nid, nd in load_image_nodes
                              if nid not in self.input_nodes]
            for node_id, node_data in remaining_nodes:
                if assigned_count >= len(uploaded_filenames):
                    break
                node_data["inputs"]["image"] = uploaded_filenames[assigned_count]
                assigned_count += 1

        # 如果没有通过配置节点分配成功，回退到旧逻辑
        if assigned_count == 0 and load_image_nodes:
            load_image_nodes[0][1]["inputs"]["image"] = uploaded_filenames[0]
            assigned_count = 1

        if assigned_count == 0:
            logger.error("[ComfyUI] 工作流中未找到 LoadImage 节点")
            return None

        if assigned_count < len(uploaded_filenames):
            logger.warning(f"[ComfyUI] 上传了 {len(uploaded_filenames)} 张图片，但只有 {assigned_count} 个 LoadImage 节点可用")

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
