import json
import math
import random
import time
from io import BytesIO
from typing import Optional

from PIL import Image as PILImage
from astrbot.api import logger

from .comfyui_api import ComfyUIAPI


class ImageToVideo:
    MAX_OUTPUT_PIXELS = 1024 * 1024

    def __init__(self, api: ComfyUIAPI, workflow_path: str,
                 positive_node: str = "3", negative_node: str = "4",
                 input_node: str = "2",
                 resolution_node: str = "1",
                 resolution_width_field: str = "width",
                 resolution_height_field: str = "height",
                 fps_node: str = "18", fps_field: str = "value",
                 length_node: str = "20", length_field: str = "value",
                 max_frames: int = 240):
        self.api = api
        self.workflow = self._load_workflow(workflow_path)
        self.positive_node = positive_node
        self.negative_node = negative_node
        self.input_node = input_node
        self.resolution_node = resolution_node
        self.resolution_width_field = resolution_width_field
        self.resolution_height_field = resolution_height_field
        self.fps_node = fps_node
        self.fps_field = fps_field
        self.length_node = length_node
        self.length_field = length_field
        self.max_frames = max_frames

    @staticmethod
    def _load_workflow(path: str) -> dict:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    @staticmethod
    def _set_prompt(node: dict, prompt: str) -> bool:
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
        """如果输入是动图（GIF/WebP），提取第一帧"""
        try:
            with PILImage.open(BytesIO(image_data)) as img:
                is_animated = getattr(img, 'is_animated', False)
                if is_animated:
                    logger.info("[ComfyUI] 检测到动图，将使用首帧")
                    img.seek(0)
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    output = BytesIO()
                    img.save(output, format='PNG')
                    return output.getvalue()
                return image_data
        except Exception as e:
            logger.error(f"[ComfyUI] 动图检测/处理失败: {e}")
            return None

    def _calc_output_size(self, image_data: bytes) -> Optional[tuple]:
        """根据输入图像计算输出分辨率

        - 输入图像像素 ≤ 1M：直接使用原始尺寸
        - 输入图像像素 > 1M：保持比例缩放至 1M 像素以内
        - 宽高对齐到 16 的倍数（视频模型常见约束）

        注意：仅计算并写入工作流的输出分辨率，不修改输入图像本身。

        Returns:
            (width, height) 或 None（解析失败）
        """
        try:
            with PILImage.open(BytesIO(image_data)) as img:
                w, h = img.size
        except Exception as e:
            logger.error(f"[ComfyUI] 读取图像尺寸失败: {e}")
            return None

        if w <= 0 or h <= 0:
            return None

        pixels = w * h
        if pixels > self.MAX_OUTPUT_PIXELS:
            scale = math.sqrt(self.MAX_OUTPUT_PIXELS / pixels)
            w = int(w * scale)
            h = int(h * scale)

        align = 16
        w = max(align, (w // align) * align)
        h = max(align, (h // align) * align)
        return w, h

    @staticmethod
    def _read_node_value(workflow: dict, node_id: str, field: str):
        """读取工作流节点上某个字段的当前值，找不到返回 None"""
        if not node_id or node_id not in workflow:
            return None
        node = workflow[node_id]
        if not isinstance(node, dict):
            return None
        return node.get("inputs", {}).get(field)

    async def generate(self, image_data: bytes, prompt: str, negative: str = "",
                       fps: Optional[float] = None, length: Optional[float] = None,
                       max_wait: float = 300.0, on_wait_callback=None,
                       on_submitted_callback=None) -> Optional[bytes]:
        """生成视频

        Args:
            image_data: 输入图片数据
            prompt: 正面提示词（可为空字符串）
            negative: 负面提示词
            fps: 帧率，None 表示沿用工作流默认值
            length: 视频长度（秒），None 表示沿用工作流默认值；
                    若 fps × length 超过 max_frames，会自动缩短 length 以满足上限
        """
        workflow = json.loads(json.dumps(self.workflow))

        # 处理输入图片（动图首帧）
        processed = self._extract_first_frame_if_gif(image_data)
        if processed is None:
            logger.error("[ComfyUI] 输入图片处理失败")
            return None

        # 根据输入图像计算输出分辨率
        size = self._calc_output_size(processed)

        filename = f"i2v_input_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.png"
        try:
            await self.api.upload_image(filename, processed)
        except Exception as e:
            logger.error(f"[ComfyUI] 上传图片失败: {e}")
            return None

        # 写入 LoadImage 节点
        load_image_set = False
        if self.input_node and self.input_node in workflow:
            node_data = workflow[self.input_node]
            if isinstance(node_data, dict) and node_data.get("class_type") == "LoadImage":
                node_data["inputs"]["image"] = filename
                load_image_set = True

        if not load_image_set:
            for node_id, node_data in workflow.items():
                if isinstance(node_data, dict) and node_data.get("class_type") == "LoadImage":
                    node_data["inputs"]["image"] = filename
                    load_image_set = True
                    break

        if not load_image_set:
            logger.error("[ComfyUI] 工作流中未找到 LoadImage 节点")
            return None

        # 写入分辨率
        if size and self.resolution_node and self.resolution_node in workflow:
            res_node = workflow[self.resolution_node]
            if isinstance(res_node, dict) and "inputs" in res_node:
                res_node["inputs"][self.resolution_width_field] = size[0]
                res_node["inputs"][self.resolution_height_field] = size[1]
                logger.info(f"[ComfyUI] 输出分辨率: {size[0]}x{size[1]}")

        # 计算最终 fps 与 length：未指定的字段沿用工作流默认值，
        # 然后按 max_frames 上限统一钳制 length
        fps_default = self._read_node_value(workflow, self.fps_node, self.fps_field)
        length_default = self._read_node_value(workflow, self.length_node, self.length_field)

        final_fps = float(fps) if fps is not None else (float(fps_default) if fps_default is not None else None)
        final_length = float(length) if length is not None else (float(length_default) if length_default is not None else None)

        if final_fps is not None:
            final_fps = max(1.0, final_fps)

        if (self.max_frames and self.max_frames > 0 and
                final_fps is not None and final_length is not None):
            max_length = self.max_frames / final_fps
            if final_length > max_length:
                logger.info(
                    f"[ComfyUI] 总帧数 {final_fps * final_length:.1f} 超过上限 {self.max_frames}，"
                    f"将视频长度从 {final_length}s 调整为 {max_length:.2f}s"
                )
                final_length = max_length

        # 写入 fps
        if fps is not None and self.fps_node and self.fps_node in workflow:
            fps_node_data = workflow[self.fps_node]
            if isinstance(fps_node_data, dict) and "inputs" in fps_node_data and final_fps is not None:
                fps_node_data["inputs"][self.fps_field] = final_fps
                logger.info(f"[ComfyUI] 帧率: {final_fps}")

        # 写入视频长度（秒）；即使用户未显式指定 length，只要被 max_frames 钳制了也要写入
        if final_length is not None and self.length_node and self.length_node in workflow:
            len_node_data = workflow[self.length_node]
            if isinstance(len_node_data, dict) and "inputs" in len_node_data:
                # 只在用户主动设置或被钳制时写入，避免覆盖工作流默认值
                user_set = length is not None
                clamped = (length_default is not None and final_length < float(length_default))
                if user_set or clamped:
                    len_node_data["inputs"][self.length_field] = max(0.1, final_length)
                    logger.info(f"[ComfyUI] 视频长度: {max(0.1, final_length)}s")

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

        # 随机化种子
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

        result = await self.api.queue_and_wait_video(
            workflow,
            max_wait=max_wait,
            on_wait_callback=on_wait_callback,
            on_submitted_callback=on_submitted_callback
        )

        if not result:
            logger.error("[ComfyUI] 图生视频生成失败或等待结果超时")

        return result
