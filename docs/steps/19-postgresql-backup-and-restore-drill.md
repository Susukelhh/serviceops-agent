# 第十九步：PostgreSQL 备份、隔离恢复与完整性验证

## 现实问题

“数据库放在 Docker 具名卷里”只能防止普通容器重建丢数据。如果硬盘损坏、具名卷被误删、迁移脚本
破坏数据，卷本身也会一起出问题。备份又不能只看文件是否存在：一个截断、损坏或从未恢复过的文件，
出事时可能完全不可用。

本步完成下面的安全闭环：

```text
真实 serviceops 数据库（只读导出）
       │
       ▼
pg_dump custom 归档 → .partial → 原子改名
       │
       ├─ SHA-256 文件指纹
       ├─ pg_restore --list 可读性检查
       └─ PostgreSQL 版本、大小、表级有限指纹清单
       │
       ▼
随机临时数据库 serviceops_restore_drill_xxxxxxxxxxxx
       │
       ├─ 单事务 pg_restore
       ├─ 表集合、行数、内容指纹逐表比较
       └─ finally 删除临时数据库
```

## 三个第一次出现的词

| 术语 | 白话解释 | 项目里的作用 |
|---|---|---|
| 逻辑备份 | 把数据库里的表结构和数据整理成可重建档案 | `pg_dump --format custom` |
| 原子改名 | 要么看到完整文件，要么看不到，不出现半成品冒充正式文件 | `.dump.partial` 完成后 `os.replace` |
| 恢复演练 | 不碰现用数据库，另开一间临时档案室实际还原 | 随机临时数据库 + `pg_restore` |

PostgreSQL 官方说明 custom 格式必须用 `pg_restore`，它支持选择和重排对象且默认压缩；`pg_dump`
会产生数据库某个时刻的一致快照。官方也强调生产备份有 SQL dump、文件系统备份和连续归档三类方案，
各自适用范围不同。

## 为什么不能直接恢复到 serviceops

恢复会执行建表、写数据、建索引和触发器。如果目标填错成当前业务库，可能覆盖或混合正在使用的数据。
本项目没有提供 `--target-database` 参数，而是在代码内部生成：

```text
serviceops_restore_drill_ + 12位小写十六进制随机值
```

创建、恢复和删除三个动作前都校验完整正则。`serviceops`、只有前缀的名称、带分号或额外字符的名称
全部在 Docker 命令执行前失败。删除动作位于 `finally`，即使恢复内容不同也会清理临时库。

## 为什么使用两种哈希

- 备份文件使用 SHA-256：检查文件复制、保存后是否发生任何字节变化；
- 数据表使用组合 MD5：只做同一次恢复演练中的快速内容相等比较，不用于密码和安全签名。

表级报告只保存表名、行数和整表组合指纹，不保存订单、用户原文、审批备注、Token 或数据库密码。
备份文件本身仍然包含数据，所以 `data/backups/` 同时加入 `.gitignore` 和 `.dockerignore`。

## 真实本机结果

2026-08-21，在当前双 Agent + PostgreSQL 18.4 Compose 环境中：

| 项目 | 结果 |
|---|---|
| 备份格式 | PostgreSQL custom |
| 文件大小 | 160047 bytes |
| 归档目录项 | 36 |
| 公共表数量 | 8 |
| 源库/恢复库指纹 | 全部一致 |
| 临时恢复数据库 | 已删除 |
| 演练耗时 | 10.507 秒 |
| SHA-256 | `bf8f22034e3f850a5cfbba71cc00755e4efcb0c5250905e7334e2864e400463a` |

八张表为 Alembic 版本表、退货、Outbox、审批审计，以及 LangGraph 的四张 Checkpoint 表。数据库查询
没有发现任何 `serviceops_restore_drill_%` 残留库。

## PyCharm 验证

先启动 Docker：

```powershell
docker compose up --detach --wait --wait-timeout 120
```

运行配置：

- Script path：`D:\serviceops-agent\examples\19_postgres_backup_restore_drill.py`；
- Parameters：留空；
- Working directory：`D:\serviceops-agent`；
- Environment variables：`PYTHONUTF8=1`；
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`。

运行成功应输出：

```text
PASS 第19步：备份可读取、可恢复、逐表一致，临时数据库已清理
```

检查没有残留临时库：

```powershell
docker compose exec -T postgres psql -U serviceops -d serviceops -tAc `
  "SELECT datname FROM pg_database WHERE datname LIKE 'serviceops_restore_drill_%';"
```

输出为空即正常。固定报告位于：

```text
D:\serviceops-agent\data\runtime\postgres_step19_report.json
```

## 面试官可能追问

### pg_dump 时数据库还能写吗？

可以。PostgreSQL 的 `pg_dump` 会导出一个一致快照，不要求普通读写全部停机。本地教学脚本还在 dump
前后计算源库有限指纹；发现演练窗口内数据变化时不会声称“恢复库与当前源库完全相同”，而是要求在
安静窗口重跑。生产大库应根据恢复目标、负载和备份窗口设计正式策略。

### 有备份为什么还必须恢复演练？

文件存在只证明“写过文件”，不能证明格式完整、工具兼容、表/触发器能创建或数据能读回。演练把备份
恢复到隔离库并逐表比较，才给出“当前归档可恢复”的证据。

### 这是不是高可用？

不是。高可用关注主库故障时由副本快速接管；本步是灾难恢复中的逻辑备份验证。它没有异地存储、备份
保留策略、加密密钥、WAL 连续归档或任意时间点恢复。真正生产还应明确 RPO（最多能丢多久的数据）和
RTO（多久必须恢复服务），并由云数据库或专用平台执行自动备份、复制和告警。

### 为什么不用 `docker cp` 复制数据目录？

直接复制正在运行的 PostgreSQL 数据目录不一定得到可恢复的一致状态，而且与数据库主版本、文件系统
和 WAL 强耦合。当前小型项目使用可移植的逻辑备份；大库可根据官方方案选择文件系统备份、基础备份和
连续 WAL 归档。

## 本步文件

- `src/serviceops_agent/operations/postgres_backup.py`：安全备份与隔离恢复核心组件；
- `examples/19_postgres_backup_restore_drill.py`：PyCharm 一键演练入口；
- `tests/unit/test_postgres_backup.py`：危险目标、成功/失败清理和敏感目录测试；
- `.github/workflows/container-image.yml`：真实 Compose 备份恢复门禁；
- `data/backups/`：被忽略的敏感本地归档目录；
- `lessons/0011-backup-restore-in-plain-chinese.html`：通俗复习课。

## 一手资料

- [PostgreSQL 18：pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html)
- [PostgreSQL 18：pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html)
- [PostgreSQL 18：备份与恢复三类方案](https://www.postgresql.org/docs/18/backup.html)
