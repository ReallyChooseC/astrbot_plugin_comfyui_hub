from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Node, Image, Reply
from pathlib import Path
import shutil
import time
import re
import json
from io import BytesIO
from PIL import Image as PILImage
from .comfyui_api import ComfyUIAPI
from .text_to_image import TextToImage


@register("astrbot_plugin_comfyui_hub", "ChooseC", "为 AstrBot 提供 ComfyUI 调用能力的插件，计划支持 ComfyUI 全功能。",
          "1.0.5", "https://github.com/ReallyChooseC/astrbot_plugin_comfyui_hub")
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
        self.blocked_users_file = data_dir / "blocked_users.json"
        self.censorship_config_file = data_dir / "censorship_config.json"
        self.sent_messages_file = data_dir / "sent_messages.json"
        self._load_block_data()

        workflow_filename = config.get("txt2img_workflow", "example_text2img.json")
        workflow_path = workflow_dir / workflow_filename

        if not workflow_path.exists():
            workflow_path = workflow_dir / "example_text2img.json"
            example_path = plugin_dir / "example_text2img.json"
            if example_path.exists() and not workflow_path.exists():
                shutil.copy(example_path, workflow_path)

        server_url = config.get("server_url", "http://127.0.0.1:8188")
        timeout = config.get("timeout", 300)

        self.api = ComfyUIAPI(server_url, timeout)
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

    def _load_block_data(self):
        self.block_tags = set()
        self.blocked_users = {}
        self.censored_groups = set()  # 存储开启审查的群组ID
        self.sent_messages = {}  # 存储插件发送的消息ID {group_id: [{message_id: timestamp}]}
        self.message_cache_ttl = 120  # 消息ID缓存时间（秒），默认2分钟
        
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
        width_pattern = r'(?:宽|宽度|w|width|x)\s*[:=]?\s*(\d+)'
        width_match = re.search(width_pattern, text, re.IGNORECASE)
        if width_match:
            params['width'] = int(width_match.group(1))
            text = re.sub(width_pattern, '', text, flags=re.IGNORECASE).strip()

        # 检查高度参数
        height_pattern = r'(?:高|高度|h|height|y)\s*[:=]?\s*(\d+)'
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

    @filter.command("draw", alias={'绘图', '文生图', '画图'})
    async def draw(self, event: AstrMessageEvent):
        """文生图指令，支持多种参数格式"""
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

        # 检查正向和反向提示词是否包含违规词（仅在审查开启时）
        if is_censorship_enabled:
            found_tags = []
            for tag in self.block_tags:
                if tag.lower() in positive.lower() or (negative and tag.lower() in negative.lower()):
                    found_tags.append(tag)
            
            if found_tags:
                self.blocked_users[user_id] = current_time + 120  # 2分钟封禁
                self._save_block_data()
                yield event.plain_result(f"⚠️ 您的绘图申请包含违规词: {', '.join(found_tags)}。您将被禁服务 2 分钟。")
                return

        # 自动补全安全提示词（仅在审查开启时）
        if is_censorship_enabled:
            if not any(word in positive.lower() for word in ["safe for work", "sfw", "cencored", "censored"]):
                positive = positive.rstrip(", ") + ", sfw" if positive else "sfw"
            
            if "nsfw" not in (negative or "").lower():
                negative = (negative.rstrip(", ") + ", nsfw") if negative else "nsfw"

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
