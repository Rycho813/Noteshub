# V3 七点整合版：测试实例升级与验收

本指南对应第 **2/5 大步骤、小步骤 4/4** 的七点整合版交付；用户已确认测试站与真机验收通过，第 3 步 Windows CPU OCR 的识别、采用、刷新保留与评论/导出已由用户验收通过。2026-11-05 第 4 步测试实例备份、独立空卷恢复及内容/整书导出比较均通过，第 **5/5 步交付收尾** 的 Windows 异机备份也已通过 SHA-256 校验，用户侧本轮验收完成。分支 `codex/noteshub-v3-unified-records`；后续凭证显示/复制修复由用户按需升级测试站；已完成的第 2 步验收无需重做，旧草稿清理无需重复。不要把浏览器手机尺寸或 WebKit 验证记为真机通过。

## 本次行为

- 首页与书籍页均进入 `/quick`。书籍页进入时自动选中该书。移除书籍页的“图片摘录 · 上传与草稿”新增入口；旧归档图片来源继续可读。
- 下拉选书增加书名/作者筛选；筛选不移动记录。新书书名必填、作者选填，离线也可创建并自动选中。建书与换书保留当前输入和图片；换书移动当前快速记录，沿用同一编号并受版本保护。只有“新一条”创建另一记录，沿用当前书籍。
- 连续拍照、批量选图、电脑 Ctrl+V / Command+V 粘贴图片均追加到本条，最多 20 张；普通文字粘贴继续正常。支持图片排序和移除。
- 缩略图打开大图，支持缩放、适应屏幕、上一张/下一张与适应比例下横向滑动切图。裁剪支持拖动框选或坐标输入，旋转按 90°；取消不修改图片，应用后本机和后续上传均使用处理后的字节。已同步图片编辑时分配新编号，旧图片和 OCR 历史保留。
- 无“确认记录”门槛；文字与图片自动保存本机，网络恢复且页面打开或重开后自动同步。状态区分草稿已保存、同步中、已同步、同步失败；只有文字与全部图片获回执后显示已同步。失败保留内容，可重试及导出本机恢复文件。
- 书籍阅读按原顺序每页 30 条，上一页/下一页/指定页跳转。每条默认展开原文与一读，二读默认折叠；已有二读和处理状态保留，各条独立自动保存，折叠不销毁输入，翻页和整书导出前等待保存。失败阻止跳转并展开失败条目。整书导出始终包含全书全部条目。
- 快速记录的“识别与校对”和书籍页按需展开的“来源图片 · 识别与校对”操作同一条记录和同一组图片。书籍页仅展开后加载编辑面板，无须重新上传。候选需明确采用，再人工校对；不静默改写三段文字。电脑未运行时任务等待，不影响记录。未启用收费 OCR，也未安装或修改 Windows/GPU 环境。

数据库仍为 schema v6；不清空或重建数据。仅 OCR 嵌入模式 `/quick?view=ocr` 允许同源页面嵌入，其他页面继续禁止嵌入，脚本和样式 CSP 不变。Service Worker 分别缓存两种无私有数据的空壳，不缓存笔记/API/图片响应；IndexedDB 保留原格式和已有记录。

## 升级前准备

目标为现有独立测试站 `https://noteshub-test.rychoz.com`，沿用项目 `noteshub-v2-ocr-test`、卷 `noteshub-v2-ocr-test_data`、服务器回环端口 **18080** 及原 Caddy/HTTPS。**18081 仅用于 WSL 临时数据自动验证**。不修改正式站 `notes.rychoz.com`、main、代理、防火墙或 Windows 环境。

先在手机/电脑快速记录页等待“已同步”；未同步内容先导出本机恢复文件。服务器备份不含尚在浏览器的内容。不要清除网站数据或卸载 PWA。

将第一行填为本次最终交付的完整远端提交号，在服务器 SSH 中一次执行下列命令。沿用已有只读部署密钥，不重新创建账号或配置 OCR。新源码、发布目录均为唯一名称。任一步失败即停止；已经停止应用时保留原卷、备份和旧发布目录，不跳过失败继续启动。

