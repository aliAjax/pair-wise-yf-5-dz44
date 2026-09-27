# 护士换班服务

纯 Python 标准库实现(零第三方依赖, Python ≥ 3.10),按 **规则 / 存储 / 请求入口** 三层分离:

```
nurse_swap/
├── models.py    # 领域模型: Shift(护士/日期/起止时间)、SwapRequest、状态机
├── rules.py     # 规则层: 纯函数校验, 违反即抛 RuleViolation
├── storage.py   # 存储层: SQLite 持久化, 重启后数据仍在
├── service.py   # 服务层: 编排规则与存储(可注入时钟, 便于测试)
├── api.py       # 请求入口: HTTP 路由/参数解析/错误映射
└── errors.py    # 领域错误(404/409)
main.py          # 启动入口
tests/           # 36 个单元/接口测试
```

## 运行

```bash
python3 main.py                 # 默认 127.0.0.1:8000, 数据库 nurse_swap.db
PORT=9000 NURSE_SWAP_DB=/data/swap.db python3 main.py
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/shifts` | 创建班次 `{nurse_id, date, start_time, end_time}` |
| GET | `/nurses/{id}/shifts` | 按护士查询班次 |
| POST | `/swaps` | 提交换班 `{shift_id, nurse_id}`(须为班主持有人) |
| POST | `/swaps/{id}/accept` | 接替 `{nurse_id}` |
| POST | `/swaps/{id}/confirm` | 确认 `{nurse_id}`(先原护士、后接替人) |
| POST | `/swaps/{id}/withdraw` | 撤回 `{nurse_id}`(仅原护士, 完成前) |
| GET | `/swaps/{id}` | 查询单个换班申请(含班次) |
| GET | `/nurses/{id}/swaps?state=pending\|accepted\|completed` | 按护士查询换班(原护士或接替人视角), 可按状态过滤 |

示例:

```bash
curl -X POST localhost:8000/shifts -d '{"nurse_id":"nurse-a","date":"2026-09-28","start_time":"08:00","end_time":"16:00"}'
curl -X POST localhost:8000/swaps -d '{"shift_id":"<shift_id>","nurse_id":"nurse-a"}'
curl -X POST localhost:8000/swaps/<id>/accept  -d '{"nurse_id":"nurse-b"}'
curl -X POST localhost:8000/swaps/<id>/confirm -d '{"nurse_id":"nurse-a"}'   # 第一次确认
curl -X POST localhost:8000/swaps/<id>/confirm -d '{"nurse_id":"nurse-b"}'   # 第二次确认 → 完成
curl localhost:8000/nurses/nurse-a/swaps?state=completed
```

## 业务规则

**状态机**: `pending`(待处理) → `accepted`(待交接) → `completed`(已完成)

- **提交换班**拒绝: 班次不存在/非本人班次、**原班次已开始**、**该班次已有未处理换班**(待处理或待交接)。
- **接替**拒绝: 非待处理状态、接替自己的班次、原班次已开始、**接替人同日班次时间重叠**(首尾相接不算重叠)。
- **确认**: 接替后由**原护士先确认、接替人再确认**, 顺序错误或重复确认会被拒绝; 两人都确认才完成, 完成后班次归属变更为接替人。
- **撤回**: 第二次确认前(即完成前)原护士可撤回, 申请回到待处理, 接替人与**前一人的确认一并作废**, 之后可被重新接替。
- **持久化**: 全部数据存 SQLite, 重启后待处理、待交接、已完成换班均可按护士查询, 流程可继续推进。

错误映射: 资源不存在 `404`、违反规则 `409`、请求格式错误 `400`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 设计取舍

- 班次不跨天(要求 `start_time < end_time`),"已开始"按本地时间 `date + start_time <= now` 判定; 服务层时钟可注入, 测试可自由推进时间。
- 撤回语义为"退回到待处理"而非删除申请, 因此撤回后可由其他护士重新接替, 且此前的确认不会残留。
- 确认/撤回不额外校验班次是否已开始(规则只对提交与接替生效), 已达成一致的交接允许在班次开始后完成登记。
