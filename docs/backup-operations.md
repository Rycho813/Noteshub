# 备份、恢复与每日定时任务

本说明仅针对 `/opt/noteshub` 的 Compose 项目 `noteshub`。不重启 Caddy、FreshRSS、RSSHub、Redis 或其他项目，不覆盖正式卷 `noteshub_data`。

## 已由用户服务器输出核实

- Windows 与 iPhone 均可访问 `https://notes.rychoz.com`；用户确认电子书核心功能验收通过。
- 首次备份 `noteshub-first-20261029-163820.tar.gz` 约 142 KiB，服务器 SHA-256 检查为 OK。
- Windows 已下载备份及 `.sha256`，PowerShell 比对通过。加密保管仍由用户落实；没有配置自动异机传输。
- 恢复到独立卷 `noteshub-restore-check_data`，正式应用仍为 healthy。
- 对照首次备份逐字段核对通过：2 本书、2 次导入、294 条笔记；账号与源文件摘要一致，各书 Markdown 导出一致，恢复后的旧会话与临时预览已清除。

上述只验证了首次备份快照，不包括其后的编辑。恢复验证卷继续保留，不自动清理，也不接入正式域名。

## 每日自动备份方案

使用 Ubuntu 已有的 systemd timer，每天 **北京时间 04:00** 执行；与服务器当前时区无关。`Persistent=true` 使关机期间漏掉的日程在定时器恢复运行后补执行一次，不代表能补回当时未备份的数据。

任务复用已验证的 `scripts/backup.sh`，固定 Compose 项目为 `noteshub`。只在备份期间短暂停止 app，完成后恢复此前运行状态。每次生成带北京时间时间戳的 `.tar.gz` 和 `.sha256`，存放在 `/var/backups/noteshub/daily/`，文件默认仅 root 可读。

先保留所有备份，不设置自动删除。注意检查磁盘余量与备份大小增长；首次备份大小不能代表日后大小。首次备份和每日目录分开存放。

开发环境已通过 systemd 单元语法、Asia/Shanghai 日程解析、文件名时间戳展开及备份失败退出码传递检查；单元语法验证使用临时 Docker 依赖占位单元，没有启动服务。腾讯服务器上的实际执行与 timer 启用仍须按下面步骤核实。

## 安装与验证

先从 GitHub 拉取经审核的部署文件。若这些 systemd 单元名已经存在，先核实来源，不覆盖其他人配置的任务。逐步成功后再继续：

```bash
cd /opt/noteshub
git -c core.sshCommand="ssh -i $HOME/.ssh/noteshub_deploy_ed25519 -o IdentitiesOnly=yes" pull --ff-only origin main
sudo install -d -m 700 /var/backups/noteshub/daily
sudo install -m 644 deploy/noteshub-backup.service deploy/noteshub-backup.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/noteshub-backup.service /etc/systemd/system/noteshub-backup.timer
sudo systemctl daemon-reload
sudo systemctl start noteshub-backup.service
sudo systemctl show noteshub-backup.service -p Result -p ExecMainStatus
sudo journalctl -u noteshub-backup.service -n 30 --no-pager
sudo docker compose -p noteshub ps
```

手动启动会实际再备份一次，笔记平台短暂不可用。预期 `Result=success`、`ExecMainStatus=0`，日志出现备份完成，应用稍后为 healthy。oneshot 服务成功后显示 inactive 是正常状态，不用设置 `RemainAfterExit`。

确认上述实际运行成功后，启用每日任务：

```bash
sudo systemctl enable --now noteshub-backup.timer
sudo systemctl list-timers --all noteshub-backup.timer
```

列表中的 NEXT 可能按服务器本地时区显示；日程指定的是 Asia/Shanghai 04:00。例如服务器若是 UTC，对应前一日 20:00 UTC。首次启用可能因补执行机制触发一次；每次服务运行均使用独立文件名。

只拉取源码并安装定时配置，不需要重建应用镜像，不会部署新的产品功能。

## 日常检查、异机副本与停用

至少定期检查任务日志、最新备份和磁盘余量：

```bash
sudo systemctl list-timers --all noteshub-backup.timer
sudo systemctl show noteshub-backup.service -p Result -p ExecMainStatus
sudo journalctl -u noteshub-backup.service -n 30 --no-pager
sudo ls -lht /var/backups/noteshub/daily/
df -h /var/backups/noteshub
```

首次核实新增备份时，也运行 `sudo sha256sum -c /var/backups/noteshub/daily/实际文件名.tar.gz.sha256`。SHA-256 一致不替代恢复验证；建议重要变更后及定期再做独立卷恢复检查。

每日任务只保存到当前服务器，不具备自动异机同步或自动失败通知。继续定期将近期备份及 `.sha256` 复制到 ubuntu 私有传输目录，下载到 Windows，核对 SHA-256 并加密保管；备份文件和密码不要提交 GitHub、发到聊天或放在网站目录。

临时停用每日计划：

```bash
sudo systemctl disable --now noteshub-backup.timer
```

这不会删除已有备份，也不会中断已经开始的备份服务；若服务正在执行，应等其完成。恢复和回退操作仍按 [部署说明](deployment.md) 使用新项目/新卷，不运行正式项目的 `docker compose down -v`。
