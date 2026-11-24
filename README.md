# wechat-ilink-muse-bridge

把个人微信号接给 AI Agent 用的桥：基于腾讯**官方 iLink Bot API**
（`https://ilinkai.weixin.qq.com`，微信 ClawBot 通道），不是 Hook、不是逆向协议。
扫码授权后，Agent 可以秒级收微信消息、回消息、主动推送，还能收发图片。

线协议存档见 [PROTOCOL.md](PROTOCOL.md)；给 Agent 的行为约束模板见
[CONTRACT.md](CONTRACT.md)。

## 功能

- **扫码登录**：`wx.py login` 本地生成二维码 PNG，微信扫码确认后凭证落盘（0600）。
- **秒级收消息**：守护进程长轮询 `getupdates`（服务端约 35 秒挂起），新消息立刻写入 `inbox.jsonl`。
- **发消息 / 连发合并**：同一人 2 秒内连发的队列行合并成一条发送；发送临时失败自动重试一次。
- **主动推送**：不等对方先开口也能发（受 `context_token` 协议限制，见下）。
- **图片双向**：发图走 CDN 加密上传，收图自动下载解密落盘。
- **防丢消息**：出队只能经 `wxsend.py` 带锁追加；万一出现粘连行，守护进程会逐个抢救解析。
- **会话过期可见**：`errcode -14` 时在 inbox 写标记并以 exit 2 退出，不默默死掉。

## 架构

三个脚本，分工固定：

| 文件 | 作用 |
|---|---|
| `wx.py` | 线协议客户端 CLI：扫码登录、一次性轮询、一次性发送；也被 `wxd.py` 当模块导入 |
| `wxd.py` | 守护进程：轮询线程收消息进 `inbox.jsonl`，出队线程消费 `outbox.jsonl` 并把结果记入 `sent.jsonl` |
| `wxsend.py` | 唯一受支持的入队方式：持 `fcntl.flock` 追加一行 JSON 到 `outbox.jsonl` 并 fsync |

数据流：

```
微信 <-> iLink API <-> wxd.py -- inbox.jsonl --> 你的 Agent
                        wxd.py <-- outbox.jsonl -- wxsend.py <-- 你的 Agent
```

凭证与状态**不在仓库里**，只存 `~/.config/wechat-bridge/`（目录 0700、文件 0600）：

| 路径 | 内容 |
|---|---|
| `~/.config/wechat-bridge/auth.json` | `bot_token`、`ilink_bot_id`、`ilink_user_id`、`baseurl` |
| `~/.config/wechat-bridge/state.json` | `get_updates_buf` 游标 + 每位联系人最新的 `context_token` |
| `~/.config/wechat-bridge/wxd-state.json` | 守护进程的 outbox 读取偏移（保证每行恰好消费一次） |

运行时数据 `inbox.jsonl` / `outbox.jsonl` / `sent.jsonl` / `media/` 都已在
`.gitignore` 里，永远不要提交——里面是真实用户 ID 和消息内容。

## 安装

需要 Python 3.10+：

```bash
git clone https://github.com/yousj666/wechat-ilink-muse-bridge.git
cd wechat-ilink-muse-bridge
pip install -r requirements.txt
```

依赖说明：`requests` 必需；二维码生成用 `segno`（或 `qrcode` 库，二选一）；
图片加解密优先用 `cryptography` / `pycryptodome`，都没有时回退到系统 `openssl` 命令行。

## 扫码登录流程

```bash
./wx.py login            # 默认等 300 秒，可加 --timeout 600
```

1. 脚本向 iLink 申请 bot 二维码，本地生成真 PNG 存为 `login-qr.png`，并打印一行
   `{"event": "qrcode", ...}`。
2. 用微信扫 `login-qr.png`，在手机上确认 bot 授权。
3. 脚本轮询扫码状态（`wait` → `scaned` → `confirmed`），每次变化打印一行 JSON。
4. 确认后凭证写入 `~/.config/wechat-bridge/auth.json`（0600），打印
   `{"event": "confirmed", ...}`。
5. 二维码过期退出码为 3，重跑 `login` 即可；无人扫码超时退出码为 4。

> 坑：接口返回的 `qrcode_img_content` 是要被二维码编码的 URL **字符串**，不是图片
> 下载地址。直接把它当图片下载存下来会得到一个 HTML 页面，手机会报「格式不支持
> 或已损坏」。本项目在本地用 segno/qrcode 生成 PNG，不要改回下载方式。

