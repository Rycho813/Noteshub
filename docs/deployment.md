# 部署准备与验收

当前进度更新：用户已确认正式站点、Windows/iPhone 与核心功能验收通过；首次备份、Windows 副本校验、独立卷恢复及内容核对通过。每日备份安装、启用与维护见 [备份运维说明](backup-operations.md)。下文部署前检查和准备内容保留为历史记录。

本文件是已获用户授权的部署方案，**不是已经执行了服务器部署**。目标为 `notes.rychoz.com`，复用现有宿主机 Caddy。服务器为腾讯云轻量应用服务器、新加坡、Ubuntu 24.04 LTS、x86_64、2 核/2GB/40GB SSD；检查时可用内存约 771MiB、磁盘可用约 25GB，Docker Compose 为 v2.32.4。

既有服务为 FreshRSS（`rss.rychoz.com`）、RSSHub 和 Redis。Caddy 通过 systemd 使用 `/etc/caddy/Caddyfile`，监听 80/443；8000 未监听。`/opt/noteshub` 不存在，现有 Compose 项目为 `freshrss`、`rsshub`，可使用独立项目 `noteshub`。用户已确认下述部署范围；实际步骤仍由服务器终端执行，目前尚未核实线上启动或 DNS 变更。

## 环境检查记录（已完成，无需重复）

在腾讯云控制台进入实例，使用“登录/远程登录”终端。运行：

```bash
cat /etc/os-release
uname -m
free -h
df -h /
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker compose version
sudo ss -ltnp | awk 'NR==1 || $4 ~ /:(80|443|8000)$/'
systemctl is-active caddy
```

只需提供输出，公网 IP 可遮盖；不要发送密码、私钥、云 API 密钥、完整 `docker inspect` 或环境变量。命令不存在、无权限或状态为 inactive 也属于有用信息，先不要安装或重启服务。

如果 Caddy 在容器内，`systemctl` 为 inactive 并不代表没有 Caddy，应同时看容器列表。明确容器名后再单独查看它的挂载路径，避免输出容器环境中的凭据。需要确认：FreshRSS 通过什么地址访问，域名由谁管理，Caddy 是宿主机服务还是容器，以及配置文件位置。

## 推荐方案：复用现有 Caddy

2GB 内存可优先尝试当前单进程电子书应用，实际余量以检查结果为准；本次不运行 OCR。

```text
Windows / iPhone → HTTPS 笔记子域名 → 已有 Caddy → Noteshub
                                            → 既有 FreshRSS（保持原站点配置）
```

- 有可管理的域名：优先新增 `notes.你的域名`，不必另买域名。
- 暂无任何域名：先选定域名方案，再配置 DNS 和 HTTPS；不把纯 HTTP 或自签证书作为最终手机验收入口。
- 新加坡服务器不属于中国大陆服务器场景；仍需按域名和服务实际情况完成 DNS、证书与网络访问验证，不凭地域承诺一定可达。
- 不启动第二套 Caddy 抢占 80/443，不替换 FreshRSS 的 Caddyfile，也不停止、升级或迁移 FreshRSS。
- 如 Caddy 运行在宿主机，可转发至 `127.0.0.1:8000`（若已占用则另选端口）。如 Caddy 是容器，必须先确认现有 Docker 网络，再确定容器间转发方式，不能把代理容器内的 localhost 当作宿主机。

## 文件与运行方式

| 文件 | 用途 |
|---|---|
| `Dockerfile` | Python 3.12 单应用镜像，非 root 用户 10001，只读根文件系统 |
| `docker-compose.yml` | 应用、私有持久卷、仅回环地址的 8000 端口映射 |
| `docker-compose.https.yml` | **仅无现有入口且 80/443 空闲时**启用独立 Caddy |
| `deploy/Caddyfile` | 独立 Caddy 的域名及反向代理模板 |
| `.env.example` | 非敏感配置；无默认密码 |
| `scripts/backup.sh` / `scripts/restore.sh` | 短暂停止本应用的整卷备份、空卷恢复 |
| `scripts/package_release.py` | 生成不含私人笔记、测试样本、账号、`.env` 或 `.git` 的部署包 |

镜像基础和 Caddy 均锁定已拉取镜像的摘要，Python 依赖使用现有锁定版本。后续更新需重新验证并显式更新摘要，不自动升级线上服务。

当前采用 GitHub 中转源码，不要求用户先下载部署包到 Windows。按下节完成推送并核实远端提交后，再由服务器拉取。归档方式保留为备用，可在开发环境生成部署包：

```bash
python3 scripts/package_release.py
```

包内 `RELEASE.json` 提供逐文件 SHA-256。部署包只含代码和部署说明，不含实际账号或数据库；现有原生 Python 用户数据不会自动迁入 Docker，若存在数据须另行备份和确认迁移。

## GitHub 中转与首次拉取

