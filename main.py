from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Node, Image
from pathlib import Path
import shutil
import time
import re
from .comfyui_api import ComfyUIAPI
from .text_to_image import TextToImage


@register("astrbot_plugin_comfyui_hub", "ChooseC", "为 AstrBot 提供 ComfyUI 调用能力的插件，计划支持 ComfyUI 全功能。",
          "1.0.2", "https://github.com/ReallyChooseC/astrbot_plugin_comfyui_hub")
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
        chain_pattern = r'(?:chain|转发|合并转发)\s*[:=]?\s*(true|false|是|否)'
        chain_match = re.search(chain_pattern, text, re.IGNORECASE)
        if chain_match:
            value = chain_match.group(1).lower()
            params['chain'] = value in ['true', '是']
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
    async def draw(self, event: AstrMessageEvent, message: MessageChain):
        """文生图指令，支持多种参数格式"""
        text = event.message_str.strip()

        for cmd in ['draw', '绘图', '文生图', '画图']:
            if text.startswith(f'/{cmd} ') or text.startswith(f'{cmd} '):
                text = text.split(maxsplit=1)[1] if ' ' in text else ""
                break

        if not text:
            yield event.plain_result("请输入提示词")
            return

        params = self._parse_params(text)
        positive, negative, chain, width, height, scale = params

        if not positive:
            yield event.plain_result("请输入正面提示词")
            return

        yield event.plain_result("正在生成图片...")

        image_data = await self.txt2img.generate(positive, negative, width, height, scale)

        if image_data:
            temp_file = self.temp_dir / f"{int(time.time())}.png"
            with open(temp_file, "wb") as f:
                f.write(image_data)

            # 检查文件大小限制（Discord 和 Telegram 都是 10MB）
            if event.get_platform_name() in ["discord", "telegram"]:
                file_size = len(image_data)
                if file_size > 10 * 1024 * 1024:
                    size_mb = file_size / (1024 * 1024)
                    yield event.plain_result(f"警告：生成的图片为 {size_mb:.1f}MB，超过平台默认 10MB 限制，可能无法发送")

            is_aiocqhttp = event.get_platform_name() == "aiocqhttp"

            if chain and is_aiocqhttp:
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
        else:
            yield event.plain_result("生成失败")
