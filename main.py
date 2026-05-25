import base64
import json
import re
import shutil
import time
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
from PIL import Image as PILImage
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import UserMessageSegment, TextPart, ImageURLPart

from .comfyui_api import ComfyUIAPI
from .image_to_image import ImageToImage
from .image_to_text import ImageToText
from .image_to_video import ImageToVideo
from .text_to_image import TextToImage

DRAW_ALIASES = ('draw', '绘图', '文生图', '画图')
IMG2IMG_ALIASES = ('img2img', '图生图', '图像编辑', 'i2i')
IMG2VIDEO_ALIASES = ('img2video', '图生视频', '生视频', 'i2v')

# 用户输入图片下载限制
MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024
INPUT_IMAGE_TIMEOUT = 30

# Discord/Telegram 单文件上限
PLATFORM_FILE_SIZE_LIMIT = 10 * 1024 * 1024
SIZE_LIMITED_PLATFORMS = ("discord", "telegram")

LLM_VIOLATION_PATTERNS = [
    re.compile(r'\byes\b', re.IGNORECASE),
    re.compile(r'\bviolation\b', re.IGNORECASE),
    re.compile(r'\bnsfw\b', re.IGNORECASE),
]
LLM_VIOLATION_LITERALS = ('违规', '不安全')


def _is_violation(text: str) -> bool:
    """判断 LLM 回复是否表示违规"""
    cleaned = text.strip()
    # 中文 \b 不工作，所以单独处理"是"
    if cleaned in ('是', 'Yes', 'YES'):
        return True
    for pattern in LLM_VIOLATION_PATTERNS:
        if pattern.search(cleaned):
            return True
    return any(literal in cleaned for literal in LLM_VIOLATION_LITERALS)


def _strip_command_prefix(text: str, aliases: tuple) -> str:
    """剥离命令前缀（包含 / 或 # 等可选前缀），未命中则返回原文"""
    for cmd in aliases:
        pattern = rf'^[\s/#]?{re.escape(cmd)}\s+'
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            return text[match.end():]
        if re.match(rf'^[\s/#]?{re.escape(cmd)}$', text, re.IGNORECASE):
            return ""
    return text


def _safe_workflow_filename(name: str, default: str) -> str:
    """对工作流文件名做 basename 截断，避免路径穿越"""
    if not name:
        return default
    base = Path(name).name
    return base or default


