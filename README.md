# 护士换班服务

零第三方依赖（Python 3.11 标准库），提供班次创建与护士间换班的完整流程。

## 运行

```bash
python3 main.py --host 0.0.0.0 --port 8000 --store data/nurse_swap.json
# 也可用环境变量 HOST / PORT / STORE_PATH
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 分层结构（规则 / 存储 / 请求入口分离）

| 文件 | 职责 |
| --- | --- |
| `nurse_swap/models.py` | 数据模型：`Shift`（班次）、`Swap`（换班单）及状态常量 |
| `nurse_swap/rules.py` | 纯业务规则：字段校验、同日时间重叠判定、班次是否已开始、是否存在未处理换班（无 IO） |
| `nurse_swap/storage.py` | 持久化：JSON 文件 + 临时文件 `os.replace` 原子写入，重启自动恢复 |
| `nurse_swap/service.py` | 业务编排：换班状态机，规则校验通过后在锁内事务性写存储 |
| `nurse_swap/webapp.py` | HTTP 入口：路由、JSON 编解码、错误码映射，不含业务规则 |
| `main.py` | 启动入口 |

## 换班状态机

```
pending（待处理）
   │  接替人 POST /accept
   ▼
handover / awaiting_original（待交接，等原护士确认）
   │  原护士 POST /confirm            ── 第二次确认前原护士 POST /withdraw
   ▼                                    └─► 回到 awaiting_original，
handover / awaiting_target                  原护士确认作废，需重新依次确认
   │  接替人 POST /confirm
   ▼
completed（已完成，班次转交给接替人）
```

- 接受后必须**原护士先确认、接替人后确认**，两人都确认才完成；
- 第二次确认（接替人确认完成）之前，原护士可撤回；撤回后两人需重新依次确认；
- 完成时班次的 `nurse` 才真正变更为接替人。

## 提交换班的拒绝规则

`POST /api/swaps` 在以下任一情况返回 `409`：

1. `target_schedule_conflict`：接替护士在同一天已有时间重叠的班次（首尾相接、不同日期不算重叠）；
2. `shift_already_started`：原班次已经开始（当前时间 ≥ 班次开始时间）；
3. `swap_in_progress`：该班次已有未处理（`pending` 或 `handover`）的换班单。

此外：非班次归属人提交 → `403`；接替人不能是本人、字段非法 → `400`。
接受和第二次确认时会再做一次“未开始 / 无重叠”防御性检查，防止提交后情况变化。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/shifts` | 创建班次 |
| GET | `/api/shifts?nurse=张三` | 查班次（可按护士过滤） |
| GET | `/api/shifts/{id}` | 查单个班次 |
| POST | `/api/swaps` | 提交换班 |
| POST | `/api/swaps/{id}/accept` | 接替人接受 |
| POST | `/api/swaps/{id}/confirm` | 当前轮到的人确认 |
| POST | `/api/swaps/{id}/withdraw` | 原护士在第二次确认前撤回 |
| GET | `/api/swaps?nurse=张三&status=handover` | 查换班单（按护士/状态可选，护士含原护士与接替人两种角色） |
| GET | `/api/swaps/{id}` | 查单个换班单 |

`status` 取值：`pending`（待处理）、`handover`（待交接）、`completed`（已完成）。

### 请求示例

```bash
# 创建班次（护士、日期、起止时间 HH:MM）
curl -X POST localhost:8000/api/shifts -H 'Content-Type: application/json' \
  -d '{"nurse":"王敏","date":"2030-05-10","start_time":"08:00","end_time":"16:00"}'

# 提交换班
curl -X POST localhost:8000/api/swaps -H 'Content-Type: application/json' \
  -d '{"shift_id":1,"original_nurse":"王敏","target_nurse":"李磊"}'

# 接替 -> 原护士确认 -> 接替人确认
curl -X POST localhost:8000/api/swaps/1/accept   -d '{"nurse":"李磊"}' -H 'Content-Type: application/json'
curl -X POST localhost:8000/api/swaps/1/confirm  -d '{"nurse":"王敏"}' -H 'Content-Type: application/json'
curl -X POST localhost:8000/api/swaps/1/confirm  -d '{"nurse":"李磊"}' -H 'Content-Type: application/json'

# 第二次确认前原护士撤回
curl -X POST localhost:8000/api/swaps/1/withdraw -d '{"nurse":"王敏"}' -H 'Content-Type: application/json'
```

错误响应统一形如：

```json
{"error": {"code": "target_schedule_conflict", "message": "护士 李磊 在 2030-05-10 已有时间重叠的班次"}}
```

## 持久化与重启

- 每次变更整体写入 JSON 文件（同目录临时文件 + `os.replace`，崩溃也不会留下半截文件）；
- 待处理、待交接、已完成三种换班单、班次归属（含已完成的转交）以及自增 ID 都会落盘；
- 进程重启后全部可查，待交接的换班单可从断点继续确认，待处理的换班单仍然占用名额。