```bash
export NOTESHUB_EXPECTED_COMMIT='替换为交付的40位远端提交号'
(
set -euo pipefail
umask 077
[[ "$NOTESHUB_EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]
task_stamp=$(date +%Y%m%d-%H%M%S)
source_dir="$HOME/noteshub-v3-unified-source-$task_stamp"
release_dir="$HOME/noteshub-v3-unified-test-$task_stamp"
test ! -e "$source_dir"
test ! -e "$release_dir"
git -c core.sshCommand="ssh -i $HOME/.ssh/noteshub_deploy_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes" \
  clone --single-branch --branch codex/noteshub-v3-unified-records \
  git@github.com:Rycho813/Noteshub.git "$source_dir"
cd "$source_dir"
test "$(git rev-parse HEAD)" = "$NOTESHUB_EXPECTED_COMMIT"
git log -1 --format='%H %s'
python3 scripts/package_release.py artifacts/v3-unified-test.tar.gz
mkdir "$release_dir"
tar --no-same-owner --no-same-permissions -xzf artifacts/v3-unified-test.tar.gz -C "$release_dir"
cd "$release_dir"
python3 scripts/check_release.py
sudo bash scripts/ocr_test.sh build
sudo bash scripts/ocr_test.sh summary
sudo bash scripts/ocr_test.sh stop
sudo bash scripts/ocr_test.sh backup
sudo bash scripts/ocr_test.sh retire-preview
sudo bash scripts/ocr_test.sh retire-apply
sudo bash scripts/ocr_test.sh up
sudo bash scripts/ocr_test.sh check
sudo bash scripts/ocr_test.sh summary
printf '\nRELEASE_DIR=%s\n' "$release_dir"
)
```

`backup` 必须校验成功并打印 `BACKUP=...`，再执行清理和启动。保存备份路径、摘要及 `RELEASE_DIR`。旧草稿清理只选 `image_drafts` 中**没有被正式 notes 引用**的行，删除这些草稿的附属 OCR 记录和对应图片；保留书籍、所有正式笔记（含快速记录和 Markdown）、成功 Markdown 源文件、已归档图片来源及其 OCR 历史。默认 `python -m noteshub.retire_drafts` 仅统计，`--apply` 才执行；测试 helper 要求应用已停止。重复执行不会扩大范围。

升级后 schema 应为 6；前后书籍和正式笔记数量应一致。草稿与旧 OCR 数量只允许按预览结果减少；此前已归档的 137 字原文/17 字一读与识别历史仍在，未归档的旧草稿按本次要求退出，不把其删除误认为新记录丢失。打开旧 Markdown、快速记录和已归档图片笔记，核对文字、来源和导出。

保留现有 HTTPS。电脑和 iPhone 联网重开 `/quick` 并刷新一次，让新 Service Worker 与脚本就绪；无需清除本机存储。离线的旧页面须先联网加载新版本。

## 一次完成的合并验收

1. **统一入口和选书**：首页与某本书进入快速记录，确认只有一个新增流程，书籍页进入自动选中。输入想法并加两张图，按作者筛选不移动记录；新建书、换书后内容仍在。“新一条”为空白且沿用当前书籍，原记录仍可打开。
2. **图片**：电脑批量选图、Ctrl+V/Command+V 截图与普通文字粘贴；iPhone 连续拍照。确认追加、顺序、移除和最多 20 张。打开大图缩放/适应/切图，拖动框选、旋转后先取消一次，再应用；关闭大图和重开后仍是处理后的图片。不要用显示缩放代替裁剪判断。
3. **离线和恢复**：新版本联网加载后切飞行模式，新建一本书并输入文字、加图、裁剪。彻底关闭再重开 Safari/PWA，确认书、三段文字、处理后图片与顺序仍在。恢复网络并打开页面，等待“已同步”，在电脑核对，刷新/重试不产生重复书或笔记。
4. **列表和导出**：在至少 31 条笔记的书中检查每页 30 条、页码跳转；二读和 OCR 默认折叠。展开两条二读修改，收起并立即翻页，再返回核对；已有处理状态保留。导出整书，确认包含所有页面与刚输入的内容。断网保存失败时翻页应停止，恢复网络重试后继续。
5. **OCR 两个位置**：快速记录可排队；也可先保存，再从书籍页展开该条“来源图片 · 识别与校对”，确认图片与文字相同且不要求重传。电脑不运行时显示等待，记录照常保存。Windows 实际启动识别、真实书页准确率留到第 3/5 步；本步骤不把模拟结果当成真实 OCR 验收。已有候选需明确采用、人工校对，一读和二读不被改写。
6. **冲突及旧数据**：电脑与手机编辑同一记录，确认旧版本不能覆盖新版本，失败时本机输入可另存。检查旧书籍、Markdown 全书导出、已归档图片及其识别历史；不要求保留已明确退出的旧草稿。

