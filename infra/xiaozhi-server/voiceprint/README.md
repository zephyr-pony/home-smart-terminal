# 声纹识别（voiceprint-api + MySQL）

小智能认出「谁在说话」，能力不在小智服务端里：上游把它拆成一个独立服务
（[voiceprint-api](https://github.com/xinnan-tech/voiceprint-api)，模型是 3D-Speaker 的
`campplus_sv_zh-cn_3dspeaker_16k`）+ 一个存特征向量的 MySQL。小智只在配置文件里填
一个 URL，识别结果随 `tools/call` 的 `params._meta` 透传给小马（D88）。

## 1. 起服务

```bash
cd infra/xiaozhi-server/voiceprint
echo "VOICEPRINT_DB_PASSWORD=$(openssl rand -hex 12)" > .env   # 只写在本地，别入库
mkdir -p data mysql
docker compose up -d
docker logs -f voiceprint-api        # 等模型下载完、看到 Uvicorn running
```

模型缓存 28MB 左右，第一次启动会从 modelscope 拉。

## 2. 写 `data/.voiceprint.yaml`

```yaml
server:
  host: 0.0.0.0
  port: 8005
  authorization: <自己定一个 key，注册和识别都用它>

mysql:
  # 同一个 docker 网络里直接用容器名，不要写 127.0.0.1（那是容器自己）
  host: "voiceprint-mysql"
  port: 3306
  user: "root"
  password: "<上面 .env 里的 VOICEPRINT_DB_PASSWORD>"
  database: "voiceprint_db"
```

健康检查（浏览器打开也行）：

```
curl "http://127.0.0.1:8005/voiceprint/health?key=<key>"
# {"total_voiceprints":0,"status":"healthy"}
```

## 3. 接到小智上

`xiaozhi-server/data/.config.yaml`：

```yaml
voiceprint:
  # 就是上面那个 health 地址（服务内部会自己拼 /voiceprint/identify）
  url: http://<服务器IP>:8005/voiceprint/health?key=<key>
  # speaker_id,名称,描述 —— 名称会进小马的称呼提示
  speakers:
    - "spk-feng,峰哥,家里的主人"
  # 0.4~0.5 起步：高了容易把自家人当陌生人，低了容易认错
  similarity_threshold: 0.45
```

重启小智；设备连上来时日志里会出现
`声纹识别功能已在连接时动态启用`，之后每次 `tools/call` 的 `_meta` 里带上
`{"speakerId": "...", "speakerName": "..."}`。

## 4. 录样本

一段 **3–10 秒**、正常语速、环境安静的语音就够。手机录音（m4a/mp3）先转成 16k 单声道：

```bash
ffmpeg -y -i 峰哥.m4a -ar 16000 -ac 1 -c:a pcm_s16le feng.wav
curl -X POST -H "Authorization: Bearer <key>" \
     -F "speaker_id=spk-feng" -F "file=@feng.wav" \
     "http://<服务器IP>:8005/voiceprint/register"
# {"success":true,"msg":"已登记: spk-feng"}
```

`speaker_id` 必须和 `.config.yaml` 里那条对得上。重复注册会覆盖。

## 5. 这套东西管什么、不管什么

- **管**：回复里称呼谁、按谁的画像说话；以后可以据此拒答陌生声音。
- **不管**：唤醒。唤醒词（ESP-SR）跑在设备上，听到「小马」就亮屏开麦，这时候
  服务端还没拿到音频。要做到「电视里的『小马』也不亮屏」，只能调高固件唤醒阈值
  或改成「长按说话」。
- 身份是**不可信输入**：只用于称呼和个性化，不参与鉴权、不改变写操作的确认流程。
