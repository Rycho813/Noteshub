# V3 图片查看：升级与真机验收

当前进度：**第 2/5 大步骤，第 3/3 小步骤：开发与本地自动验证完成，待测试实例升级及真机验收**。功能分支 `codex/noteshub-v3-photo-viewer`；本步骤完成电脑、手机快速记录页的图片查看。图片查看经真机验收通过后，才进入第 3/5 步 Windows 本地 OCR 安装与验收。

## 查看方式

在 `/quick` 点击缩略图或“查看大图”，图片在当前页内打开。初始适应屏幕，可用“放大”“缩小”在 100%–400% 间调整（百分比以适应屏幕大小为基准）；放大后滑动或滚动查看。点击“关闭大图”，电脑也可按 Esc，返回原记录继续编辑。查看不修改原文、一读、二读、图片顺序或同步版本。

查看使用本浏览器已有的图片副本，离线可用；未下载的图片显示“联网后可查看”，已清理且无本机副本的图片显示“已确认清理”，不能打开。另一台设备首次查看时需联网打开该记录、等待缩略图出现，再做离线验收。浏览器中仍有本机副本时，即使服务器图片已清理，本机副本仍可查看；本步骤不清理本机存储。

## 独立测试实例升级（服务器 SSH）

目标是现有 `https://noteshub-test.rychoz.com`，沿用项目 `noteshub-v2-ocr-test`、数据卷 `noteshub-v2-ocr-test_data` 和服务器回环端口 **18080**。**18081 只用于 WSL 临时数据验证**。保留现有 Caddy/HTTPS、正式服务和 main。不要使用 `docker compose down -v`，不要创建账号或启用腾讯 OCR。

先在手机的快速记录页等到“已同步”；仍仅在本机的内容先“导出本机恢复文件”。服务器备份不包含手机尚未上传的数据。

将下面第一行的提交号替换成本次交付的完整提交号，再一次执行整个命令块。部署密钥沿用已有配置；新源码和发布目录使用唯一名称，不覆盖旧目录。任一步失败即停止；若应用已停止，保留备份和原卷，不要从旧目录启动已升级数据。

```bash
export NOTESHUB_EXPECTED_COMMIT='替换为本次交付的完整提交号'
(
set -euo pipefail
umask 077
[[ "$NOTESHUB_EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]]
task_stamp=$(date +%Y%m%d-%H%M%S)
source_dir="$HOME/noteshub-v3-photo-source-$task_stamp"
release_dir="$HOME/noteshub-v3-photo-test-$task_stamp"
test ! -e "$source_dir"
test ! -e "$release_dir"
git -c core.sshCommand="ssh -i $HOME/.ssh/noteshub_deploy_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes" \
  clone --single-branch --branch codex/noteshub-v3-photo-viewer \
  git@github.com:Rycho813/Noteshub.git "$source_dir"
cd "$source_dir"
test "$(git rev-parse HEAD)" = "$NOTESHUB_EXPECTED_COMMIT"
git log -1 --format='%H %s'
python3 scripts/package_release.py artifacts/v3-photo-test.tar.gz
mkdir "$release_dir"
tar --no-same-owner --no-same-permissions -xzf artifacts/v3-photo-test.tar.gz -C "$release_dir"
cd "$release_dir"
python3 scripts/check_release.py
sudo bash scripts/ocr_test.sh build
sudo bash scripts/ocr_test.sh summary
sudo bash scripts/ocr_test.sh stop
sudo bash scripts/ocr_test.sh backup
sudo bash scripts/ocr_test.sh up
sudo bash scripts/ocr_test.sh check
sudo bash scripts/ocr_test.sh summary
printf '\nRELEASE_DIR=%s\n' "$release_dir"
)
```

`backup` 须打印校验成功及 `BACKUP=...`，之后才允许 `up`。记录备份路径及 `RELEASE_DIR`。升级后应为 schema 6；核对前后书籍、草稿、正式笔记和 OCR 历史数量，以及既有 137 字原文/17 字一读（如已归档则在来源记录核对），并打开旧笔记核对文字、图片及 Markdown 导出。本次只有前端查看器变化，不新增数据库迁移。

升级不修改 HTTPS 配置。手机、电脑沿用测试站登录，**联网重新打开 `/quick` 并刷新一次**，等缩略图出现，再进行离线验收。Service Worker 缓存版本会更新，但不会删除 IndexedDB 中的文字和照片；不要清除网站数据或卸载 PWA。已离线的旧页面须先联网加载新版本。

## 电脑与 iPhone 验收（按顺序一次完成）

1. **电脑在线**：在测试站新建一条多图记录，填写一读、原文、二读，等待“已同步”。分别点两张缩略图，验证大图清晰、放大/缩小/适应屏幕、放大后滚动，关闭或 Esc 返回；文字、页序和同步状态不变。继续输入并刷新，确认最新内容保留。
2. **iPhone Safari / 主屏幕 PWA**：打开同一条记录，等待所有缩略图出现。逐张点击、放大、滑动查看，横竖屏切换后关闭按钮仍可点，关闭后仍在原记录且三段文字保留。添加图片、排序、移除仍正常。
3. **iPhone 离线**：先确保新版本已联网加载。飞行模式下打开本机已有图片、放大并关闭；继续编辑三段文字，确认“仅本地保存”。关闭再打开 PWA，确认文字、图片、页序及大图仍可查看。恢复网络并回到前台，等待“已同步”，在电脑刷新核对。
4. **回归与冲突**：电脑与手机分别编辑同一条记录，确认冲突保留本机文字，可“另存一条”；原有 Markdown 导入、二读编辑、整书导出和 V2 来源图片/历史可用。已有书籍无需重新导入。

验收反馈可合并为：`电脑大图：通过/问题；iPhone 大图：通过/问题；离线重开：通过/问题；文字与同步：通过/问题；Markdown：通过/问题`，有问题注明 Safari/PWA、操作及现象。**收到图片查看通过的反馈后，再继续第 3/5 步 Windows 本地 OCR**；本步骤不安装 Windows OCR、不识别真实书页。

## WSL 自动验证

使用 Python 3.12。开发依赖包含 Playwright；首次需安装浏览器及其系统依赖。下面仅创建临时合成数据，测试串行使用回环端口 18081，结束停止临时服务并清理测试数据；截图保留在被 Git 忽略的 `test-results/`。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium webkit
export NOTESHUB_BROWSER_PORT=18081
.venv/bin/python -m unittest discover -v
.venv/bin/python tests/browser_photo_viewer.py
.venv/bin/python tests/browser_photo_viewer.py --browser webkit
.venv/bin/python tests/browser_quick.py
.venv/bin/python tests/browser_archive.py
```

无系统 Chromium 时自动使用 Playwright 下载的 Chromium。Linux 若报告缺少系统库，再按 Playwright 输出安装相应依赖。WebKit 使用测试临时普通资料目录，避免无痕模式保存 Blob 的限制；截图由 Chromium 生成，两个引擎都验证交互与布局。手机尺寸模拟及 WebKit 自动化不能代替真实 iPhone Safari/PWA 验收。实际执行结果和本机临时运行库说明见 [V3 验证记录](v3-validation.md)。
