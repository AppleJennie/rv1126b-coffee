# deploy/ —— 板端部署文件（TASK 30）

`cafe-backend.service`：咖啡机器人后端（kiosk_server）的 systemd 单元，
实现开机自启、失败自动重启、journal 日志、网络就绪等待。

## 板端与用户 VM 的路径差异

| | 板端（RV1126B） | 用户 VM（开发机） |
|---|---|---|
| 仓库路径（约定） | `/home/rock/rv1126b` | `/home/applejennie/rv1126b` |
| service 里三处路径 | `User=rock`、`WorkingDirectory`、`ExecStart` 即为此路径 | 不要在 VM 上 enable 本服务 |
| 用途 | 真机常驻运行 | 仅编辑/验证单元文件语法 |

路径不同时，改 service 里 `User` / `WorkingDirectory` / `ExecStart` 三处即可，
其余无需动。

## 四条命令

```bash
# 1) 安装（在板端仓库根目录执行；装完改单元文件后需重新 daemon-reload）
sudo cp deploy/cafe-backend.service /etc/systemd/system/
sudo systemctl daemon-reload

# 2) 启用并立即启动 / 停止 / 重启
sudo systemctl enable --now cafe-backend     # 开机自启 + 现在启动
sudo systemctl stop cafe-backend
sudo systemctl restart cafe-backend

# 3) 查状态与日志
systemctl status cafe-backend
journalctl -u cafe-backend -f                # 实时跟随
journalctl -u cafe-backend -b                # 本次开机以来的全部
journalctl -t cafe-backend                   # 按 SyslogIdentifier 查（等价）

# 4) 卸载
sudo systemctl disable --now cafe-backend
sudo rm /etc/systemd/system/cafe-backend.service
sudo systemctl daemon-reload
```

## 模式选择

service 里 `ExecStart` 默认 `--mode HYBRID`（设备真/假由
`projects/coffee_fsm/config.json` 的 devices 段决定）。板端首启验证服务本身时
建议先改成 `--mode SIM`（全模拟、不碰硬件），确认自启/日志/重启都正常后再切回。
全真硬件用 `--mode REAL`（需先 teach 位姿 + 手眼标定）。

## VM 上的验证说明

开发 VM 是容器环境，没有运行中的 systemd（`systemctl` 不可用），**无法在 VM 上
真测自启**。已做的替代验证：

- `systemd-analyze verify deploy/cafe-backend.service`（若本机有该工具）做静态检查
- ini 语法人工自查（段头/键值/无非法字符）

真机首启 checklist：`daemon-reload` → `enable --now` → `status` 看 active →
`journalctl -u cafe-backend -f` 看到 `[kiosk]` 启动日志 → 浏览器访问
`http://<板IP>:8080/` → 重启板子确认自动拉起。
