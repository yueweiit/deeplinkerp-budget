# AI 导入银行流水到 DeepLinkERP

供财务、AI 工作流开发人员和 ERP 接口调用方使用。导入成功后，流水出现在所选公司、银行账户和交易月份的「银行日记账」中。接口登记日记账收入和支出，不发起银行付款，也不生成总账凭证。

**选择输入方式。** 有原始 CSV 或 XLSX 时，直接传原文件。ERP 会解析金额、余额和银行流水号，并可保留既有日记账模板。只有 PDF、图片或其他格式时，由调用方的 AI 提取逐笔明细，再提交 JSON。两种方式使用同一账簿和流水去重规则。文件预览可调用 DeepSeek 识别脱敏表头。交易金额和余额由 ERP 从原文件读取和校验。

**一次导入分三步。** 先选公司和银行账户，再预览笔数、收支合计及问题行，最后确认写入。取得预览回执只表示解析通过。只有确认接口返回操作编号，才表示这次操作已被 ERP 接受。

[机器可读的 OpenAPI 3.1 文件](bank-journal.openapi.json) · [打开 ERP 银行日记账](https://deeplinkerp.com/desk/china-cash-journal)

## 接口与身份

正式站前缀为 `https://deeplinkerp.com/api/method/`。将下表中的方法名直接拼在前缀后。POST 请求的类型为 `application/json`，成功响应的数据在 `message` 内。

| 用途 | HTTP 方法 | 方法名 |
| --- | --- | --- |
| 取得有权使用的公司、账簿和当前月份 | GET | `budget.api.bank_journal.get_options` |
| 创建缺少的银行账簿 | POST | `budget.api.bank_journal.create_book` |
| 预览 AI 提取的明细 | POST | `budget.api.bank_journal_files.preview_rows` |
| 确认 AI 明细并写入账簿 | POST | `budget.api.bank_journal_files.apply_rows` |
| 预览原始 CSV／XLSX | POST | `budget.api.bank_journal_files.preview_import` |
| 确认原文件并写入账簿 | POST | `budget.api.bank_journal_files.apply_import` |
| 按账簿和月份读回流水 | GET | `budget.api.bank_journal.get_month` |

使用 ERP 接入用户的 API Key 和 API Secret。请求头格式为 `Authorization: token <API_KEY>:<API_SECRET>`。由调用系统的凭据配置注入这两个值，不要放进模型提示词、共享文档或流水文件。认证方式见 [Frappe REST API](https://docs.frappe.io/framework/user/en/api/rest)。

用户需有目标公司的访问权限和 `CN Cash Journal Entry` 的读取权限，新建账簿和确认导入还需创建权限。若需关联、补充已有流水，还需对应记录的写权限。预览和确认使用同一个 ERP 用户。

## 在 AI 工作流里选择公司和账户

1. 在支持 OpenAPI 的工具中导入本文的 OpenAPI 文件。给工具配置自定义认证请求头，名称填写 `Authorization`，值填写 `token <API_KEY>:<API_SECRET>`。认证信息放在工具的凭据设置中。
2. 每次导入先调用 `getJournalOptions`。用 `message.companies` 生成公司选项，包含尚未建账簿的已授权公司。用户选公司后，从该公司下的 `message.books[].account` 生成账户选项，并用该项的 `name` 作为账簿标识。公司改变时重新选择账户。
3. 目标账户缺少账簿且 `can_create=true` 时，原始文件可以直接按公司预览，确认时一起建账。结构化 JSON 明细仍可先调用 `createJournalBook`，再用返回的 `message.book` 继续。期初资料缺失或有冲突时，先核实具体字段。
4. CSV 或 XLSX 调用 `previewJournalFile`，从其他格式提取的结构化明细调用 `previewJournalRows`。显示选定的公司、银行账户、账簿币种、笔数和收支合计。用户确认后再调用对应的 `applyJournalFile` 或 `applyJournalRows`，并保留原请求和回执。

专用接入用户通过 `Bank Journal Import API` 角色限制到本页列出的接口。管理员用 ERP 的 `User Permission` 逐一授权目标公司，选择 `Company`，启用「适用于所有单据类型」和「隐藏下级」，再核对该公司的可用账簿。

## 创建账簿

使用 `createJournalBook` 时，`company` 取自 `message.companies`，`account` 填该银行账户的完整名称。`currency` 从 `getJournalOptions` 返回的 `currencies` 中选择。`opening_date` 为开始记账的日期，`opening` 为该日期首笔交易发生前的原币余额，使用十进制字符串。确认银行余额为零时填 `"0.00"`。

```json
{
  "company": "示例公司",
  "account": "示例银行基本户",
  "currency": "CNY",
  "opening": "1000.00",
  "opening_date": "2026-11-01"
}
```

成功返回 `message.book`、`message.company` 和 `message.account`，可直接用于流水预览。重复提交同一公司、账户和期初设置时，ERP 返回原账簿。若同名账簿的币种、启用日期或期初余额不同，ERP 返回冲突，按财务确认的账簿继续。

## JSON 明细的请求

取 `get_options` 返回的 `message.books`，按用户选择的 **公司全称＋银行账户名称** 精确匹配。用匹配项的 `name` 或新建返回的 `book` 作账簿标识，并把其 `company` 和 `account` 原样传给接口。不同公司可能使用同名账户，禁止取列表第一项作为默认账簿。

下例的公司、账户、流水和编号均为演示数据。实际调用应使用目标环境返回的账簿标识。

```json
{
  "book": "从 get_options 取得的账簿 name",
  "company": "示例公司",
  "account": "示例银行基本户",
  "currency": "CNY",
  "rows": [
    {
      "date": "2026-11-16",
      "income": "100.00",
      "bank_ref": "DEMO-000001",
      "summary": "客户回款",
      "balance": "1100.00"
    },
    {
      "date": "2026-11-16",
      "expense": "2.50",
      "bank_ref": "DEMO-000002",
      "summary": "银行手续费",
      "category": "费用",
      "level2": "财务费用",
      "balance": "1097.50"
    }
  ]
}
```

示例的交易前余额为 1,000.00 元。若在测试账簿联调，请确保其交易前余额与示例相符。

| 行字段 | 如何填写 |
| --- | --- |
| `date` | 必填，银行交易日期，格式为 `YYYY-MM-DD`。 |
| `income`、`expense` | 每行一方为正金额字符串，如 `"2.50"`。另一方省略或填 `null`。金额最多两位小数。 |
| `balance` | 可选，本笔交易后的余额，允许负数和零。原单缺少余额时省略或填 `null`。 |
| `bank_ref` | 银行交易流水号。原文传字符串，保留前导零，最长 140 字符。银行流水号与钉钉单号分开。 |
| `refs` | 钉钉审批单号的字符串数组，最多 40 个。ERP 根据单号查原单，调用方无需传链接。 |
| `summary` | 银行摘要。按原文保留，供财务核对并参与缺少流水号时的去重。 |
| `category`、`level2`、`level3`、`project` | 类别、二级明细、三级明细、所属项目。没有依据的分类可省略。 |
| `invoice`、`dept` | 可选的发票寄送信息、归属部门。 |

顶层 `currency` 填所选账簿返回的币种，例如 `MXN`。每本账只收本币种流水，所有金额为账户原币。金额用无千分位、无币符的十进制字符串。编号始终使用文本。AI 遇到收支方向不清、账号归属冲突、日期或金额识别不清时，应把原文交给操作人核对。一次传同一账簿 1 至 10,000 笔，明细数组紧凑编码后上限 6 MiB。按银行原单顺序提供，同日顺序会影响余额校验。

### 预览、确认和回读

1. 将上述请求 POST 至 `preview_rows`。检查 `error_count`、`errors`、`row_count`、`income_total`、`expense_total`，并记录 `preview_id` 和 `version`。全批收支合计与原单核对。预览最多展示 100 笔和 100 项问题，合计覆盖整批已解析的行。有行被拒绝时，不能用这些合计代替原单总额。JSON 的问题行号从 1 开始。
2. 在发送确认前，保存**完整预览请求、预览回执、本次唯一的 request_id**。生成一个 UUID 作 `request_id` 即可。确认请求沿用完整原始明细，额外加入 `preview_id`、`version`、`request_id`，POST 至 `apply_rows`。只要存在错误或缺少有效的 `preview_id`，就先修正并重新预览。
3. 成功回执如下。保存 `operation`，其中 `created` 为新建笔数，`matched` 为与既有流水匹配的笔数，`skipped` 为已有相同导入记录而跳过的笔数。使用 `get_month?book=<账簿标识>&month=2026-11` 回读对应月份，核对 `opening`、`income`、`expense`、`closing` 和明细。省略 `month` 时只读当前月。

```json
{"message":{"operation":"示例操作编号","version":8,"created":2,"matched":0,"skipped":0}}
```

### 可运行的调用示例

以下命令适用于 Bash，需要 curl、jq 和 Python 3。在调用环境中配置 `ERP_API_KEY`、`ERP_API_SECRET`、`ERP_COMPANY`、`ERP_ACCOUNT`。准备一个 `rows.json`，内容是上例的 `rows` 数组。联调示例请在测试站执行，将 `ERP_BASE` 指向该站点。

```bash
set -euo pipefail
: "${ERP_API_KEY:?}" "${ERP_API_SECRET:?}" "${ERP_COMPANY:?}" "${ERP_ACCOUNT:?}"
base="${ERP_BASE:-https://deeplinkerp.com}/api/method"
umask 077
work=$(mktemp -d)
auth="Authorization: token ${ERP_API_KEY}:${ERP_API_SECRET}"
rpc() {
  curl --silent --show-error --fail-with-body -H "$auth" -H 'Accept: application/json' "$@"
}
post() {
  rpc -H 'Content-Type: application/json' --data-binary "@$2" "$base/budget.api.bank_journal_files.$1"
}
rpc "$base/budget.api.bank_journal.get_options" |
  jq -e --arg c "$ERP_COMPANY" --arg a "$ERP_ACCOUNT" '
    [.message.books[] | select(.company == $c and .account == $a)] |
    if length == 1 then .[0] else error("找不到唯一的公司和账户") end
  ' > "$work/target.json"

jq -n --slurpfile t "$work/target.json" --slurpfile rows rows.json '
  {book:$t[0].name,company:$t[0].company,account:$t[0].account,currency:$t[0].currency,rows:$rows[0]}
' > "$work/input.json"
post preview_rows "$work/input.json" > "$work/preview.json"
jq '.message | {company,account,row_count,income_total,expense_total,error_count,errors}' "$work/preview.json"
```

下段会写入日记账。在操作人确认账簿、笔数和金额后，在同一终端执行。若确认请求超时，保留工作目录，使用同一份 `confirm.json` 重发最后的 POST。

```bash
jq -e '.message | .error_count == 0 and (.preview_id | length > 0)' "$work/preview.json" >/dev/null
request_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
jq --slurpfile p "$work/preview.json" --arg id "$request_id" '
  . + {preview_id:$p[0].message.preview_id, version:$p[0].message.version, request_id:$id}
' "$work/input.json" > "$work/confirm.json"
post apply_rows "$work/confirm.json" > "$work/receipt.json"
jq '.message | {operation,version,created,matched,skipped}' "$work/receipt.json"
```

## 原始 CSV 或 Excel

`preview_import` 的请求体包含 `book`、带扩展名的 `filename` 和 `content_base64`。可选 `mapping` 形如 `{"date":0,"summary":2,"expense":5}`，整数为从 0 开始的列序号。传原始文件字节的标准 Base64，保留编码和换行。来源表明确标有账户时，核对预览中的 `account_label` 与选定的账户。通用流水可能没有这个字段的值，应以银行原单和用户选择为准。

导入新账簿时，传 `company` 并省略 `book`。传 `use_ai=1` 可为陌生表头启用 DeepSeek，传 `review_offset=0` 读取首屏可编辑的流水。新账簿需明确传 `currency` 才可确认。`suggested_currency` 是文件中有明确依据的建议，单独的 `$` 需要用户选择。已有账簿直接用其币种。`book_settings` 提供账户、币种、文件期初及启用日的建议值。可在预览中修改 `opening_date`、`opening` 后重新校验。调整期初会显示 `opening_adjustment`，原表余额用于核对，账簿余额按确认的差额计算。缺项集中在 `book_issues`。需要修改流水时，通过 `edits` 按原表行号提交修改，收支两列一起传。提交确认沿用同一公司、文件、账簿参数、修改值及返回的 `header_row`、`mapping_used`（作为 `mapping`）。确认成功后，`message.book` 是新账簿编号。取消预览即可放弃本次建账。

可以复用上例的 `work/target.json` 和 `post` 函数。下面的 `FILE` 是本地文件路径。

```bash
python3 - "$work/target.json" "$FILE" > "$work/file-input.json" <<'PY'
import base64, json, pathlib, sys
target = json.loads(pathlib.Path(sys.argv[1]).read_text())
file = pathlib.Path(sys.argv[2])
print(json.dumps({"book": target["name"], "filename": file.name,
                  "content_base64": base64.b64encode(file.read_bytes()).decode("ascii")}))
PY
post preview_import "$work/file-input.json" > "$work/file-preview.json"
jq '.message | {account_label,headers,suggested_mapping,needs_mapping,row_count,income_total,expense_total,error_count,errors}' "$work/file-preview.json"
```

- `needs_mapping=true` 时，根据 `headers` 补上 `mapping` 并重新预览。仅在 `needs_mapping=false`、`error_count=0` 且有 `preview_id` 时确认。确认沿用相同文件、参数和列映射，加上此次预览的 `preview_id`、`version` 及新的 `request_id`，调用 `apply_import`。跨列日期按 `mapping_used` 中的 `year`、`month`、`day` 合成，也支持单列 `date`。
- CSV 支持 UTF-8（含 BOM）和 GB18030。XLSX 需仅有一个可见工作表，系统核对受支持的逐笔滚动余额公式。无法核实的公式会在预览中提示。单文件上限 6 MiB。原件按文件方式导入，才能保留受支持的原表模板；JSON 只包含提取后的明细。

有日期和说明、原始收支均为空的行会保留在预览。用户可补金额，或把明确跳过的原表行号放进 `excluded_rows`，例如 `[274,275]`。预览与确认传同一列表；收支有值的行须完整核对。

已有账簿的文件导入支持两种方式。专用 AI 导入账号沿用 `mode=append`（默认），相同来源的流水自动去重。持有本公司流水新增、修改及删除权限的财务用户，在页面选「按文件日期覆盖」时，预览和确认都传 `mode=replace_range`。区间为核对后第一笔至最后一笔流水日期，含首尾和中间日期。预览中的 `impact` 给出旧、新笔数和区间末余额。修改日期或切换方式需要重新预览。区间末余额改变时，后续流水的滚动余额随之重算。

确认覆盖后，操作人可在同一账簿点「撤回上次覆盖」整批恢复。若账簿又有修改，需先按时间倒序撤回较新的操作，每位操作人撤回自己的操作。预览、取消和校验失败均不写账。

## 异常与重试

| 调用结果 | 客户端处理 |
| --- | --- |
| 预览 HTTP 200，但 `errors` 有内容或需列映射 | 展示问题行并修正，重新预览整批。该批没有写入。 |
| HTTP 401／403 | 检查接入用户凭据、公司权限和日记账权限。 |
| 预览签名无效、公司或账户不匹配 | 重新选定目标账簿并预览。文件和 JSON 两种模式各用各自的预览回执。 |
| `TimestampMismatchError` | 其他操作已更新账簿。重新预览并核对最新数据后发起新的确认。 |
| 收支不平、原流水已变化、重复的银行流水号或无法唯一匹配 | 检查原表、期初余额、同日顺序及完整银行流水号。确认中出错会整批回滚。 |
| 写入超时、断连或响应丢失 | 同一用户向同一接口重发完全相同的确认请求，保留原 `request_id` 和版本。已成功时会返回原操作回执。 |
| 同一 `request_id` 已用于不同内容 | 查调用方保存的请求记录。已经修改的数据需重新预览，并为新操作分配新 ID。 |

`request_id` 范围为 8–100 位英文字母、数字、下划线或连字符。服务端在账簿锁内检查回执和版本，再落账。已有流水优先依据银行流水号识别；缺少流水号时会使用交易日期、收支、余额、摘要等证据。跨文件证据不足时，交由财务核对。同一张钉钉单可以对应多笔银行付款，不能直接把钉钉单号当成银行流水唯一键。

HTTP 错误体可能包含 `exc_type` 和面向用户的消息。业务校验和版本冲突都可能返回 417，客户端应同时读取错误类型和消息。仅将必要字段显示给操作人，保留失败批次与原文件的对应关系以便复核。