登录必须有人在场扫码，不要无人值守跑。

## 运行守护进程

```bash
./wxd.py           # 前台运行，Ctrl-C 停
./wxd.py --once    # 单轮轮询 + 单轮出队后退出（测试用）
```

24 小时常驻建议放到一台常开机器上用 systemd 托管（`Restart=always`、
`WorkingDirectory` 指到本目录），不要放在会回收会话的临时沙箱里——守护死了消息就收不到。

收到的消息长这样（一行一条）：

```json
{"ts": 1700000000000, "from_user_id": "...", "text": "...", "items": [...], "message_id": 123, "create_time_ms": 1700000000000}
```

非文字内容在 `text` 里呈现为 `[image]` / `[voice]` / `[file]` / `[video]` 占位，
结构化原文保留在 `items` 字段里。

发消息只能经 `wxsend.py`，**禁止手写追加 `outbox.jsonl`**：

```bash
./wxsend.py <ilink_user_id> "你好"
./wxsend.py --image <图片路径> <ilink_user_id>
```

`wxsend.py` 持独占文件锁追加一整行 JSON 并 fsync 后才释放——两个写者同时追加会把
两条 JSON 粘成一行、解析失败、消息全丢，这不是假想事故，是真丢过消息换来的规矩。
守护侧另有兜底：粘连行会用 `JSONDecoder.raw_decode` 逐个抢救，只有真正无法解析的
残渣才记入 `sent.jsonl` 作废（恰好一次，不死循环）。

## 主动推送与 context_token 的协议限制

`sendmessage` 必须回带收件人的 `context_token`，这个 token 只能从对方发来的消息里
学到，并按用户存进 `state.json`。由此得出两条协议事实（不是功能缺失）：

- **主动推送可以**：只要对方以前给 bot 发过任意一条消息，token 就在册，且协议没有
  时效检查——Agent 随时可以经 `wxsend.py` 主动推送（任务结果、提醒、异常都行）。
- **冷启动不行**：从没给 bot 发过消息的人没有 token，协议上无法先联系对方。
  `wxsend.py` 对这种收件人直接以退出码 2 报错并拒绝入队，不会默默排队假装能发。

## 发图通道说明

发图（已实测可用）：

```
getuploadurl -> 本地生成随机 AES 密钥 -> AES-128-ECB + PKCS7 加密
-> POST CDN 上传（响应头 x-encrypted-param 为下载参数）
-> sendmessage（item type 2，image_item 只带 mid_size）
```

两个实测踩过的坑，改代码时别改回去：

- 出站 `CDNMedia.aes_key` 必须是 **base64(hex 字符串)**（腾讯官方 openclaw-weixin
  发送端的做法），不是 base64(原始 16 字节密钥)。写错时接口照样返回成功，但微信
  客户端解不开图，会显示「图片已过期或已被清理」。
- `image_item` 只发 `mid_size`（密文大小），不要多发 `hd_size`。
- 注意区分：`getuploadurl` 请求里的 `aeskey` 字段是裸 hex 字符串，和上面那条不是一回事。

收图（已实测可用）：入站图片的 `image_item.media` 带下载参数与 `aes_key`（入站编码
两种都可能出现：base64(原始密钥) 或 base64(hex 字符串)，解码需都兼容），`wxd.py`
下载解密后存入 `media/`，并在 inbox 记录里写 `media: [{kind, local_path, bytes}]`。

语音 / 文件 / 视频的媒体收发与 `sendtyping` 尚未封装。

## 一次性客户端用法

```bash
./wx.py poll             # 持续长轮询，每条消息打印一行 JSON
./wx.py poll --once      # 单次 getupdates 后退出
./wx.py send <ilink_user_id> "你好"
./wx.py send-image <ilink_user_id> <图片路径>
```

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 用法错误 / 未登录 / 网络或接口错误 |
| 2 | 会话过期（`errcode -14`）——重跑 `./wx.py login` |
| 3 | 二维码过期（仅 `login`） |
| 4 | 登录超时（仅 `login`） |

`wxsend.py` 自己的退出码：0 已入队 · 1 用法错误 · 2 收件人无已存 `context_token`（未入队）。

## 测试

```bash
python3 tests_send_path.py
```

离线测试（发送端打桩、不碰真实接口）：粘连行抢救、半行暂缓、垃圾行只记一次、
发送重试一次、-14 立即过期、10 路并发 `wxsend.py` 入队、无 token 拒发。

## License

MIT，见 [LICENSE](LICENSE)。