合并反馈可用：`选书/建书：…；图片/裁剪：…；离线重开/同步：…；30条分页/二读：…；OCR入口：…；旧数据/导出：…`。问题请注明电脑或 iPhone、Safari 或 PWA、操作和现象。真机通过后再安排第 3/5 步 Windows 本地 OCR。

## 回退与验证

应用启动或验收失败时，从本次 `RELEASE_DIR` 执行 `sudo bash scripts/ocr_test.sh stop`，保留现有卷和备份。不要用旧目录启动已升级卷，不执行 `down -v`。需要回退或恢复已清理旧草稿时，用升级前完整备份恢复到**新的空卷/新项目**，按 [部署与恢复](deployment.md) 操作；原卷保持不覆盖。

本地验证使用临时合成数据和回环端口 18081：

```bash
export NOTESHUB_BROWSER_PORT=18081
.venv/bin/python -m unittest discover -v
.venv/bin/python tests/browser_unified.py
.venv/bin/python tests/browser_unified.py --browser webkit
.venv/bin/python tests/browser_photo_viewer.py
.venv/bin/python tests/browser_photo_viewer.py --browser webkit
.venv/bin/python tests/browser_quick.py
.venv/bin/python tests/browser_archive.py
```

浏览器依赖与当前结果见 [V3 验证记录](v3-validation.md)。所有测试服务依次启动并在结束后停止，不复用真实用户数据。发布包排除凭证、账号、数据库、备份、私有样本和临时浏览器环境。

## 用户验收后的日常使用与版本边界

2026-11-05，用户已验收七点整合版、Windows CPU 真实书页 OCR、独立恢复及 Windows 异机备份。测试站仍使用七点整合版镜像 `noteshub:v2-ocr-test-3f788ace4fcd`（源码提交 `416651787a6ddf8f5abae95a88bff3c3d4033ce6`）；功能分支后续提交 `704117bc000e80fb4a6a06b020e0823aaafbfc8b` 增加网页凭证显示/复制及 worker 剪贴板配置。Windows worker 已由用户更新，未核实测试站网页已升级该修复；日常使用现有凭证无需重新配置或重新生成。

1. 手机或电脑使用测试站 `/quick` 记录、加图；联网时等到“已同步”。仍在浏览器中的离线内容需用本机恢复文件保护，不在服务器备份内。
2. 需要 OCR 时，在网页排队，再在自己的 Windows PowerShell 运行以下命令。电脑退出或关机时新任务等待；刷新网页只查询状态，不会启动 OCR。

   ```powershell
   & {
       $ErrorActionPreference = "Stop"
       Set-Location (Join-Path $env:LOCALAPPDATA "NotesHub\ocr-worker")
       .\.venv-ocr\Scripts\python.exe .\worker.py run --max-jobs 20 --device cpu
       if ($LASTEXITCODE -ne 0) { throw "本地 OCR 未完成，请反馈报错" }
   }
   ```

3. 回网页刷新识别状态，明确采用候选、人工校对并等待同步；一读、二读按需填写，书籍页可导出整书 Markdown。
4. 本轮测试实例全量备份已由用户下载至 `%LOCALAPPDATA%\NotesHub\backups` 并校验，仅覆盖备份时已同步的数据。继续编辑后的内容需重新备份；恢复使用新空卷，现有恢复验证卷保留。

历史 [备份运维说明](backup-operations.md) 的 systemd 任务针对正式项目 `noteshub`，不代表本轮测试实例已设置自动备份，不直接套用到测试实例。当前交付不更新 main、不改正式站、HTTPS 或 Windows 环境；正式部署、测试站定时备份与 GPU 如需另行实施，应单独明确范围。
