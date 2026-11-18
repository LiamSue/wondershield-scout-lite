# WonderShield Scout Lite — 开发者预览

本地命令行和 Python Engine 用于有限范围的 CSV、JSON、固定合同 SQLite 和基础 XLSX 文件发现及证据定位。它不提供通用业务问答或生产身份认证。索引会保存提取内容，只应处理有权使用的数据并放在受控位置。本包的示例均为虚构数据。

项目主体为上海素旺达数据科技有限公司 / Shanghai Suewonder Data Technology Co., Ltd.；项目材料的许可证见 `LICENSE`，归属与外部材料说明见 `NOTICE` 和 `THIRD_PARTY_NOTICES.md`。

## 离线构建、安装和运行

从全新的源码目录执行。需要已有 Python 3.11 和该环境已有的 pip；构建和安装不下载依赖。`dist`、`.venv` 和 `demo_run` 须事先不存在。构建器校验运行代码和声明的哈希，安装器校验 wheel 并使用已有 pip 的离线模式。

```sh
python3.11 -I -B tools/build_wheel.py --output dist
python3.11 -I -B tools/install_offline.py dist/wondershield_scout_lite-0.1.0.dev2+internalxlsx1-py3-none-any.whl .venv --sha256 fa5b1d4129c4aa466b180eb7ce8c498d4bf633f4b5c8bf20b6812c36f2e65c62
source .venv/bin/activate
python -I -B examples/prepare_demo.py demo_run
cd demo_run
python -I -B -m scout_lite_preview --config config.json --request request.json --sync
```

示例工作簿是合成 OOXML，不是经过本轮验证的 Microsoft Excel 另存文件。结果可核对 `订单!A1=华星` 和 `复核!A1=蓝舟` 的来源、工作表与格位；同时应看到 `INCOMPLETE` 与覆盖缺口。再次运行请使用新的源码副本或输出目录。

检查 JSON 的 `status`、`reason`、`coverage.complete`、`coverage.gaps`、`complete_scope`、`absence_proven` 与证据。CLI 退出码 0 不保证结果完整或授权通过。`INCOMPLETE`、`UNKNOWN`、`AMBIGUOUS`、`DENIED` 不表示没有匹配；通用的图表未覆盖提示也不表示每个工作簿都有图表。

## 范围

* **CSV**：有效 UTF-8，可含开头 BOM；要求有效且不冲突的表头和一致列数。引用字段内换行属于同一逻辑记录，无可靠关系时不跨记录拼接联系人。
* **JSON**：支持的叶值提供 JSON Pointer；有深度与容量限制，未声明关系时不拼接不同对象。
* **SQLite**：只读固定 `records` 和 `source_state` 表的合同，不是任意表或通用 SQL。未提交 WAL/journal 或合同不符会拒绝或报告缺口。
* **基础 XLSX**：可对支持的单元格返回原始值、工作表、格位和来源；所有 XLSX 保守标记覆盖不完整。公式不会求值，日期数字不解释；图表、绘图、图片与 XLSX 字段分类未覆盖，不支持的单元格类型可能影响普通格返回。一次 inspect 最多 64 个证据单元，另有大小等资源上限。

PDF、OCR、Word、PPT、图片、视频、GUI、联邦、Gateway 和完整业务执行不在本包范围。真实 Excel 另存、GUI 和生产认证没有由这些合成检查证明；无法从合成案例推断总体准确率或自动放行未覆盖内容。
