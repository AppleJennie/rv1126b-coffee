# GitHub 操作手册（版本管理 / 文档整理）

> 给任何在本仓库做"整理 + 提交 + 推送"的操作者（AI 或人工）。读完即可独立操作，
> 不需要重新摸索环境。本手册本身也归它管：规范有变化就更新本文件。

## 一、环境档案（已验证事实，直接信任，别重踩坑）

| 项目 | 值 |
|---|---|
| 仓库根目录 | `~/rv1126b`（终端别名 `cdrv`） |
| 分支 | `main`（本地 + 远程同名） |
| GitHub | 账号 `AppleJennie`，仓库 `rv1126b-coffee`（Public），远程名 `origin`，走 SSH |
| 认证 | `~/.ssh/id_ed25519`（公钥已挂到 GitHub 账号） |
| git 版本 | 2.25.1 —— **没有** `git init -b`、`git switch`、`git restore`，用旧语法 |
| 提交身份 | 仓库本地配置 `applejennie <applejennie@localhost>`（非全局，够用，勿改全局） |
| 未安装 | `gh` CLI（用不了 `gh auth`/`gh repo`，一律走 git+SSH） |

### 网络坑（已踩过，2026-08-24）

本机网络可能把 `github.com` 解析到 `::1`（DNS 污染），导致 SSH 撞上本机自己的
sshd、报 `REMOTE HOST IDENTIFICATION HAS CHANGED`。**已在 `~/.ssh/config`
固定真实 IP 绕过**：

```
Host github.com
    Hostname 20.205.243.166
```

GitHub 官方 ECDSA 主机密钥指纹（只有这一个是真的）：

```
SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM
```

## 二、推送失败排查顺序（严禁第一步就删 known_hosts）

1. `ssh -T git@github.com` → 正常应回 `Hi AppleJennie!`
2. `getent hosts github.com` → 若返回 `::1` / `127.x`，说明 DNS 又被污染：
   给 `~/.ssh/config` 里的 `Hostname` 换一个当时真实可达的 GitHub IP
   （先 `ssh-keyscan -t ecdsa <候选IP>` 拿指纹，**必须等于第一节那串官方指纹**才可用）
3. 任何"主机密钥已改变"警告：按上面核对指纹，对不上就停止操作并报告用户，
   **不要**用 `StrictHostKeyChecking=no` 或删 known_hosts 强绕

## 三、日常提交流程

```bash
cdrv
git status                 # 自查：哪些文件变了，有没有不该提交的
git diff                   # 过一眼改动
git add -A
git commit -m "摘要（≤50字）

- 要点1
- 要点2"                 # 中文，首行说清做了什么，正文列要点
git push origin main       # 日常推 main 即可，不用带 --tags
```

`.gitignore` 已排除：`__pycache__/`、`*.o`、C 编译产物、`reference/qt_comp|qt_basic`
（133M 厂商资料）、`downloads/`、`transfer/`。新增大文件前先想想要不要进库。

## 四、版本号与打 tag

- 格式 `vX.Y.Z`：**Z**=文档/小修补；**Y**=模块级里程碑；**X**=整机大版本
- 已定义里程碑：
  - `v0.1.0` 仿真验证版（2026-08-24 全模块回归通过）
  - `v0.1.1` 文档整理版（docs/ 收拢 + 总索引）
  - 下一节点 `v0.2.0` = 舵机到货、`servo_tool scan` 真机识别成功
  - `v1.0.0` = 整机联调通过
- 打 tag：

```bash
git tag -a vX.Y.Z -m "版本说明"
git push origin main --tags
```

- 每个里程碑必须在 `docs/03-交付文档.md` 第七节"版本记录"追加条目
  （日期、范围、验证结果、已知待办）

## 五、文档整理规范

- **所有文档只放 `docs/`**，代码目录里不再留 README.md
- 总体文档：两位数字编号 + 短名（`01-快速上手.md` … `05-GitHub操作手册.md`），
  按阅读顺序编号
- 模块文档：`docs/modules/<模块名>.md`，与 `projects/` 下代码目录同名；
  文件顶部必须有一行 `> 代码位置：projects/xxx/`（相对路径以代码目录为基准）
- 新增/改名/移动文档后必做三件事：
  1. 更新 `docs/README.md` 索引表
  2. 全库 grep 旧文件名/旧路径，把引用（含代码注释、字符串）全部改到新位置
  3. 涉及结构变化时在 `03-交付文档.md` 版本记录里记一笔
- 文档里的命令必须是**在这台机器上真实跑过**的，没跑过的标"未验证"

## 六、提交前回归清单（仿真环境全绿标准）

```bash
cd ~/rv1126b/projects/servo_bus && make clean && make        # 零警告
cd ~/rv1126b/projects/coffee_fsm && python3 fsm.py simulate  # exit=0
python3 wifi_switch.py                                       # 自测全部通过
cd ~/rv1126b/projects/ai_host && python3 host_fsm.py simulate
cd ~/rv1126b/projects/kiosk_server && python3 kiosk_server.py --simulate --port 8090 &
curl -s http://127.0.0.1:8090/api/menu                       # 有菜单 JSON
curl -s -X POST http://127.0.0.1:8090/api/order \
  -H 'Content-Type: application/json' \
  -d '{"drink_id":1,"opts":{"cup":"large"},"qty":2}'         # drink_id 必须是 int
```

注意：`drink_id` 传字符串 `"1"` 会报 `bad_drink`（menu.json 里 id 是 int），
这是接口约定不是 bug。

## 七、安全红线（不可违反）

- `~/.ssh/id_ed25519`（**私钥**）永不复制、永不外发、永不入库；换机器 = 生成新钥匙对加新公钥
- 密码、token 一律不写进仓库和文档；发现泄露先改密再清理
- 禁止 `git push --force`；禁止 `git reset --hard` / `git clean -f` 等破坏性操作，
  除非用户明确点名要求
- `reference/` 是厂商/参考工程资料，只读，不整理不改动
- 不安装 `gh`、不改全局 git 配置，除非用户要求

## 八、30 秒上手版（给新接手者）

1. 读本手册 + `docs/README.md`（文档地图）
2. `ssh -T git@github.com` 确认连通（异常走第二节）
3. 按第三节提交、第四节打版本、第五节整文档、第六节回归、第七节守红线
