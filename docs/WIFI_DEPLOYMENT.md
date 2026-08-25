# WiFi 部署与手机访问（TASK 31）

日期：2026-08-25 ｜ 状态：部署手册；板端镜像差异处已标注不确定性，实操以板端实际镜像为准

目标：板子上电自动连 WiFi、拿到固定地址，顾客手机打开
`http://coffee.local:8080/`（或固定 IP）即达点单屏。
板端服务本体（kiosk_server）的开机自启见 `deploy/`（TASK 30）。

**前置事实**（来自 `docs/README.md` 与 `docs/04-WiFi与网页通讯设计.md`）：
主控是正点原子 ATK-DLRV1126B，官方镜像为 **Buildroot**，板端已有 `wpa_supplicant`；
若刷了 Ubuntu/Debian 类镜像则走 netplan/NetworkManager。三种写法都给，
**先 `cat /etc/os-release` 确认镜像再对号入座**。

## 一、连 WiFi（按镜像三选一）

### A. Buildroot（wpa_supplicant，官方镜像，最可能）

`/etc/wpa_supplicant.conf`：

```
ctrl_interface=/var/run/wpa_supplicant
network={
    ssid="你的WiFi名"
    psk="你的WiFi密码"
}
```

手动连一次验证：

```bash
wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant.conf
udhcpc -i wlan0
ip addr show wlan0        # 拿到 IP 即成功（无 ip 命令用 ifconfig）
```

开机自动连：Buildroot 通常把上面两行写进 `/etc/init.d/` 自启脚本或
`/etc/network/interfaces`（`auto wlan0` + `pre-up` 拉起 wpa_supplicant）。
**不确定性**：不同厂商 Buildroot 根文件系统的自启位置不同，以镜像内现有
`/etc/init.d/S*` 网络脚本为准，顺着改，不要另起炉灶。

### B. Ubuntu Server 类镜像（netplan）

`/etc/netplan/01-wifi.yaml`：

```yaml
network:
  version: 2
  wifis:
    wlan0:
      dhcp4: true
      access-points:
        "你的WiFi名":
          password: "你的WiFi密码"
```

`sudo netplan apply`。静态 IP 见第二节。

### C. NetworkManager 类镜像（nmcli）

```bash
nmcli dev wifi connect "你的WiFi名" password "你的WiFi密码" ifname wlan0
nmcli con show        # 连接配置已持久化，开机自动重连
```

**插座同网要求**：智能插座/点动继电器必须与板子在**同一局域网**
（`docs/04` 已写明）；路由器别开 AP 隔离。

## 二、固定地址与 hostname

**首选方案：路由器侧 DHCP 绑定（MAC 地址绑固定 IP）。**
板端零配置、换镜像不失效、插座同样绑一遍（grinder/brewer 的 IP 写死在
`projects/coffee_fsm/config.json` 的 actuators.host，换 IP 要改配置）。

板端 hostname：

```bash
echo coffee > /etc/hostname        # Buildroot/Debian 通用
hostname coffee                     # 立即生效
```

**备选方案：板端静态 IP**（路由器不方便配置时才用）：

- netplan：上面 yaml 里把 `dhcp4: true` 换成
  `addresses: [192.168.1.50/24]` + `gateway4: 192.168.1.1` +
  `nameservers: {addresses: [192.168.1.1]}`
- Buildroot：`/etc/network/interfaces` 里 `iface wlan0 inet static`
  + `address/netmask/gateway`（镜像若无 ifupdown 则改自启脚本，
  把 `udhcpc` 换成 `ip addr add ... && ip route add default via ...`）
- nmcli：`nmcli con mod <连接名> ipv4.method manual ipv4.addresses 192.168.1.50/24 ipv4.gateway 192.168.1.1 ipv4.dns 192.168.1.1`

## 三、mDNS：手机访问 http://coffee.local

板端跑 **avahi-daemon** 把 hostname 广播到局域网，手机浏览器直接打
`http://coffee.local:8080/`（端口 8080 是 kiosk 默认，不可省）。

Debian/Ubuntu 类镜像：

```bash
sudo apt-get install avahi-daemon
sudo systemctl enable --now avahi-daemon
```

Buildroot 镜像：需要固件编进 `BR2_PACKAGE_AVAHI` + `BR2_PACKAGE_AVAHI_DAEMON`；
现成镜像多半没有，需要重编固件或拷预编译包。**不确定性：取决于固件构建权在谁手里。**

装好后 avahi 默认即按 `/etc/hostname` 广播，无需额外配置；
想显式声明 HTTP 服务可放 `/etc/avahi/services/http.service`（可选，不做也能解析）。

验证（板端执行）：`avahi-resolve -n coffee.local` 应返回板子 IP。

**手机端现实（重要）**：

- iPhone/iPad 的 Safari：原生支持 mDNS，`coffee.local` 直接可用 ✅
- 多数 Android 浏览器（Chrome）：**不解析 mDNS**，`.local` 打不开 ❌
- 结论：mDNS 是加分项，**不是唯一入口**，必须有下面的回退方案。

### 回退方案（明确立场）

**若 avahi 在板端镜像不便安装（Buildroot 要重编固件），不死磕**：
固定 IP（路由器绑定）+ 页面提示即可。点单屏/机身贴纸上直接印
`http://192.168.1.50:8080/` 与二维码，体验和 `.local` 没有本质差别。
后续换镜像/重编固件时再补 avahi 也不迟。

## 四、演示动线建议

1. 板子上电 → systemd 拉起 `cafe-backend`（deploy/）→ 屏幕浏览器
   （若配了自启）打开 `http://127.0.0.1:8080/` 当主点单屏
2. 顾客手机二选一：
   - **扫码**：机身贴二维码，内容为 `http://coffee.local:8080/`
     （iPhone 直接可用）；Android 为主场合理所当然用固定 IP 版
     `http://192.168.1.50:8080/`。二维码两个都贴或只贴 IP 版最稳
   - **直输地址**：贴纸同时印明文短地址
3. 页面打开即联机模式（`docs/04`：`http(s)://` 自动走 API，
   服务端挂了自动回退演示模式兜底）
4. 断网演练：拔掉 WAN（保留局域网）服务不受影响——全链路不依赖外网
   （插座本地 HTTP、页面本地托管），这是架构红线，演示时值得展示

## 五、上线 checklist

- [ ] 板子连 WiFi 成功，重插电源后自动重连
- [ ] 路由器绑定板子 + 两个插座（grinder/brewer）固定 IP
- [ ] `config.json` 的 actuators.host 与绑定 IP 一致
- [ ] hostname = coffee；（可选）avahi 运行且 `coffee.local` 可解析
- [ ] `systemctl status cafe-backend` active；`journalctl -u cafe-backend` 无异常
- [ ] 手机浏览器打开地址，下一单走完全流程（SIM 模式先验，再切真机）
