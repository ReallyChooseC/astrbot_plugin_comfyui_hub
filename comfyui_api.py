import asyncio
import uuid
from typing import Optional

import aiohttp


class ComfyUIAPI:
    def __init__(self, server_url: str = "http://127.0.0.1:8188", timeout: int = 300):
        self.server_url = server_url
        self.timeout = timeout
        # 用 UUID 避免多 AstrBot 实例共享同一 ComfyUI 时 client_id 撞号导致的归属判错
        self.client_id = uuid.uuid4().hex
        # 插件级别的异步锁，确保同一时刻只有一个任务进入"等待队列空闲→提交"流程
        self._submit_lock = asyncio.Lock()

    async def get_queue_info(self) -> dict:
        """获取 ComfyUI 队列状态（运行中和等待中的任务）

        返回格式: {"queue_running": [[prompt_id, workflow, client_id], ...], "queue_pending": [...]}
        每个队列项为 [prompt_id, prompt_workflow_dict, client_id]
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.server_url}/queue") as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception:
            pass
        return {"queue_running": [], "queue_pending": []}

    async def is_queue_busy(self) -> bool:
        """通过 ComfyUI 接口检测队列中是否有未完成的任务"""
        info = await self.get_queue_info()
        return len(info.get("queue_running", [])) > 0 or len(info.get("queue_pending", [])) > 0

    def _is_own_task(self, queue_item) -> bool:
        """判断队列项是否为本插件提交的任务

        ComfyUI 队列项格式为 (number, prompt_id, prompt_workflow_dict, client_id)
        也可能为 (prompt_id, prompt_workflow_dict, client_id) 旧格式
        client_id 可能在索引 3 或 2，需要兼容两种格式
        """
        try:
            if isinstance(queue_item, (list, tuple)):
                # 4元素格式: (number, prompt_id, workflow, client_id)
                if len(queue_item) >= 4:
                    return str(queue_item[3]) == self.client_id
                # 3元素格式: (prompt_id, workflow, client_id)
                if len(queue_item) >= 3:
                    return str(queue_item[2]) == self.client_id
        except Exception:
            pass
        return False

    async def get_own_queue_status(self) -> dict:
        """获取本插件提交的任务在队列中的状态

        Returns:
            {"own_running": int, "own_pending": int, "total_running": int, "total_pending": int}
        """
        info = await self.get_queue_info()
        running = info.get("queue_running", [])
        pending = info.get("queue_pending", [])
        return {
            "own_running": sum(1 for item in running if self._is_own_task(item)),
            "own_pending": sum(1 for item in pending if self._is_own_task(item)),
            "total_running": len(running),
            "total_pending": len(pending),
        }

    def _get_prompt_id_from_item(self, queue_item) -> Optional[str]:
        """从队列项中提取 prompt_id

        ComfyUI 队列项格式为 (number, prompt_id, prompt_workflow_dict, client_id)
        也可能为 (prompt_id, prompt_workflow_dict, client_id) 旧格式
        prompt_id 可能在索引 1 或 0，需要兼容两种格式
        """
        try:
            if isinstance(queue_item, (list, tuple)):
                if len(queue_item) >= 4:
                    return str(queue_item[1])
                if len(queue_item) >= 1:
                    return str(queue_item[0])
        except Exception:
            pass
        return None

    async def is_prompt_in_queue(self, prompt_id: str) -> bool:
        """检测指定 prompt_id 是否仍在 ComfyUI 队列中（运行中或等待中）"""
        info = await self.get_queue_info()
        for item in info.get("queue_running", []):
            if self._get_prompt_id_from_item(item) == str(prompt_id):
                return True
        for item in info.get("queue_pending", []):
            if self._get_prompt_id_from_item(item) == str(prompt_id):
                return True
        return False

    async def _wait_queue_idle(self, poll_interval: float = 2.0, max_wait: float = 300.0, on_wait_callback=None) -> float:
        """轮询等待本插件的任务完成（仅关注本 client_id 提交的任务）

        只在队列中没有本插件提交的运行中任务时才返回，
        不受外部其他客户端提交的任务影响。
        超过 max_wait 后放弃等待直接提交，由 ComfyUI 自身队列接管排队。

        Args:
            poll_interval: 轮询间隔秒数
            max_wait: 最大等待秒数，超时后放弃排队直接提交任务
            on_wait_callback: 等待中的回调函数，签名为 async (running: int, pending: int, waited: float) -> None，
                              每分钟调用一次，用于向用户通知等待状态

        Returns:
            已等待的秒数
        """
        from astrbot.api import logger
        waited = 0.0
        last_notify_time = -60.0  # 初始化为-60，确保首次检测到排队时立即通知
        while True:
            status = await self.get_own_queue_status()

            # 只有本插件没有运行中的任务时才返回（允许排队中，因为我们要提交的会排在后面）
            if status["own_running"] == 0:
                return waited

            # 超过最大等待时间，放弃排队直接提交
            if waited >= max_wait:
                logger.warning(f"[ComfyUI] 队列等待超时（{max_wait:.0f}s），放弃排队直接提交任务")
                return waited

            logger.info(f"[ComfyUI] 本插件任务运行中，等待... (本插件运行: {status['own_running']}, 本插件排队: {status['own_pending']}, 总运行: {status['total_running']}, 总排队: {status['total_pending']}, 已等待: {waited:.0f}s)")

            # 通过回调通知用户（首次立即通知，之后每隔60秒通知一次）
            if on_wait_callback and waited - last_notify_time >= 60:
                last_notify_time = waited
                try:
                    await on_wait_callback(status["total_running"], status["total_pending"], waited)
                except Exception as e:
                    logger.error(f"[ComfyUI] 队列等待回调异常: {e}")

            await asyncio.sleep(poll_interval)
            waited += poll_interval

    async def _submit_prompt(self, workflow: dict) -> Optional[str]:
        """提交任务到 ComfyUI，返回 prompt_id"""
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

    async def queue_prompt(self, workflow: dict) -> Optional[str]:
        """提交任务，返回 prompt_id（仅提交，不等待结果，不经过队列缓冲）"""
        return await self._submit_prompt(workflow)

    async def _queue_and_wait(self, workflow: dict, fetch_fn, label: str,
                              max_wait: float = 300.0,
                              on_wait_callback=None,
                              on_submitted_callback=None):
        """提交工作流并通过 fetch_fn 获取结果（带队列缓冲）

        使用 asyncio.Lock 确保同一时刻只有一个任务在"等待队列空闲→提交"流程中，
        避免 API 检查与提交之间的竞态条件。等待结果在锁外进行，允许后续任务排队。

        Args:
            workflow: 工作流字典
            fetch_fn: async (prompt_id, extra_timeout) -> Optional[result] 的回调
            label: 日志标识，例如 "图片"/"视频"/"文本"
            max_wait: 队列最大等待秒数，超时后直接提交
            on_wait_callback: 队列等待回调
            on_submitted_callback: 提交成功后的回调
        """
        from astrbot.api import logger

        async with self._submit_lock:
            queue_waited = await self._wait_queue_idle(max_wait=max_wait, on_wait_callback=on_wait_callback)

            extra_timeout = 0
            if queue_waited >= max_wait:
                info = await self.get_queue_info()
                pending_count = len(info.get("queue_pending", []))
                extra_timeout = max(120, pending_count * 60)
                logger.info(f"[ComfyUI] 强制提交，额外增加结果等待超时 {extra_timeout}s（前方排队: {pending_count}）")

            logger.info(f"[ComfyUI] 开始提交{label}任务")
            prompt_id = await self._submit_prompt(workflow)
            if not prompt_id:
                logger.error("[ComfyUI] 提交任务失败")
                return None

            await asyncio.sleep(1)
            queue_position, tasks_ahead, queue_extra = await self._calc_queue_timeout(prompt_id)
            extra_timeout = max(extra_timeout, queue_extra)

            if on_submitted_callback:
                try:
                    await on_submitted_callback(prompt_id, queue_position, tasks_ahead)
                except Exception as e:
                    logger.error(f"[ComfyUI] 提交回调异常: {e}")

        logger.info(f"[ComfyUI] 任务 {prompt_id} 已提交，等待{label}结果...（总超时: {self.timeout + extra_timeout}s）")
        result = await fetch_fn(prompt_id, extra_timeout)

        if result:
            logger.info(f"[ComfyUI] 任务 {prompt_id} 完成")
        else:
            logger.error(f"[ComfyUI] 任务 {prompt_id} 等待结果超时或失败")

        return result

    async def queue_and_wait_image(self, workflow: dict, max_wait: float = 300.0,
                                   on_wait_callback=None, on_submitted_callback=None) -> Optional[bytes]:
        """提交工作流并等待图片结果（带队列缓冲）"""
        return await self._queue_and_wait(
            workflow, self.wait_result, "图片",
            max_wait=max_wait,
            on_wait_callback=on_wait_callback,
            on_submitted_callback=on_submitted_callback,
        )

    async def queue_and_wait_video(self, workflow: dict, max_wait: float = 300.0,
                                   on_wait_callback=None, on_submitted_callback=None) -> Optional[bytes]:
        """提交工作流并等待视频结果（带队列缓冲）"""
        return await self._queue_and_wait(
            workflow, self.wait_video_result, "视频",
            max_wait=max_wait,
            on_wait_callback=on_wait_callback,
            on_submitted_callback=on_submitted_callback,
        )

    async def queue_and_wait_text(self, workflow: dict, output_node: str = "",
                                  max_wait: float = 300.0,
                                  on_wait_callback=None, on_submitted_callback=None) -> Optional[str]:
        """提交工作流并等待文本结果（带队列缓冲）"""
        async def fetch(prompt_id: str, extra_timeout: int):
            return await self.wait_text_result(prompt_id, output_node, extra_timeout=extra_timeout)

        return await self._queue_and_wait(
            workflow, fetch, "文本",
            max_wait=max_wait,
            on_wait_callback=on_wait_callback,
            on_submitted_callback=on_submitted_callback,
        )

    async def wait_video_result(self, prompt_id: str, extra_timeout: int = 0) -> Optional[bytes]:
        """等待并下载视频结果

        SaveVideo 节点的输出格式类似图片，存储在 outputs[node]["videos"] 中，
        每项包含 filename / subfolder / type 字段。
        """
        total_timeout = self.timeout + extra_timeout
        loop = asyncio.get_event_loop()
        deadline = loop.time() + total_timeout
        async with aiohttp.ClientSession() as session:
            while loop.time() < deadline:
                await asyncio.sleep(1)
                try:
                    async with session.get(f"{self.server_url}/history/{prompt_id}") as resp:
                        if resp.status == 200:
                            history = await resp.json()
                            if prompt_id in history:
                                outputs = history[prompt_id].get("outputs", {})
                                for node_output in outputs.values():
                                    # 兼容多种字段：videos / gifs / images（部分视频节点输出沿用 images 字段）
                                    for key in ("videos", "gifs", "images"):
                                        items = node_output.get(key)
                                        if not items:
                                            continue
                                        for item in items:
                                            filename = item.get("filename", "")
                                            # 跳过纯图片输出
                                            if key == "images" and not self._is_video_filename(filename):
                                                continue
                                            url = (
                                                f"{self.server_url}/view?"
                                                f"filename={filename}"
                                                f"&subfolder={item.get('subfolder', '')}"
                                                f"&type={item.get('type', 'output')}"
                                            )
                                            async with session.get(url) as v_resp:
                                                if v_resp.status == 200:
                                                    return await v_resp.read()
                except (aiohttp.ClientError, asyncio.TimeoutError, KeyError):
                    continue
        return None

    @staticmethod
    def _is_video_filename(filename: str) -> bool:
        if not filename:
            return False
        lower = filename.lower()
        return any(lower.endswith(ext) for ext in (".mp4", ".webm", ".mov", ".mkv", ".avi", ".gif"))

    async def _calc_queue_timeout(self, prompt_id: str) -> tuple:
        """根据任务在队列中的位置计算额外超时

        检测提交的任务是否已在运行中：
        - 如果已在运行，无需额外超时
        - 如果在排队中，根据前方排队数量计算额外超时

        Returns:
            (queue_position, tasks_ahead, extra_timeout)
            queue_position: 0=已在运行, >0=排队位置(1-based), -1=不在队列
            tasks_ahead: 前方任务数（运行中+排队前方）
            extra_timeout: 额外超时秒数
        """
        from astrbot.api import logger
        info = await self.get_queue_info()
        running = info.get("queue_running", [])
        pending = info.get("queue_pending", [])

        # 检查任务是否已在运行中
        for item in running:
            if self._get_prompt_id_from_item(item) == str(prompt_id):
                logger.info(f"[ComfyUI] 任务 {prompt_id} 已在运行中，无需额外超时")
                return (0, 0, 0)

        # 计算任务在排队中的位置（前方有多少个任务）
        own_position = 0
        found = False
        for item in pending:
            if self._get_prompt_id_from_item(item) == str(prompt_id):
                found = True
                break
            own_position += 1

        if found:
            # 前方 own_position 个排队任务 + 运行中的任务
            tasks_ahead = own_position + len(running)
            extra = max(120, tasks_ahead * 60)
            logger.info(f"[ComfyUI] 任务 {prompt_id} 在队列第 {own_position + 1} 位（前方 {tasks_ahead} 个任务），额外超时 {extra}s")
            return (own_position + 1, tasks_ahead, extra)
        else:
            # 任务不在队列中（可能已执行完毕或提交失败），不增加超时
            return (-1, 0, 0)

    async def wait_result(self, prompt_id: str, extra_timeout: int = 0) -> Optional[bytes]:
        """等待并下载图片结果

        Args:
            prompt_id: 任务ID
            extra_timeout: 额外超时秒数，用于强制提交时补偿队列排队时间
        """
        total_timeout = self.timeout + extra_timeout
        loop = asyncio.get_event_loop()
        deadline = loop.time() + total_timeout
        async with aiohttp.ClientSession() as session:
            while loop.time() < deadline:
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

    async def wait_text_result(self, prompt_id: str, output_node: str = "", extra_timeout: int = 0) -> Optional[str]:
        """等待并获取文本结果

        Args:
            prompt_id: 任务ID
            output_node: 输出节点ID
            extra_timeout: 额外超时秒数，用于强制提交时补偿队列排队时间
        """
        total_timeout = self.timeout + extra_timeout
        loop = asyncio.get_event_loop()
        deadline = loop.time() + total_timeout
        async with aiohttp.ClientSession() as session:
            while loop.time() < deadline:
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
