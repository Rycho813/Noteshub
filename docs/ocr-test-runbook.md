# V2 第 3 步：腾讯服务器独立 OCR 联调

本手册准备独立测试应用，不更新正式站点。当前 V2 改动在开发工作区，GitHub main 仍为 `401423b7234598d6ceb0f9119046d92a0a383d96`，直接 git pull 不能取得未提交的功能。本轮使用源码部署包及校验文件，经 SCP 传输，不需要推送或给服务器新增 GitHub 凭证。

代理开发环境不会连接你的服务器，不读取你的 SecretId/SecretKey，也不会选择图片发起收费调用。需要你在自己的 SSH 终端执行服务器命令、在电脑浏览器指定一张图片。每组操作成功后再继续；任一命令失败先停止，不删除或重置已有正式资源。

## 固定隔离边界

| 项目 | 本轮测试配置 |
| --- | --- |
| 源码目录 | `~/noteshub-v2-ocr-test-20261102`，新建目录，不覆盖 `/opt/noteshub` |
| Compose 项目 | `noteshub-v2-ocr-test` |
| 数据卷 | `noteshub-v2-ocr-test_data`，标签 `org.noteshub.test=ocr-v2` |
| 镜像 | `noteshub:v2-ocr-test-<发布清单摘要前12位>` |
| 端口 | `127.0.0.1:18080`，通过电脑 SSH 隧道访问 |
| 凭证文件 | `/etc/noteshub-v2-ocr-test/tencent-ocr.json`，独立于源码与业务备份 |
| 资源 | 应用内存 512 MiB、CPU 0.5 核、128 PID，日志最多约 10 MiB |
| OCR | 默认关闭，只有明确启用后点击识别才提交；本轮只点击一次 |

不启动 Caddy，不变更域名/TLS，不开放新公网端口，不操作 FreshRSS、RSSHub、Redis 或正式笔记数据。测试使用回环 HTTP，因此 Cookie secure=false；SSH 隧道保护电脑与服务器之间的传输，不能把测试端口改绑公网。

以下 host 脚本使用系统 python3，应用自身的 Python 3.12 和依赖在 Docker 镜像中。构建只进行一项依赖安装，不并行编译大型组件；需预留镜像/构建缓存空间，先检查实际余量。

## 1. 服务器只读预检

使用你现有的方法登录腾讯服务器，逐条执行：

```bash
uname -m
python3 --version
df -h /
free -m
sudo docker compose version
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
sudo ss -H -ltn 'sport = :18080'
```

这些命令不读取凭证或笔记。18080 应无监听；若有输出先停止检查，不停止占用它的服务。当前云开发容器验证架构是 amd64；源码包将在服务器本机构建，其他架构仍须在本机验证。

可以把这组输出发给开发者，不要发 env、密钥 JSON、完整 docker inspect 或 Compose 环境内容。

## 2. 下载与 SCP 传输

下载开发者提供的两个文件到电脑同一目录，保留文件名：

- `noteshub-v2-ocr-test-20261102.tar.gz`
- `noteshub-v2-ocr-test-20261102.tar.gz.sha256`

Windows PowerShell 示例，把登录地址替换成你平时使用的地址；如果你使用 SSH 别名/密钥选择，继续采用自己的既有登录方式：

```powershell
$notesServer = "ubuntu@你的服务器公网IP"
Set-Location "$HOME\Downloads"
ssh $notesServer 'umask 077; mkdir noteshub-v2-ocr-incoming-20261102'
if ($LASTEXITCODE -ne 0) { throw "接收目录创建失败，先停止，不覆盖已有文件。" }
scp .\noteshub-v2-ocr-test-20261102.tar.gz .\noteshub-v2-ocr-test-20261102.tar.gz.sha256 "${notesServer}:~/noteshub-v2-ocr-incoming-20261102/"
if ($LASTEXITCODE -ne 0) { throw "传输未完成，先停止。" }
```

