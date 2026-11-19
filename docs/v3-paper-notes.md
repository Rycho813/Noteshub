# NotesHub V3：纸质书快速记录

当前为第 **2/5 大步骤、小步骤 4/4：七点整合版验证与交付**，分支 `codex/noteshub-v3-unified-records`。本次请使用 [统一记录升级与验收](v3-unified-records.md)；下文保留旧版方案与历史命令。真实 iPhone、测试站升级与 Windows OCR 验收尚待执行。

## 增量方案

保留 FastAPI、SQLite、单用户会话、现有书籍与 notes 表、Markdown 解析器及导出器。V2 已归档图片和 OCR 历史保持不变。纸质书记录使用 notes 的新来源 `paper`，一读、原文、二读沿用现有字段；多图、同步版本和本地识别任务使用附属表。每书一次 Zotero Markdown 导入规则不变，纸质书记录可持续追加，导出继续使用同一格式。

分三个阶段验证：P0 手机 PWA 和同步；P1 电脑本地 OCR；P1 校对确认、可恢复清理及全量回归。开发和自动测试只使用独立合成数据，不更新 main 或正式服务，不调用收费 OCR。

## P0 快速记录

从首页或书籍页进入 `/quick`。先选择书籍、写想法，使用 iPhone 系统键盘的语音输入即可；原文和图片都不是保存前提。可连续拍照或批量选图，每条最多 20 张，逐张缩放至最长边 2400 像素并转成 JPEG（去除 EXIF）；支持前移、后移、移除。书籍和最近编辑记录在本机记忆。

首次联网登录并打开后，Safari 可添加到主屏幕。PWA 需要 HTTPS；电脑 localhost 可以测试，手机访问普通 HTTP IP 不能作为离线验收。Service Worker 仅缓存无私有数据的空记录页和固定静态资源，不缓存 API、登录页、笔记页或图片响应。离线数据单独存于 IndexedDB，按资料库区分。

每次输入、每张照片均先提交 IndexedDB 再显示本机已保存。页面显示「仅本地保存 / 同步中 / 已同步 / 同步失败」。照片逐张上传，最后提交有序图片清单；全部服务端确认后才显示已同步。窗口打开、网络恢复、回到前台时继续同步，不依赖 iOS 不保证支持的后台同步；关闭应用后在下次打开继续。

固定记录 ID、不可变图片 ID/摘要、同步操作 ID/回执保证网络重试不重复。SQLite 事务及版本比较拒绝旧设备覆盖；冲突时保留本机输入，展示云端文字，用户可另存一条，两份均保留。另一标签页的本机修改也以版本检查保护，冲突输入写入恢复副本。保存失败不会把状态写为已同步。

浏览器存储可能被用户清理，或被系统回收。请使用自己的设备、避免无痕模式，及时同步；页面可申请持久存储，但不能保证系统授予。内置本机恢复文件导出/导入（含照片、未同步内容和恢复副本）；导入另存为新记录，不覆盖现有内容。完整服务器备份只包括已传到服务器的数据，不包括仍仅在手机本地的数据。

## 数据与升级边界

空间不足或本机写入失败时，不显示已同步；未落盘照片暂留本页内存，仍可随本机恢复文件导出，腾出空间后用“重试本机保存并同步”重试。离开页面会提示尚未保存；在成功重试或导出前不要关闭页面。

schema v5 在事务中保留 v4 旧 notes 全部字段，增加 paper 附表；v6 再增加本地 OCR 租约/回执表和索引。当前最终版本为 **schema v6**，支持 v1–v5 启动迁移。迁移既不重新识别，也不搬动原 V2 图片。原文、一读、二读与图片顺序一起受版本保护；统一阅读页对纸质书二读也检查并提升版本，原 Zotero Markdown 保存规则不变。

快速记录同步后即为书籍中的待整理笔记，无须等待 OCR 或强制归档；V2 图片草稿新增入口已从书籍页移除；已归档来源仍可查看，未归档旧草稿由用户备份后用限定清理命令退出。升级前停止独立测试实例并完整备份。不要让旧 v1–v5 程序直接打开 v6 数据。回退需把升级前备份恢复到新空卷，保留现有数据卷。

## P1 本地 OCR 与后续整理

Windows x64 默认 CPU，使用 RapidOCR 携带的 PaddleOCR ONNX 中英文模型；安装和按需批处理见 [Windows 使用说明](../tools/local_ocr/README.md)。运行库仅装到电脑，不增加云端推理依赖、新服务或公网端口。用户已说明有 NVIDIA 显卡，GPU 待型号/驱动确认后单独配置，CPU 不受影响。

网页可逐条或批量对未识别图片排队。电脑以专用可撤销凭证向服务器主动领取任务；凭证在服务器仅保存哈希，不能用于一般笔记 API。30 分钟租约、运行续期、领取回执和结果 outbox 保护中断/重复回传。每页记录图片 ID、顺序及结果，后续结果作为独立历史保留，不自动覆盖原文、一读或二读。采用结果需用户明确操作，再人工校对和同步。

完成当前全部图片的本地识别、人工确认非空原文且版本已同步后，才能安排清理。确认后默认保留 30 天，仍可查看、取消清理；任何文字/图片/顺序修改都会取消确认，未完成的任务也会阻止清理。只有确认仍对应当前版本、图片仍关联、期限已到且无运行/待处理任务时才删除文件。先提交清理标记再删文件，意外中断可重试；文字、评注和 OCR 逐页历史均保留。尚未确认、已取消清理的图片不会自动删除，V2 原有图片也不套用新清理机制。