仓库为 `Rycho813/Noteshub`。不改变仓库的可见性；个人使用推荐 Private。部署代码不含账号、密钥、数据库、笔记源文件、备份、截图、压缩包或虚拟环境。`tests/fixtures/` 仅保留在原开发工作区，真实样本回归用摘要断言，不在测试和说明中公开笔记正文。

先由开发工作区完成审核、提交和推送，并核实 GitHub 的 `main` 分支能看到 `Dockerfile`、`docker-compose.yml`、`noteshub/`。在此之前不要在服务器空仓库上启动应用。

若仓库为 Private，在服务器现有 `ubuntu` 账号下创建本项目独用的密钥。以下命令不修改 SSH 服务、SSH 端口或已有登录密钥；若同名密钥已存在，`ssh-keygen` 会询问，选择不覆盖并先核实：

```bash
install -d -m 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/noteshub_deploy_ed25519" -C "noteshub-readonly-deploy" -N ''
cat "$HOME/.ssh/noteshub_deploy_ed25519.pub"
```

将输出的**公钥**添加到 GitHub → 仓库 Settings → Deploy keys → Add deploy key，不勾选 **Allow write access**。私钥留在服务器，不上传、不发到聊天，不使用 GitHub 密码或在 URL 中嵌入 token。首次连接 GitHub 的主机指纹要与 GitHub 官方 SSH key fingerprints 文档核对，不关闭主机验证或盲目接受；连不上时先诊断，不改服务器 SSH 配置。

已确认 `/opt/noteshub` 原先不存在，创建专用目录并拉取；如果现在已存在，则先查看，不覆盖原目录。先确认 Git 已安装，再使用下列命令。Git 未安装时先报告，另行提供只安装 Git 的步骤，不执行系统整体升级。

```bash
git --version
sudo mkdir /opt/noteshub
sudo chown ubuntu:ubuntu /opt/noteshub
git -c core.sshCommand="ssh -i $HOME/.ssh/noteshub_deploy_ed25519 -o IdentitiesOnly=yes" clone --branch main --single-branch git@github.com:Rycho813/Noteshub.git /opt/noteshub
cd /opt/noteshub
git config core.sshCommand "ssh -i $HOME/.ssh/noteshub_deploy_ed25519 -o IdentitiesOnly=yes"
git rev-parse HEAD
git status --short
```

核对提交号与已发布版本一致。公有仓库也可用 `https://github.com/Rycho813/Noteshub.git` 只读克隆，不需要账号凭据；不要为方便拉取而把 Private 仓库改为 Public。新建目录失败或克隆失败时停止后续命令，不在错误目录继续构建。

源代码拉取后才复制非敏感环境模板、构建镜像和创建平台账号。账号与 GitHub 访问凭据互相独立：

```bash
cd /opt/noteshub
test ! -e .env && cp .env.example .env
sudo docker compose config --quiet
sudo docker compose build app
sudo docker compose run --rm --no-deps app python -m noteshub.auth
sudo docker compose up -d app
sudo docker compose ps
curl --fail --silent --show-error -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login
```

逐步确认命令成功后再执行下一条；账号初始化交互输入用户名和至少 12 字符密码，不把密码放进命令行或聊天。预期本机 `/login` 返回 200。保持 Secure Cookie，不在未配置 HTTPS 前通过公网 HTTP 登录。使用基础 Compose 即可，不启用独立 HTTPS Compose。

之后为 `notes.rychoz.com` 配置正确 DNS，再备份、校验并平滑重载现有 Caddy；仅增加下述站点。现有 FreshRSS 配置、容器和数据卷保持原样。再次更新源码时先核对版本、备份本应用并使用 `git pull --ff-only`；拉取代码不会自动重建或部署镜像，不使用强制重置丢弃线上改动。

## 已授权的服务器操作

使用独立目录 `/opt/noteshub`、Compose 项目 `noteshub`、数据卷 `noteshub_data`，应用仅绑定 `127.0.0.1:8000`。用户已确认以下范围，无需为这些步骤重复请求授权；任何超出范围的操作另行说明：

1. 将审阅过的代码推送到 GitHub，服务器拉取至新目录（部署包上传解压作为备用），复制 `.env.example` 为 `.env`；使用独立项目名与回环端口，域名由宿主机 Caddy 配置。
2. 使用现有 Docker 构建镜像，创建专用数据卷；如果 Docker/Compose 版本不适用，先说明升级影响再请求决定，不自动升级现有基础设施。
3. 用户在终端交互创建平台唯一账号，密码不进入聊天或命令行参数。
4. 启动 Noteshub，只验证本机响应，8000 不开放公网。
5. 先备份现有 Caddy 配置，再**只增加新的笔记域名站点**，验证配置后平滑重载。具体命令取决于 Caddy 实际运行方式，确认后提供，不假定服务名或路径。
6. DNS 新增子域名解析到服务器；若有无效 AAAA 记录先核实，避免证书或手机优先走错误 IPv6。安全组/系统防火墙仅检查 80/443；如需变更先确认，不放行 8000 或改 SSH。
7. 验证 FreshRSS 原网址仍可用，再用 HTTPS 登录笔记平台，交由用户验收。

