# Island Download Bot

私人 Telegram 下载机器人，面向 qBittorrent + MoviePilot + Rclone 媒体流程。

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
- 普通中文消息接入本地 Ollama AI 助手；下载与删除仍须使用机器人操作确认
- 顺序下载队列：同一时间只下载一个任务；默认预留 10GB 空间，空间不足时暂停并在清理后自动继续

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

`MIN_FREE_GIB` 可调整下载目录的安全预留空间，默认值为 `10`。