如果新目录已经存在，不改成自动覆盖；先检查是否来自之前的本轮操作，再决定使用新的接收目录。macOS/Linux 可使用同样的 ssh/scp 命令，替换本地文件路径。

## 3. 校验并解压到新目录

回到服务器 SSH 终端。下面的子 shell 在失败时停止，不会关闭整个 SSH 会话：

```bash
(
set -e
umask 077
cd "$HOME/noteshub-v2-ocr-incoming-20261102"
sha256sum -c noteshub-v2-ocr-test-20261102.tar.gz.sha256
mkdir "$HOME/noteshub-v2-ocr-test-20261102"
tar --no-same-owner --no-same-permissions -xzf noteshub-v2-ocr-test-20261102.tar.gz -C "$HOME/noteshub-v2-ocr-test-20261102"
cd "$HOME/noteshub-v2-ocr-test-20261102"
python3 scripts/check_release.py
sudo bash scripts/ocr_test.sh preflight
)
```

预期外部 SHA 校验和内部发布清单都通过，preflight 显示独立项目/端口/镜像。清单脚本拒绝变更、符号链接和额外加入应用目录的未记录文件。以后 helper 每次操作都会重新验证清单；不要手改发布包代码。

## 4. CAM 权限与私密凭证

你已创建 SecretId/SecretKey。请在腾讯 CAM 核对它们属于预期子用户，并在可视化策略中只授予 OCR `GeneralAccurateOCR` 所需权限，确认 OCR 服务开通、账户可用及计费规则。不要授予不相关的管理权限，不要把值发到聊天或截图。

在服务器交互式 SSH 终端执行：

```bash
cd "$HOME/noteshub-v2-ocr-test-20261102"
sudo python3 scripts/configure_ocr_credentials.py
```

分别粘贴 SecretId 和 SecretKey，输入不会回显。脚本使用独占创建，已有文件时拒绝覆盖；只设置私密文件与 UID 10001 可读权限，完全不调用腾讯。不要把命令通过会话日志中的明文 echo 或命令参数替代。

可以检查**权限元数据**，不要 cat 文件：

```bash
sudo stat -c '%a %u:%g %n' /etc/noteshub-v2-ocr-test/tencent-ocr.json
```

预期 `400 10001:10001`。该文件在仓库和数据卷之外，不包含在部署包/业务备份中。需要修正已存在文件时先告知，仅用你自己的安全编辑器处理，不把值交给开发者。

## 5. 构建、创建账号并启动（OCR 仍关闭）

```bash
cd "$HOME/noteshub-v2-ocr-test-20261102"
sudo bash scripts/ocr_test.sh build
sudo bash scripts/ocr_test.sh account
sudo bash scripts/ocr_test.sh up
sudo bash scripts/ocr_test.sh check
sudo bash scripts/ocr_test.sh network
```

- build 使用专属清单摘要标签，不覆盖正式镜像标签；同一已验证镜像存在时复用。没有在服务器构建前就声称服务器成功。
- account 创建你自行选择的独立测试账号，密码至少 12 字符、隐藏输入，不覆盖已有账号。
- up 只启动测试 app，OCR 默认关闭。check 验证私密配置可读和登录页面功能，不提交 OCR。输出 `OCR enabled/configured: False` 是此时的正常结果。
- network 仅做**无图片、无凭证、无 OCR Action** 的 HTTPS 请求，保持证书验证，用来检查连接；不验证 CAM 权限，也不产生文字识别任务。HTTP 403/异常状态或 TLS 失败要先诊断，不能算准备完成。

网络需要 HTTPS 访问 `ocr.tencentcloudapi.com:443`；构建另需允许 Docker Hub、PyPI 和其文件下载域名。保持服务器既有代理/CA 配置，不关闭 TLS、不使用不可信镜像源。网络测试失败时不要反复点击 OCR。

## 6. 电脑通过 SSH 隧道访问