class ComfyUIHub(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.default_negative = config.get("default_negative_prompt", "")
        self.default_chain = config.get("default_chain", False)

        plugin_dir = Path(__file__).parent
        data_root = plugin_dir.parent.parent / "plugin_data"
        data_dir = data_root / "astrbot_plugin_comfyui_hub"
        data_dir.mkdir(parents=True, exist_ok=True)

        workflow_dir = data_dir / "workflows"
        workflow_dir.mkdir(exist_ok=True)

        self.temp_dir = data_dir / "temp"
        self.temp_dir.mkdir(exist_ok=True)

        self.block_tags_file = data_dir / "block_tags.json"
        self.output_block_tags_file = data_dir / "output_block_tags.json"
        self.blocked_users_file = data_dir / "blocked_users.json"
        self.censorship_config_file = data_dir / "censorship_config.json"
        self.sent_messages_file = data_dir / "sent_messages.json"
        self._load_block_data()

        server_url = config.get("server_url", "http://127.0.0.1:8188")
        timeout = config.get("timeout", 300)
        self.api = ComfyUIAPI(server_url, timeout)

        self.txt2img = self._init_txt2img(config, plugin_dir, workflow_dir)
        self.img2txt = self._init_img2txt(config, plugin_dir, workflow_dir)
        self._img2img_engine = self._init_img2img(config, plugin_dir, workflow_dir)
        self._img2video_engine = self._init_img2video(config, plugin_dir, workflow_dir)

        # 输入审查
        self.enable_input_censorship = config.get("enable_input_censorship", True)
        self.input_censorship_use_llm = config.get("input_censorship_use_llm", True)
        self.censorship_prompt = config.get("censorship_prompt", "")
        self.llm_provider_id = config.get("llm_provider_id", "")
        self.admin_bypass_censorship = config.get("admin_bypass_censorship", True)
        self.censorship_failure_mode = config.get("censorship_failure_mode", "fail_open")

        # 输出图片审查
        self.enable_output_censorship = config.get("enable_output_censorship", False)
        self.output_censorship_use_llm = config.get("output_censorship_use_llm", True)
        self.output_censorship_use_tagger = config.get("output_censorship_use_tagger", True)
        self.output_censorship_prompt = config.get("output_censorship_prompt", "")

        # 图生图输入图片审查
        self.enable_img2img_input_censorship = config.get("enable_img2img_input_censorship", False)
        self.img2img_input_censorship_use_llm = config.get("img2img_input_censorship_use_llm", True)

        # 图生图输出图片审查
        self.enable_img2img_output_censorship = config.get("enable_img2img_output_censorship", False)
        self.img2img_output_censorship_use_llm = config.get("img2img_output_censorship_use_llm", True)
        self.img2img_output_censorship_use_tagger = config.get("img2img_output_censorship_use_tagger", True)

    def _init_txt2img(self, config, plugin_dir, workflow_dir):
        if not config.get("enable_txt2img", True):
            return None
        workflow_filename = _safe_workflow_filename(
            config.get("txt2img_workflow", "example_text2img.json"),
            "example_text2img.json",
        )
        workflow_path = workflow_dir / workflow_filename
        if not workflow_path.exists():
            workflow_path = workflow_dir / "example_text2img.json"
            example_path = plugin_dir / "example_text2img.json"
            if example_path.exists() and not workflow_path.exists():
                shutil.copy(example_path, workflow_path)

        return TextToImage(
            self.api,
            str(workflow_path),
            config.get("txt2img_positive_node", "6"),
            config.get("txt2img_negative_node", "7"),
            config.get("resolution_node", ""),
            config.get("resolution_width_field", "width"),
            config.get("resolution_height_field", "height"),
            config.get("upscale_node", ""),
            config.get("upscale_scale_field", "resize_scale"),
        )

    def _init_img2txt(self, config, plugin_dir, workflow_dir):
        if not config.get("enable_tagger", True):
            return None
        workflow_filename = _safe_workflow_filename(config.get("tagger_workflow", ""), "example_tagger.json")
        tagger_workflow_path = workflow_dir / workflow_filename
        if not tagger_workflow_path.exists():
            tagger_workflow_path = workflow_dir / "example_tagger.json"
            example_tagger_path = plugin_dir / "example_tagger.json"
            if example_tagger_path.exists() and not tagger_workflow_path.exists():
                shutil.copy(example_tagger_path, tagger_workflow_path)

        if not tagger_workflow_path.exists():
            return None

        return ImageToText(
            self.api,
            str(tagger_workflow_path),
            config.get("tagger_output_node", ""),
            config.get("tagger_input_node", ""),
        )

    def _init_img2img(self, config, plugin_dir, workflow_dir):
        if not config.get("enable_img2img", True):
            return None
        workflow_filename = _safe_workflow_filename(
            config.get("img2img_workflow", "example_img2img.json"),
            "example_img2img.json",
        )
        workflow_path = workflow_dir / workflow_filename
        if not workflow_path.exists():
            example_path = plugin_dir / "example_img2img.json"
            if example_path.exists() and not workflow_path.exists():
                shutil.copy(example_path, workflow_path)

        if not workflow_path.exists():
            return None

        input_node_str = config.get("img2img_input_node", "15")
        input_nodes_list = [n.strip() for n in input_node_str.split(",") if n.strip()]
        return ImageToImage(
            self.api,
            str(workflow_path),
            config.get("img2img_positive_node", "20"),
            config.get("img2img_negative_node", "21"),
            input_nodes_list,
        )

    def _init_img2video(self, config, plugin_dir, workflow_dir):
        if not config.get("enable_img2video", True):
            return None
        workflow_filename = _safe_workflow_filename(
            config.get("img2video_workflow", "example_image2video.json"),
            "example_image2video.json",
        )
        workflow_path = workflow_dir / workflow_filename
        if not workflow_path.exists():
            example_path = plugin_dir / "example_image2video.json"
            if example_path.exists():
                shutil.copy(example_path, workflow_path)

        if not workflow_path.exists():
            return None

        return ImageToVideo(
            self.api,
            str(workflow_path),
            config.get("img2video_positive_node", "3"),
            config.get("img2video_negative_node", "4"),
            config.get("img2video_input_node", "2"),
            config.get("img2video_resolution_node", "1"),
            config.get("img2video_resolution_width_field", "width"),
            config.get("img2video_resolution_height_field", "height"),
            config.get("img2video_fps_node", "18"),
            config.get("img2video_fps_field", "value"),
            config.get("img2video_length_node", "20"),
            config.get("img2video_length_field", "value"),
            int(config.get("img2video_max_fps", 24)),
        )

    # ----- 数据加载与持久化 -----

    def _load_block_data(self):
        self.block_tags = set()
        self.output_block_tags = set()
        self.blocked_users = {}
        self.censored_groups = set()
        self.sent_messages = {}
        self.message_cache_ttl = 120

        self._load_json_set(self.output_block_tags_file, "output_block_tags")
        self._load_json_set(self.block_tags_file, "block_tags")

        if self.blocked_users_file.exists():
            try:
                with open(self.blocked_users_file, "r", encoding='utf-8') as f:
                    self.blocked_users = json.load(f)
            except Exception as e:
                logger.error(f"Error loading blocked users: {e}")

        if self.censorship_config_file.exists():
            try:
                with open(self.censorship_config_file, "r", encoding='utf-8') as f:
                    cfg = json.load(f)
                    self.censored_groups = set(cfg.get("groups", []))
            except Exception as e:
                logger.error(f"Error loading censorship config: {e}")

        if self.sent_messages_file.exists():
            try:
                with open(self.sent_messages_file, "r", encoding='utf-8') as f:
                    data = json.load(f)
                    self.sent_messages = {str(k): v for k, v in data.items()}
                    self._cleanup_expired_messages()
            except Exception as e:
                logger.error(f"Error loading sent messages: {e}")

    def _load_json_set(self, path: Path, attr: str):
        if not path.exists():
            return
        try:
            with open(path, "r", encoding='utf-8') as f:
                setattr(self, attr, set(json.load(f)))
        except Exception as e:
            logger.error(f"Error loading {attr}: {e}")

    def _cleanup_expired_messages(self):
        """清理过期的消息ID"""
        current_time = time.time()
        for group_id in list(self.sent_messages.keys()):
            valid_messages = [
                msg_data for msg_data in self.sent_messages[group_id]
                if isinstance(msg_data, dict) and
                current_time - msg_data.get('timestamp', 0) <= self.message_cache_ttl
            ]
            self.sent_messages[group_id] = valid_messages
            if not valid_messages:
                del self.sent_messages[group_id]

    @staticmethod
    def _atomic_write_json(path: Path, payload):
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            tmp.replace(path)
        except Exception as e:
            logger.error(f"Error saving {path.name}: {e}")

    def _save_block_tags(self):
        self._atomic_write_json(self.block_tags_file, list(self.block_tags))

    def _save_output_block_tags(self):
        self._atomic_write_json(self.output_block_tags_file, list(self.output_block_tags))

    def _save_blocked_users(self):
        self._atomic_write_json(self.blocked_users_file, self.blocked_users)

    def _save_censorship(self):
        self._atomic_write_json(self.censorship_config_file, {"groups": list(self.censored_groups)})

    def _save_sent_messages(self):
        self._cleanup_expired_messages()
        self._atomic_write_json(self.sent_messages_file, self.sent_messages)

    # ----- 消息发送 -----

    async def _send_text_message(self, event: AstrMessageEvent, message: str) -> Optional[str]:
        """发送文字消息并记录消息ID"""
        result = await self._call_send_api(event, message)
        return self._extract_and_record_message(result, event)

    async def _send_image_message(self, event: AstrMessageEvent, image_file: Path, chain: bool = False) -> Optional[str]:
        """发送图片消息并记录消息ID"""
        group_id = event.get_group_id()

        if group_id and chain:
            try:
                forward_msg = [{
                    "type": "node",
                    "data": {
                        "user_id": int(event.get_sender_id()),
                        "nickname": "ComfyUI",
                        "content": [
                            {"type": "image", "data": {"file": f"file://{image_file}"}}
                        ],
                    },
                }]
                result = await event.bot.api.call_action(
                    "send_group_forward_msg",
                    group_id=int(group_id),
                    messages=forward_msg,
                )
            except Exception as e:
                logger.error(f"合并转发发送失败: {e}")
                result = await self._call_send_api(event, f"[CQ:image,file=file://{image_file}]")
        else:
            result = await self._call_send_api(event, f"[CQ:image,file=file://{image_file}]")

        return self._extract_and_record_message(result, event)

    async def _call_send_api(self, event: AstrMessageEvent, message):
        """调用发送消息 API"""
        if event.get_platform_name() != "aiocqhttp":
            return None

        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if group_id:
            return await event.bot.api.call_action(
                "send_group_msg",
                group_id=int(group_id),
                message=message,
            )
        if user_id:
            return await event.bot.api.call_action(
                "send_private_msg",
                user_id=int(user_id),
                message=message,
            )
        return None

    def _extract_and_record_message(self, result, event: AstrMessageEvent) -> Optional[str]:
        """从API返回结果中提取并记录消息ID"""
        message_id = None

        if result:
            if isinstance(result, dict):
                data = result.get('data')
                if isinstance(data, dict):
                    message_id = data.get('message_id')
                elif data:
                    message_id = str(data)
                elif 'message_id' in result:
                    message_id = result['message_id']
            elif isinstance(result, (int, str)):
                message_id = str(result)

        if message_id:
            key = str(event.get_group_id()) if event.get_group_id() else str(event.get_sender_id())
            self.sent_messages.setdefault(key, []).append({
                'message_id': str(message_id),
                'timestamp': time.time(),
                'user_id': str(event.get_sender_id()),
            })
            self._save_sent_messages()

        return message_id

    # ----- 审查 -----

    def _on_censorship_error(self, scope: str, error: Exception) -> Tuple[bool, str]:
        """统一处理审查链路异常，按 censorship_failure_mode 决定放行或拦截"""
        logger.error(f"[{scope}] 审查异常: {error}")
        if self.censorship_failure_mode == "fail_closed":
            return False, "⚠️ 审查服务暂不可用，请稍后再试。"
        return True, f"审查出错: {error}"

    async def _check_safety_with_llm(self, event: AstrMessageEvent, text: str) -> Tuple[bool, str]:
        """使用 AstrBot 内置 LLM 检查文本安全（用于输入审查）"""
        try:
            if not self.input_censorship_use_llm:
                return True, "LLM Disabled"

            provider_id = self.llm_provider_id
            if not provider_id:
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if not provider_id:
                    return True, "No Provider"

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=text,
                system_prompt=self.censorship_prompt,
            )

            if not llm_resp or not llm_resp.completion_text:
                return True, "No Response"

            result = llm_resp.completion_text.strip()
            logger.info(f"[输入审查] LLM原始响应: {result}")

            violation = _is_violation(result)
            logger.info(f"[输入审查] 判定结果: {'违规' if violation else '通过'}")

            if violation:
                return False, "AI审查拦截"
            return True, ""

        except Exception as e:
            return self._on_censorship_error("输入审查", e)

    async def _check_image_safety_with_llm(self, event: AstrMessageEvent, image_data: bytes,
                                           is_img2img_input: bool = False) -> Tuple[bool, str]:
        """使用多模态 LLM 检查图片安全"""
        try:
            if is_img2img_input:
                if not self.img2img_input_censorship_use_llm:
                    return True, "LLM Disabled"
            else:
                if not self.output_censorship_use_llm:
                    return True, "LLM Disabled"

            provider_id = self.llm_provider_id
            if not provider_id:
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if not provider_id:
                    return True, "No Provider"

            image_base64 = base64.b64encode(image_data).decode('utf-8')
            try:
                img = PILImage.open(BytesIO(image_data))
                mime_type = f"image/{img.format.lower()}"
            except Exception:
                mime_type = "image/png"
            image_url = f"data:{mime_type};base64,{image_base64}"

            check_type = "图生图输入" if is_img2img_input else "输出"
            user_msg = UserMessageSegment(content=[
                TextPart(text="请审查这张图片是否包含违规内容。"),
                ImageURLPart(image_url=ImageURLPart.ImageURL(url=image_url)),
            ])

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                contexts=[user_msg],
                system_prompt=self.output_censorship_prompt,
            )

            if not llm_resp or not llm_resp.completion_text:
                return True, "No Response"

            result = llm_resp.completion_text.strip()
            logger.info(f"[{check_type}审查] 多模态LLM原始响应: {result}")

            violation = _is_violation(result)
            logger.info(f"[{check_type}审查] 判定结果: {'违规' if violation else '通过'}")

            if violation:
                return False, "AI审查拦截"
            return True, ""

        except Exception as e:
            return self._on_censorship_error("图片审查", e)

    def _resolve_output_censor_options(self, check_type: str) -> tuple:
        """根据 check_type 选择对应的输出审查开关"""
        if check_type == "文生图":
            return (
                self.enable_output_censorship,
                self.output_censorship_use_llm,
                self.output_censorship_use_tagger,
            )
        return (
            self.enable_img2img_output_censorship,
            self.img2img_output_censorship_use_llm,
            self.img2img_output_censorship_use_tagger,
        )

    async def _check_output_censorship(self, event: AstrMessageEvent, image_data: bytes,
                                       check_type: str = "文生图") -> Tuple[bool, str]:
        """统一的输出图片审查"""
        group_id = event.get_group_id()
        is_censorship_enabled = group_id and group_id in self.censored_groups

        is_admin = event.is_admin()
        should_bypass_censorship = is_admin and self.admin_bypass_censorship

        enable, use_llm, use_tagger = self._resolve_output_censor_options(check_type)

        if not (is_censorship_enabled and not should_bypass_censorship and enable):
            logger.info(f"[{check_type}] 未开启输出审查或已绕过")
            return True, ""

        if use_tagger and self.img2txt:
            tags_text = await self.img2txt.generate(image_data)
            if tags_text:
                logger.info(f"[{check_type}] 输出图片标签: {tags_text}")
                is_safe_simple, reason_simple = self._check_simple_tags(tags_text)
                if not is_safe_simple:
                    logger.info(f"[{check_type}] 输出图片关键词审查拦截: {reason_simple}")
                    return False, f"⚠️ 生成的图片{reason_simple}，已被审查系统拒绝。"
            else:
                logger.info(f"[{check_type}] Tagger 未返回标签，跳过 tagger 审查")
        elif use_tagger and not self.img2txt:
            logger.info(f"[{check_type}] Tagger 不可用，跳过 tagger 审查")

        if use_llm:
            is_safe, _ = await self._check_image_safety_with_llm(event, image_data, is_img2img_input=False)
            if not is_safe:
                logger.info(f"[{check_type}] 输出图片多模态LLM审查拦截")
                return False, "⚠️ 生成的图片包含敏感内容，已被AI审查系统拒绝。"

        logger.info(f"[{check_type}] 输出图片审查通过")
        return True, ""

    async def _check_img2img_input_censorship(self, event: AstrMessageEvent,
                                              image_data: bytes) -> Tuple[bool, str]:
        """图生图输入图片审查（多模态LLM）"""
        group_id = event.get_group_id()
        is_censorship_enabled = group_id and group_id in self.censored_groups
        is_admin = event.is_admin()
        should_bypass_censorship = is_admin and self.admin_bypass_censorship

        if not (is_censorship_enabled and not should_bypass_censorship and self.enable_img2img_input_censorship):
            logger.info("[图生图] 未开启输入图片审查或已绕过")
            return True, ""

        if self.img2img_input_censorship_use_llm:
            is_safe, _ = await self._check_image_safety_with_llm(event, image_data, is_img2img_input=True)
            if not is_safe:
                logger.info("[图生图] 输入图片多模态LLM审查拦截")
                return False, "⚠️ 您输入的图片包含敏感内容，已被AI审查系统拒绝。"

        logger.info("[图生图] 输入图片审查通过")
        return True, ""

    async def _run_input_text_censorship(self, event: AstrMessageEvent, positive: str) -> Tuple[Optional[str], Optional[str]]:
        """对正面提示词跑一遍（封禁检查 + block tag + LLM）输入审查

        Returns:
            (positive_or_None, message_or_None):
              - positive_or_None: 通过则返回（可能附加 sfw 后缀的）正面提示词；不通过返回 None
              - message_or_None: 不通过时的提示文本
        """
        user_id = event.get_sender_id()
        current_time = time.time()

        # 封禁期检查
        if user_id in self.blocked_users:
            expire_time = self.blocked_users[user_id]
            if current_time < expire_time:
                remaining = int(expire_time - current_time)
                return None, f"由于触发违规词，您已被禁止使用绘图功能。剩余时间: {remaining} 秒。"
            del self.blocked_users[user_id]
            self._save_blocked_users()

        group_id = event.get_group_id()
        is_censorship_enabled = group_id and group_id in self.censored_groups
        is_admin = event.is_admin()
        should_bypass_censorship = is_admin and self.admin_bypass_censorship

        if not (is_censorship_enabled and not should_bypass_censorship and self.enable_input_censorship):
            return positive, None

        # 本地 block tag
        for tag in self.block_tags:
            if tag.lower() in positive.lower():
                self.blocked_users[user_id] = current_time + 120
                self._save_blocked_users()
                return None, f"⚠️ 违规：检测到敏感词 '{tag}'。您将被禁服务 2 分钟。"

        # LLM 审查
        if self.input_censorship_use_llm:
            is_safe, _ = await self._check_safety_with_llm(event, positive)
            if not is_safe:
                self.blocked_users[user_id] = current_time + 120
                self._save_blocked_users()
                logger.info("LLM 审查拦截")
                return None, "⚠️ 您的请求包含敏感内容，已被AI审查系统拒绝。您将被禁服务 2 分钟。"

        # 自动追加 sfw
        if "sfw" not in positive.lower() and "safe" not in positive.lower():
            positive = (positive + ", sfw, safe for work").strip(", ")
        return positive, None

    def _check_simple_tags(self, tags_text: str) -> tuple:
        """精确匹配 tag token，避免子串误命中（man → woman/human）"""
        tags = [tag.strip().lower() for tag in tags_text.split(',')]
        block_lower = {kw.lower() for kw in self.output_block_tags}
        for tag in tags:
            tokens = re.split(r'[\s_]+', tag)
            for keyword in block_lower:
                # 允许 keyword 自身含空格/下划线，作为整体匹配
                kw_tokens = re.split(r'[\s_]+', keyword) if (' ' in keyword or '_' in keyword) else None
                if kw_tokens:
                    # 子序列匹配
                    n = len(kw_tokens)
                    for i in range(len(tokens) - n + 1):
                        if tokens[i:i + n] == kw_tokens:
                            return False, f"检测到敏感内容 '{keyword}'"
                elif keyword in tokens:
                    return False, f"检测到敏感内容 '{keyword}'"
        return True, ""

    # ----- 参数解析 -----

    def _parse_params(self, text: str) -> tuple:
        """解析用户输入的参数"""
        params = {
            'positive': '',
            'negative': self.default_negative,
            'chain': self.default_chain,
            'width': None,
            'height': None,
            'scale': None,
        }

        chain_pattern = r'(?:chain|转发|合并转发)\s*[:=]?\s*(true|false|是|否|开|关)'
        m = re.search(chain_pattern, text, re.IGNORECASE)
        if m:
            params['chain'] = m.group(1).lower() in ['true', '是', '开']
            text = re.sub(chain_pattern, '', text, flags=re.IGNORECASE).strip()

        scale_pattern = r'(?:scale|倍率|超分|放大)\s*[:=]?\s*(\d+(?:\.\d+)?)'
        m = re.search(scale_pattern, text, re.IGNORECASE)
        if m:
            params['scale'] = float(m.group(1))
            text = re.sub(scale_pattern, '', text, flags=re.IGNORECASE).strip()

        width_pattern = r'(?:\s+|^)(?:宽|宽度|w|width|x)\s*[:=]?\s*(\d+)'
        m = re.search(width_pattern, text, re.IGNORECASE)
        if m:
            params['width'] = int(m.group(1))
            text = re.sub(width_pattern, '', text, flags=re.IGNORECASE).strip()

        height_pattern = r'(?:\s+|^)(?:高|高度|h|height|y)\s*[:=]?\s*(\d+)'
        m = re.search(height_pattern, text, re.IGNORECASE)
        if m:
            params['height'] = int(m.group(1))
            text = re.sub(height_pattern, '', text, flags=re.IGNORECASE).strip()

        positive_aliases = r'(?:正面|正向|正面提示词|正向提示词)'
        negative_aliases = r'(?:负面|反向|负面提示词|反向提示词)'
        new_format_pattern = (
            rf'({positive_aliases})\s*[:=]?\s*[\[{{]([^\]}}]+?)[\]}}]'
            rf'|({negative_aliases})\s*[:=]?\s*[\[{{]([^\]}}]+?)[\]}}]'
        )
        matches = list(re.finditer(new_format_pattern, text, re.IGNORECASE))

        if matches:
            for match in matches:
                if match.group(1):
                    params['positive'] = match.group(2).strip()
                elif match.group(3):
                    params['negative'] = match.group(4).strip()

            if not params['positive']:
                remaining = re.sub(new_format_pattern, '', text, flags=re.IGNORECASE).strip()
                if remaining:
                    params['positive'] = remaining
        else:
            parts = text.split('|')
            params['positive'] = parts[0].strip()
            if len(parts) > 1:
                params['negative'] = parts[1].strip()

        return (params['positive'], params['negative'], params['chain'],
                params['width'], params['height'], params['scale'])

    # ----- 队列回调工厂 -----

    def _make_queue_callbacks(self, event: AstrMessageEvent, generating_msg: str):
        async def on_queue_wait(running_count: int, pending_count: int, waited: float):
            await self._send_text_message(
                event,
                f"仍在排队等待中...（前面还有 {pending_count} 个任务）",
            )

        async def on_submitted(prompt_id: str, queue_position: int, tasks_ahead: int):
            if queue_position > 0:
                await self._send_text_message(
                    event,
                    f"已提交，队列第 {queue_position} 位（前方 {tasks_ahead} 个任务）",
                )
            else:
                await self._send_text_message(event, generating_msg)

        return on_queue_wait, on_submitted

    # ----- 图片下载 / 压缩 -----

    @staticmethod
    async def _get_image_data(image_component) -> Optional[bytes]:
        """从 Image 组件获取图片数据，带超时和大小限制"""
        try:
            if hasattr(image_component, 'url') and image_component.url:
                timeout = aiohttp.ClientTimeout(total=INPUT_IMAGE_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(image_component.url) as resp:
                        if resp.status != 200:
                            return None
                        # 通过 Content-Length 提前拒绝
                        content_length = resp.headers.get("Content-Length")
                        if content_length and int(content_length) > MAX_INPUT_IMAGE_BYTES:
                            logger.error(
                                f"图片过大 ({content_length} 字节)，拒绝下载"
                            )
                            return None
                        # 按 chunk 累加，超过限制中断
                        buffer = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            buffer.extend(chunk)
                            if len(buffer) > MAX_INPUT_IMAGE_BYTES:
                                logger.error(
                                    f"图片下载超过 {MAX_INPUT_IMAGE_BYTES} 字节限制，已中断"
                                )
                                return None
                        return bytes(buffer)
        except Exception as e:
            logger.error(f"通过URL获取图片失败: {e}")

        try:
            if hasattr(image_component, 'convert_to_file_path') and callable(image_component.convert_to_file_path):
                file_path = await image_component.convert_to_file_path()
                if file_path and Path(file_path).exists():
                    if Path(file_path).stat().st_size > MAX_INPUT_IMAGE_BYTES:
                        logger.error("本地图片文件过大，拒绝读取")
                        return None
                    with open(file_path, 'rb') as f:
                        return f.read()
        except Exception as e:
            logger.error(f"通过文件路径获取图片失败: {e}")

        return None

    def _maybe_compress_for_platform(self, image_data: bytes, platform_name: str) -> Tuple[Path, Optional[str]]:
        """对超大图片做平台限制下的压缩，返回 (临时文件路径, 警告信息或 None)"""
        temp_file = self.temp_dir / f"{int(time.time())}.png"
        with open(temp_file, "wb") as f:
            f.write(image_data)

        if platform_name not in SIZE_LIMITED_PLATFORMS:
            return temp_file, None

        file_size = len(image_data)
        if file_size <= PLATFORM_FILE_SIZE_LIMIT:
            return temp_file, None

        size_mb = file_size / (1024 * 1024)
        logger.info(f"图片大小 {size_mb:.1f}MB 超过限制，尝试压缩...")

        try:
            img = PILImage.open(BytesIO(image_data))
        except Exception as e:
            logger.error(f"图片压缩失败: {e}")
            return temp_file, f"⚠️ 警告：生成的图片为 {size_mb:.1f}MB，超过平台默认 10MB 限制，压缩失败"

        # 依次尝试 WebP90 → AVIF85 → WebP 80/70/60/50
        attempts = [
            ('WEBP', 90, '.webp'),
            ('AVIF', 85, '.avif'),
            ('WEBP', 80, '.webp'),
            ('WEBP', 70, '.webp'),
            ('WEBP', 60, '.webp'),
            ('WEBP', 50, '.webp'),
        ]
        for fmt, quality, ext in attempts:
            try:
                buffer = BytesIO()
                img.save(buffer, format=fmt, quality=quality)
                if buffer.tell() <= PLATFORM_FILE_SIZE_LIMIT:
                    new_temp = self.temp_dir / f"{int(time.time())}{ext}"
                    with open(new_temp, "wb") as f:
                        f.write(buffer.getvalue())
                    final_mb = buffer.tell() / (1024 * 1024)
                    logger.info(f"成功压缩为 {fmt}（quality={quality}），大小 {final_mb:.1f}MB")
                    return new_temp, None
            except Exception as e:
                logger.error(f"{fmt}(quality={quality}) 压缩失败: {e}")
                continue

        return temp_file, f"⚠️ 警告：原图 {size_mb:.1f}MB，压缩后仍超过 10MB 限制，可能无法发送"

    # ----- 子命令分发（管理员） -----

    async def _handle_admin_subcommand(self, event: AstrMessageEvent, text: str):
        """处理 #draw $xxx 形式的管理员子命令，返回 (handled, message_or_None)"""
        if not text.startswith('$'):
            return False, None
        if not event.is_admin():
            return True, "❌ 仅管理员可执行此操作。"

        if text.startswith('$enable_censorship'):
            group_id = event.get_group_id()
            if not group_id:
                return True, "⚠️ 此命令仅支持在群组中使用。"
            self.censored_groups.add(group_id)
            self._save_censorship()
            return True, "✅ 已在当前群组开启审查功能。"

        if text.startswith('$disable_censorship'):
            group_id = event.get_group_id()
            if not group_id:
                return True, "⚠️ 此命令仅支持在群组中使用。"
            if group_id in self.censored_groups:
                self.censored_groups.remove(group_id)
                self._save_censorship()
            return True, "✅ 已在当前群组关闭审查功能。"

        if text.startswith('$add_block_tag'):
            tags_part = text[len('$add_block_tag'):].strip()
            new_tags = self._parse_tag_list(tags_part)
            if not new_tags:
                return True, "用法: #draw $add_block_tag tag1,tag2 或 [tag1] [tag2]"
            self.block_tags.update(new_tags)
            self._save_block_tags()
            return True, f"✅ 已成功添加违规词: {', '.join(new_tags)}"

        if text.startswith('$add_output_block_tag'):
            tags_part = text[len('$add_output_block_tag'):].strip()
            new_tags = self._parse_tag_list(tags_part)
            if not new_tags:
                return True, "用法: #draw $add_output_block_tag tag1,tag2"
            self.output_block_tags.update(new_tags)
            self._save_output_block_tags()
            return True, f"✅ 已添加输出违规词: {', '.join(new_tags)}"

        if text.startswith('$remove_output_block_tag'):
            tags_part = text[len('$remove_output_block_tag'):].strip()
            rem_tags = self._parse_tag_list(tags_part)
            if not rem_tags:
                return True, "用法: #draw $remove_output_block_tag tag1,tag2"
            removed = [t for t in rem_tags if t in self.output_block_tags]
            self.output_block_tags -= set(rem_tags)
            self._save_output_block_tags()
            if removed:
                return True, f"✅ 已移除输出违规词: {', '.join(removed)}"
            return True, "⚠️ 未找到指定的输出违规词"

        if text.startswith('$remove_block_tag'):
            tags_part = text[len('$remove_block_tag'):].strip()
            rem_tags = self._parse_tag_list(tags_part)
            if not rem_tags:
                return True, "用法: #draw $remove_block_tag tag1,tag2 或 [tag1] [tag2]"
            removed = []
            for t in rem_tags:
                if t in self.block_tags:
                    self.block_tags.remove(t)
                    removed.append(t)
            self._save_block_tags()
            if removed:
                return True, f"✅ 已成功移除违规词: {', '.join(removed)}"
            return True, "⚠️ 未找到指定的违规词。"

        return False, None

    @staticmethod
    def _parse_tag_list(tags_part: str) -> list:
        raw_tags = re.split(r',|\[|\]', tags_part)
        return [t.strip() for t in raw_tags if t.strip()]

    # ----- 提取消息中的图片 -----

    async def _collect_images_from_event(self, event: AstrMessageEvent, take_first_only: bool = False) -> list:
        """从消息（含 Reply）中收集图片数据"""
        from astrbot.api.message_components import Image as ImageComponent, Reply as ReplyComponent
        chain = event.get_messages()
        images = []
        for msg in chain:
            if isinstance(msg, ReplyComponent) and msg.chain:
                for chain_msg in msg.chain:
                    if isinstance(chain_msg, ImageComponent):
                        data = await self._get_image_data(chain_msg)
                        if data:
                            images.append(data)
                            if take_first_only:
                                return images
            elif isinstance(msg, ImageComponent):
                data = await self._get_image_data(msg)
                if data:
                    images.append(data)
                    if take_first_only:
                        return images
        return images
    # ----- 命令：文生图 -----

    @filter.command("draw", alias=set(DRAW_ALIASES[1:]))
    async def draw(self, event: AstrMessageEvent):
        """文生图指令"""
        if not self.txt2img:
            yield event.plain_result("⚠️ 文生图功能未开启")
            return

        text = _strip_command_prefix(event.message_str.strip(), DRAW_ALIASES)

        # 管理员子命令
        handled, msg = await self._handle_admin_subcommand(event, text)
        if handled:
            if msg:
                yield event.plain_result(msg)
            return

        if not text:
            yield event.plain_result("请输入提示词")
            return

        positive, negative, chain, width, height, scale = self._parse_params(text)

        positive, censor_msg = await self._run_input_text_censorship(event, positive)
        if positive is None:
            yield event.plain_result(censor_msg or "⚠️ 已被审查系统拒绝。")
            return

        if not positive:
            yield event.plain_result("请输入正面提示词")
            return

        on_wait, on_submitted = self._make_queue_callbacks(event, "正在生成图片...")
        image_data = await self.txt2img.generate(
            positive, negative, width, height, scale,
            on_wait_callback=on_wait,
            on_submitted_callback=on_submitted,
        )

        if not image_data:
            yield event.plain_result("生成失败")
            return

        is_safe, message = await self._check_output_censorship(event, image_data, "文生图")
        if not is_safe:
            yield event.plain_result(message)
            return

        temp_file, warn = self._maybe_compress_for_platform(image_data, event.get_platform_name())
        if warn:
            yield event.plain_result(warn)

        if event.get_platform_name() != "aiocqhttp":
            yield event.image_result(str(temp_file))
        else:
            await self._send_image_message(event, temp_file, chain)

        event.stop_event()

    # ----- 命令：撤回 -----

    @filter.command("delete", alias={'撤回', 'recall'})
    async def delete_msg(self, event: AstrMessageEvent):
        """引用撤回绘图功能输出的消息"""
        chain = event.get_messages()
        if not chain:
            return

        first_seg = chain[0] if len(chain) > 0 else None
        if not first_seg:
            return

        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("❌ 此功能仅支持 aiocqhttp 平台")
            return

        if not isinstance(first_seg, Reply):
            yield event.plain_result("❌ 请引用要撤回的绘图消息")
            return

        group_id = event.get_group_id()
        current_time = time.time()
        is_admin = event.is_admin()

        is_valid_message = is_admin
        msg_index_to_remove = None

        if not is_admin and group_id:
            group_id_str = str(group_id)
            sent_msgs = self.sent_messages.get(group_id_str, [])
            valid_msgs = []
            for i, msg_data in enumerate(sent_msgs):
                if not isinstance(msg_data, dict):
                    continue
                msg_id = msg_data.get('message_id')
                msg_timestamp = msg_data.get('timestamp', 0)
                if current_time - msg_timestamp > self.message_cache_ttl:
                    continue
                if msg_id == str(first_seg.id):
                    is_valid_message = True
                    msg_index_to_remove = i
                valid_msgs.append(msg_data)
            self.sent_messages[group_id_str] = valid_msgs

        if not is_valid_message:
            return

        try:
            client = event.bot
            await client.delete_msg(message_id=int(first_seg.id))
            if is_valid_message and group_id and msg_index_to_remove is not None:
                group_id_str = str(group_id)
                self.sent_messages[group_id_str].pop(msg_index_to_remove)
                self._save_sent_messages()
            event.stop_event()
        except Exception as e:
            logger.error(f"撤回失败: {e}")

    # ----- 命令：图片标签识别 -----

    @filter.command("tagger", alias={'tag', '标签'})
    async def tagger(self, event: AstrMessageEvent):
        """图片标签识别指令"""
        if not self.img2txt:
            yield event.plain_result("⚠️ 图片标签识别功能未开启")
            return

        images = await self._collect_images_from_event(event, take_first_only=True)
        if not images:
            yield event.plain_result("请发送或回复一张图片")
            return

        image_data = images[0]
        logger.info(f"成功获取图片数据，大小: {len(image_data)} 字节")

        on_wait, on_submitted = self._make_queue_callbacks(event, "正在识别图片标签...")
        result_text = await self.img2txt.generate(
            image_data,
            on_wait_callback=on_wait,
            on_submitted_callback=on_submitted,
        )
        logger.info(f"标签识别结果: {result_text}")

        if not result_text:
            yield event.plain_result("识别失败")
            return

        # Tagger 输出按 Danbooru 风格用下划线连接，替换为空格更可读
        formatted_tags = result_text.replace('_', ' ')
        logger.info(f"格式化后的标签: {formatted_tags}")
        yield event.plain_result(f"【标签】\n{formatted_tags}")
        event.stop_event()

    # ----- 命令：图生图 -----

    @filter.command("img2img", alias=set(IMG2IMG_ALIASES[1:]))
    async def cmd_img2img(self, event: AstrMessageEvent):
        """图生图指令"""
        if not self._img2img_engine:
            yield event.plain_result("⚠️ 图生图功能未开启")
            return

        image_data_list = await self._collect_images_from_event(event)
        if not image_data_list:
            yield event.plain_result("请发送或回复一张图片")
            return

        text = _strip_command_prefix(event.message_str.strip(), IMG2IMG_ALIASES)
        positive, negative, chain_param, _w, _h, _s = self._parse_params(text)

        if not positive:
            yield event.plain_result("请输入提示词")
            return

        logger.info(
            f"成功获取 {len(image_data_list)} 张图片，大小: "
            f"{', '.join(f'{len(d)} 字节' for d in image_data_list)}"
        )

        # 输入图片审查
        for img_data in image_data_list:
            is_safe, message = await self._check_img2img_input_censorship(event, img_data)
            if not is_safe:
                yield event.plain_result(message)
                return

        # 输入文本审查
        positive, censor_msg = await self._run_input_text_censorship(event, positive)
        if positive is None:
            yield event.plain_result(censor_msg or "⚠️ 已被审查系统拒绝。")
            return

        on_wait, on_submitted = self._make_queue_callbacks(event, "正在生成图片...")
        result_image = await self._img2img_engine.generate(
            image_data_list, positive, negative,
            on_wait_callback=on_wait,
            on_submitted_callback=on_submitted,
        )

        if not result_image:
            yield event.plain_result("生成失败")
            return

        is_safe, message = await self._check_output_censorship(event, result_image, "图生图")
        if not is_safe:
            yield event.plain_result(message)
            return

        temp_file, warn = self._maybe_compress_for_platform(result_image, event.get_platform_name())
        if warn:
            yield event.plain_result(warn)

        if event.get_platform_name() != "aiocqhttp":
            yield event.image_result(str(temp_file))
        else:
            await self._send_image_message(event, temp_file, chain_param)

        event.stop_event()

    # ----- 命令：图生视频 -----

    @filter.command("img2video", alias=set(IMG2VIDEO_ALIASES[1:]))
    async def cmd_img2video(self, event: AstrMessageEvent):
        """图生视频指令"""
        if not self._img2video_engine:
            yield event.plain_result("⚠️ 图生视频功能未开启")
            return

        from astrbot.api.message_components import Video as VideoComponent

        images = await self._collect_images_from_event(event, take_first_only=True)
        image_data = images[0] if images else None

        text = _strip_command_prefix(event.message_str.strip(), IMG2VIDEO_ALIASES)
        positive, negative, _chain_param, _w, _h, _s = self._parse_params(text)

        # 从 positive 中再额外提取 fps / length
        fps_value: Optional[float] = None
        length_value: Optional[float] = None
        if positive:
            fps_pattern = r'(?:\s+|^)(?:fps|帧率)\s*[:=]?\s*(\d+(?:\.\d+)?)'
            m = re.search(fps_pattern, positive, re.IGNORECASE)
            if m:
                fps_value = float(m.group(1))
                positive = re.sub(fps_pattern, '', positive, flags=re.IGNORECASE).strip()

            length_pattern = r'(?:\s+|^)(?:length|len|时长|长度|秒)\s*[:=]?\s*(\d+(?:\.\d+)?)'
            m = re.search(length_pattern, positive, re.IGNORECASE)
            if m:
                length_value = float(m.group(1))
                positive = re.sub(length_pattern, '', positive, flags=re.IGNORECASE).strip()

        if not image_data:
            yield event.plain_result("⚠️ 图生视频需要提供图片")
            return

        logger.info(
            f"[图生视频] 已接收图片 {len(image_data)} 字节，"
            f"提示词: {positive or '(空)'}, fps={fps_value}, length={length_value}"
        )

        is_safe, message = await self._check_img2img_input_censorship(event, image_data)
        if not is_safe:
            yield event.plain_result(message)
            return

        positive, censor_msg = await self._run_input_text_censorship(event, positive)
        if positive is None:
            yield event.plain_result(censor_msg or "⚠️ 已被审查系统拒绝。")
            return

        on_wait, on_submitted = self._make_queue_callbacks(event, "正在生成视频...")
        video_data = await self._img2video_engine.generate(
            image_data, positive, negative,
            fps=fps_value, length=length_value,
            on_wait_callback=on_wait,
            on_submitted_callback=on_submitted,
        )

        if not video_data:
            yield event.plain_result("生成失败")
            return

        temp_file = self.temp_dir / f"{int(time.time())}.mp4"
        with open(temp_file, "wb") as f:
            f.write(video_data)

        if event.get_platform_name() == "aiocqhttp":
            try:
                await self._call_send_api(event, f"[CQ:video,file=file://{temp_file}]")
            except Exception as e:
                logger.error(f"[图生视频] 视频发送失败: {e}")
                yield event.plain_result(f"⚠️ 视频已生成但发送失败: {e}")
                return
        else:
            yield event.chain_result([VideoComponent.fromFileSystem(str(temp_file))])

        event.stop_event()


