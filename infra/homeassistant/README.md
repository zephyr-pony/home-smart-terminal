# Home Assistant（小米设备网关）

小马不直连米家，也不说 miIO 协议：让 HA 吸收几千个型号的差异，小马只跟 HA 的本地
REST 说话。取舍见 `docs/ROADMAP.md` 第 21 节。

## 为什么是这条路

- 米家开放平台只对硬件厂商开放（要企业组），个人拿不到控制接口。
- 小爱开放平台的方向是「用户对小爱说话 → 你的云服务」，反了；小爱音箱也没有官方
  TTS 出口，想让它播报只能逆向，且型号依赖。
- 直连 miIO 要逐台设备 token，新设备是 MIoT-Spec-V2，换设备就失效。

## 启动

```bash
docker compose -f infra/homeassistant/docker-compose.yml up -d
```

- 端口 `8123`，配置在 `data/homeassistant/`（已被 `.gitignore` 忽略，里面有小米账号的
  token 和证书，**不能入库**）
- 首次启动约 1 分钟；`docker logs -f homeassistant` 看进度
- 当前只映射到本机：Docker Desktop 的端口发布在 LAN 上不可达。要用手机浏览器访问
  HA，需要管理员权限做一次端口转发：

  ```powershell
  netsh interface portproxy add v4tov4 listenport=8123 listenaddress=0.0.0.0 connectport=8123 connectaddress=127.0.0.1
  netsh advfirewall firewall add rule name="HA 8123" dir=in action=allow protocol=TCP localport=8123
  ```

## 首次配置（浏览器，只有这一步需要小米账号）

1. 打开 <http://127.0.0.1:8123>，创建管理员账号。
2. **设置 → 设备与服务 → 添加集成**，搜索 `Xiaomi Home`。
3. 选择地区（中国大陆），用小米账号授权登录。
   - **授权后必然跳向 `http://homeassistant.local:8123/api/webhook/...`**：这个地址在
     `custom_components/xiaomi_home/const.py:142` 里**写死**，表单下拉框也只有这一个值，
     改不了。所以本机必须能解析这个名字，否则授权完成后直接失败（曾表现为 503）。
     在 `C:\Windows\System32\drivers\etc\hosts` 加一行（需要管理员）：

     ```
     127.0.0.1 homeassistant.local
     ```

     验证：`curl http://homeassistant.local:8123/manifest.json` 返回 200。
     备用办法：授权失败后，把地址栏里的 `homeassistant.local` 手动换成 `127.0.0.1`
     再回车，webhook 照样能收到 code。
   - 回调页显示「Success / 请关闭此页」后，回到 HA 页面继续点下一步。
   - 集成默认走**小米云**；本地控制要小米中枢网关（固件 3.3.0_0023+，仅国行）。
     没有中枢网关时，云控就是唯一选择，此时设备状态与控制指令都会过小米云。
4. 选择要导入的家庭与设备。
5. **个人资料 → 安全 → 长期访问令牌**，创建一个令牌（名字随便，如 `xiaoma`），
   复制出来填进 `.env` 的 `HOME_TOKEN`。

## 装的是什么

`custom_components/xiaomi_home` 是小米官方集成 `XiaoMi/ha_xiaomi_home` v0.5.0
（Release 里的 `xiaomi_home.zip` 解压后的内容必须放在 `custom_components/xiaomi_home/`
目录下，而不是直接摊在 `custom_components/` 里）。

许可证注意：该集成及其云端接口版权归小米，只授权在 Home Assistant 内非商业使用，
禁止用来开发其他应用或 Web 服务。小马调的是 HA 自己的本地 API，属于「在 HA 内使用」；
不要绕过 HA 直接抄它的云端接口。

## 常见问题

| 现象 | 处理 |
|---|---|
| 8123 打不开 | `docker ps` 看容器是否在跑；`docker logs --tail 50 homeassistant` |
| 集成不出现在列表 | 检查 `data/homeassistant/custom_components/xiaomi_home/manifest.json` 存在，然后 `docker restart homeassistant` |
| 授权后回调打不开 / 503 | 集成把回调写死在 `homeassistant.local`；hosts 里加 `127.0.0.1 homeassistant.local`，或把地址栏里的主机名换成 `127.0.0.1` |
| 小马报「设备网关连不上」 | `.env` 里 `HOME_TOKEN` 没填或已失效；重新生成长期访问令牌 |
| 设备状态不动 | 云控模式下刷新有延迟；控制类指令才需要更快，考虑上中枢网关 |
