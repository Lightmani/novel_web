# novelsite —— 极简个人 TXT 小说站

为「几万到十几万本 TXT 小说」设计的个人阅读站点。**只依赖 Python 3.8+ 标准库**（`http.server` + `sqlite3`），没有任何 `pip install`。Python 约 1800 行、CSS + JS 约 250 行，可以完整读一遍。

## 功能

- **书库**：递归扫描目录，从文件名和文件头提取书名、作者、简介；子目录即分类，可逐级浏览
- **检索**：书名 / 作者模糊搜索（多关键词 AND）、按作者筛选、分类内搜索、随机一本
- **阅读**：自动识别编码与章节、分卷目录、上一章 / 下一章、键盘 ← → 翻章、预取下一章
- **阅读设置**：四种配色（默认 / 纸张 / 护眼 / 夜间，跟随系统暗色）、字号、行距、版心宽度、宋体 / 黑体
- **进度**：自动记录到章节内滚动位置，**存在服务端**，手机和电脑之间同步；「继续阅读」直接回到原位置
- **书架**、最近阅读、**书内全文搜索**（结果高亮并跳转）
- **下载**：任何编码一律转成 UTF-8 下载，方便导入阅读器
- **增量扫描**：只处理新增 / 修改 / 删除的文件；**移动或改名的文件会被识别，书架和进度不丢**

## 快速开始

```bash
git clone https://github.com/Lightmani/novel_web.git
cd novel_web
python3 -m novelsite serve --library /path/to/novels
```

打开 <http://127.0.0.1:8080/>。首次启动会在后台扫描书库，页面可以立即使用，书会陆续出现（「管理」页可看进度）。

数据库默认是当前目录下的 `novelsite.db`，可用 `--db` 指定。小说目录**只读**，程序从不修改你的 TXT 文件。

### 在局域网 / 公网访问

监听非本机地址时**必须设置密码**，否则拒绝启动：

```bash
NOVEL_PASSWORD='一个长密码' python3 -m novelsite serve --library /data/novels --host 0.0.0.0 --port 8080
```

或用 `--password-file /etc/novelsite/password` 从文件读取（避免密码出现在 shell 历史和进程列表里）。

### 命令一览

| 命令 | 说明 |
|---|---|
| `serve --library DIR` | 启动网站，默认 `127.0.0.1:8080`，启动时做一次增量扫描 |
| `  --host / --port` | 监听地址与端口 |
| `  --db FILE` | 数据库位置 |
| `  --name 书库` | 站点名称 |
| `  --password-file F` | 密码文件（或环境变量 `NOVEL_PASSWORD`） |
| `  --no-scan` / `--quiet` | 启动时不扫描 / 不打印访问日志 |
| `scan --library DIR` | 只扫描然后退出（适合放进 cron） |
| `  --prune` | 缺失文件超过一半时仍执行删除（默认会拒绝，见下文） |

## 推荐部署

### systemd

```ini
# /etc/systemd/system/novelsite.service
[Unit]
Description=novelsite
After=network.target

[Service]
User=novel
WorkingDirectory=/opt/novel_web
ExecStart=/usr/bin/python3 -m novelsite serve --library /data/novels --db /var/lib/novelsite/novelsite.db --password-file /etc/novelsite/password --quiet
Restart=on-failure
# 加固：程序只需要读小说目录、写数据库目录
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/novelsite
PrivateTmp=true
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

### 反向代理（HTTPS）

`http.server` 没有针对公网做加固（Python 官方文档也这么说），**不要把它直接暴露到公网**。二选一：

- **最省事也最安全**：不开放公网端口，用 Tailscale / WireGuard 之类的组网工具从手机访问。
- **需要公网访问**：前面放 Caddy 或 nginx 负责 HTTPS，本程序只监听 `127.0.0.1`，同时**依然设置密码**。

Caddy（自动申请证书，默认透传 Host 头）：

```
novel.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

