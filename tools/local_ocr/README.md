# Windows 本地 OCR

默认 Windows x64、Python 3.12、CPU。使用 RapidOCR 1.4.4 携带的 PaddleOCR ONNX 中文/英文模型，安装后识别在电脑进行；图片不发送到外部识别服务。这个目录的依赖不加入服务器 requirements，不需要电脑开放端口，也不需要电脑全天开机。

2026-11-05 用户已在 Windows Python 3.12.10（64 位）、CPU 环境完成真实书页识别、回传、候选采用、刷新保留、评论和整书导出验收。已安装的独立 worker 位于 `%LOCALAPPDATA%\NotesHub\ocr-worker`；日常运行命令见 [统一记录指南](../../docs/v3-unified-records.md#用户验收后的日常使用与版本边界)，无需重复安装或生成凭证。

## 首次配置

在 Windows 项目根目录的 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv-ocr
.\.venv-ocr\Scripts\python.exe -m pip install -r tools\local_ocr\requirements.txt
```

使用浏览器登录**独立测试实例**的 `/quick`，展开“电脑本地 OCR”，生成电脑专用凭证。凭证只显示本次，可复制选中的密码框；不保存到浏览器离线数据库，不要放入聊天、Git 或命令参数。它只能领取本地 OCR 任务、下载任务图片及回传结果，不能访问一般笔记 API。

将下方域名换成独立测试实例的 HTTPS 根地址：

```powershell
.\.venv-ocr\Scripts\python.exe tools\local_ocr\worker.py configure --url https://notes-test.example.com
```

网页凭证默认隐藏，可用“显示凭证/隐藏凭证”切换，或点击“复制凭证”；关闭面板会清空显示。只有新生成凭证的本次页面可取回原文，下面“有效”列表不是凭证原文。

交互粘贴凭证，输入不显示。终端报告“格式无效”时，先核对复制的是以 `nh_` 开头的完整凭证；不要把圆点、网页标签或命令当作凭证。Windows 终端粘贴不便时，可直接从剪贴板配置：

```powershell
Read-Host "现在去网页复制凭证，返回终端后只按回车" | Out-Null
.\.venv-ocr\Scripts\python.exe tools\local_ocr\worker.py configure --url https://noteshub-test.rychoz.com --clipboard
```

先把整块命令粘贴到终端，等待提示后再去网页复制凭证，回来只按回车；不要再次复制命令，以免覆盖剪贴板。该方式不回显凭证，保持格式检查、HTTPS 和已有配置不覆盖的规则，不改全局剪贴板内容。

默认配置位于 `%LOCALAPPDATA%\NotesHub\local-ocr.json`，请使用自己的 Windows 用户账户，保留该目录默认的私人访问权限；不要放入共享目录。已有配置不会被覆盖。轮换时可用 `--config` 指定另一个私人文件，或先在网页撤销旧凭证，再另存配置。

本机 SSH 隧道测试允许 `http://127.0.0.1:18080`；普通公网 HTTP 地址不接受。HTTPS 证书必须可信，不提供跳过校验选项，API 重定向也会拒绝，防止凭证被转发。

## 每次按需批处理

先在网页中对某条记录点“交给电脑识别”，或在“电脑本地 OCR”中点“为未识别图片批量排队”。这只创建任务，不会调用收费 OCR。一次批量排队最多 200 条，重复操作会跳过已有任务或当前图片已有结果的记录，后续批次继续处理其他记录。

在电脑执行：

```powershell
.\.venv-ocr\Scripts\python.exe tools\local_ocr\worker.py run --max-jobs 20
```

程序处理已排队任务后退出。多图按保存顺序逐页识别、验证下载摘要、回传图片编号与结果。任务有 30 分钟租约，运行时续期；程序中断后下次运行恢复领取，超时可重新领取。电脑关闭期间任务等待，不影响手机保存和同步。

回传前将结果写入私人 outbox，网络回执丢失时下次运行重发相同结果，不重新识别；过期租约的本机结果移到 `expired` 目录保留，任务可重新领取。单份配置同时只允许一个工作进程。请定期整理本机私人配置目录，里面可能包含原文结果。

回到网页刷新本地识别状态，查看逐图结果，明确点击“采用为原文，再人工校对”。已有原文会要求确认；一读与二读均保留。校对后先同步，再确认 30 天后清理图片。改动文字或图片会自动取消原确认。

## NVIDIA GPU

CPU 版本不要求显卡。`--device cuda` 入口会检查 CUDA provider，不提供自动安装驱动或切换运行库。显卡型号、显存、驱动/CUDA 版本确认后再单独配置 GPU 环境；本次只验证了 CPU，不声称已验证你的 Windows/NVIDIA 设备。

## 开发验证

在 Linux 开发环境已实际安装本目录依赖，使用合成多页图片完成模型推理和浏览器—服务器—本地 worker 的整个流程。Windows CPU 真机与真实书页流程已由用户验收通过；这不代表所有书页的准确率保证，GPU 性能尚未验收。
