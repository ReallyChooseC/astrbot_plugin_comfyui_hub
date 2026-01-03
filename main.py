import json
import re
import shutil
import time
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Node, Image, Reply
from astrbot.api.star import Context, Star, register

from .comfyui_api import ComfyUIAPI
from .image_to_text import ImageToText
from .text_to_image import TextToImage


@register("astrbot_plugin_comfyui_hub", "ChooseC", "为 AstrBot 提供 ComfyUI 调用能力的插件，计划支持 ComfyUI 全功能。",
          "1.0.7", "https://github.com/ReallyChooseC/astrbot_plugin_comfyui_hub")
class ComfyUIHub(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 初始化默认值
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

        # 初始化文生图设置
        server_url = config.get("server_url", "http://127.0.0.1:8188")
        timeout = config.get("timeout", 300)
        self.api = ComfyUIAPI(server_url, timeout)

        self.txt2img = None
        if config.get("enable_txt2img", True):
            workflow_filename = config.get("txt2img_workflow", "example_text2img.json")
            workflow_path = workflow_dir / workflow_filename

            if not workflow_path.exists():
                workflow_path = workflow_dir / "example_text2img.json"
                example_path = plugin_dir / "example_text2img.json"
                if example_path.exists() and not workflow_path.exists():
                    shutil.copy(example_path, workflow_path)

            self.txt2img = TextToImage(
                self.api,
                str(workflow_path),
                config.get("txt2img_positive_node", "6"),
                config.get("txt2img_negative_node", "7"),
                config.get("resolution_node", ""),
                config.get("resolution_width_field", "width"),
                config.get("resolution_height_field", "height"),
                config.get("upscale_node", ""),
                config.get("upscale_scale_field", "resize_scale")
            )

        # 初始化 tagger 设置
        self.img2txt = None
        if config.get("enable_tagger", True):
            tagger_workflow_filename = config.get("tagger_workflow", "")
            tagger_workflow_path = workflow_dir / tagger_workflow_filename if tagger_workflow_filename else None

            if not tagger_workflow_path or not tagger_workflow_path.exists():
                tagger_workflow_path = workflow_dir / "example_tagger.json"
                example_tagger_path = plugin_dir / "example_tagger.json"
                if example_tagger_path.exists() and not tagger_workflow_path.exists():
                    shutil.copy(example_tagger_path, tagger_workflow_path)

            if tagger_workflow_path and tagger_workflow_path.exists():
                self.img2txt = ImageToText(
                    self.api,
                    str(tagger_workflow_path),
                    config.get("tagger_output_node", ""),
                    config.get("tagger_input_node", "")
                )

        # 初始化审查设置
        self.use_astrbot_llm = config.get("use_astrbot_llm", True)
        self.censorship_prompt = config.get("censorship_prompt", "")
        self.llm_provider_id = config.get("llm_provider_id", "")
        self.admin_bypass_censorship = config.get("admin_bypass_censorship", True)
        
        # 输出图片审查设置
        self.enable_output_censorship = config.get("enable_output_censorship", False)
        self.output_censorship_use_llm = config.get("output_censorship_use_llm", True)

    def _load_block_data(self):
        self.block_tags = set()
        self.output_block_tags = set()
        self.blocked_users = {}
        self.censored_groups = set()  # 存储开启审查的群组ID
        self.sent_messages = {}  # 存储插件发送的消息ID {group_id: [{message_id: timestamp}]}
        self.message_cache_ttl = 120  # 消息ID缓存时间（秒），默认2分钟

        if self.output_block_tags_file.exists():
            try:
                with open(self.output_block_tags_file, "r", encoding='utf-8') as f:
                    self.output_block_tags = set(json.load(f))
            except Exception as e:
                logger.error(f"Error loading output block tags: {e}")

        
        if self.block_tags_file.exists():
            try:
                with open(self.block_tags_file, "r", encoding='utf-8') as f:
                    self.block_tags = set(json.load(f))
            except Exception as e:
                logger.error(f"Error loading block tags: {e}")
                
        if self.blocked_users_file.exists():
            try:
                with open(self.blocked_users_file, "r", encoding='utf-8') as f:
                    self.blocked_users = json.load(f)
            except Exception as e:
                logger.error(f"Error loading blocked users: {e}")
                
        if self.censorship_config_file.exists():
            try:
                with open(self.censorship_config_file, "r", encoding='utf-8') as f:
                    config = json.load(f)
                    # 兼容旧版配置：如果旧版 enabled=True，则暂时不处理，等待新指令
                    # 这里直接加载 groups 列表
                    self.censored_groups = set(config.get("groups", []))
            except Exception as e:
                logger.error(f"Error loading censorship config: {e}")
                
        if self.sent_messages_file.exists():
            try:
                with open(self.sent_messages_file, "r", encoding='utf-8') as f:
                    # 转换键为字符串类型（JSON默认键为字符串）
                    data = json.load(f)
                    self.sent_messages = {str(k): v for k, v in data.items()}
                    # 清理过期的消息ID
                    self._cleanup_expired_messages()
            except Exception as e:
                logger.error(f"Error loading sent messages: {e}")

    def _cleanup_expired_messages(self):
        """清理过期的消息ID"""
        current_time = time.time()
        for group_id in list(self.sent_messages.keys()):
            # 过滤出未过期的消息
            valid_messages = [
                msg_data for msg_data in self.sent_messages[group_id]
                if isinstance(msg_data, dict) and
                current_time - msg_data.get('timestamp', 0) <= self.message_cache_ttl
            ]
            self.sent_messages[group_id] = valid_messages
            # 如果群组没有有效消息，删除该群组记录
            if not valid_messages:
                del self.sent_messages[group_id]

    def _save_block_data(self):
        try:
            with open(self.block_tags_file, "w", encoding='utf-8') as f:
                json.dump(list(self.block_tags), f, ensure_ascii=False)
            with open(self.output_block_tags_file, "w", encoding='utf-8') as f:
                json.dump(list(self.output_block_tags), f, ensure_ascii=False)
            with open(self.blocked_users_file, "w", encoding='utf-8') as f:
                json.dump(self.blocked_users, f, ensure_ascii=False)
            with open(self.censorship_config_file, "w", encoding='utf-8') as f:
                json.dump({"groups": list(self.censored_groups)}, f, ensure_ascii=False)
            # 保存前清理过期消息
            self._cleanup_expired_messages()
            with open(self.sent_messages_file, "w", encoding='utf-8') as f:
                json.dump(self.sent_messages, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving block data: {e}")

    async def _check_safety_with_llm(self, event: AstrMessageEvent, text: str, is_output_check: bool = False) -> tuple:
        """使用 AstrBot 内置 LLM 检查文本安全"""
        try:
            # 检查输出审查时，根据配置决定使用 LLM
            if is_output_check and not self.output_censorship_use_llm:
                return True, "LLM Disabled"
            
            # 检查输入审查时，根据配置决定使用 LLM
            if not is_output_check and not self.use_astrbot_llm:
                return True, "Disabled"

            # 优先使用配置的提供商 ID，否则使用当前会话的提供商
            provider_id = self.llm_provider_id
            if not provider_id:
                # 如果配置中没有指定，则使用会话默认提供商
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if not provider_id:
                    return True, "No Provider"

            # 系统提示词（审查指导原则）
            system_prompt = self.censorship_prompt

            # 使用 AstrBot 内置 LLM 生成，传入系统提示词和用户输入
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=text,
                system_prompt=system_prompt
            )

            if not llm_resp or not llm_resp.completion_text:
                return True, "No Response"
                
            result = llm_resp.completion_text.strip()
            
            # 使用严格的正则表达式匹配：只匹配独立的 "yes"（大小写不敏感）
            # 确保不会误判 "yes" 作为单词的一部分（如 "yesterday"）
            # 同时也支持中文的 "是" 作为违规的判定
            is_violation = bool(re.search(r'\byes\b', result, re.IGNORECASE) or
                              bool(re.search(r'违规', result)) or
                              bool(re.search(r'\b是\b', result, flags=re.IGNORECASE)))
            
            if is_violation:
                return False, result
            
            # 默认放行（任何其他回答都视为不违规）
            return True, ""
            
        except Exception as e:
            logger.error(f"AstrBot LLM 审查失败: {e}")
            # 失败默认放行，避免服务不可用
            return True, f"审查出错: {e}"

    def _parse_params(self, text: str) -> tuple:
        """解析用户输入的参数"""
        params = {
            'positive': '',
            'negative': self.default_negative,
            'chain': self.default_chain,
            'width': None,
            'height': None,
            'scale': None
        }

        # 检查 chain 参数
        chain_pattern = r'(?:chain|转发|合并转发)\s*[:=]?\s*(true|false|是|否|开|关)'
        chain_match = re.search(chain_pattern, text, re.IGNORECASE)
        if chain_match:
            value = chain_match.group(1).lower()
            params['chain'] = value in ['true', '是', '开']
            text = re.sub(chain_pattern, '', text, flags=re.IGNORECASE).strip()

        # 检查超分倍率参数
        scale_pattern = r'(?:scale|倍率|超分|放大)\s*[:=]?\s*(\d+(?:\.\d+)?)'
        scale_match = re.search(scale_pattern, text, re.IGNORECASE)
        if scale_match:
            params['scale'] = float(scale_match.group(1))
            text = re.sub(scale_pattern, '', text, flags=re.IGNORECASE).strip()

        # 检查宽度参数
        width_pattern = r'(?:\s+|^)(?:宽|宽度|w|width|x)\s*[:=]?\s*(\d+)'
        width_match = re.search(width_pattern, text, re.IGNORECASE)
        if width_match:
            params['width'] = int(width_match.group(1))
            text = re.sub(width_pattern, '', text, flags=re.IGNORECASE).strip()

        # 检查高度参数
        height_pattern = r'(?:\s+|^)(?:高|高度|h|height|y)\s*[:=]?\s*(\d+)'
        height_match = re.search(height_pattern, text, re.IGNORECASE)
        if height_match:
            params['height'] = int(height_match.group(1))
            text = re.sub(height_pattern, '', text, flags=re.IGNORECASE).strip()

        # 检查正面/负面提示词
        positive_aliases = r'(?:正面|正向|正面提示词|正向提示词)'
        negative_aliases = r'(?:负面|反向|负面提示词|反向提示词)'

        new_format_pattern = rf'({positive_aliases})\s*[:=]?\s*[\[{{]([^\]}}]+?)[\]}}]|({negative_aliases})\s*[:=]?\s*[\[{{]([^\]}}]+?)[\]}}]'
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

        return params['positive'], params['negative'], params['chain'], params['width'], params['height'], params['scale']

    def _check_simple_tags(self, tags_text: str) -> tuple:
        """使用简单关键词检查标签是否违规"""
        tags = [tag.strip().lower() for tag in tags_text.split(',')]

        for tag in tags:
            for keyword in self.output_block_tags:
                if keyword.lower() in tag:
                    return False, f"包含违规标签: {tag}"

        return True, ""

    @filter.command("draw", alias={'绘图', '文生图', '画图'})
    async def draw(self, event: AstrMessageEvent):
        """文生图指令，支持多种参数格式"""
        # 检查文生图功能是否开启
        if not self.txt2img:
            yield event.plain_result("⚠️ 文生图功能未开启")
            return

        user_id = event.get_sender_id()
        current_time = time.time()

        # 检查是否在封禁期
        if user_id in self.blocked_users:
            expire_time = self.blocked_users[user_id]
            if current_time < expire_time:
                remaining = int(expire_time - current_time)
                yield event.plain_result(f"由于触发违规词，您已被禁止使用绘图功能。剩余时间: {remaining} 秒。")
                return
            else:
                del self.blocked_users[user_id]
                self._save_block_data()

        text = event.message_str.strip()

        # 统一剥离命令前缀
        for cmd in ['draw', '绘图', '文生图', '画图']:
            pattern = rf'^[\/#]?{re.escape(cmd)}\s+'
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                text = text[match.end():]
                break
            # 如果只是命令本身（无参数）
            if re.match(rf'^[\/#]?{re.escape(cmd)}$', text, re.IGNORECASE):
                text = ""
                break

        # 处理子命令（仅管理员）
        if text.startswith('$'):
            if not event.is_admin():
                yield event.plain_result("❌ 仅管理员可执行此操作。")
                return
            
            if text.startswith('$enable_censorship'):
                group_id = event.get_group_id()
                if not group_id:
                    yield event.plain_result("⚠️ 此命令仅支持在群组中使用。")
                    return
                
                self.censored_groups.add(group_id)
                self._save_block_data()
                yield event.plain_result(f"✅ 已在当前群组开启审查功能。")
                return
            
            if text.startswith('$disable_censorship'):
                group_id = event.get_group_id()
                if not group_id:
                    yield event.plain_result("⚠️ 此命令仅支持在群组中使用。")
                    return
                
                if group_id in self.censored_groups:
                    self.censored_groups.remove(group_id)
                    self._save_block_data()
                yield event.plain_result(f"✅ 已在当前群组关闭审查功能。")
                return
            
            if text.startswith('$add_block_tag'):
                tags_part = text[len('$add_block_tag'):].strip()
                raw_tags = re.split(r',|\[|\]', tags_part)
                new_tags = [t.strip() for t in raw_tags if t.strip()]
                
                if not new_tags:
                    yield event.plain_result("用法: #draw $add_block_tag tag1,tag2 或 [tag1] [tag2]")
                    return
                
                self.block_tags.update(new_tags)
                self._save_block_data()
                yield event.plain_result(f"✅ 已成功添加违规词: {', '.join(new_tags)}")
                return

            if text.startswith('$add_output_block_tag'):
                tags_part = text[len('$add_output_block_tag'):].strip()
                raw_tags = re.split(r',|\[|\]', tags_part)
                new_tags = [t.strip() for t in raw_tags if t.strip()]

                if not new_tags:
                    yield event.plain_result("用法: #draw $add_output_block_tag tag1,tag2")
                    return

                self.output_block_tags.update(new_tags)
                self._save_block_data()
                yield event.plain_result(f"✅ 已添加输出违规词: {', '.join(new_tags)}")
                return

            if text.startswith('$remove_output_block_tag'):
                tags_part = text[len('$remove_output_block_tag'):].strip()
                raw_tags = re.split(r',|\[|\]', tags_part)
                rem_tags = [t.strip() for t in raw_tags if t.strip()]

                if not rem_tags:
                    yield event.plain_result("用法: #draw $remove_output_block_tag tag1,tag2")
                    return

                removed = [t for t in rem_tags if t in self.output_block_tags]
                self.output_block_tags -= set(rem_tags)
                self._save_block_data()

                if removed:
                    yield event.plain_result(f"✅ 已移除输出违规词: {', '.join(removed)}")
                else:
                    yield event.plain_result("⚠️ 未找到指定的输出违规词")
                return

            if text.startswith('$remove_block_tag'):
                tags_part = text[len('$remove_block_tag'):].strip()
                raw_tags = re.split(r',|\[|\]', tags_part)
                rem_tags = [t.strip() for t in raw_tags if t.strip()]
                
                if not rem_tags:
                    yield event.plain_result("用法: #draw $remove_block_tag tag1,tag2 或 [tag1] [tag2]")
                    return
                
                removed = []
                for t in rem_tags:
                    if t in self.block_tags:
                        self.block_tags.remove(t)
                        removed.append(t)
                
                self._save_block_data()
                if removed:
                    yield event.plain_result(f"✅ 已成功移除违规词: {', '.join(removed)}")
                else:
                    yield event.plain_result("⚠️ 未找到指定的违规词。")
                return

        if not text:
            yield event.plain_result("请输入提示词")
            return

        params = self._parse_params(text)
        positive, negative, chain, width, height, scale = params

        # 检查是否开启审查（仅针对群聊且在开启列表中）
        group_id = event.get_group_id()
        is_censorship_enabled = group_id and group_id in self.censored_groups

        # 检查是否为管理员且开启了管理员绕过选项
        is_admin = event.is_admin()
        should_bypass_censorship = is_admin and self.admin_bypass_censorship

        if is_censorship_enabled and not should_bypass_censorship:
            # 1. 本地 Block Tag 检查
            for tag in self.block_tags:
                if tag.lower() in positive.lower(): # 简单的子串匹配
                     # 封禁用户
                    self.blocked_users[user_id] = current_time + 120  # 2分钟封禁
                    self._save_block_data()
                    yield event.plain_result(f"⚠️ 违规：包含禁止词 '{tag}'。您将被禁服务 2 分钟。")
                    return

            # 2. LLM 审查 (仅在开启了 AstrBot LLM 审查时进行)
            if self.use_astrbot_llm:
                is_safe, reason = await self._check_safety_with_llm(event, positive)
                if not is_safe:
                    self.blocked_users[user_id] = current_time + 120  # 2分钟封禁
                    self._save_block_data()
                    logger.info(f"LLM 审查拦截: {reason}")
                    yield event.plain_result(f"⚠️ 您的绘图申请包含敏感内容（{reason}），已被AI审查系统拒绝。您将被禁服务 2 分钟。")
                    return
            # 如果没有开启审查，则直接原样通过（跳过 LLM 审查）


            # 自动添加 safe prompt (可选，这里保留最基本的)
            if "sfw" not in positive.lower() and "safe" not in positive.lower():
                positive += ", sfw, safe for work"


        if not positive:
            yield event.plain_result("请输入正面提示词")
            return

        # 发送"正在生成图片..."消息（使用 API 以获取消息ID）
        text_msg_id = None
        group_id = event.get_group_id()
        is_aiocqhttp = event.get_platform_name() == "aiocqhttp"

        if is_aiocqhttp and group_id:
            try:
                client = event.bot
                result = await client.api.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message="正在生成图片..."
                )
                if result:
                    # 尝试多种可能的返回结构
                    if isinstance(result, dict):
                        if 'data' in result and result['data']:
                            text_msg_id = result['data'].get('message_id')
                        elif 'message_id' in result:
                            text_msg_id = result['message_id']
                        elif 'retcode' in result and result['retcode'] == 0:
                            text_msg_id = result.get('data', {}).get('message_id')
                    elif isinstance(result, (int, str)):
                        text_msg_id = str(result)
            except Exception as e:
                logger.error(f"发送文字消息失败: {e}")

        image_data = await self.txt2img.generate(positive, negative, width, height, scale)

        if image_data:
            # 输出图片审查
            if is_censorship_enabled and not should_bypass_censorship and self.enable_output_censorship and self.img2txt:
                yield event.plain_result("正在审查生成的图片...")
                
                # 使用 tagger 获取图片标签
                tags_text = await self.img2txt.generate(image_data)
                
                if tags_text:
                    logger.info(f"输出图片标签: {tags_text}")

                    # 检查方式1: 使用 LLM 审查
                    if self.output_censorship_use_llm:
                        is_safe, reason = await self._check_safety_with_llm(event, tags_text, is_output_check=True)
                        if not is_safe:
                            logger.info(f"输出图片 LLM 审查拦截: {reason}")
                            yield event.plain_result(
                                f"⚠️ 生成的图片包含敏感内容（{reason}），已被AI审查系统拒绝。图片已销毁。")
                            return

                    # 检查方式2: 使用简单关键词审查
                    is_safe_simple, reason_simple = self._check_simple_tags(tags_text)
                    if not is_safe_simple:
                        logger.info(f"输出图片关键词审查拦截: {reason_simple}")
                        yield event.plain_result(
                            f"⚠️ 生成的图片包含敏感内容（{reason_simple}），已被审查系统拒绝。图片已销毁。")
                        return
                    
                    logger.info("输出图片审查通过")
            
            temp_file = self.temp_dir / f"{int(time.time())}.png"
            with open(temp_file, "wb") as f:
                f.write(image_data)

            # 检查文件大小限制（Discord 和 Telegram 都是 10MB）
            if event.get_platform_name() in ["discord", "telegram"]:
                file_size = len(image_data)
                max_size = 10 * 1024 * 1024  # 10MB
                
                if file_size > max_size:
                    size_mb = file_size / (1024 * 1024)
                    logger.info(f"图片大小 {size_mb:.1f}MB 超过限制，尝试压缩...")
                    
                    # 尝试转换为WebP格式
                    try:
                        img = PILImage.open(BytesIO(image_data))
                        
                        # 先尝试WebP（质量90）
                        webp_buffer = BytesIO()
                        img.save(webp_buffer, format='WEBP', quality=90)
                        webp_size = webp_buffer.tell()
                        
                        if webp_size <= max_size:
                            temp_file = self.temp_dir / f"{int(time.time())}.webp"
                            with open(temp_file, "wb") as f:
                                f.write(webp_buffer.getvalue())
                            webp_size_mb = webp_size / (1024 * 1024)
                            logger.info(f"成功转换为WebP格式，大小: {webp_size_mb:.1f}MB")
                        else:
                            # WebP仍然太大，尝试AVIF（质量85）
                            try:
                                avif_buffer = BytesIO()
                                img.save(avif_buffer, format='AVIF', quality=85)
                                avif_size = avif_buffer.tell()
                                
                                if avif_size <= max_size:
                                    temp_file = self.temp_dir / f"{int(time.time())}.avif"
                                    with open(temp_file, "wb") as f:
                                        f.write(avif_buffer.getvalue())
                                    avif_size_mb = avif_size / (1024 * 1024)
                                    logger.info(f"成功转换为AVIF格式，大小: {avif_size_mb:.1f}MB")
                                else:
                                    # 还是太大，尝试降低WebP质量
                                    for quality in [80, 70, 60, 50]:
                                        webp_buffer = BytesIO()
                                        img.save(webp_buffer, format='WEBP', quality=quality)
                                        if webp_buffer.tell() <= max_size:
                                            temp_file = self.temp_dir / f"{int(time.time())}.webp"
                                            with open(temp_file, "wb") as f:
                                                f.write(webp_buffer.getvalue())
                                            final_size_mb = webp_buffer.tell() / (1024 * 1024)
                                            logger.info(f"使用WebP质量{quality}压缩成功，大小: {final_size_mb:.1f}MB")
                                            break
                                    else:
                                        # 所有尝试都失败
                                        yield event.plain_result(f"⚠️ 警告：原图 {size_mb:.1f}MB，压缩后仍超过 10MB 限制，可能无法发送")
                            except Exception as e:
                                logger.error(f"AVIF转换失败: {e}，使用WebP")
                                # AVIF失败，继续尝试降低WebP质量
                                for quality in [80, 70, 60, 50]:
                                    webp_buffer = BytesIO()
                                    img.save(webp_buffer, format='WEBP', quality=quality)
                                    if webp_buffer.tell() <= max_size:
                                        temp_file = self.temp_dir / f"{int(time.time())}.webp"
                                        with open(temp_file, "wb") as f:
                                            f.write(webp_buffer.getvalue())
                                        final_size_mb = webp_buffer.tell() / (1024 * 1024)
                                        logger.info(f"使用WebP质量{quality}压缩成功，大小: {final_size_mb:.1f}MB")
                                        break
                                else:
                                    yield event.plain_result(f"⚠️ 警告：原图 {size_mb:.1f}MB，压缩后仍超过 10MB 限制，可能无法发送")
                    except Exception as e:
                        logger.error(f"图片压缩失败: {e}")
                        yield event.plain_result(f"⚠️ 警告：生成的图片为 {size_mb:.1f}MB，超过平台默认 10MB 限制，压缩失败")

            sent_msg_id = None

            if is_aiocqhttp and group_id:
                # 使用 aiocqhttp 底层 API 发送消息，以获取消息 ID
                client = event.bot

                if chain:
                    # 合并转发
                    try:
                        node = Node(
                            uin=event.get_sender_id(),
                            name="ComfyUI",
                            content=[Image.fromFileSystem(str(temp_file))]
                        )
                        # 使用 send_group_forward_msg 发送合并转发
                        result = await client.api.call_action(
                            "send_group_forward_msg",
                            group_id=int(group_id),
                            messages=[node]
                        )
                        if result:
                            # 尝试多种可能的返回结构
                            if isinstance(result, dict):
                                if 'data' in result and result['data']:
                                    sent_msg_id = result['data'].get('message_id') if isinstance(result['data'], dict) else result['data']
                                elif 'message_id' in result:
                                    sent_msg_id = result['message_id']
                            elif isinstance(result, (int, str)):
                                sent_msg_id = str(result)
                    except Exception as e:
                        logger.error(f"合并转发发送失败: {e}，改用普通图片发送")
                        # 失败则回退到普通图片发送
                        result = await client.api.call_action(
                            "send_group_msg",
                            group_id=int(group_id),
                            message=[Image.fromFileSystem(str(temp_file))]
                        )
                        if result:
                            if isinstance(result, dict):
                                if 'data' in result and result['data']:
                                    sent_msg_id = result['data'].get('message_id')
                                elif 'message_id' in result:
                                    sent_msg_id = result['message_id']
                            elif isinstance(result, (int, str)):
                                sent_msg_id = str(result)
                else:
                    # 普通图片消息
                    result = await client.api.call_action(
                        "send_group_msg",
                        group_id=int(group_id),
                        message=[Image.fromFileSystem(str(temp_file))]
                    )
                    if result:
                        if isinstance(result, dict):
                            if 'data' in result and result['data']:
                                sent_msg_id = result['data'].get('message_id')
                            elif 'message_id' in result:
                                sent_msg_id = result['message_id']
                        elif isinstance(result, (int, str)):
                            sent_msg_id = str(result)
            else:
                # 非 aiocqhttp 平台或私聊，使用默认方法
                if chain:
                    try:
                        node = Node(
                            uin=event.get_sender_id(),
                            name="ComfyUI",
                            content=[Image.fromFileSystem(str(temp_file))]
                        )
                        yield event.chain_result([node])
                    except Exception:
                        yield event.image_result(str(temp_file))
                else:
                    yield event.image_result(str(temp_file))

            # 记录所有发送的消息ID（带时间戳）
            if group_id:
                group_id_str = str(group_id)
                if group_id_str not in self.sent_messages:
                    self.sent_messages[group_id_str] = []
                # 先记录文字消息ID
                if text_msg_id:
                    self.sent_messages[group_id_str].append({
                        'message_id': str(text_msg_id),
                        'timestamp': time.time(),
                        'user_id': str(event.get_sender_id())
                    })
                # 再记录图片消息ID
                if sent_msg_id:
                    self.sent_messages[group_id_str].append({
                        'message_id': str(sent_msg_id),
                        'timestamp': time.time(),
                        'user_id': str(event.get_sender_id())
                    })
                self._save_block_data()
            # 停止事件传播，避免触发 LLM
            event.stop_event()
        else:
            yield event.plain_result("生成失败")

    @filter.command("delete", alias={'撤回', 'recall'})
    async def delete_msg(self, event: AstrMessageEvent):
        """引用撤回绘图功能输出的消息"""
        chain = event.get_messages()
        if not chain:
            return

        first_seg = chain[0] if len(chain) > 0 else None
        if not first_seg:
            return

        # 检查是否为 aiocqhttp 平台（仅支持此平台）
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("❌ 此功能仅支持 aiocqhttp 平台")
            return

        # 必须引用消息
        if not isinstance(first_seg, Reply):
            yield event.plain_result("❌ 请引用要撤回的绘图消息")
            return

        group_id = event.get_group_id()
        current_time = time.time()
        is_admin = event.is_admin()

        # 管理员可以撤回任何消息，普通用户只能撤回绘图插件输出的消息
        is_valid_message = is_admin
        msg_index_to_remove = None

        # 对于普通用户，验证消息是否在缓存中
        if not is_admin and group_id:
            group_id_str = str(group_id)
            sent_msgs = self.sent_messages.get(group_id_str, [])
            # 清理过期消息并验证
            valid_msgs = []
            for i, msg_data in enumerate(sent_msgs):
                if not isinstance(msg_data, dict):
                    continue
                msg_id = msg_data.get('message_id')
                msg_timestamp = msg_data.get('timestamp', 0)
                # 检查是否过期
                if current_time - msg_timestamp > self.message_cache_ttl:
                    continue
                # 检查是否为目标消息
                if msg_id == str(first_seg.id):
                    is_valid_message = True
                    msg_index_to_remove = i
                valid_msgs.append(msg_data)
            # 更新清理后的消息列表
            self.sent_messages[group_id_str] = valid_msgs
        if not is_valid_message:
            return

        try:
            client = event.bot
            await client.delete_msg(message_id=int(first_seg.id))
            # 从记录中移除已撤回的消息ID
            if is_valid_message and group_id and msg_index_to_remove is not None:
                group_id_str = str(group_id)
                self.sent_messages[group_id_str].pop(msg_index_to_remove)
                self._save_block_data()
            # 停止事件传播，不触发 LLM
            event.stop_event()
        except Exception as e:
            logger.error(f"撤回失败: {e}")

    @filter.command("tagger", alias={'tag', '标签'})
    async def tagger(self, event: AstrMessageEvent):
        """图片标签识别指令，输入图片输出文本标签"""
        # 检查 tagger 功能是否开启
        if not self.img2txt:
            yield event.plain_result("⚠️ 图片标签识别功能未开启")
            return

        # 获取消息中的图片
        from astrbot.api.message_components import Image as ImageComponent, Reply as ReplyComponent
        chain = event.get_messages()
        image_data = None

        for msg in chain:
            # 情况1：处理 Reply 消息中的图片
            if isinstance(msg, ReplyComponent) and msg.chain:
                for chain_msg in msg.chain:
                    if isinstance(chain_msg, ImageComponent):
                        image_data = await self._get_image_data(chain_msg)
                        if image_data:
                            break
            # 情况2：处理直接的图片消息
            elif isinstance(msg, ImageComponent):
                image_data = await self._get_image_data(msg)
            
            if image_data:
                break

        if not image_data:
            yield event.plain_result("请发送或回复一张图片")
            return

        logger.info(f"成功获取图片数据，大小: {len(image_data)} 字节")

        # 发送"正在识别图片..."消息
        text_msg_id = None
        group_id = event.get_group_id()
        is_aiocqhttp = event.get_platform_name() == "aiocqhttp"

        if is_aiocqhttp and group_id:
            try:
                client = event.bot
                result = await client.api.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message="正在识别图片标签..."
                )
                if result:
                    if isinstance(result, dict):
                        if 'data' in result and result['data']:
                            text_msg_id = result['data'].get('message_id')
                        elif 'message_id' in result:
                            text_msg_id = result['message_id']
                    elif isinstance(result, (int, str)):
                        text_msg_id = str(result)
            except Exception as e:
                logger.error(f"发送文字消息失败: {e}")

        # 生成标签
        result_text = await self.img2txt.generate(image_data)
        logger.info(f"标签识别结果: {result_text}")

        if result_text:
            # 格式化输出标签（只替换下划线为空格）
            formatted_tags = result_text.replace('_', ' ')
            logger.info(f"格式化后的标签: {formatted_tags}")
            yield event.plain_result(f"【标签】\n{formatted_tags}")

            # 记录发送的消息ID（带时间戳）
            if group_id:
                group_id_str = str(group_id)
                if group_id_str not in self.sent_messages:
                    self.sent_messages[group_id_str] = []
                if text_msg_id:
                    self.sent_messages[group_id_str].append({
                        'message_id': str(text_msg_id),
                        'timestamp': time.time(),
                        'user_id': str(event.get_sender_id())
                    })
                self._save_block_data()
            # 停止事件传播，避免触发 LLM
            event.stop_event()
        else:
            yield event.plain_result("识别失败")

    async def _get_image_data(self, image_component):
        """从 Image 组件获取图片数据"""
        import aiohttp
        try:
            # 尝试通过 URL 下载
            if hasattr(image_component, 'url') and image_component.url:
                async with aiohttp.ClientSession() as session:
                    async with session.get(image_component.url) as resp:
                        if resp.status == 200:
                            return await resp.read()
        except Exception as e:
            logger.error(f"通过URL获取图片失败: {e}")

        try:
            # 尝试转换为文件路径并读取
            if hasattr(image_component, 'convert_to_file_path') and callable(image_component.convert_to_file_path):
                file_path = await image_component.convert_to_file_path()
                if file_path and Path(file_path).exists():
                    with open(file_path, 'rb') as f:
                        return f.read()
        except Exception as e:
            logger.error(f"通过文件路径获取图片失败: {e}")

        return None