nginx：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;                 # 必须：POST 请求要校验 Origin 与 Host 一致
    proxy_set_header X-Forwarded-Proto $scheme;  # 让登录 Cookie 带上 Secure 标记
}
```

## 设计取舍

**为什么不做全文检索？** 十万本小说按平均 2–3MB 计有两三百 GB，中文全文索引（n-gram）的体积与原文同量级甚至更大，和「不占太多资源」直接冲突。所以只做书名 / 作者检索，加上单本书内搜索（现场扫描一本书，几 MB 的文件在百毫秒内完成）。

**正文不进数据库。** 数据库只存每章在原文件中的**字节偏移**；阅读时 `seek` 到对应位置读一章。数据库因此很小（实测 10 万本约 20MB），原文件保持原样。

**目录懒解析。** 扫描时只 `stat` 文件并读开头 4KB（提取作者、简介、粗判编码），章节目录在第一次打开某本书时才解析，然后压缩缓存。没读过的书不占解析成本。

**流式解析，内存与文件大小无关。** 按 1MB 分块解码与匹配章节标题。解码用 `surrogateescape`，保证「解码→再编码」字节长度严格一致，即使文件中间有坏字节，偏移也不会漂移。

### 实测（本机 SSD，Python 3.13）

| 场景 | 结果 |
|---|---|
| 首次扫描 10 万个文件 | 5.3 秒，进程峰值内存 61MB |
| 无变化重扫 / 移动 1000 + 删除 1000 | 0.4 秒 / 0.5 秒 |
| 数据库体积（10 万本） | 约 21MB（真实简介更长时约 40–60MB） |
| 解析 26.7MB、1300 万字、4000 章的 GBK 长篇 | 0.22 秒，峰值内存 23MB，目录压缩后 52KB |
| 10 万本库中的页面响应 | 绝大多数 < 30ms；全库模糊搜索翻到第 3000 页约 70ms |
| 服务常驻内存 | 约 25MB |

> 测试库的文件很小且在页缓存中。**机械硬盘上首次冷扫描主要耗在磁头寻道**（每个文件要读开头 4KB），十万个文件可能需要十几分钟，但只有第一次如此。

## 编码与章节识别

- 编码：BOM → UTF-8 → 在 GB18030 / Big5(cp950) / UTF-16 之间按「常用汉字占比 − 坏字符率」打分选择。夹杂个别坏字节的 UTF-8 文件不会被误判为 GBK。
- 换行：LF、CRLF、老式 Mac 的纯 CR 都支持。
- 章节标题（单独成行、长度有限）：`第X章/节/回/话/幕`（中文或阿拉伯数字、大写数字）、`Chapter N`、序章、楔子、引子、尾声、后记、番外等；`第X卷/部/集/篇` 作为分卷。
- 文首「目录」（一串没有正文的标题）会并入「前言」，不会产生几百个空章节；连续重复的标题行会被去重。
- 没有任何章节标题的书按约 60KB 一页分页；超过 120KB 的单章也会拆成若干页。**任何情况下都不丢正文**。

### 已知局限

- 用 `1、` `001` 这类纯数字编号章节的书识别不出章节，会退化为分页阅读（可读，但目录不友好）。
- 文件名格式五花八门，作者提取是启发式的：优先使用文件名中明确的「作者：」，其次文件头的「作者：」行，最后才猜 `书名_作者`。
- 分类即目录结构；如果所有书都堆在一个目录里，就只有「全部书籍」。

## 安全说明

- 零第三方依赖，没有供应链风险；但这不等于安全——**真正的风险在暴露面**，见上面的部署建议。
- 所有动态内容转义输出；CSP 禁止内联脚本，即使转义有遗漏也难以执行注入脚本。
- 文件只能通过数据库 id 访问，路径从不来自用户输入，不存在目录穿越。
- 登录 Cookie：HMAC 签名、`HttpOnly`、`SameSite=Lax`，经 HTTPS 代理时自动加 `Secure`；修改密码后旧 Cookie 全部失效。连续 5 次密码错误锁定 5 分钟。
- 所有 POST 校验 `Origin`，防跨站请求伪造；登录后的跳转只允许站内路径。
- 未设密码时只允许 `localhost` 访问并校验 `Host` 头，防 DNS rebinding。
- 扫描保护：如果一次扫描发现超过一半的书「消失」（典型情况是移动硬盘 / NAS 没挂载），**不会删除**数据库记录，避免书架和进度被清空。

## 数据与备份

需要备份的只有数据库文件（书架、阅读进度、目录缓存都在里面）。运行中备份请用 SQLite 自带的在线备份：

```bash
sqlite3 novelsite.db ".backup novelsite-backup.db"
```

删除数据库只会丢失书架和进度，重新扫描即可重建书目。

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：各种编码与换行、Big5、文首目录、重复标题、无章节分页、标题误判、文件名解析、页面渲染、XSS 转义、进度与续读、CSRF、开放重定向、DNS rebinding、下载转码、登录流程。已在 Python 3.8 / 3.11 / 3.12 / 3.13 上通过。

```
novel_web/
├── novelsite/
│   ├── textproc.py   编码识别、元数据、章节切分（纯函数，无 I/O 状态）
│   ├── library.py    SQLite、增量扫描、目录缓存、阅读进度
│   ├── web.py        路由、页面、鉴权
│   ├── __main__.py   命令行
│   └── static/       style.css、app.js（无框架，ES5）
└── tests/
```
