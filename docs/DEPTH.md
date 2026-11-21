# 深度登记 商圈客流设备重叠与人数区间估计

规模：中型。核心规则：无法识别个人时只输出满足区域交集约束的人数上下界；矛盾观测隔离而非伪造精确人数。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 设备采信 | 查看并复算设备采信 | `/api/m1_ingest` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py` |
| M2 区域映射 | 查看并复算区域映射 | `/api/m2_mapping` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 观测分桶 | 查看并复算观测分桶 | `/api/m3_versions` | `app/modules/m3_versions/rules.py` | `tests/test_versions.py` |
| M4 重叠约束 | 查看并复算重叠约束 | `/api/m4_segments` | `app/modules/m4_segments/rules.py` | `tests/test_segments.py` |
| M5 人数估界 | 查看并复算人数估界 | `/api/m5_calculate` | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py` |
| M6 日汇对账 | 查看并复算日汇对账 | `/api/m6_reconcile` | `app/modules/m6_reconcile/compare.py` | `tests/test_reconcile.py` |
| M7 报告归档 | 查看并复算报告归档 | `/api/m7_archive` | `app/modules/m7_archive/export_out.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 固定UTF-8失败 | 按编码链回退 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_legacy_encoding` |
| TRAP-02 | 表头BOM | 首列键失配 | 去除BOM | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom` |
| TRAP-03 | 引号内换行 | 分行误计 | csv逻辑记录 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_preserves_multiline_note` |
| TRAP-05 | 列数错位 | 静默补列 | WIDTH隔离 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_width_isolation` |
| TRAP-06 | 全角千分位 | float失败 | NFKC与Decimal | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_thousands` |
| TRAP-07 | 空值哨兵 | 空值当零 | MISSING隔离 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_missing_numeric` |
| TRAP-08 | 前导零ID | 编码失真 | 字符串保形 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_identifier_zeroes` |
| TRAP-10 | 浮点误差 | 累计失准 | Decimal求和 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_decimal_precision` |
| TRAP-14 | 同版冲突 | 最后覆盖 | 整组隔离 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_real_conflict_quarantine` |
| TRAP-18 | 三种空态 | 除零或伪成功 | 三个错误码 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_empty_three_states` |
| TRAP-26 | 旧版本复核 | 覆盖新结论 | 事务条件更新 | `app/store.py` | `tests/test_archive.py::test_stale_review` |
| TRAP-11 | 时区混用 | 同刻不同桶 | UTC归整 | `app/modules/m2_mapping/coerce.py` | `tests/test_versions.py::test_same_instant` |
| TRAP-19 | 三方约束矛盾 | 输出伪精确数 | 非负原子区域可行检查 | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py::test_three_way_contradiction` |
| TRAP-30 | 部分修正包 | 逐项并入把新旧观测混成正式证据 | 修正先入账本，整桶观测齐备才晋级 | `app/patch/ledger.py` | `tests/test_patch.py::test_partial_patch_keeps_formal_interval_and_logs_pending` |
| TRAP-31 | 修正重试 | 重复提交新增证据版本 | 内容哈希幂等，同版/低版 STALE_VERSION 拒绝 | `app/store.py` | `tests/test_patch.py::test_identical_patch_retry_is_idempotent` |
| TRAP-32 | 证据包切换失败 | 失败丢失补丁或旧区间 | 补丁先独立提交，切换事务回滚保留旧版 | `app/store.py` | `tests/test_patch.py::test_switch_failure_rolls_back_but_keeps_records` |
| TRAP-33 | 页面与导出证据漂移 | 两处各自重算口径不一 | 共用 active_evidence 与同一指纹 | `app/web.py` | `tests/test_patch.py::test_page_and_export_share_same_evidence` |

## 对账规则

输入行数=采纳+隔离+空行+重复表头+完全重复；业务总数与计算证据独立求和。隔离行保留来源行、ID、原因码与原值。

## 已知不覆盖

不做多租户权限和外部服务联调。行业规则以项目数据字典为准。文件上限2MB，逐行解析后在上限内完成分组计算。