电脑上另开 PowerShell 终端，使用同一登录地址：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 18080:127.0.0.1:18080 $notesServer
```

保持这个终端打开，在电脑浏览器输入 `http://127.0.0.1:18080`，登录测试账号。若本地 18080 已占用，只改 SSH 左侧为 18081，并在电脑浏览器用 18081；服务器端仍是 18080，不改正式服务。

本轮先在电脑完成真实联调。手机不能直接访问电脑的 localhost，移动端的安全访问方式后续另行安排，不为这次测试暴露公网端口。

## 7. 指定一张图片，最多一次真实识别

先在测试应用建一本测试书，上传你指定的一张**无敏感内容的中文图片**，裁剪、确认范围并保存。进入“识别与一读”，暂时保持原文/一读为空，避免把手写内容当作未编辑的初始状态。

然后在服务器明确启用测试 OCR：

```bash
sudo bash scripts/ocr_test.sh enable-ocr
sudo bash scripts/ocr_test.sh check
```

这两条只启用与检查，不识别图片。回浏览器刷新编辑页，点击一次“识别文字”，等待完成。**不要再点击重试或重新识别，包括失败/超时场景。** 现有后端防重复点击且 SDK 无自动重试，但明确的再次点击仍可能计费，本轮不授权额外尝试。

查看安全状态汇总：

```bash
sudo bash scripts/ocr_test.sh summary
```

预期 `ocr_runs 1`，可见 succeeded/failed 状态；只输出计数和文字长度，不输出密钥或正文。计数不等于腾讯最终账单；实际用量以控制台为准。首次失败时保留状态，回传安全错误分类后先排查，不自动进行第二次调用。

识别有结果后先关闭测试 OCR，防止后续误操作：

```bash
sudo bash scripts/ocr_test.sh disable-ocr
```

禁用会重新创建本测试容器，保留同一测试数据卷；不会删除草稿。识别任务尚未结束时应先等待/核对状态，避免把“未确认完成”当作未计费。

## 8. 验证校对和一读保存（不再调用 OCR）

1. 刷新测试编辑页，确认已保存的识别原文和来源图片仍在；OCR 按钮因禁用而不可用。
2. 校对一处原文，写一段一读，等“已保存”。若首次识别时已有手写内容，使用已保存候选的“采用”按钮，不需要再次识别。
3. 刷新后核对，退出并重新登录后再核对。
4. 再运行 summary，确认 OCR 仍为一次、编辑字段长度符合预期，正式 `notes` 数量仍为 0。
5. 可执行 stop、up 后再核对，验证同卷重启恢复；up 保持 OCR 禁用，不会重放任务。

可以回传：预检、check/network、summary 的输出以及无敏感文字的界面问题。不要回传密钥文件、env、完整 docker inspect、完整 Compose 配置或带凭证的调试日志。

## 9. 停止与清理

优先保留数据停用，便于排查：

```bash
sudo bash scripts/ocr_test.sh stop
```

删除本测试容器/网络、保留测试卷和私密文件：

```bash
sudo bash scripts/ocr_test.sh down
```

helper 只管理固定标记的测试命名空间，不使用全局 prune，也不删除任何数据卷。关闭电脑 SSH 隧道终端即可结束转发。

仅当你明确决定丢弃测试书、图片和一读时，先确认已 down，再核对卷标签，最后手动删除**这个测试卷**：

```bash
sudo docker volume inspect noteshub-v2-ocr-test_data --format '{{index .Labels "org.noteshub.test"}}'
# 确认为 ocr-v2，且不需要保留测试数据后才执行：
sudo docker volume rm noteshub-v2-ocr-test_data
```

不删除正式 `noteshub_data`，不运行 docker system/volume prune。私密文件和专用 CAM key 独立保留；需要彻底清理时由你在服务器和腾讯 CAM 单独删除/停用，不把密钥输出给开发者。
