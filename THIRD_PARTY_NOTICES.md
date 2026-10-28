# Third-party notices

本项目为独立的 AstrBot 插件，使用开源的 PDFMathTranslate-next/BabelDOC 路线；不隶属于、也不代表「沉浸式翻译 / Immersive Translate」或上游维护团队。不提供其商业服务、会员权益或免费页数额度。项目名称、商标及标识仍归各自权利人所有。

## 本插件的许可

Copyright (C) 2026 VellowK and contributors.

本插件采用 **GNU Affero General Public License version 3 (AGPL-3.0)**。完整许可文本随发行版的 [LICENSE](<LICENSE>) 提供；权威文本见 [GNU AGPL v3](https://www.gnu.org/licenses/agpl-3.0.html)。程序按现状提供，不作适销性、特定用途适用性等保证。

选择 AGPL-3.0 是考虑到所采用的上游引擎及其依赖的许可。宿主和引擎使用独立 Python 环境、经回环 HTTP 和子进程交互，是依赖隔离与部署设计，**不是对 GPL/AGPL 义务的自动豁免**。分发、修改、组合及向远程用户提供服务时，应评估适用的源码、许可副本、版权声明及网络交互义务；如适用，应向用户提供实际运行版本（包括修改）的对应源码。本说明不是法律意见，不能替代逐项许可审查或专业建议。

插件源码与发布入口：<https://github.com/VellowK/astrbot_plugin_immersivetranslation>。部署了修改版本的运营者不能只链接未包含其修改的上游仓库来代替适用的对应源码义务。

## 主要第三方项目

下表是主要直接依赖与引擎组件的归属说明，不是完整的传递依赖清单，也不重新授予第三方许可。安装时应保留所安装版本随附的 LICENSE、NOTICE、版权及免责声明；上游链接可能随版本变动，应以实际安装包为准。

| 项目 / 权利人 | 用途与版本范围 | 源码与许可 |
| --- | --- | --- |
| aiohttp contributors | AstrBot 宿主内的下载与本机桥接 HTTP；`>=3.9,<4` | [源码](https://github.com/aio-libs/aiohttp) · [Apache-2.0 许可](https://github.com/aio-libs/aiohttp/blob/master/LICENSE.txt) |
| PDFMathTranslate-next contributors | 独立引擎入口；`pdf2zh-next==2.9.0` | [源码](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) · [AGPL-3.0 许可](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/blob/main/LICENSE) |
| BabelDOC contributors | PDF 版面分析、翻译排版；版本由上述引擎的依赖约束解析 | [源码](https://github.com/funstory-ai/BabelDOC) · [AGPL-3.0 许可](https://github.com/funstory-ai/BabelDOC/blob/main/LICENSE) |
| Artifex Software / PyMuPDF contributors | PDF 读取、预检与渲染；遵循引擎约束 `<1.25.3` | [源码](https://github.com/pymupdf/PyMuPDF) · [许可说明（AGPL 或商业许可）](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright) |
| Pillow contributors / Python Imaging Library contributors | 图片读取与处理；`>=10,<12` | [源码](https://github.com/python-pillow/Pillow) · [HPND / PIL 许可及第三方声明](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| RapidAI / RapidOCR contributors | 可选 OCR；`rapidocr-onnxruntime==1.4.4` | [源码](https://github.com/RapidAI/RapidOCR) · [Apache-2.0 许可](https://github.com/RapidAI/RapidOCR/blob/main/LICENSE) |
| Microsoft / ONNX Runtime contributors | 可选 OCR 等组件使用的推理运行时 | [源码](https://github.com/microsoft/onnxruntime) · [MIT 许可](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) |

PyMuPDF 的商业许可是上游提供的另一种许可方式，本项目不附带该商业授权。底层 MuPDF、其他传递依赖、各平台二进制包中包含的代码或数据亦可能具有单独的许可与归属；本表不保证已穷举。

## 字体、版面模型及 OCR 权重

引擎首次运行可能下载字体、版面分析模型和其他资源，可选 OCR 也可能使用单独分发的模型权重。**代码许可不自动覆盖字体、模型权重、训练数据或其他资源。** 这些资源保留各自上游许可与通知；复制、离线打包、再分发或商业部署前应核查实际资源包的来源、版本与授权。请保留引擎资源包附带的许可、NOTICE 和版权声明，不得把它们统一标注为本插件许可。

资源管理与上游背景见 [BabelDOC](https://github.com/funstory-ai/BabelDOC)、[PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) 及其依赖清单。本文不承诺任何特定字体或模型的授权适用于所有使用方式。

## 用户文档、隐私与输出

用户输入文档及其文字内容的权利属于各自权利人。上传者与运营者应具备处理、发送至所选模型供应商并使用译文的权限；本项目的开源许可不会授予输入文档的版权、保密信息或个人信息处理权。

模型调用将向 AstrBot 中配置的供应商发送待翻译原文，并可能产生费用；供应商及平台的服务条款、数据保留政策另外适用。引擎可能在 `~/.cache/babeldoc` 等上游缓存中持久保存翻译文本、字体和模型。插件作业缓存清理不等于引擎缓存清理，运营者应分别管理访问权限、保留期限与删除。不要提交文档、日志中的敏感文字、真实 API key 或临时桥接令牌。
