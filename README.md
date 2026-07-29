# Island Download Bot

私人 Telegram 下载机器人，面向 QAS、Aria2、qBittorrent、MoviePilot 和
Google Drive 的媒体流程。

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
- 极简主页：添加下载、任务、服务器状态、账户权限、帮助
- 任务详情、暂停、继续、二次确认删除
- 显示 qBittorrent 实时上传/下载速度和完成目录可用空间
- 状态页显示 VPS 下载盘与 Google Drive 的实时总量、已用量、可用量
- 自动兼容 qBittorrent 5 的 HTTP 204 登录响应；会话过期自动重连，短暂网络失败自动重试
- 下载完成提醒；后续由 MoviePilot 监控下载器，自动识别、命名、上传 Google Drive 并清理源文件
- 下载完成提示会在 5 分钟后自动清理，保持聊天简洁
- 支持发送 `pan.quark.cn` 分享链接：QAS 转存后将文件直链交给 Aria2，完成后由 MoviePilot 入库 Google Drive
- 普通中文消息接入本地 Ollama AI 助手；下载与删除仍须使用机器人操作确认
- 下载队列：默认最多同时下载 2 个任务；默认预留 10GB 空间，空间不足时暂停并在清理后自动继续

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
3. 构建并启动：

```bash
docker build -t island-download-bot:1 .
docker run -d --name island-download-bot --network media-net --restart unless-stopped \
  --env-file .env -v "$(pwd)/data:/data" \
  -v /opt/media/downloads:/downloads:ro \
  -v /opt/media/moviepilot/config/rclone/rclone.conf:/rclone/rclone.conf:ro \
  island-download-bot:1
```

`.env` 不应上传至 GitHub。

如需夸克分享链接功能，额外填写 `QAS_URL`、`QAS_USERNAME`、`QAS_PASSWORD`。QAS 与机器人须位于同一个 Docker 网络。

`MIN_FREE_GIB` 可调整下载目录的安全预留空间，默认值为 `10`。

## 测试

```bash
python3 -m py_compile simplebot.py islandbot/*.py
python3 -m unittest discover -s tests -v
```
