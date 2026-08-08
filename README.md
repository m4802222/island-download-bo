# Island Download Bot

私人 Telegram 下载机器人，面向 QAS、Aria2、qBittorrent、MoviePilot 和
Google Drive 的媒体流程。

当前稳定版本：`v2.1.0`。

## 2.0 重构重点

- 下载前先建立结构化媒体身份：名称、年份、TMDB、类型、季数。
- 电视剧目录统一为 `名称 (年份) {tmdb-ID} Sxx`，裸文件 `01.mkv`
  也会按已确认季数生成正确的 `SxxE01`。
- 只按“同一 TMDB 对应的名称 + 同一季 + 同一集”查重。第一季不会再导致
  第二季整季被跳过。
- 自动识别第二季时拒绝使用“某某 第2季”这类独立重复 TMDB 条目；
  找不到主剧时必须由用户回复 `主剧TMDB 第2季`。
- 用户手动确认的 TMDB 会写入独立的 v2 身份库，不再读取旧版污染缓存。
- 所有队列和确认状态使用原子写入，异常退出不会留下半截 JSON。
- 核心规则、外部服务、状态存储和 Telegram 入口分层，并由自动测试覆盖。

## 功能

- 磁力链接或 Telegram 上传的 `.torrent` 种子文件，分类后添加到 qBittorrent
- 支持“智能分类”：不指定下载器分类，由 MoviePilot 按媒体元数据和现有规则整理
- 极简主页：Emby 开号、任务、服务器状态
- 仅机器人所有者可用“开号”：输入用户名即创建普通观看账号，默认密码
  `123456`，自动关闭管理、删除、下载、字幕管理和共享权限
- 任务详情、暂停、继续、二次确认删除
- 显示 qBittorrent 实时上传/下载速度和完成目录可用空间
- 状态页显示 VPS 下载盘与 Google Drive 的实时总量、已用量、可用量
- 自动兼容 qBittorrent 5 的 HTTP 204 登录响应；会话过期自动重连，短暂网络失败自动重试
- 下载完成后立即触发 MoviePilot，自动识别、命名并上传 Google Drive；只有成功整理记录精确覆盖全部视频后才删除普通任务和源文件，刷流任务始终保留
- 下载完成提示会在 5 分钟后自动清理，保持聊天简洁
- 支持发送 `pan.quark.cn` 分享链接：QAS 转存后将文件直链交给 Aria2，完成后由 MoviePilot 入库 Google Drive
- 普通中文消息接入本地 Ollama AI 助手；下载与删除仍须使用机器人操作确认
- 下载队列：默认最多同时下载 2 个任务；默认预留 10GB 空间，空间不足时暂停并在清理后自动继续
- MoviePilot 的 rclone 上传失败时保留源文件、暂停普通下载并按错误类型退避重试；
  Google Drive 恢复后自动续传并恢复被系统暂停的任务，刷流任务始终不受影响

## 夸克确认流程

1. 发送完整资源帖或夸克链接。
2. 机器人提取明确的标题行，调用 MoviePilot/TMDB 确认身份。
3. 显示名称、年份、TMDB、季数，点击“确认下载”。
4. 若自动确认失败，回复 `259231 第2季` 这类“主剧 TMDB + 季数”。
5. 机器人只提交 Google Drive 中缺少的准确集数。

没有写季数时默认第一季；写了第二季以后，季数是身份的一部分，不会与
第一季共用缓存或查重结果。

## 代码结构

- `islandbot/media.py`：标题、季、集与媒体身份规则（纯函数）。
- `islandbot/resolver.py`：MoviePilot/TMDB 的保守识别与人工确认。
- `islandbot/library.py`：云盘和 MoviePilot 历史的精确缺集判断。
- `islandbot/storage.py`：原子 JSON 状态和 v2 身份库。
- `islandbot/clients.py`：Telegram、Aria2、QAS、MoviePilot 适配器。
- `islandbot/parsing.py`：Telegram 资源帖、夸克和磁力链接解析。
- `islandbot/app.py`：业务编排与交互入口。
- `tests/`：历史故障回归测试。

## 部署

1. 复制 `.env.example` 为 `.env`，填写机器人令牌、Telegram 数字 ID 和 qBittorrent 账号。
2. 确保机器人和 qBittorrent 位于同一个 Docker 网络（默认示例为 `media-net`）。
3. 使用固定发布版部署：

```bash
curl -fsSL https://raw.githubusercontent.com/m4802222/island-download-bo/v2.1.0/scripts/deploy-vps.sh -o /tmp/deploy-vps.sh
bash /tmp/deploy-vps.sh
```

部署脚本会先执行编译和测试，保留上一版源码用于失败回滚，并以目录方式将
MoviePilot 的 rclone 配置挂载到 `/rclone:rw`，避免 OAuth 刷新后容器仍读取旧文件。
部署完成后还会安装上传恢复定时器；它只重试源文件仍存在的 rclone 上传失败记录，
不会重试识别失败、文件不存在或已经成功的历史记录。

`.env` 不应上传至 GitHub。

如需夸克分享链接功能，额外填写 `QAS_URL`、`QAS_USERNAME`、`QAS_PASSWORD`。QAS 与机器人须位于同一个 Docker 网络。

如需 Emby 开号功能，填写 `EMBY_API_KEY` 和 `EMBY_PUBLIC_URL`。API 密钥只保存在
VPS 的 `.env` 中，不会显示在 Telegram 消息里。

`MIN_FREE_GIB` 可调整下载目录的安全预留空间，默认值为 `10`。

`QBIT_SAVE_PATH` 固定为 `/downloads/complete/islandbot`。`AUTO_CLEANUP_COMPLETED`
开启后，仅在 MoviePilot 成功整理记录精确匹配全部视频文件时清理普通下载任务；
刷流分类和刷流标签不会自动删除。

## 测试

```bash
python3 -m py_compile simplebot.py islandbot/*.py
python3 -m unittest discover -s tests -v
```