完整备份含已上传的全部未清理图片（含尚未提交关联的上传）、页序、历史与笔记。恢复只允许新空目录/卷；恢复后撤销电脑凭证、把未完成任务恢复为待处理，并取消旧清理计划，避免旧备份的过期日期导致刚恢复的图片立即被删。需要识别时重新生成专用凭证，需要清理时重新确认。

## 独立测试实例升级

目的：在原独立测试项目升级，保留已有图片、一读、账号和 OCR 历史；不改 main、正式服务、代理或防火墙。保留当前服务器资源配置。下面沿用先前已经配置的只读 GitHub 部署密钥，不重新创建账号或配置腾讯 OCR。

服务器 SSH 中执行；新路径已存在时会停止，避免覆盖。克隆后先核对 `git log -1` 的版本与本次发布一致，再执行后续命令：

```bash
git -c core.sshCommand="ssh -i $HOME/.ssh/noteshub_deploy_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes" \
  clone --single-branch --branch codex/noteshub-v3-paper-notes \
  git@github.com:Rycho813/Noteshub.git "$HOME/noteshub-v3-paper-source-20261102"
cd "$HOME/noteshub-v3-paper-source-20261102"
git log -1 --format='%H %s'
```

核对版本后，同一组执行以下命令；任一步失败就停止：

```bash
(
set -e
umask 077
cd "$HOME/noteshub-v3-paper-source-20261102"
test ! -e "$HOME/noteshub-v3-paper-test-20261102"
python3 scripts/package_release.py artifacts/v3-paper-test.tar.gz
mkdir "$HOME/noteshub-v3-paper-test-20261102"
tar --no-same-owner --no-same-permissions -xzf artifacts/v3-paper-test.tar.gz \
  -C "$HOME/noteshub-v3-paper-test-20261102"
cd "$HOME/noteshub-v3-paper-test-20261102"
python3 scripts/check_release.py
sudo bash scripts/ocr_test.sh build
sudo bash scripts/ocr_test.sh stop
sudo bash scripts/ocr_test.sh backup
sudo bash scripts/ocr_test.sh up
sudo bash scripts/ocr_test.sh check
sudo bash scripts/ocr_test.sh summary
)
```

helper 名称仍为 `ocr_test.sh`，使用原独立项目 `noteshub-v2-ocr-test`、原测试数据卷及 loopback 18080。backup 校验成功并打印 `BACKUP=...` 后，up 才迁移；失败不启动新应用。默认腾讯 OCR 关闭，不执行 `enable-ocr`、`account` 或删除卷。新程序兼容升级前的 v3/v4 数据，原 137/17 字草稿及一次成功 OCR 记录不应变化；若此前已归档，原正式笔记也应保留。

电脑沿用 SSH 隧道访问 `http://127.0.0.1:18080/quick` 可测试 PWA。iPhone 安装、离线重开和连续拍照必须使用有可信证书的**独立测试 HTTPS 地址**；普通 IP/HTTP 不足以验收。本次开发没有修改 Caddy 或正式域名。不要从旧版本目录启动已升级测试卷。

## 验收顺序

1. 先核对 schema 6、旧书籍/图片/一读、旧 OCR 记录及原 Markdown 导出，确认升级保留数据。
2. 手机只输入想法，不拍照也能保存；开启飞行模式，多图记录、排序，彻底关闭再重开 PWA。确认本机文字和图片仍在，联网打开后最终为“已同步”。iPhone 系统语音输入需真机检查。
3. 电脑浏览器补充同一记录，制造双设备冲突，确认旧设备不会覆盖新版本，可保留两份。刷新、重新登录后核对文字和图片页序。
4. 云端排队后关闭电脑，确认手机可继续记录；之后按 Windows 文档启动批处理。核对逐页结果、明确采用、人工校对，并确认一读不变。
5. 核对完整 Markdown 导出和备份；确认图片清理后显示 30 天恢复期，能取消。不要为验收直接改真实数据库日期或清理原有图片。30 天后清理的边界由自动化时钟/夹具测试覆盖。
6. 验收结束从新发布目录 `sudo bash scripts/ocr_test.sh stop`，保留数据卷和备份。

## 自动验证

```bash
.venv/bin/python -m unittest discover -v
python3 tests/browser_quick.py
python3 tests/browser_archive.py
python3 tests/browser_local_ocr.py --ocr-python /path/to/separate-ocr-venv/bin/python
```

P0 阶段先通过 141 项中的 134 项后端/回归测试，7 项因私有样本未纳入 Git 跳过；随后再加入 P1 测试。真实 Chromium 的 SW/IndexedDB 覆盖离线重开、文字、多图排序/移除、丢失回执重试、双设备/双标签页冲突另存、配额不足、重新登录、本机恢复和手机宽度。独立 OCR 环境已实际对合成中英文多页图片推理，端到端验证浏览器上传、任务领取、逐页结果、显式采用、校对、确认/取消清理和统一阅读页导出。自动化没有调用腾讯 OCR。

尚未进行你的 iPhone 真机、Windows/NVIDIA 真机及真实书页准确率验收；实际服务器尚未升级，不能用开发环境通过代替这些验收。