以上不包括停止 FreshRSS、改它的数据卷、重装服务器、替换整个反向代理配置、开放数据库或上传用户密钥。

## 审阅用启动命令

拉取源码并核对提交号（或上传并校验部署包）后，从 `/opt/noteshub` 执行。要求 Docker Compose v2；当前服务器已满足：

```bash
docker compose build app
docker compose run --rm --no-deps app python -m noteshub.auth
docker compose up -d app
docker compose ps
```

首次账号初始化只运行一次，已有账号不覆盖。数据位于 Compose 命名卷 `/data`，独立于镜像及容器生命周期。应用 rootfs 只读，上传临时空间为内存 tmpfs；没有必要挂载 Docker socket 进入应用。

复用已有 Caddy 时只运行上面的基础 Compose；在现有入口中追加的新站点示意：

```caddyfile
notes.rychoz.com {
    reverse_proxy 127.0.0.1:8000
}
```

这只适用于宿主机 Caddy，不可直接复制进代理容器后假定可达。应用静态资源使用相对路径，避免 HTTPS 代理后出现 HTTP 混合内容；不信任任意客户端转发头。

若经确认没有现成入口、80/443 空闲，才使用可选文件：

```bash
docker compose -f docker-compose.yml -f docker-compose.https.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
```

`.env` 中 `NOTESHUB_DOMAIN` 必须填写实际主机名，不带协议或路径。Caddy 首次签发公开证书需要 DNS 正确、相关端口可达和证书服务可访问；当前本地测试不代表公网签发已完成。

## 备份和恢复

### 备份

确认部署后，从同一目录执行：

```bash
bash scripts/backup.sh /安全备份目录/noteshub-唯一日期.tar.gz
```

脚本仅短暂停止当前 Compose 项目的 app，备份后恢复原运行状态，不重启 Caddy 或 FreshRSS；期间笔记网站可能短暂不可用。生成压缩包和 `.sha256`，已有同名文件不会覆盖。

备份包含账号密码哈希、SQLite、成功导入源文件，并检查数据库、外键和源文件哈希。它也可能包含备份时尚未到期的临时预览；请将备份视为私人数据，权限限制为本人可读，保留一份加密的异机副本，不上传公网目录或提交 Git。清理策略可先保留最近 7 个每日备份，不自动删除任何现有备份。

独立 Caddy 的证书状态另存在 `caddy_data`、`caddy_config` 卷；应用备份不含其私钥。域名和公网条件正常时可重新签发；如需备份现有共享 Caddy，必须按它自己的维护方案执行，不能擅自停止它。

### 恢复：用新项目/空卷，不覆盖旧数据

恢复前核对压缩包 SHA-256，保留旧项目和旧卷。从同一部署目录，用新项目名、另一未占用回环端口执行：

```bash
COMPOSE_PROJECT_NAME=noteshub-restore NOTESHUB_HTTP_PORT=18000 bash scripts/restore.sh /安全备份目录/noteshub-唯一日期.tar.gz
COMPOSE_PROJECT_NAME=noteshub-restore NOTESHUB_HTTP_PORT=18000 docker compose up -d app
```

恢复只允许空目录，拒绝路径穿越、符号链接、重复条目、清单不一致或内容哈希错误。默认解压上限为 2 GiB，可通过 `--max-bytes` 明确提高。恢复会取消旧会话和临时预览，保留书籍、正式笔记、二读状态、源文件及账号；需要重新登录。

验证恢复项目中的书籍、二读文字、导出文件和源文件后，才讨论切换代理到恢复实例；不直接把它切换成线上服务。**禁止使用 `docker compose down -v` 清理正式项目**，因为这会删除数据卷。

## 回退与验收

- 回退发布：只回退本应用镜像/代码；本阶段 schema 没有变化，仍为 v1。不要借回退删除数据卷。
- 回退入口：只撤销新增笔记站点配置并验证后重载，保留 FreshRSS 原站点。DNS 与安全组变更也需记录原值。
- 验收：FreshRSS 仍可用；未登录不能读取笔记或下载；Windows/iPhone 通过 HTTPS 登录；上传、修正、二读保存、刷新、状态、整书导出正常；Obsidian 实际打开；容器重启不丢数据；新卷恢复能读出原内容。

尚未核实用户可访问的网址。当前按已授权方案推进；不要启动独立 Caddy、修改 FreshRSS/RSSHub/Redis、公开 8000 或改动 SSH。若发现端口/目录冲突、Docker 构建失败或 Caddy 验证失败，先诊断具体问题，不擅自扩大范围。
