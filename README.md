# AstrBot ComfyUI Hub 插件

为 AstrBot 提供 ComfyUI API 调用能力的插件，暂时只文生图功能。

## 功能特性

- ✅ 文生图指令 `/draw`（别名：绘图、文生图、画图）
- ✅ 多种参数格式支持
- ✅ 分辨率控制（宽/高）
- ✅ 超分倍率控制
- ✅ 合并转发发送
- ✅ 智能节点识别

## 安装

1. 在 AstrBot 的 plugins 目录下克隆仓库
2. 重启 AstrBot
3. 在配置文件中设置 ComfyUI 服务器地址

## 配置

在 AstrBot 配置界面或 `config/astrbot_plugin_comfyui_hub_config.json` 中配置：

```json
{
  "astrbot_plugin_comfyui_hub": {
    "server_url": "http://127.0.0.1:8188",
    "timeout": 300,
    "default_negative_prompt": "bad hands, low quality, blurry",
    "default_chain": false,
    "txt2img_workflow": "example_text2img.json",
    "txt2img_positive_node": "6",
    "txt2img_negative_node": "7",
    "resolution_node": "",
    "resolution_width_field": "width",
    "resolution_height_field": "height",
    "upscale_node": "",
    "upscale_scale_field": "resize_scale"
  }
}
```

### 配置说明

- `server_url`: ComfyUI 服务器地址
- `timeout`: 生成超时时间（秒）
- `default_negative_prompt`: 默认负面提示词
- `default_chain`: 是否默认使用合并转发
- `txt2img_workflow`: 工作流文件名（需放在 `data/astrbot_plugin_comfyui_hub/workflows/`）
- `txt2img_positive_node`: 正面提示词节点 ID
- `txt2img_negative_node`: 负面提示词节点 ID
- `resolution_node`: 分辨率节点 ID（留空自动查找 EmptyLatentImage）
- `resolution_width_field`: 宽度字段名
- `resolution_height_field`: 高度字段名
- `upscale_node`: 超分节点 ID（可选）
- `upscale_scale_field`: 超分倍率字段名

## 使用方法

### 基础用法

```
/draw 1girl, solo, smile
```

### 指定负面提示词

```
/draw 1girl, solo | bad hands, low quality
```

### 高级格式（支持任意顺序）

```
/draw 正面[1girl, solo] 负面[bad hands, low quality]
```

支持的标记：
- 正面：`正面`、`正向`、`正面提示词`、`正向提示词`
- 负面：`负面`、`反向`、`负面提示词`、`反向提示词`

支持的括号：`[]` 或 `{}`

### 分辨率控制

```
/draw 1girl, solo 宽1024 高768
```

支持的参数：
- 宽度：`宽`、`宽度`、`w`、`width`、`x`
- 高度：`高`、`高度`、`h`、`height`、`y`

### 超分倍率控制

```
/draw 1girl, solo 放大2
```

支持的参数：
- `scale`、`倍率`、`超分`、`放大`

### 合并转发

```
/draw 1girl, solo 转发=true
```

支持的参数：
- `chain`、`转发`、`合并转发`
- 值：`true`/`false` 或 `是`/`否`

### 组合使用

```
/draw 正面[1girl, solo] 负面[bad hands] 宽1024 高768 放大2 转发=是
```

## 工作流配置

1. 在 `data/astrbot_plugin_comfyui_hub/workflows/` 目录下放置工作流（在 ComfyUI 上使用导出为 API）文件（如 `txt2img.json`）
2. 在配置中指定工作流文件名和节点 ID
3. 插件会自动：
   - 设置正面/负面提示词
   - 修改分辨率（如果指定了宽高）
   - 修改超分倍率（如果指定了倍率）
   - 随机化种子

## 文件结构

```
astrbot_plugin_comfyui_hub/
├── main.py                    # 插件入口
├── comfyui_api.py            # ComfyUI API 封装
├── text_to_image.py          # 文生图功能
├── _conf_schema.json         # 配置模式
└── example_workflow.json     # 示例工作流
```

## 注意事项

- 需要先启动 ComfyUI 服务器
- 确保工作流文件中包含指定的节点 ID
- 超分功能需要工作流中包含对应的超分节点
- 生成时间取决于 ComfyUI 服务器性能和图片复杂度