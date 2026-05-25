# AstrBot ComfyUI Hub 插件

为 AstrBot 提供 ComfyUI 调用能力的插件，支持文生图、图生图、图生视频、图片标签识别。

## 功能特性

- 文生图 `/draw`（别名：绘图、文生图、画图）
- 图生图 `/img2img`（别名：图生图、图像编辑、i2i），支持多图输入
- 图生视频 `/img2video`（别名：图生视频、生视频、i2v）
- 图片标签识别 `/tagger`（别名：tag、标签）
- 撤回 `/delete`（别名：撤回、recall），仅 aiocqhttp 平台
- 多种参数格式（宽高、超分倍率、合并转发、fps、视频长度）
- 队列缓冲与排队进度反馈
- 多层审查链路：本地 block tag → 输入文本 LLM → Tagger 关键词 → 多模态 LLM
- 管理员可绕过审查；审查异常的处理策略可配置（fail_open / fail_closed）
- 输出图片在 Discord/Telegram 上自动压缩到 10MB 以内

## 安装

1. 在 AstrBot 的 plugins 目录下克隆仓库
2. 重启 AstrBot
3. 在 AstrBot 管理面板的插件配置中设置 ComfyUI 服务器地址

## 工作流文件

工作流文件位置：`data/plugin_data/astrbot_plugin_comfyui_hub/workflows/`

首次启动时，如果配置指定的工作流文件不存在，插件会自动从插件目录复制示例工作流（`example_text2img.json`、`example_img2img.json`、`example_image2video.json`、`example_tagger.json`）。

工作流必须是 ComfyUI 的 **API 格式**（导出时选择"保存（API 格式）"），不是普通的拖拽文件。

## 配置项概览

完整字段见 `_conf_schema.json`。常用项：

| 字段 | 说明 |
| --- | --- |
| `server_url` | ComfyUI 服务器地址 |
| `timeout` | 单任务结果等待超时（秒） |
| `default_negative_prompt` | 默认负面提示词 |
| `default_chain` | 是否默认以合并转发发送 |
| `enable_txt2img` / `enable_img2img` / `enable_img2video` / `enable_tagger` | 功能总开关 |
| `txt2img_workflow` / `txt2img_positive_node` / `txt2img_negative_node` | 文生图工作流与节点 |
| `resolution_node` / `resolution_width_field` / `resolution_height_field` | 分辨率节点配置（留空自动查找 EmptyLatentImage） |
| `upscale_node` / `upscale_scale_field` | 超分节点配置 |
| `img2img_workflow` / `img2img_positive_node` / `img2img_negative_node` / `img2img_input_node` | 图生图配置（输入节点支持逗号分隔多图） |
| `img2video_workflow` / 各 `img2video_*_node` | 图生视频节点 |
| `enable_input_censorship` / `input_censorship_use_llm` / `censorship_prompt` | 输入文本审查 |
| `enable_output_censorship` / `output_censorship_use_llm` / `output_censorship_use_tagger` | 文生图输出审查 |
| `enable_img2img_input_censorship` / `enable_img2img_output_censorship` | 图生图输入/输出审查 |
| `admin_bypass_censorship` | 管理员绕过审查 |
| `censorship_failure_mode` | LLM 异常处理策略：`fail_open`（默认放行） / `fail_closed`（拦截） |
| `llm_provider_id` | 用于审查的 LLM 提供商，留空用会话默认 |

## 使用方法

### 文生图

```
/draw 1girl, solo, smile
/draw 1girl, solo | bad hands, low quality
/draw 正面[1girl, solo] 负面[bad hands]
/draw 1girl, solo 宽1024 高768 放大2 转发=是
```

支持的参数标记：

- 正面：`正面`、`正向`、`正面提示词`、`正向提示词`
- 负面：`负面`、`反向`、`负面提示词`、`反向提示词`，括号 `[]` 或 `{}`
- 宽度：`宽`、`宽度`、`w`、`width`、`x`
- 高度：`高`、`高度`、`h`、`height`、`y`（自动钳制到合法范围并按 8 对齐）
- 倍率：`scale`、`倍率`、`超分`、`放大`
- 转发：`chain`、`转发`、`合并转发`，值 `true`/`false`/`是`/`否`/`开`/`关`

### 图生图

```
/img2img 把人物背景改成沙滩
```

发送或回复包含图片的消息（支持多图，按工作流中 `LoadImage` 节点的顺序分配），加上提示词。

### 图生视频

```
/i2v 一个微笑的女孩 fps=16 时长3
```

提供图片（必填）和提示词。可在提示词中追加 `fps=N`（受 `img2video_max_fps` 限制）和 `length=秒数`（别名 `时长`、`长度`、`秒`）。

### 图片标签识别

```
/tagger
```

发送或回复一张图片即可，输出 Danbooru 风格标签（下划线已替换为空格）。

### 撤回

回复绘图插件输出的消息并发送 `/delete`：

- 普通用户：只能撤回 2 分钟内由本插件发出的消息
- 管理员：可以撤回任意消息

仅 aiocqhttp 平台支持。

### 管理员子命令

管理员可在 `/draw $...` 后接子命令管理审查与违规词：

- `/draw $enable_censorship` / `$disable_censorship` —— 当前群启用/关闭审查
- `/draw $add_block_tag tag1,tag2` —— 添加输入违规词
- `/draw $remove_block_tag tag1,tag2`
- `/draw $add_output_block_tag tag1,tag2` —— 添加输出（Tagger 审查）违规词
- `/draw $remove_output_block_tag tag1,tag2`

## 注意事项

- ComfyUI 服务器需正常运行
- 工作流文件必须是 API 格式
- 用户输入图片大小上限 20 MB；输出图片在 Discord/Telegram 平台自动压缩到 10 MB 以内（先 WebP 90，再 AVIF 85，再降低 WebP 质量）
- 输入审查命中后，用户被禁服务 2 分钟
- 输出审查命中后，图片不会发送，用户不会被禁
