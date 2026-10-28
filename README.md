# 沉浸式文档翻译 · AstrBot

使用 [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) / [BabelDOC](https://github.com/funstory-ai/BabelDOC) 开源引擎，将用户发送的 PDF 或图片翻译为译文 PDF / 双语 PDF，尽力保留版面、图片与公式。模型调用复用 AstrBot 已配置的供应商，无需另填模型 API key。

- 插件：`astrbot_plugin_immersivetranslation`，作者 VellowK，版本 `v0.1.0`。
- 源码及问题反馈：<https://github.com/VellowK/astrbot_plugin_immersivetranslation>。
- 兼容 AstrBot `>=4.16,<5`；宿主额外依赖仅 `aiohttp>=3.9,<4`。
- 本插件与商业「沉浸式翻译 / Immersive Translate」无隶属或官方合作关系；**不使用其账号、会员权益或免费额度，不是浏览器自动化方案**。

> **先了解费用与隐私：** 私聊附件默认自动翻译。文档原文会发送给 AstrBot 当前或指定的模型供应商，可能产生费用；请仅上传有权处理的内容。首次运行引擎可能下载较大的字体、版面分析模型等资源，需要网络、磁盘空间与等待时间。版面保留为尽力而为，非无损转换；复杂表格、公式、跨栏段落、扫描件和照片均可能产生错误。

## 工作原理与部署边界

```text
当前消息 / 同一用户同一会话的近期附件
    → AstrBot 插件（权限检查、附件下载、资源限制、任务管理）
    → 独立 engine_python 子进程（PDFMathTranslate-next → BabelDOC）
    → 每个作业独立的 127.0.0.1 临时认证 OpenAI 兼容桥
    → context.llm_generate → AstrBot 已配置模型
    → 返回译文 PDF / 双语 PDF
```

桥接器使用短期随机令牌和临时端口，仅用于当前作业；不会提取、复制或传给引擎真实供应商 API key。引擎得到的是本机桥接地址与临时令牌。桥接调用不附带聊天历史、不传工具，也不会让文档内容调用 AstrBot 工具。

**引擎是独立 Python 环境，不是独立网络服务。** 宿主进程与引擎子进程必须位于相同网络命名空间，以便访问同一个 `127.0.0.1`。AstrBot 在 Docker 中时，引擎环境应在同一容器内创建，路径也必须是容器内部的绝对路径；不能把宿主机 Python 路径或另一个普通 Docker 容器当作 `engine_python`。不要对公网开放桥接端口。

## 安装

### 1. 安装插件与宿主依赖

在 AstrBot 插件管理中使用上述仓库地址安装插件，或由管理员将插件放入 AstrBot 插件目录。只在 AstrBot 实际使用的 Python 环境安装 [requirements.txt](<requirements.txt>)，例如：

```bash
"/path/to/astrbot/.venv/bin/python" -m pip install -r "/path/to/astrbot/data/plugins/astrbot_plugin_immersivetranslation/requirements.txt"
```

Windows PowerShell：

```powershell
& "C:\AstrBot\.venv\Scripts\python.exe" -m pip install -r "C:\AstrBot\data\plugins\astrbot_plugin_immersivetranslation\requirements.txt"
```

路径均为示例，请替换为实际安装位置。安装器若已在正确宿主环境处理依赖，不必重复安装。

### 2. 建立独立引擎环境

基础引擎支持 **Python 3.10–3.13，推荐 3.12**。引擎固定 `pdf2zh-next==2.9.0`、`Pillow>=10,<12`，并保留上游 `PyMuPDF<1.25.3` 约束；其他传递依赖仍由 pip 解析，并非完整锁定的依赖树。不要在 AstrBot 宿主环境安装引擎依赖；宿主的 Python 版本不必等于引擎版本。

Linux（先安装系统提供的 Python 3.12 与 venv 支持）：

```bash
python3.12 -m venv "/opt/astrbot-translation/.engine"
"/opt/astrbot-translation/.engine/bin/python" -m pip install --upgrade pip
"/opt/astrbot-translation/.engine/bin/python" -m pip install -r "/path/to/astrbot/data/plugins/astrbot_plugin_immersivetranslation/requirements-engine.txt"
"/opt/astrbot-translation/.engine/bin/python" "/path/to/astrbot/data/plugins/astrbot_plugin_immersivetranslation/engine_runner.py" --check
```

Windows PowerShell（已安装 Python 3.12 与 Python Launcher）：

```powershell
py -3.12 -m venv "C:\AstrBot Translation\.engine"
& "C:\AstrBot Translation\.engine\Scripts\python.exe" -m pip install --upgrade pip
& "C:\AstrBot Translation\.engine\Scripts\python.exe" -m pip install -r "C:\AstrBot\data\plugins\astrbot_plugin_immersivetranslation\requirements-engine.txt"
& "C:\AstrBot Translation\.engine\Scripts\python.exe" "C:\AstrBot\data\plugins\astrbot_plugin_immersivetranslation\engine_runner.py" --check
```

`--check` 检查引擎依赖版本与导入情况；它不是实际模型翻译验收，也不表示字体、版面模型、供应商连接及所有文档都已验证。检查失败请在独立环境修复依赖，不要把引擎包装进宿主。

### 3. 配置引擎路径与供应商

在 AstrBot 插件配置中，将 `engine_python` 设置为引擎 Python **可执行文件的绝对路径**，例如：

- Linux：`/opt/astrbot-translation/.engine/bin/python`
- Windows：`C:\AstrBot Translation\.engine\Scripts\python.exe`

不要填写环境目录、shell 命令、`python` 相对命令或另一台机器的路径。默认为空，表示尚未配置引擎，不会自动安装依赖。`provider_id` 留空时使用当前会话选择的 AstrBot 供应商；也可以填写已存在的供应商 ID。确保该供应商能进行普通文本生成。

保存配置并按 AstrBot 提示重载插件。先用非敏感、较小的文字型英文 PDF 验证实际模型、附件返回与排版，再逐步扩大文件规模。

### 可选：扫描件与图片 OCR

默认 `ocr_mode="off"`。需要处理扫描 PDF 或图片中的文字时，由管理员明确安装可选 OCR 依赖并启用 `ocr_mode="auto"`。OCR 环境限定 **Python 3.10–3.12**（推荐 3.12，不能使用 3.13）：

Linux：

```bash
"/opt/astrbot-translation/.engine/bin/python" -m pip install -r "/path/to/astrbot/data/plugins/astrbot_plugin_immersivetranslation/requirements-engine-ocr.txt"
```

Windows PowerShell：

```powershell
& "C:\AstrBot Translation\.engine\Scripts\python.exe" -m pip install -r "C:\AstrBot\data\plugins\astrbot_plugin_immersivetranslation\requirements-engine-ocr.txt"
```

[requirements-engine-ocr.txt](<requirements-engine-ocr.txt>) 已包含基础引擎依赖并固定 `rapidocr-onnxruntime==1.4.4`。运行过程中不会自动执行 pip 安装。OCR 面向清晰的英文 / 中文文档，不应视为通用多语言、照片或手写识别方案。

**OCR 使用黑字白底 / 白色遮盖兼容策略**：扫描页可能栅格化，白色覆盖层可能遮挡原图、底色或细节，识别与译文位置也可能不准确。图片输入的目标产物是**翻译后的 PDF**，不保证返回 PNG/JPEG，也不承诺保留照片视觉效果。开启 OCR 不代表每一份扫描件都能成功翻译。

## 使用

### 命令与自动触发

| 操作 | 用法 |
| --- | --- |
| 翻译 | 上传 PDF / 图片并发送 `/沉浸翻译`，或在近期附件仍有效时发送该命令 |
| 帮助 | `/沉浸翻译 帮助` |
| 状态 | `/沉浸翻译 状态` |
| 取消 | `/沉浸翻译 取消` |
| 私聊自动翻译 | 默认开启，发送支持的 PDF / 图片附件即可触发；不需要再重复提交命令 |

命令与模型工具只使用**当前消息附件**，或**同一用户、同一会话**中近期收到的附件；默认近期有效期为 120 秒。其他用户、其他会话的附件不会被用作当前请求。没有有效附件时请重新上传；不能传入任意本地路径或 URL 让插件读取。

群聊默认关闭。要在群聊使用，管理员需开启 `enable_group`；默认仍要求 @ 机器人（`group_require_at=true`），并受用户白名单等权限控制。平台适配器必须能提供可下载附件并支持文件返回；不同聊天平台的行为、消息大小限制可能不同。

### 模型工具

注册工具为 `@filter.llm_tool(name='immersive_translate')`，参数**只有 `target_language`（字符串）**；它沿用当前 / 近期附件，不接受文件路径、URL、供应商密钥或用户 ID。自然语言示例：「把刚才那份 PDF 翻译成中文」。

默认配置为英译中，支持语言代码（例如 `en`、`zh`、`ja`、`zh-CN`）传给开源引擎与模型。实际语言质量取决于引擎字体与模型能力，不能理解为所有语言已验证。可选本地 OCR 仅面向英文/中文。

## 配置参考

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `engine_python` | `""` | 独立引擎 Python 可执行文件的绝对路径；必须手动配置 |
| `provider_id` | `""` | 空时使用当前会话的供应商；非空指定已有供应商 ID |
| `source_language` | `"en"` | 源语言，默认英文 |
| `target_language` | `"zh"` | 目标语言，默认中文；工具参数用于该次请求 |
| `output_mode` | `"both"` | `mono` 译文 PDF、`dual` 双语 PDF、`both` 两者 |
| `ocr_mode` | `"off"` | 默认关闭；`auto` 需管理员明确启用并安装可选 OCR 依赖 |
| `allowed_users` | `[]` | 用户 ID 白名单；空列表不额外按用户 ID 限制，其他权限限制仍适用 |
| `enable_group` | `false` | 是否允许群聊使用 |
| `group_require_at` | `true` | 群聊允许时仍要求 @ 机器人 |
| `auto_translate_attachment` | `true` | 私聊 PDF / 图片附件自动触发；注意模型费用 |
| `max_file_mb` | `20` | 单个输入文件大小上限，MB |
| `max_pages` | `20` | 输入 PDF 页数上限 |
| `max_pixels_million` | `20` | 图片 / 渲染像素上限，百万像素；不是文件大小 |
| `max_model_calls` | `200` | 单作业模型调用次数预算；不是免费额度或金额上限 |
| `model_timeout` | `120` | 单次模型调用超时，秒 |
| `job_timeout` | `1800` | 整个作业超时，秒 |
| `cache_minutes` | `60` | 插件作业缓存保留时间，分钟；不控制引擎自身缓存 |
| `recent_timeout` | `120` | 同用户同会话近期附件有效期，秒 |
| `allow_private_downloads` | `false` | 默认阻止附件 URL 下载到私有 / 回环网络；仅可信平台部署确有需要时开启 |

`allow_private_downloads` 不是任意 URL 翻译入口。允许内网下载会增加访问内部服务的风险，不应为处理陌生链接而随意开启。资源与调用次数限制只是运行保护，不保证最终花费；取消任务也不能撤销供应商已接收请求产生的费用。

## 缓存、日志与运营者责任

- 插件作业目录保存处理期间的输入、副本及输出，按插件缓存策略清理；取消、超时和进程异常后仍应核查残留文件。不要把作业目录、测试样本或令牌加入仓库。
- **引擎缓存与插件作业缓存独立。** 插件启动引擎时，为子进程单独设置 `HOME` / `USERPROFILE`，根目录为 AstrBot 数据目录下 `plugin_data/astrbot_plugin_immersivetranslation/engine_home`，避免写入宿主用户配置。BabelDOC 仍可能在该目录下的 `.cache/babeldoc` 等上游缓存中保存翻译文本及字体 / 模型；`cache_minutes` 不会清除这些缓存。直接手动运行引擎命令时，不经过插件环境隔离，默认可能使用系统用户主目录。
- 运营者应按运行账号管理引擎缓存的访问权限、保留期限与删除；共享机器、备份和容器卷也要一起考虑。删除资源缓存可能导致再次下载。
- 上游诊断日志可能包含原文；只在可信本地检查，分享日志前去除文档内容、账号信息、令牌及密钥。
- 翻译不是离线推理承诺：OCR / 排版在本地运行，但文字交给 AstrBot 所配置的供应商；即使供应商是本地模型，首次资源下载仍可能联网。

## 常见问题与限制

- **提示缺少引擎 / 版本不兼容：** 检查 `engine_python` 是绝对可执行路径，并使用同一个解释器执行 `--check` 与安装 [requirements-engine.txt](<requirements-engine.txt>)。不要将 `pdf2zh-next` 升级到未经适配的版本。
- **Docker 中连接失败：** 检查引擎与 AstrBot 是否位于同一网络命名空间、引擎路径是否存在于该容器；本机桥不是给其他容器连接的常驻服务器。
- **扫描件无可翻译文字：** 默认不自动开启 OCR。管理员需安装可选 OCR、使用兼容 Python 并启用 `auto`；或先用文字型 PDF 验证基础路径。
- **首次运行很慢：** 字体、版面模型等下载可能较大；确保网络、磁盘与超时设置合理。不要据此反复重复提交消耗预算的任务。
- **附件链接被拒绝：** 仅处理平台消息附件，不接收任意 URL；内网附件服务需运营者评估后显式配置，不能绕过默认网络保护。
- **译文排版不理想：** 复杂公式、表格、跨栏、嵌入字体及扫描质量都可能影响结果。保留原件、检查译文，不用于未经人工复核的高风险决策。

## 开发检查与验证范围

推荐使用 Python **3.11 / 3.12**，安装宿主依赖 `aiohttp`、离线测试 I/O 所需的 `PyMuPDF<1.25.3` / `Pillow>=10,<12` 与 `ruff`，不必安装完整翻译引擎或 OCR，运行：

```bash
python -m unittest discover tests -v
ruff check .
ruff format --check .
```

提供 [GitHub Actions 模板](<tests/ci-workflow.yml>)，仓库默认未启用自动 CI；管理员可使用具备 workflow 权限的凭据将其复制到 `.github/workflows/tests.yml` 后启用。自动化测试使用离线假响应，不下载真实模型 / 字体或调用真实供应商。需要 `fitz` / Pillow 的可选 runner 测试在依赖缺失时跳过。

本地额外完成了 **真实 `pdf2zh-next 2.9.0` / `BabelDOC 0.6.2` 引擎 + 模拟模型** 的单页文字 PDF 冒烟验证：5 次桥接请求，生成译文及左右双语 PDF，并检查了渲染结果和图形保留。依赖兼容检查发现 `bitstring 5` 移除了引擎所需的 `BitStream`，因此在 [requirements-engine.txt](<requirements-engine.txt>) 限制为 `<5`。

以上不代表接入真实 AstrBot 服务、真实供应商或真实 OCR 完成端到端验收，模拟模型输出也不代表翻译质量。部署者仍需在自己的聊天平台、模型和非敏感样本上验证完整流程。

## 开源许可与致谢

本插件采用 **AGPL-3.0**；完整文本见 [LICENSE](<LICENSE>) 与 [GNU AGPL v3](https://www.gnu.org/licenses/agpl-3.0.html)。第三方归属与资源许可说明见 [THIRD_PARTY_NOTICES.md](<THIRD_PARTY_NOTICES.md>)。

感谢 [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next)、[BabelDOC](https://github.com/funstory-ai/BabelDOC)、[PyMuPDF](https://github.com/pymupdf/PyMuPDF)、[Pillow](https://github.com/python-pillow/Pillow)、[RapidOCR](https://github.com/RapidAI/RapidOCR) 与 [aiohttp](https://github.com/aio-libs/aiohttp)。上游引擎的 AGPL 及相关 [GPL](https://www.gnu.org/licenses/gpl-3.0.html) / 其他依赖条款应按各组件实际许可遵守；单独环境 / 子进程不自动免除许可义务，字体与模型继续保留其上游通知。此处不是法律意见。
