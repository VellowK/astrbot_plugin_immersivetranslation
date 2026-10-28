# 沉浸式文档翻译 · AstrBot

使用 [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) / [BabelDOC](https://github.com/funstory-ai/BabelDOC) 开源引擎，将用户发送的 PDF 或图片翻译为译文 PDF / 双语 PDF，尽力保留版面、图片与公式。模型调用复用 AstrBot 已配置的供应商，无需另填模型 API key。

- 插件：`astrbot_plugin_immersivetranslation`，作者 VellowK，版本 `v0.2.1`。
- 源码及问题反馈：<https://github.com/VellowK/astrbot_plugin_immersivetranslation>。
- 兼容 AstrBot `>=4.16,<5`；宿主额外依赖仅 `aiohttp>=3.9,<4`。
- 本插件与商业「沉浸式翻译 / Immersive Translate」无隶属或官方合作关系；**不使用其账号、会员权益或免费额度，不是浏览器自动化方案**。

> **先了解费用与隐私：** 单独发送 PDF / 图片不会启动翻译、下载文档或安装引擎；只有明确说翻译或发送翻译命令后才处理。获准翻译后，文档原文会发送给 AstrBot 当前或指定的模型供应商，可能产生费用；请仅上传有权处理的内容。首次运行引擎可能下载较大的字体、版面分析模型等资源，需要网络、磁盘空间与等待时间。版面保留为尽力而为，非无损转换；复杂表格、公式、跨栏段落、扫描件和照片均可能产生错误。

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

### 推荐：首次翻译自动准备独立引擎

1. 在 AstrBot 插件管理中使用上述仓库地址安装插件，或由管理员上传插件 ZIP / 放入插件目录。插件宿主依赖见 [requirements.txt](<requirements.txt>)；通常由 AstrBot 安装器处理。
2. **无需填写或复制模型 key / 模型配置。** `provider_id` 留空即复用当前会话选中的 AstrBot 供应商；也可指定已有供应商 ID。先确认该供应商可进行普通文本生成。
3. 保持 `engine_python=""`、`auto_install_engine=true`。这表示选择在首次翻译时**联网安装独立引擎**，不是缺少路径；若不允许自动安装，应先关闭 `auto_install_engine` 并按下方手动部署。
4. 保存配置并按 AstrBot 提示重载插件，发送一份非敏感、较小的文字型英文 PDF，再说「帮我翻译一下这个文件」或 `/沉浸翻译`。也可先说「翻译」，再在 120 秒内发送附件。单独发送附件不会处理，旧版 `auto_translate_attachment=true` 也不再生效。

**准备发生在翻译后台任务中，不在 AstrBot 启动、插件安装或重载阶段执行 pip。** 插件先验证模型配置，再在首次需要时发送准备通知，使用 AstrBot 当前 Python 对应的基础解释器（`sys._base_executable`，不可用时回退 `sys.executable`）创建持久化隔离环境，不从 PATH 选择其他 Python：

```text
data/plugin_data/astrbot_plugin_immersivetranslation/runtime/venv
```

其中 `data` 指 AstrBot 实际数据目录。自动准备要求当前宿主 Python 为 **3.10–3.13**（基础翻译；推荐 **3.12**），且提供可用的 `venv` / `ensurepip`。pip 仅在受管环境中安装引擎，不向 AstrBot 宿主安装、升级引擎依赖，也不修改系统 Python。环境准备好后会继续当前翻译，后续作业复用该环境。

- `/沉浸翻译 状态` **只读**显示准备阶段和错误，不启动解释器、不触发安装或重试；手动引擎也仅显示最近一次自检状态，实际自检在翻译前完成。
- `/沉浸翻译 取消` 取消当前后台任务，包括正在运行的 pip 安装进程树。
- 准备失败、取消或超时后，先解决错误，再**重新提交翻译任务**重试；不会无限后台静默重试。
- `engine_setup_timeout` 默认 `1800` 秒，范围 `60–7200` 秒，仅用于引擎准备；它与翻译阶段的 `job_timeout` 独立。字体、版面模型等仍可能在第一次真实运行引擎时下载，不能用准备完成替代实际翻译验收。

基础引擎依赖见 [requirements-engine.txt](<requirements-engine.txt>)，固定 `pdf2zh-next==2.9.0`、`Pillow>=10,<12`、`bitstring>=4.3,<5` 并将 `PyMuPDF` 限定为 `>=1.25.2,<1.25.3`（旧版存在中英混排 OCR 文本层提取兼容问题）；其他传递依赖由 pip 解析，**不是完整锁定依赖树，也不保证所有 CPU、操作系统或容器镜像都有可用原生 wheel**。

### 网络、ZIP 安装与持久化

v0.2.1 一键安装 ZIP 包含**插件代码，不包含 Python 引擎依赖、字体或模型资源**，不是离线安装包。pip 继承管理员提供的 `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL`、HTTP(S) 代理环境变量、显式 `PIP_CONFIG_FILE` 与系统级 pip 配置；仍需正常的 DNS、联网权限与足够磁盘空间。由于引擎使用隔离的 `HOME`，**原宿主用户主目录下的 pip 配置不会自动沿用**；需要时请管理员用上述环境变量或绝对路径 `PIP_CONFIG_FILE` 明确指定。插件不会自动配置镜像或修复网络。

若 AstrBot 下载插件时遇到 `hk.gh-proxy.com` 解析失败，错误发生在插件代码运行**之前**。可由管理员在可联网设备下载 ZIP，再本地上传，以绕过 GitHub / 代理下载阶段；**不能因此修复后续 pip 包索引或字体 / 模型下载的 DNS 问题**。网络受限或管理员统一维护依赖时，可设置 `auto_install_engine=false`，使用下方手动环境；这也不等于翻译和首次模型资源下载自动离线。

Docker / 数据卷必须持久化**整个 `data/plugin_data/` 目录**，包括本插件的 `astrbot_plugin_immersivetranslation/` 全部内容，不仅是 `jobs` 或 `engine_home`，否则重建容器后可能丢失受管环境并重新下载。`runtime/venv` 是引擎环境，`engine_home` 是独立的引擎主目录 / 缓存；隐私与清理责任见下文。

**Windows 注意：不支持使用 Microsoft Store / Windows「应用执行别名」的 Python 自动准备引擎。** 这类解释器可能通过系统代理启动子进程，使取消任务无法可靠回收进程树；插件会在安装前明确拒绝，不修改已有环境。推荐用独立 Python 3.12 运行 AstrBot，或配置由独立 Python 创建的手动 `engine_python`。别名绑定的手动虚拟环境也会被拒绝。使用正常独立解释器创建的环境不受此限制。

### 可选：扫描件与图片 OCR

默认 `ocr_mode="off"`，不会因为图片 / 扫描件自动开启 OCR。管理员明确设置 `ocr_mode="auto"` 后，**受管环境且允许自动安装时，会在翻译后台准备任务中补装可选 OCR 依赖，无需手动 pip**。OCR 要求当前受管环境 Python 为 **3.10–3.12**（推荐 3.12，不能使用 3.13）；不兼容时请改用下方手动环境。

[requirements-engine-ocr.txt](<requirements-engine-ocr.txt>) 包含基础引擎依赖并固定 `rapidocr-onnxruntime==1.4.4`。若指定了手动 `engine_python`，插件不会为其补装 OCR，需管理员自行安装该依赖文件；`auto_install_engine=false` 时同样不会自动补装。OCR 面向清晰的英文 / 中文文档，不应视为通用多语言、照片或手写识别方案。

**OCR 使用黑字白底 / 白色遮盖兼容策略**：扫描页可能栅格化，白色覆盖层可能遮挡原图、底色或细节，识别与译文位置也可能不准确。图片输入的目标产物是**翻译后的 PDF**，不保证返回 PNG/JPEG，也不承诺保留照片视觉效果。开启 OCR 不代表每一份扫描件都能成功翻译。

### 高级：管理员手动部署独立环境

宿主 Python 不在支持范围、缺少 `venv` / `ensurepip`、平台无可用原生 wheel，或需要管理员控制 / 预置依赖时，使用此方案。**推荐独立 Python 3.12**；基础引擎可用 3.10–3.13，OCR 仅 3.10–3.12。管理员需自行提供适配系统和 CPU 的解释器、系统库与依赖；换成手动安装不保证不支持的平台即可运行。手动环境 Python 版本不必等于 AstrBot 宿主版本。

仅在 AstrBot 实际使用的 Python 中补齐宿主依赖（安装器已处理则跳过）：

```bash
"/path/to/astrbot/.venv/bin/python" -m pip install -r "/path/to/astrbot/data/plugins/astrbot_plugin_immersivetranslation/requirements.txt"
```

Windows PowerShell：

```powershell
& "C:\AstrBot\.venv\Scripts\python.exe" -m pip install -r "C:\AstrBot\data\plugins\astrbot_plugin_immersivetranslation\requirements.txt"
```

**不要在 AstrBot 宿主环境安装引擎依赖。** Linux（先由管理员安装系统 Python 3.12 与 venv / ensurepip 支持）：

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

手动环境如需 OCR，再安装可选依赖并明确启用 `ocr_mode="auto"`：

```bash
"/opt/astrbot-translation/.engine/bin/python" -m pip install -r "/path/to/astrbot/data/plugins/astrbot_plugin_immersivetranslation/requirements-engine-ocr.txt"
```

```powershell
& "C:\AstrBot Translation\.engine\Scripts\python.exe" -m pip install -r "C:\AstrBot\data\plugins\astrbot_plugin_immersivetranslation\requirements-engine-ocr.txt"
```

路径均为示例，请替换成实际安装位置。在插件配置中将 `engine_python` 设为该环境 Python **可执行文件的绝对路径**（Linux 示例 `/opt/astrbot-translation/.engine/bin/python`，Windows 示例 `C:\AstrBot Translation\.engine\Scripts\python.exe`），不要填写目录、shell 命令、相对命令或另一台机器的路径。Docker 中必须使用同一容器内的解释器。

**显式 `engine_python` 始终由管理员维护：插件只检查与运行，不自动对它执行 pip 安装或升级，即使 `auto_install_engine=true` 也不例外。** 可以关闭 `auto_install_engine`，明确禁止插件自动准备受管环境。网络受限环境的依赖与引擎首次资源需管理员另外预置。

[engine_runner.py](<engine_runner.py>) 的 `--check` 检查依赖版本与导入情况，不是实际模型翻译验收，也不表示字体、版面模型、供应商连接及所有文档都已验证。失败时请在独立环境修复，勿把引擎包装进宿主。配置后按 AstrBot 提示重载，先用小型非敏感文字 PDF 验证模型、文件返回与排版，再逐步扩大规模。

## 使用

### 明确请求才翻译

| 操作 | 用法 |
| --- | --- |
| 只发 PDF / 图片 | 仅在内存记录同一用户的近期附件引用，不回复、不拦截、不下载、不安装、不翻译 |
| 发文件后要求翻译 | 在 120 秒内说「翻译」「帮我翻译一下这个文件」「把刚才那份PDF翻译成中文」，或 `/沉浸翻译` |
| 先要求翻译，再发文件 | 先说「帮我翻译一下」或 `/沉浸翻译`；在提示的 120 秒内发送一个无其他指令的附件 |
| 帮助 | `/沉浸翻译 帮助` |
| 状态 | `/沉浸翻译 状态`；只读查看任务、引擎准备阶段 / 错误，不触发安装 |
| 取消 | `/沉浸翻译 取消`；取消任务，包括引擎准备中的 pip 进程树 |

「帮我总结」「这个不用翻译」或讨论翻译插件不视为翻译授权；「先别翻译」会清除同一用户的等待请求。正在运行的任务请用取消命令中止。自然语言仅识别保守的明确请求，模糊说法不猜测，请改用 `/沉浸翻译`。旧版 `auto_translate_attachment` 已停用，旧配置中的 `true` 不会恢复附件自动翻译。

命令与模型工具只使用**当前消息附件**，或**同一用户、同一会话**中近期收到的附件；默认近期有效期为 120 秒。其他用户、其他会话的附件不会被用作当前请求。没有有效附件时请重新上传；不能传入任意本地路径或 URL 让插件读取。

群聊默认关闭。要在群聊使用，管理员需开启 `enable_group`；默认仍要求 @ 机器人（`group_require_at=true`），并受用户白名单等权限控制。平台适配器必须能提供可下载附件并支持文件返回；不同聊天平台的行为、消息大小限制可能不同。

### 模型工具

注册工具为 `@filter.llm_tool(name='immersive_translate')`，参数**只有 `target_language`（字符串）**；它沿用当前 / 近期附件，不接受文件路径、URL、供应商密钥或用户 ID。工具调用也会检查实际消息中的明确翻译请求，不能由模型自行猜测授权；单独附件、总结请求、工具参数和文件名均不构成授权。自然语言示例：「把刚才那份 PDF 翻译成中文」。

默认配置为英译中，支持语言代码（例如 `en`、`zh`、`ja`、`zh-CN`）传给开源引擎与模型。实际语言质量取决于引擎字体与模型能力，不能理解为所有语言已验证。可选本地 OCR 仅面向英文/中文。

## 配置参考

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `engine_python` | `""` | 空时使用持久化受管环境；非空为管理员维护的 Python 绝对可执行路径，不自动安装 / 升级 |
| `auto_install_engine` | `true` | 允许在翻译后台任务中联网准备受管引擎 / OCR；关闭后须预置依赖，不修改手动环境 |
| `engine_setup_timeout` | `1800` | 引擎准备超时，秒，范围 `60–7200`；独立于翻译 `job_timeout` |
| `provider_id` | `""` | 空时使用当前会话的供应商；非空指定已有供应商 ID，无需复制模型 key |
| `source_language` | `"en"` | 源语言，默认英文 |
| `target_language` | `"zh"` | 目标语言，默认中文；工具参数用于该次请求 |
| `output_mode` | `"both"` | `mono` 译文 PDF、`dual` 双语 PDF、`both` 两者 |
| `ocr_mode` | `"off"` | 默认关闭；明确启用 `auto` 后受管环境可自动补装 OCR，手动环境须管理员安装；仅 Python 3.10–3.12 |
| `allowed_users` | `[]` | 用户 ID 白名单；空列表不额外按用户 ID 限制，其他权限限制仍适用 |
| `enable_group` | `false` | 是否允许群聊使用 |
| `group_require_at` | `true` | 群聊允许时仍要求 @ 机器人 |
| `auto_translate_attachment` | `false` | 已停用，仅保留旧配置兼容；即使旧值为 `true`，也必须明确请求翻译 |
| `max_file_mb` | `20` | 单个输入文件大小上限，MB |
| `max_pages` | `20` | 输入 PDF 页数上限 |
| `max_pixels_million` | `20` | 图片 / 渲染像素上限，百万像素；不是文件大小 |
| `max_model_calls` | `200` | 单作业模型调用次数预算；不是免费额度或金额上限 |
| `model_timeout` | `120` | 单次模型调用超时，秒 |
| `job_timeout` | `1800` | 翻译阶段超时，秒；引擎准备单独使用 `engine_setup_timeout`，真实引擎首次资源下载仍在翻译阶段 |
| `cache_minutes` | `60` | 插件作业缓存保留时间，分钟；不控制引擎自身缓存 |
| `recent_timeout` | `120` | 同用户同会话近期附件有效期，秒 |
| `allow_private_downloads` | `false` | 默认阻止附件 URL 下载到私有 / 回环网络；仅可信平台部署确有需要时开启 |

`allow_private_downloads` 不是任意 URL 翻译入口。允许内网下载会增加访问内部服务的风险，不应为处理陌生链接而随意开启。资源与调用次数限制只是运行保护，不保证最终花费；取消任务也不能撤销供应商已接收请求产生的费用。

## 缓存、日志与运营者责任

- 插件作业目录保存处理期间的输入、副本及输出，按插件缓存策略清理；取消、超时和进程异常后仍应核查残留文件。不要把作业目录、测试样本或令牌加入仓库。
- **引擎缓存与插件作业缓存独立。** 插件启动引擎时，为子进程单独设置 `HOME` / `USERPROFILE`，根目录为 AstrBot 数据目录下 `plugin_data/astrbot_plugin_immersivetranslation/engine_home`，避免写入宿主用户配置。BabelDOC 仍可能在该目录下的 `.cache/babeldoc` 等上游缓存中保存原文、译文及字体 / 模型；`cache_minutes` 不会清除这些缓存。直接手动运行引擎命令时，不经过插件环境隔离，默认可能使用系统用户主目录。
- 运营者应按运行账号管理引擎缓存的访问权限、保留期限与删除；共享机器、备份和容器卷也要一起考虑。删除资源缓存可能导致再次下载。
- 上游诊断日志可能包含原文；只在可信本地检查，分享日志前去除文档内容、账号信息、令牌及密钥。
- 翻译不是离线推理承诺：OCR / 排版在本地运行，但文字交给 AstrBot 所配置的供应商；即使供应商是本地模型，首次资源下载仍可能联网。

## 常见问题与限制

- **引擎准备失败：** 先用 `/沉浸翻译 状态` 只读查看阶段 / 错误。v0.2.1 的失败提示附安全的 Python 版本、操作系统、架构和位数，以及插件数据目录下 `runtime/setup.log` 的位置；仅在严格识别到已知依赖时显示包名，不把原始 pip 输出、索引 URL 或凭据发送到聊天。日志有大小上限并保留 stdout / stderr 的末尾（分流保存，非精确时间顺序）。请管理员检查末尾约 80 行，分享前遮掉账号、私有索引、密钥和文档内容；通常位于 `data/plugin_data/astrbot_plugin_immersivetranslation/runtime/setup.log`，其中 `data` 为实际 AstrBot 数据目录。
- **没有适用安装包 / 构建 wheel 失败：** 这只是错误类别，不能仅凭消息收发日志判定是哪个依赖。需结合安装日志中 `No matching distribution` / `Could not find a version` / `Failed building wheel` 的具体行与实际宿主 Python / CPU / 容器系统检查。Alpine / musl、不同 ARM 平台、32 位解释器或不完整镜像可能缺少原生 wheel；需要按具体依赖选择兼容系统或手动引擎，不能保证只切换镜像即可解决。网络失败有时也伴随“找不到版本”，应先修复网络。插件不会自动换源或放宽依赖版本。修复后重新提交翻译；不要向 AstrBot 宿主安装引擎依赖，也不要靠反复重载触发安装。
- **手动环境缺少引擎 / 版本不兼容：** 检查 `engine_python` 是绝对可执行路径，并使用同一个解释器执行 `--check` 与安装 [requirements-engine.txt](<requirements-engine.txt>)。插件不会替手动环境安装或升级，不要将 `pdf2zh-next` 升级到未经适配的版本。
- **Docker 中连接失败：** 检查引擎与 AstrBot 是否位于同一网络命名空间、引擎路径是否存在于该容器；本机桥不是给其他容器连接的常驻服务器。持久化整个插件数据目录，避免容器重建后丢失引擎。
- **扫描件无可翻译文字：** 默认不自动开启 OCR。明确启用 `auto` 并使用 Python 3.10–3.12；受管环境允许自动安装时会补装可选依赖，手动环境仍须管理员安装。可先用文字型 PDF 验证基础路径。
- **首次运行很慢：** pip 准备和字体、版面模型下载是不同阶段；确保 DNS、网络、磁盘与各阶段超时设置合理。ZIP 上传仅绕过插件下载阶段，不使 pip / 模型下载离线。不要反复重复提交消耗预算的任务。
- **附件链接被拒绝：** 仅处理平台消息附件，不接收任意 URL；内网附件服务需运营者评估后显式配置，不能绕过默认网络保护。
- **译文排版不理想：** 复杂公式、表格、跨栏、嵌入字体及扫描质量都可能影响结果。保留原件、检查译文，不用于未经人工复核的高风险决策。

## 开发检查与验证范围

推荐使用 Python **3.11 / 3.12**，安装宿主依赖 `aiohttp`、离线测试 I/O 所需的 `PyMuPDF==1.25.2` / `Pillow>=10,<12` 与 `ruff`，不必安装完整翻译引擎或 OCR，运行：

```bash
python -m unittest discover tests -v
ruff check .
ruff format --check .
```

提供 [GitHub Actions 模板](<tests/ci-workflow.yml>)，仓库默认未启用自动 CI；管理员可使用具备 workflow 权限的凭据将其复制到 `.github/workflows/tests.yml` 后启用。自动化测试使用离线假响应，不下载真实模型 / 字体或调用真实供应商。需要 `fitz` / Pillow 的可选 runner 测试在依赖缺失时跳过。

构建可上传的源码安装包：

```bash
python tools/build_package.py
```

输出 `dist/astrbot_plugin_immersivetranslation-v0.2.1.zip`，包含一个插件根目录。打包器按显式源码清单及测试文件生成，不打包本地虚拟环境、Git 状态、缓存、日志、文档样本或凭据；目标文件已存在时拒绝覆盖，可用 `--output` 指定新路径。[打包测试](<tests/test_package.py>) 会检查 ZIP 清单与默认配置。

本地额外完成了 **真实 `pdf2zh-next 2.9.0` / `BabelDOC 0.6.2` 引擎 + 模拟模型** 的单页文字 PDF 冒烟验证：5 次桥接请求，生成译文及左右双语 PDF，并检查了渲染结果和图形保留。依赖兼容检查发现 `bitstring 5` 移除了引擎所需的 `BitStream`，因此在 [requirements-engine.txt](<requirements-engine.txt>) 限制为 `<5`。

v0.2.0 自动安装器另外在 **Windows / 独立 Python 3.12.13** 上真实创建了受管 venv 并执行 pip 安装，随后完成引擎导入及缓存复用验证（缓存自检约 3.7 秒）；最终复用验证窗口中，宿主依赖清单前后未变化。完整离线回归共 **213 项，212 项通过、1 项因 Windows 无符号链接权限跳过**；包括实际子进程归属检查、退出后代清理与双重取消测试。Windows 商店别名环境已作为不支持情形明确拒绝。以上不是所有平台的冷启动安装、离线安装或真实 OCR 验收。

v0.2.1 修正了附件默认触发与模型工具的授权检查，加入完整请求尾部校验，避免能力 / 费用询问、否定、撤回和未知目标语言启动翻译；同时改进安装失败分类和限长私有日志。Windows / Python 3.12 离线回归共 **235 项，234 项通过、1 项符号链接权限跳过**，Ruff 检查通过。某个实际部署的依赖安装失败仍须结合其脱敏安装日志诊断，这些改动不表示已修复所有平台的 wheel 或构建兼容问题。

上述检查**不是已部署声明**，不代表接入真实 AstrBot 服务、真实供应商或真实 OCR 完成端到端验收，模拟模型输出也不代表翻译质量。部署者仍需在自己的 Python / 网络、聊天平台、模型和非敏感样本上验证准备与翻译完整流程。

## 开源许可与致谢

本插件采用 **AGPL-3.0**；完整文本见 [LICENSE](<LICENSE>) 与 [GNU AGPL v3](https://www.gnu.org/licenses/agpl-3.0.html)。第三方归属与资源许可说明见 [THIRD_PARTY_NOTICES.md](<THIRD_PARTY_NOTICES.md>)。

感谢 [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next)、[BabelDOC](https://github.com/funstory-ai/BabelDOC)、[PyMuPDF](https://github.com/pymupdf/PyMuPDF)、[Pillow](https://github.com/python-pillow/Pillow)、[RapidOCR](https://github.com/RapidAI/RapidOCR) 与 [aiohttp](https://github.com/aio-libs/aiohttp)。上游引擎的 AGPL 及相关 [GPL](https://www.gnu.org/licenses/gpl-3.0.html) / 其他依赖条款应按各组件实际许可遵守；单独环境 / 子进程不自动免除许可义务，字体与模型继续保留其上游通知。此处不是法律意见。
