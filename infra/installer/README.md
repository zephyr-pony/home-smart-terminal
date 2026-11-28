# 一键安装盘（Ubuntu Server + autoinstall）

从**官方 Ubuntu Server ISO** 生成一张「开机就自己装完」的安装盘，再把这张盘挂到
机器上启动。适合两种场景：

- **新机器到场**：没有显示器键盘，靠 BMC/iLO 的虚拟光驱装；
- **重装/复现**：同样的主机名、用户、公钥、镜像源、整盘布局，一条命令重来。

生成物里没有密钥文件，只有**公钥**和**密码哈希**（和云镜像的 cloud-init 一样），
所以磁盘镜像本身可以给同事；但别把带自己密码的 ISO 传到公开渠道。

> ⚠️ **它会抹掉目标机的所有磁盘。** 脚本不弹确认，别对着还在跑的服务器执行。

```
infra/installer/
├─ build_iso.py            # 官方 ISO → 带 autoinstall 的 ISO
├─ serve_http.py           # 给 BMC 虚拟光驱用的 Range HTTP 服务
├─ ilo_deploy.py           # HPE iLO（Redfish）：挂 ISO、一次性启动、找新机
└─ autoinstall/user-data.tmpl
```

## 0. 前置

| 需要 | 说明 |
|---|---|
| `xorriso` | Linux 直接 `apt install xorriso`；**Windows 上不用装**，脚本会自动借 WSL 里的（`wsl -d Ubuntu -u root -- apt install -y xorriso`） |
| BMC 虚拟光驱 | HPE 机器要 **iLO Advanced/Essentials**（Gen10 Plus 需 iLO Enablement Kit）；其它厂商的 IPMI/Redfish 也可以手动挂 |
| 放行 HTTP | BMC 会主动来拉 ISO。Windows 防火墙按 **exe 路径**放行，`.venv\Scripts\python.exe` 通常不在规则里——要么用被放行的解释器跑，要么加一条只对局域网开放的规则 |
| 目标机有网 | 安装时要 `apt` 装包（镜像源默认清华 TUNA，可用 `--mirror` 换） |

## 1. 生成安装盘

```bash
python infra/installer/build_iso.py \
    --iso  ~/Downloads/ubuntu-26.04.1-live-server-amd64.iso \
    --out  data/installer/xiaoma.iso \
    --hostname xiaoma --user xiaoma \
    --ssh-key ~/.ssh/id_ed25519.pub \
    --mirror https://mirrors.tuna.tsinghua.edu.cn/ubuntu/
```

不传 `--password` 会**随机生成一个并只打印一次**；要给固定密码就 `--password`，
或者用现成的 SHA-512 哈希 `--password-hash '$6$...'`（适合 CI/一次性环境）。
`--ssh-key` 可以给多次，强烈建议给：装完直接免密进，不用再配密码。

生成的内容：主机名、用户、公钥、整盘 ext4（`storage.layout.direct`，**抹盘**）、
`openssh-server`、`--package` 追加的包（默认 vim/curl/htop/git/gnupg/smartmontools/
lm-sensors/python3-venv）、`shutdown: reboot`。

## 2. 挂到机器上启动

**HPE iLO**（本仓库的主力场景）：

```bash
ILO_PASSWORD='...' python infra/installer/ilo_deploy.py \
    --ilo 192.168.2.200 --user Administrator \
    --iso data/installer/xiaoma.iso \
    --scan 192.168.2.0/24          # 装完扫一下新机在哪
```

脚本会：起一个 Range HTTP 服务 → Redfish 挂虚拟光驱 → 设「一次性从光驱启动」→
重启 →（可选 `--wait-host <IP>`）等 22 端口起来后自动弹介质。只挂不启 `--no-boot`，
只弹介质 `--eject`。密码走 `ILO_PASSWORD`，别写进命令行历史。

**其它机器 / 物理 USB**：把 ISO 挂到 BMC 的虚拟光驱即可；写 U 盘请用 Rufus/Ventoy
这类能按 UEFI 引导的工具（这张 ISO 没有做 isohybrid，`dd` 直接写 U 盘不会启动）。

## 3. 踩过的坑（别重复踩）

1. **GRUB 里 `;` 是语句分隔符。** 内核参数写成 `ds=nocloud;s=/cdrom/autoinstall/`
   会被截成 `ds=nocloud`，剩下的当一条不存在的命令——现象是安装器正常起来、
   停在「选语言」，`autoinstall` 像没生效一样。必须写成 `ds="nocloud;s=..."`。
2. **Ubuntu 的 EFI 镜像不在 ISO9660 目录里**，而是拷在 ISO 末尾的附加分区。用
   pycdlib 这类「只重写文件系统」的工具改，EFI 镜像会丢，机器直接起不来。
   本脚本用 xorriso 原样解包/重打包，并把 EFI 镜像单独抽出来重新挂。
3. **iLO 的虚拟光驱要「已连接」**（Redfish 里 `ConnectedVia: URI`）。只在 CLI 里
   `vm cdrom insert <url>` 会留下 `Image Connected = No`——URL 写进去了但没连上，
   UEFI 看不到这张盘。
4. **冷启动才认介质**：插好介质后 `power off hard` + `power on`，热重启往往直接跳过。
   另外 Redfish 的「一次性从光驱启动」在 iLO 5 2.33 实测会被 POST 清掉而不生效，
   稳妥做法是在 iLO 里把虚拟光驱调到**引导顺序第一位**。
5. **iLO 5 2.33 在「虚拟媒体 + 频繁调接口」下会整机卡死**（连 ping 都不通，SSH 和
   HTTPS 一起没响应）。这种状态只能**断 AC** 让它复位（iLO 走待机供电，关机不算）。
   装的时候别一边读盘一边轮询状态。
6. **autoinstall 一定要显式写 `network:` 段。** 不写时 subiquity 会拿 live 环境的
   配置去套目标机，`netplan apply` 返回 1，安装就地中断（停在「An error occurred.
   Press enter to start a shell」）。模板里给的是 `match: {name: "e*"}, dhcp4: true`。

## 4. 装完接着做什么

装完只是「有了一台 Ubuntu」。本项目的服务侧步骤在 `infra/ubuntu/README.md`
（Docker、数据卷恢复、开机自启），迁移清单在 `docs/server-migration.local.md`。
