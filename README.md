# budget

预算与费用审批 app：把 `dingtalk-budget`（Express）与 `dingtalk-expense-sync`（TypeScript）
两个 Node 项目重写成一套 Python 实现，按 DeepLinkERP 的分层纪律组织：

> **命名**：app 按业务域命名（对齐 `china_finance` / `overseas_costing` / `mes_integration`），
> 不按数据源命名。钉钉与 `dingtalk_oa` 库只是数据来源，不是这个 app 的边界。

与新版 `deeplinkerp_branding` 联合安装且拥有全部统一页面权限时，主入口为“支出管理”（`Expense Management`），合并原 CN、MX 的管理统计入口。仅拥有原区域权限的用户及独立安装继续使用原入口。Sidebar、Desktop Icon 及每个 Page/DocType 仍按原生权限过滤，不额外授权业务数据。

原 CN **只读审批工作台**继续提供经营总览、预算执行、支出分析、报表中心、审批台账、数据核对，作为审批核对及配置入口保留。覆盖预算申报、运营支出、采购支出三类单据，保留审批原单入口。查看页面和刷新不会办理付款、记账、修改 OA 审批或同步预算台账。

页面实现位于 `public/js/cn_approval_workbench.js`、`public/js/cn_approval_management.js`、`public/css/cn_approval_workbench.css`，品牌 App 的 `china_expenses_page.js` 只负责选择并加载资源。复用四个现有 Page，Budget 新增预算执行与报表中心两个 Page；未新增会计业务表。`budget.setup.cn_navigation_items` 仅在六个 Page 都存在时启用新侧栏；品牌 App 的导航合并不会给新只读工作台插入手工日记账。侧栏种子文件直接定义新版六个页面与三个可折叠分组，避免标准元数据同步重新写入旧三项菜单。仅独立安装或 CN 页面尚未安装完整时，安装/迁移钩子显式生成兼容入口；品牌 App 未安装时不使用残留 CN Page 记录。独立 Budget、品牌 App 备用页面及既有财务服务保留各自权限。

ERPNext 的“预算”恢复在“会计”文件夹下，保留原生 Budget、Cost Center、Accounting Dimension、Cost Center Allocation、Budget Variance 五个入口。本 App 的 `setup.apply_budget_nav` 在安装/迁移时清理旧导航挂载，同步独立 CN 入口；`public/js/budget_navigation.js` 清理定制页面旧的 Budget 侧栏记忆，并在 CN 启用时于 Page 渲染前处理退役地址。旧菜单与 CN 旧页面挂载逻辑移除，旧收藏链接只转入新版，不在旧地址下展示新正文。DocType、台账数据及接口保留，不把 MX 排除明细自动写入国内财务数据。导航定义见 `budget/workspace_sidebar/china_operating_expenses.json` 和 `budget/desktop_icon/china_operating_expenses.json`。

模块文档：[docs/README.md](docs/README.md) — 分层、接口清单、配置、错误处理、时序。

## 统一支出管理（2026-11-24）

五个新 Page：`expense-management`（子公司总览）、`expense-analysis`（费用构成/近六个月）、`expense-budget`（公司预算执行）、`expense-details`、`expense-review`。首页默认当月、全部获授权子公司；仅提供月份、子公司、管理分类筛选，没有国家或展示币种选择。点击公司、类别或月份下钻，返回全部公司和原生侧栏切换保留月份。金额卡片/分类/趋势下钻使用可见的“仅已核实审批支出”范围，可切换全部状态；浏览器历史和重新加载保留选择。

- **管理口径**：费用按明确申请日期所在月份、来源状态 `approved`、有效公司归属和可核实原币金额统计。用户确认的例外：MX 来源表单没有申请日期字段时使用已有 OA 提交日期 `request_date`，明细与导出标识 `date_basis=submission`。日期字段存在但空白、异常或冲突时继续待核对，不用提交日期掩盖。旧 CN 接口日期规则不变；旧 MX 财务通过日期口径不变，不能与新的已审批管理金额直接等同。
- **唯一来源**：共享 `regional_expenses.management_projection()` 读取 CN/MX 分配，先按获授权公司过滤；审批的企业/实例身份去重后，补入未被共享分配接管的 CN 本地行。历史 MX 人工归属作为兼容例外读取该审批全部有效明细（含自动映射的同行），逐行检查公司及跨区权限，并保留排除与来源版本校验，不修改人工决定；已被共享分配接管的审批不再补读。仅有可见历史明细时也可展示、导出，但不据此宣称来源同步完整。预算申报独立保留，不能当支出相加。
- **管理分类**：MX 分配原本不保存 CN 管理分类；统一报表传入当前分类规则，按原始类别完整路径在读取时精确映射，不回写分配记录。已保存的 CN 分类决定保持不变；未知 MX 类别仍进入未分类和数据核对。
- **双币种**：人民币 CNY 为大字号主金额，比索 MXN 为小字号辅金额。两者分别从原币金额按该行申请月月初汇率计算，预算仍使用预算月。共享 `Conversion` 和 `reconcile_converted_lines()` 分配舍入尾差，计算使用完整分摊方案，因此只查看部分公司的结果也可与总表核对。API 金额使用十进制字符串；任一纳入行缺少某币种汇率时，该币种合计为 `null`，不返回部分合计或假零。原币、两个金额、汇率、日期来源均可查验和导出。
- **预算**：复用最新已审批公司/月预算规则。没有预算、预算版本不确定或金额缺失时，相应预算/差额/执行率不可用；不以另一公司的预算掩盖缺口。筛选“未分类”时复用 `unclassified_comparison()`，保留预算和支出金额，但差额、预计差额和执行率不可用，不生成缺预算或超支判断。预算页 Excel 独立导出同口径公司预算矩阵，其他页面按当前明细范围导出。
- **完整性**：未归属、无日期、金额问题留在数据核对；已核实支出旁明确提示排除数量，汇率缺失不冒充完整金额。来源同步不完整、两期数据待核对或缺少可比较历史时不显示变化率；当月使用同期已过天数，完整月份比较自然月。统计说明、日期来源及更新时间集中在“统计口径”弹窗。刷新只重读已有快照；使用已有自动归属重算守卫，不主动请求 OA 同步。
- **权限和兼容**：新 Page 与 API 均要求 `System Manager` / `Accounts Manager`，聚合、明细、导出共用授权范围；不向调用方返回完整分摊方案或其他公司的原始审批。`expense_navigation.boot()` 只在统一 Page 原生授权与侧栏完整时隐藏旧桌面图标，旧 URL 和原财务核对能力保留；统一页菜单改为同页同步设置，不跳往旧工作台，未扩大 `Accounts User` 权限。独立 Budget/版本不完整的站点保留原导航。

统一页通过 `budget/services/expense_sync.py` 和 `public/js/expense_sync.js` 提供一个 OA 同步入口、整体状态和“立即同步”；展开后查看模板进度及重试失败项。相同 `corp_id + process_code` 的审批模板只显示一行，只有全部已启用解析任务成功才算该模板成功；CN 整批扫描完成但未发布不能算更新成功。“最近完整更新时间”仅在所有启用模板成功、有更新时间且本地归属重算结束后提供；扫描中、失败或时间缺失时显示不可用，不把 MX 每页推进的时间当作整轮完成。失败保留原数据。`budget.api.expenses.get_sync_status` 返回汇总和不含模板码/企业范围的进度，原 `sources` 字段兼容保留。`enqueue_sync(source, retry_failed=False)` 每次仍仅提交一个来源，前端的统一按钮分别处理入队结果，避免 CN 入队事务重试影响 MX 请求；失败重试保留 CN 整批原子发布语义，MX 优先重试失败模板，分发任务本身失败且没有失败模板时重新分发。

`public/js/expense_sync_settings.js` 与 `services/expense_sync_settings.py` 提供一个原生配置弹窗，集中维护同步开关、去重模板、子公司映射与管理分类。2026-11-25 起统一采用整单审批口径，界面不再提供“处理规则”、财务节点名称或活动 ID。模板只需配置启停、审批类型、名称和流程编码，来源企业共用，单一授权企业自动带入；字段/分类映射折叠在模板高级设置中。整单 `COMPLETED + AGREE` 且公司、月份、原币金额等可核实才计入已审批支出，审批中、拒绝、撤销和终止不计入；财务节点提前通过不能替代整单通过，审批通过仍不代表付款。预算单只参与预算计算，申请月份及 MX 缺少申请日期字段时使用 OA 提交日期的既有规则不变。

自动处理复用原解析器：新增预算使用申请解析，新增费用使用费用明细解析，新增运营/采购使用两者；后端按审批类型决定，不信任客户端指定的处理规则。已有模板保留原来源身份、解析绑定、字段映射、独立显示名称及启停状态，避免扩展拉取范围或改变历史归属；所有已有模板在统一报表中仍遵循同一整单审批条件。历史模板停用而非删除。原来源开关不一致时保留“部分启用”状态。保存继续检查两来源版本和公共设置版本，同一事务写入，失败整体回滚，死锁重试整个命令。

共用模板的日期规则按 `corp_id + process_code` 精确匹配费用解析配置，不由先完成同步的来源决定：即使共享记录由 CN 首先创建，只要模板配置了费用解析，缺少申请日期字段时同样可以使用 OA 提交日期。停用模板保留历史规则，历史 MX 来源继续兼容；仅配置申请解析的 CN 模板及旧 CN 接口仍要求申请日期。`regional_expenses.management_date()` 由共享报表、历史 MX 人工明细、统一预览及管理模式保存预览复用。日期字段为空、无效或冲突、提交日期也缺失时继续提示期间异常；预览仅消除被有效管理日期替代的旧 CN 期间提示，其他异常保留。该修复在读取时生效，不改写来源维护方、人工分配或历史财务日期，不需要重拉 OA 或迁移历史数据。

`save_sync_settings(values)` 和 `preview_sync_template` 接受可选 `processing_mode="approval"`，新界面固定使用该值；省略时保留旧协议。公共默认值仍存于 `DefaultValue(parent="__budget_expense_sync", defkey="template_defaults")`，新版仅维护来源企业，旧处理规则默认值不影响自动选择。不存在新增数据表或字段。统一保存从服务器回填旧财务节点配置，不接受前端删除或覆盖；新增模板不猜测财务节点。品牌 App 的模板标准化及保存增加显式管理模式，允许节点为空，仍执行字段结构、企业授权、类型、重复、版本及历史保护检查。旧接口可继续保存此前创建的不含财务节点的管理模板，但旧模式创建模板或清空已有财务配置仍受原校验限制。

`services/expense_sync_preview.py` 显式读取同一模板最多 3 张 OA 审批，供所需解析器共同检查，返回一个审批数据预览，不重复统计样本。管理预览跳过财务任务解析，只检查审批数据及相关日期；没有样本时提示先保存为停用模板。保存新增/重新启用/改变解析配置的费用模板仍执行有界结构预览，既有模板数据不因打开配置而重新拉取。后端不向浏览器返回原始载荷或任务记录。流程编码在新增时去除首尾空白；历史身份不能移动或改类型。预览保留原行号，选中行、字段草稿、来源企业、时区或弹窗状态改变后，旧响应和旧错误均不能覆盖新状态。

旧 MX 页面及财务归期保留，管理预览的 `include_finance=False` 不修改默认财务解析，不将整单通过时间写入财务通过时间。统一报表对历史人工核对明细排除旧 `finance` 提示并按申请日期重新判断期间，金额/币种/公司/来源版本等异常继续阻止纳入，不修改人工决定和原记录。现有每 5 分钟调度、去重和失败保留数据机制继续复用。

本轮 699 项 Python 测试及 180 个子场景、65 项前端测试通过，覆盖配置自动选择、历史绑定及财务证据保护、原子保存、单次抽样预览、审批状态、金额与日期边界及旧 MX 解析。原生 Desk 用模拟命令验证中/英/西界面、新增模板无财务字段、预览和序列化保存；开发数据库事务验证四类模板实际保存不带入旧节点默认值、旧配置接口兼容及完整回滚，禁止真实 OA 请求和任务入队。对现有 696 条本地分析行做只读前后对比，公司/月份/原币审批金额及行数一致。测试数据已回滚，不代表生产部署或真实 OA 联调。

后续审查修复新增 42 个回归场景，先复现 CN 先同步导致缺少月份及预览误报，再验证两种同步顺序、运营/采购模板、明确/缺失/无效日期、缺少提交日期、历史日期元数据、停用模板及企业范围隔离。当前回归为 741 项 Python 测试、180 个子场景和 65 项前端测试通过；本次修复未修改站点配置、发起真实 OA 请求或执行数据迁移。

“删除所选模板草稿”仅移除本次打开后新增的行，已保存行继续受前后端保护；删除草稿和切换候选页不丢失其他未保存编辑。子公司映射的来源名称使用长文本控件，保持后端 500 字符上限。管理员可显式读取待映射来源组织、费用主体/部门及管理类别，每页 50 项；前两者复用原授权查询，类别使用统一报表的本地数据口径，包括 MX 历史人工归属。选择候选项后带入精确身份，子公司仍需管理员确认；类别路径以可读文字显示，原始 JSON 隐藏保存，不要求手写。重复添加、读取失败重试及过期响应均有处理。类别路径只增加到内部计算行，不扩大普通报表/明细的公开字段。

`get_data.metadata.sync_revision` 附带本次报表实际使用的 CN/MX 本地版本；轮询与成功展示的数据版本比较，不把状态响应当作报表已更新。失败或被新筛选替代的请求不确认版本，本地归属重算结束即使版本未变也重新读取；请求进行中不重复刷新。更新完成后保持月份/公司筛选及正在选择的月份弹层，离开页面停止轮询并关闭设置；旧请求不得覆盖新页面。

两个来源仍使用现有 OA 只读 HTTP 审批接口，不新增直连 OA 数据库配置。CN/MX 的原生 scheduler cron 均为 `*/5 * * * *`，不新增第二套重复拉取任务；CN 每个定时周期尝试同步，已有排队/运行中的任务继续复用，不再叠加“上次成功后满 300 秒”的条件，避免耗时扫描导致跳过下一周期。CN 页面自动触发仍保留 300 秒的过期和重试间隔，原排队/运行租期不缩短。MX 调度及模板分页采用稳定任务 ID 去重，过时游标任务在读取 OA 前退出；同步开关、模板启停和分页检查点继续有效。界面展示来源进度、更新时间和失败重试入口，并对站点 scheduler 未启用单独提示；仅有定时规则不代表站点调度进程已运行。同步代码不修改 OA、付款或会计记录。

代码入口：`budget/services/expense_management.py`、`budget/api/expenses.py`、`budget/public/js/expense_management.js`、`budget/public/css/expense_management.css`、`budget/expense_navigation.py`。查询 API 为 `budget.api.expenses.get_options/get_data/get_detail/export_data`，同步状态为 `get_sync_status`，手动入队为仅允许 POST 的 `enqueue_sync(source, retry_failed=False)`（`cn/mx`）；统一配置读取为 `get_sync_settings`，候选读取为 `get_sync_mapping_candidates(kind, start=0)`（`organizations/entities/categories`），均要求原来源的管理权限。保存 `save_sync_settings(values)` 和预览 `preview_sync_template(template, business_timezone, template_defaults=None, processing_mode=None)` 均限 POST 和原来源管理权限。筛选键为 `month/company/category/view/measure`，`get_data` 明细每页 50 行，导出上限 20000 行。没有新增业务字段或数据表；五个 Page、Sidebar、Icon 由安装/迁移钩子同步。英文、中文、西文资源在现有 CSV 中维护。

发布须同时更新两个 App，按目标站点既有发布流程同步 Page/导航和资源、翻译缓存，浏览器完整刷新。开发验证分开记录：Python 合成数据回归；浏览器使用真实前端资源和模拟 API 检查交互；`dev.localhost` 定向安装新页面并验证真实 boot 与本地快照读取。以上不代表生产部署或生产数据验收。

统一同步改造及审查修复验证：402 项 Python 测试及 69 个子测试、79 项前端测试通过，覆盖权限、双版本冲突、整体回滚、去重模板往返、失败重试、分页时间不冒充整轮完成、候选分页与跨来源分类。原生 Desk 的模拟命令验证 15 个交互场景，包含统一编辑、两套 JSON 规则保存、模板草稿删除、长来源名称、候选选择与翻页、中文和西文展示；真实开发站点只读验证配置接口和三类候选查询，先前数据库回滚事务验证无修改往返及第二来源失败后整体回滚。中文、英文、西文各检查 68 条运行时翻译，并定向刷新开发站点翻译缓存。`dev.localhost` 的两条支出 Scheduled Job Type 此前已定向更新为 `*/5 * * * *`；全站调度保持关闭，未触发真实 OA 拉取或生产部署。目标环境需按既有流程发布配套资源、刷新翻译缓存，并单独确认 scheduler/worker 状态。

## CN 只读审批工作台（2026-11-20）

### 数据链路与页面

三类单据统一由 `integrations/cn_approval_source.py` 经 `shared_operating_oa.client()` 调用品牌 App 的 `operating_oa_http.read_template_page`，使用 **GET `/api/v1/approvals`** 的 `corpId`、`processCode`、`limit`、`cursor` 分页。复用 MX 的客户端、字段绑定、分类提取、精确金额及审批状态工具；CN 采用独立展示投影，**不读取独立预算数据库，复用 MX 汇率服务但不把 MX 财务节点或会计归期套用到 CN**。

| 页面 | 路由 | 内容 |
| --- | --- | --- |
| 经营总览 | `china-expenses` | 公司预算/运营/采购/待批/差额矩阵、超支预警及趋势；金额下钻 |
| 预算执行 | `china-budget-execution` | 公司→月份→管理费用类别，最新有效预算与历史替代版本 |
| 支出分析 | `china-expense-reports` | 公司/类别排名、月度支出、已结束期间同比/环比、变化贡献及大额明细 |
| 报表中心 | `china-management-reports` | 公司预算执行、类别差异、月度趋势、人事支出、采购支出五张固定报表；Excel 与下钻 |
| 审批台账 | `china-expense-details` | 每页 25 条分析明细、全部匹配结果导出、原单详情；表头不重复加计 |
| 数据核对 | `china-expense-review` | 公司/类别映射、预算版本、分摊、金额、期间、币种及结构问题 |


侧栏使用原生可折叠两级菜单：“预算与支出”包含经营总览、预算执行，“经营分析”包含支出分析、报表中心，“明细与核对”包含审批台账、数据核对；保留原生展开状态记忆及当前项高亮。面包屑按“支出（CN）／分组／当前页面”显示，Desk 页签标题使用同一套译名。内容区直接展示筛选和数据，不重复放置工作台标题或页面导航页签；审批类型仅在台账和核对侧栏上方展示，包含全部审批、预算申报、运营支出、采购支出，并显示当前选中项。月份范围复用品牌 App 的 `mexico_expense_date_range.js`，以月份模式选择起止月份；本月、上月、近三个月（含本月的三个完整月份）、本年（完整自然年）提供快捷选择，前后端仍使用 `from_month/to_month`，不改变统计归期。刷新与更新时间合并在筛选工具栏。普通点击 CN 侧栏的其他工作台视图时，在原生路由处理前传入当前筛选，沿用内容入口的规则清空核对事项；分类下钻、查看台账及进入核对也通过对应 Page 路由传递筛选。显式带查询或锚点的链接、新标签页手势、侧栏编辑及非 CN 导航保留原生行为。Desk 页签和浏览器前进/后退恢复各页面自己的条件。启动时仅调整已授权的 Page 标题，不修改品牌 App 独立安装时的原页面元数据。

页面结果记录对应的筛选条件、核对视图和页码；筛选、搜索、重置及返回缓存页面时会替换当前 URL 的筛选参数，保留其他参数及锚点，不新增浏览器历史记录，避免返回或刷新时恢复过期条件。缓存命中时只同步 URL 并渲染，不额外查询或重置页码。离开页面时销毁范围弹层和当前审批类型侧栏，返回后重新挂载；侧栏原生重建后恢复当前选择，CN 与 MX 不共享选中状态。同一路由收到不同条件或 `cn_report` 报表选择时，立即使旧请求失效并重查；相同选择不重复启动查询，旧请求的成功或失败均不能覆盖新结果。离开时未完成的查询和搜索防抖在返回后重试；后台同步继续运行，返回时只检查同步状态，不重复提交任务。已完成且条件一致的结果立即复用，新快照发布后重新读取本地结果。旧 `china-personnel-report` 路由转为报表中心的人事报表。详情仅展示审批状态、业务字段、说明、原表明细及附件名称，附件通过原审批查看；不向浏览器返回完整 OA 原始载荷，表头银行卡字段不纳入详情。原单 URL 由已有可信钉钉链接构建器生成，不能由调用者传入任意地址。

### 金额、时间与核对口径

子公司筛选复用当前 CN 的 Company 权限和精确组织映射，下拉仅列出有权访问的非集团公司；不按来源名称模糊关联，也不套用 MX 的墨西哥国家限制。页面已移除来源组织筛选，旧链接中的 `entity` 条件不再带入查询、导航或导出；列表与详情仍展示原始来源组织，后端 `entity` 参数保留兼容。未匹配公司仅管理员在“全部子公司”范围内核对，选择具体公司后排除；无权公司参数直接拒绝，列表、详情及导出仍逐次执行权限检查。`get_filter_options` 独立读取权限内的子公司及支持的折算币种，不依赖 OA 快照、组织映射或汇率服务；首次审批/折算失败后仍可切换子公司。选项请求失败时单独提示并允许只重试选项，不把汇总失败显示成成功；页面离开、返回及并发刷新会丢弃过期选项响应。

折算适配器 `services/cn_approval_fx.py` 调用品牌 App 的 `mexico_expense_fx.Conversion.rate` 和 `converted_amount`，复用汇率获取、缓存、交叉汇率及 Decimal 运算。**预算使用预算月份的月初汇率，运营和采购使用申请月份的月初汇率**；不读取 MX 财务节点，也不以申请月替代缺失的预算月。来源币种兑 CNY 汇率除以目标币种兑 CNY 汇率，保留 12 位汇率，每笔审批四舍五入到 2 位后求和，同币种直接按 1 折算。汇率服务必须返回精确月初日期，缺失时请求失败，不使用最近日期、零或 1 替代；无法识别的币种或跨币种折算所需月份缺失时进入核对，金额显示不完整。该折算是审批统计口径，不代表付款或会计本位币记账。核对问题先按来源事实判定（包括币种或月份无法折算），再应用完整筛选条件，最后只为命中的审批查询汇率；不相关审批的汇率缺失不再阻断核对列表、汇总及导出。命中审批自身需要的汇率仍必须有效，不使用替代汇率或部分金额报成功。

`get_data`、`refresh_data`、`export_data` 接受可选 `company`、`reporting_currency`（CNY/USD/MXN）；显式指定 `reporting_currency` 时汇总全部原币，忽略兼容字段 `currency`。未指定折算币种的旧 API 调用仍按原币分列，旧 `currency` 参数仍筛选原币。页面旧币种书签迁移到新的折算币种选择；子公司与折算币种随导航及 URL 保存，重置为全部子公司/CNY。`get_detail(key, reporting_currency=None)` 向后兼容。折算结果新增 `converted_amount/reporting_currency/exchange_rate/exchange_rate_date/fx_date_basis/fx_status`，不覆盖原 `amount/currency` 或审批快照。详情显示汇率归期，原表明细及预算快照保持原始数据。导出保留原币列并追加折算金额、币种、汇率与归期；超出 Excel 15 位有效数字的金额按文本保留分位。折算字段不改写来源数据；本地持久化快照需要执行下述存储迁移。

- 页面默认按 CNY 折算，可选择 USD、MXN，保留原币金额。管理报表按下文有效预算及分析明细口径计算，不重复加计表头；兼容旧 `get_data` 的三类卡片仍累计已通过的申报表头金额，不作为新版公司有效预算。
- 金额卡片与图表只汇总审批完成且同意的单据；审批中、拒绝、终止、撤回保留在台账，不混入已通过金额。审批通过不代表实际付款。零金额有效；缺失或无效金额显示待核对，不转换成零或伪装为完整合计。
- 预算按表内预算月份归期，运营和采购按表内申请日期归期，不回退到创建日期。缺失期间的单据只在核对视图出现，不进入期间金额汇总。
- 预算已知明细表以人民币列核对；工资、社保分摊以总金额列核对，不把公司/个人分项再加一次。这些明细以 CNY 展示，表头为其他币种时提示不能直接比较。采购支持明细、规格文字、预算快照、附件不叠加到申请金额。
- 预算快照只表示填单时的预算和已用金额，不计算当前余额。核对清单为自动检查结果，修正应在原审批进行，再刷新；不保存人工确认状态。
- CN 审批工作台按配置的来源企业与已启用模板纳入预算、运营和采购审批，不按执行地区筛选；地区为空、无法识别或为其他地区均不排除。已有 `field_mapping.region` 配置保留兼容，但不再参与解析、范围判断或快照标识；新增、修改或删除该映射不会触发全量同步，也不会打断已排队或执行中的同步。运营及采购的其他字段映射继续有效，来源组织仍按精确规则对应公司。
- 取消地区限制后，`cn_approval_source.cache_key` 将投影版本纳入持久化快照标识；旧的地区筛选快照保留，但不作为新范围的完整结果。首次需按现有后台同步流程重建一次快照，完成后仍读取本地数据；停用模板的已存历史及独立的旧台账/额度同步规则不受此调整影响。


管理计算集中在 `services/cn_approval_management.py`，类别规则在 `cn_approval_management_rules.py`。用户确认的口径如下：

- **公司＋预算月份只取最后通过的一张预算单**。跨部门预算也按公司归并选择；按 OA `finishTime` 转 UTC 后排序，不用申请/更新时间。多单缺少完成时间或完成时间相同则无法唯一选择，保留待核对，金额/差额不伪造为零。历史预算标记为替代版本，可下钻但不累计占用。
- **所有已通过运营和采购审批均占用预算**，含投资类采购；审批中金额单列，只参与“全部待批通过后的差额”，拒绝/撤销不占用。预算差额＝有效预算－已批运营－已批采购，仍不是实际付款或会计余额。
- 可核验的预算明细替代表头参与类别分析，不重复累计。运营工资/社保分摊只有在 CNY 明细金额精确合计等于表头时才拆分，并逐行按部门映射公司；不一致或外币无法直接核验时保留表头金额并列为未分配，绝不把全额强行归给表头公司。
- 管理类别采用表单末级选项精确映射；设置可把预算项目、运营/采购类别路径对应到相同管理类别。未知类别单列，不把不同未知项目相互冲抵；筛选“未分类”后的总计、公司、月份、报表和 Excel 同样保留金额，但差额/执行率不可用并标记不完整，不显示正常或超支结论。仅当公司当月有效预算全部项目均已映射且金额可核验时，未列出的类别预算才认定为零，否则不可用。
- 缺少预算的公司/月份不能用其他公司/月份的结余掩盖；总额仍展示已知预算和已批支出，但差额/执行率不可用。零预算与缺失预算严格区分；近限预警默认 90%，可在设置调整。未归属和未分类数量始终展示。
- 拆分明细折算后按最大余数法分配分位，精确保持整单折算结果；公司/类别筛选之后仍使用同一分位分配，保证报表、明细及导出可核对。部分公司权限仅返回相应分摊明细，隐藏其他公司明细、整单事实/附件及原单链接。
- 同比/环比仅比较已结束月份，使用当前审批状态；环比取前一个等长区间，同比向前一年。无历史基准不等于零，当前未结束月份不与完整历史月份直接比较。影响项按公司和类别分别展示，不将两种维度重复加总。
- 五张报表复用相同数据与计算：采购按公司/月/类别/项目，其他按公司或公司/月/类别；人事包含工资、社保公积金、奖金、福利。公司权限与有效预算先在完整分析投影上确定，采购/人事报表再限定业务范围，最后仅折算范围内的明细；无关记录缺汇率不阻断这些报表及导出，范围内缺汇率仍失败，分类筛选不会复活已被替代的预算。项目空值单独下钻，所有金额入口保留当前公司、月份、类别、币种及项目条件。

新增 API：`get_management`、`get_management_lines`、`get_management_detail`、`get_management_report`、`export_management_report`、`export_management_lines`、`get_unmapped_categories`。管理查询/导出按已有读者角色及 Company 权限开放；导出至多 20000 行，包含口径、过滤条件、币种、数据时间、质量说明，保留 Excel 大金额精度及公式防护。旧 `get_data/get_detail/export_data` 契约保留，旧导出仍仅系统管理员且限 10000 笔。页面切换到管理视图会清除审批类型/审批状态/关键词等单边条件，避免预算和支出比较被隐藏筛选扭曲。

新增 `CN Approval Settings.management_rules` 保存预警比例与精确类别映射；`patches/add_cn_management_views.py` 仅同步设置元数据和两个新 Page，正常迁移后由既有导航 hook 更新侧栏。无历史财务数据修改，也不自动补公司归属。

### 权限与配置

`api/cn_approvals.py` 保留兼容入口 `get_filter_options`、`get_data`、`get_detail`、`refresh_data`（POST）、`enqueue_sync`（POST）、`get_sync_status`、`export_data`。读者限 `System Manager` / `Accounts Manager`，导出仅 `System Manager`，最多 10000 笔。每次返回列表、详情、汇总、筛选选项或导出前均重新应用原生 Company 用户权限；公司身份识别与授权分开：服务器内部查询完整 Company 名称集识别主体，再按用户可读 Company 集过滤；完整名称集不返回客户端。公司不在可读范围内的单据不可见，即使用户具有 System Manager 角色，也不能将其重新归为未知组织。只有系统管理员可看到尚未匹配公司的审批，并将其列入核对；普通财务用户不能通过未知归属绕过公司限制。此 API 不继承原财务单据的办理权限。

页面右侧“同步设置”提供 CN 独立配置，参考 MX 原生 Dialog/Table：支持维护审批类型、模板名称、来源企业、模板码、启停、字段/分类映射，以及抽样预览。公司映射表支持一个子公司对应多个来源名称，待映射来源从当前及已核验的停用模板本地快照分页读取（每页 50 项），点击添加只写入草稿，保存后才生效。接口沿用共享 OA URL/企业白名单；连接地址和白名单仍只由服务器配置，不在此对话框修改。首次使用时，未配置的审批模板、公司映射与自定义分类映射均为空，同步默认停用，由管理员手动添加并启用；新增模板或公司映射行也不自动选择唯一的来源企业，字段映射编辑器初始留空。首次迁移仅兼容服务器上已明确设置的 `cn_approval_templates`、`cn_approval_company_map`、`cn_approval_sync_enabled`，不生成内置模板；已经保存的配置保留，后续以数据库设置为准。

原 `cn_approval_templates` 的配置结构是模板数组：`approval_type`（`budget`/`operating`/`purchase`）、`corp_id`、`process_code`；运营和采购可附带共享字段规则的 `field_mapping`、`classification_fields`。没有显式配置时返回空模板列表，不再自动加入预算、运营或采购的内置模板码；首次未启用同步时，页面和定时任务均不自动拉取审批。企业必须在共享白名单中，同一企业/模板不可重复。

可选 `cn_approval_company_map` 为 `{corp_id, source_name, company}` 数组，按企业和来源主体原名精确匹配；其次复用 Budget 已有且来源指纹一致的组织 ID 映射，最后按原名与 Company 名完全一致的匹配识别公司，再统一检查读取权限。这些映射现由同步设置维护，按企业与去除首尾空白后的原名校验唯一性，不做模糊匹配；保存必须指定有效、非集团且有权限的 Company。重复映射阻止保存。无需启用台账同步。

设置存入内部 Single `CN Approval Settings`，子表为 `CN Approval Template` / `CN Approval Company Map`；无通用 DocType 读写权限，Controller 拒绝绕过服务的通用保存、删除和重命名。新增 `get_settings`、`save_settings`（POST）、`preview_template`（POST）、`get_unmapped_sources`；仅 Administrator 或拥有全部非集团公司访问权限的 System Manager 可读取/维护，普通财务及公司受限管理员仍可查看获授权的审批，但不能改全局来源配置。

保存使用版本校验和父记录锁，锁后重新读取 Single 与子表；MariaDB 快照冲突完整回滚后最多重试三次。保存只写配置，不发 OA 请求；预览是独立的至多 3 笔只读抽样，仅返回 CN 纳入数和问题标签，不返回原始审批数据。已保存模板不能删除、修改企业/模板码身份或变更审批类型；需停用旧模板、添加新模板。新行默认停用。`CN Approval Template.history_state` 为服务器维护的隐藏只读状态：新建且从未启用为 `never_enabled`，无需历史快照；一经启用即为 `expected`，以后停用仍要求历史证据。旧行空值按历史未知处理，不自动降级；保存忽略客户端传入的状态，按锁定后的旧值计算。新建停用模板不改变当前快照身份，也不会使现有预算差额失效。仅启用模板参与上游同步，全部停用不会回退默认模板，下一次后台任务发布当前空快照且不访问 OA。管理报表通过 `integrations/cn_approval_history.py` 继续纳入停用模板最后一次已核验快照，并标注历史时间；只接受当前企业白名单内、相同 OA 连接指纹且附模板身份的快照，不混用不同来源。旧快照缺少可核验身份时不猜测来源，明确提示历史缺失并暂停差额判断；停用后 OA 的变化须重新启用模板同步才可更新。切换模板范围/解析规则会使用新快照标识，首次完整同步前明确显示等待状态；模板名称或公司映射变化不改变快照身份，公司映射在查询时重新应用。同步状态附带 `settings_version`，其他已打开页面也会在轮询时重读本地数据。

预算字段解析沿用内置规则；字段和分类映射用于运营/采购，三类审批的 `region` 映射均不参与处理。CN 保留原有预算月/申请月口径，不引入 MX 财务节点日期、时区或非 MX 排除规则。

CN 页面直接读取站点数据库中的内部 `CN Approval Snapshot`，不再依赖 10 分钟 Redis 缓存。`integrations/cn_approval_store.py` 保存一份完整投影，按投影版本、来源 URL 与模板配置（排除无效的 `region` 映射）的 SHA-256 标识隔离；清理 Redis/翻译缓存或重启服务不会删除它。该 DocType 无通用读写权限，原始快照不能通过普通 DocType REST/报表获取；专用查询 API 每次都执行上述 Company 权限。当前实现保留原有有界全量投影，未引入逐审批 SQL 查询或增量游标。

`services/cn_approval_sync.py` 在 `long` 队列读取 OA，页面查询、详情和导出不扫描上游审批。`enqueue_sync(if_stale=False)` 只提交任务并返回同步状态，`get_sync_status` 只读任务进度及快照版本，不触发折算；兼容 `refresh_data` 现在排队后返回现有本地结果及 `sync` 状态，**返回不代表本轮已完成**。首次无快照明确显示等待同步，禁止导出空结果冒充成功；有效的空快照可正常显示零条。刷新期间旧快照继续可读，成功发布新版本后前端按当前条件重读本地数据；失败保留旧快照并显示失败状态。

后台调度每 5 分钟尝试提交同步，已有排队/运行任务时复用；不因上次扫描刚完成而跳过下一定时周期。设置中的同步开关控制自动与手动同步，关闭后保留同配置的本地查询。首次初始化使用显式配置的 `cn_approval_sync_enabled`，未配置时默认 0。启用后，页面也会在后台提交过期/首次快照的刷新请求，先显示已有数据，因此站点 scheduler 关闭时仍可使用；无页面访问且调度器关闭时不会定时更新。页面自动重试距上次入队至少 5 分钟，手动重试不受此间隔限制；定时任务由 cron 控制频率。前端使用 `if_stale=1/0` 区分自动与手动请求，接口同时兼容原生布尔值及表单编码的 `true/false` 字符串。任务以数据库主键、行锁与令牌去重；入队命令独占其事务，MariaDB 并发创建/锁定时的快照冲突会完整回滚并最多尝试三次，不使用已失效的保存点继续执行；任务提交在事务提交后执行，排队失败被记录。排队租期 15 分钟、worker 总时限 15 分钟；运行心跳租期为 worker 时限加 60 秒（当前 16 分钟），避免归属计算和原子发布期间因未提交心跳而误判过期；同时从 `started_at` 计算绝对截止时间（当前启动后 16 分钟），后续心跳不能延长该截止时间，确保 worker 已超时退出后能及时重试。缺少开始时间的历史记录仍按心跳判断。过期任务显示超时且允许重新入队，旧令牌不能覆盖新结果。轮询在页面隐藏时停止，返回后检查版本；状态变化只局部更新同步提示和按钮，不重建筛选控件。新快照完成后的数据更新也保留正在选择的月份范围，已提交月份改变或离开页面时才重建/销毁。UI 将同步状态错误和金额/汇率查询错误分开呈现。

每个模板分页要求稳定 `syncUntil`，拒绝重复身份、游标循环及部分结果；不同模板各自取得水位，不承诺整个企业跨模板的同一时点快照。全量扫描仍限 20000 条来源审批、250 页，扫描期限为 600 秒，在开始下一页及本页解析完成后检查，最后一页也不能超期发布（单次 HTTP 超时/重试沿用共享客户端）。后台任务另留 300 秒用于归属计算和完整快照发布。页级进度单独保存，所有模板完成且来源配置未改变后才原子替换快照；失败不发布部分金额。规模超过该边界时需另行设计增量同步或分批投影。

同步失败码区分 `scan_timeout`、`scan_limit`、`job_timeout`、`scan_failed`、`allocation_failed`、`publish_failed`、`configuration_failed`，继续兼容原有配置变化、入队失败及旧 `source_failed`。前端按当前语言说明失败阶段；已有旧失败记录不猜测具体原因。`cn_approval_sync.log` 在回滚前记录来源摘要、阶段、异常类型、数字错误码及调用文件/行号，不记录异常正文、SQL、局部变量、OA 内容或令牌。连接失效导致回滚/失败状态写回失败时保留原异常及诊断，不重新连接继续事务，也不绕过采购连接保护；此时仍可能由状态租期兜底显示超时。RQ 的 `FINISHED` 仅说明函数返回，业务成功以 `CN Approval Snapshot.sync_status=success` 为准。

此次调整不新增字段或修改快照格式，无需专门数据库迁移；部署时后台进程与 Web 使用相同新版 Budget 代码，更新前端资源和翻译缓存。已入队任务保留其原有 RQ 时限，更新后需新发起同步。数据库 `max_allowed_packet` 独立限制整份 JSON 的单次写入大小，本次扩大扫描时限不替代数据库容量核查，也不将大快照改为分块存储。服务器临时 `SET GLOBAL` 参数需要另行固化。

新增 DocType 的 patch 为 `budget.patches.add_cn_approval_snapshot` 与 `budget.patches.add_cn_approval_settings`，均登记于 `budget/patches.txt`；模板历史状态由 `budget.patches.add_cn_template_history_state` 定向同步子表字段，保留旧行空值，不补写历史配置或审批数据；保存前检查字段已迁移，避免静默丢失状态。正式发布需迁移并重载 worker、更新页面资产和翻译。迁移仅创建内部快照存储，若仍存在同配置的完整 v3 Redis 快照则保留到数据库；无缓存时由首次后台同步建立。设置迁移只保留原 CN 配置，不请求 OA；未配置共享连接的独立 Budget 安装可待首次保存再初始化。不修改 Budget Quota、Budget Ledger 或 MX 业务表。

### CN / MX 双向支出归属

支出审批详情和筛选区提供“跨区支出归属”入口，支持 MX → CN、CN → MX，以及一条费用按明确金额分摊到多个承担公司。共享规则及发布步骤见品牌 App 的 [CN / MX 双向明细归属](../deeplinkerp_branding/docs/mexico-expenses.md#cn--mx-双向明细归属2026-11-23)，不再在两边分别维护同一审批的归属。

默认按明细承担公司自动归属：精确识别公司或复用已有来源映射后，按公司档案国家分流，中国进入 CN，墨西哥进入 MX，其他或缺失国家、公司不明和映射冲突留待核对。预算申报规则保持不变。人工分摊作为例外保留；首次打开及公司/分类映射变化后，后台重算本地共享归属，期间暂停管理汇总，现有状态轮询完成后自动重读。自动归属元数据存入既有 JSON，无新增 Budget 字段或迁移，也不因本地重算请求 OA 审批。

自动归属与原生 CN 查询共用 `services/organization_mapping.approval_companies()` 中来源绑定有效的企业/组织 ID 映射；工资、社保和费用明细使用各行组织 ID，表头金额使用表头 ID，名称与 ID 的映射冲突则待核对。分类映射使用页面提供的完整稳定路径，已有映射无需重填；具体解析及版本兼容边界统一见上述品牌 App 文档。

Budget 的 `cn_approval_management.with_regional_allocations` 排除已由共用记录接管的同源审批，并合入权限范围内的 CN 明细；经营总览、预算执行、支出分析、五类报表、明细和导出复用这一结果。预算申报及最后通过版本选择保持原有规则；MX 的“费用支出”分配到 CN 后按运营支出占用预算。新来源只有已通过金额占用预算，待批单列；未知或过期归属金额留待核对，`regional_allocation` 纳入共用筛选允许范围。分配保留来源项目，采购项目筛选、报表和报表导出保持一致。重新确认已有分配必须找到维护方的最新本地来源，不能用归属记录中的旧载荷恢复已缺失审批的预算占用。原始审批查询兼容 API 仍呈现来源审批，不作为分配后的公司报表使用。

运营/采购审批的内部快照附带 `_routing_source`，仅供服务器建立分配；公开列表、详情及导出不返回原始载荷。快照版本 v5 触发一次后台重建，之后继续读本地数据。共用归属保存及来源状态变更通过 `allocation_revision` 参与现有轮询，不调用 OA。跨区分配需要品牌 App 同步迁移 `Regional Expense Allocation` 和 `Mexico Expense Line.regional_company`，不改 Budget Quota、付款或会计业务表。2026-11-23 经授权已在 `dev.localhost` 定向同步这两个 DocType 并刷新缓存；双向分配、重复投影、撤回和来源金额变化通过真实数据库回滚事务验证，未保留临时记录、未主动发起 OA 同步，生产尚未迁移或部署。

### 验证与发布边界

回归见 `tests/test_cn_approval_fx.py`、`tests/test_cn_approvals.py`、`tests/cn-approval-workbench.test.js`、`test_cn_navigation.py`、`test_cn_approval_cache.py`、`test_cn_approval_settings.py`；品牌 App 的 `tests/regional-expense-page-loading.test.js`、`tests/test_operating_expense_navigation.py` 覆盖资源加载和导航边界。测试使用合成数据，不提交用户原始 JSON。前端使用 Frappe `__()`，后端 `_()`；中、西文维护于 `translations/zh.csv`、`es.csv`，英文使用源文案，来源字段标签保留原文。

本次开发验证包含合成数据浏览器交互与 390px 布局，以及 `dev.localhost` 的真实审批后台同步、本地读取和缓存清理后的持久性检查；开发站点已执行该新增表的定向 patch 并登记定时任务，全站 scheduler 仍关闭。数据库三轮四并发首次请求仅入队一个任务，普通业务角色不能通过通用 DocType 接口读取完整快照。以上不等同于生产验收。发布需配套更新 Budget 与品牌 App、刷新资产及翻译缓存；已持久化的侧栏需通过批准的安装/迁移流程重放。新增 CSV 词条后，需清理 Frappe 翻译及 boot 缓存并重新加载 Desk，已有浏览器会话不会自动取得新词库；不要通过页面私有词典覆盖站点翻译。生产部署与生产迁移须另行执行，历史业务数据不作调整。



```
api/（只做参数入口 + 权限 + 结果封装）
  ↓
services/（判定逻辑真源：归因、分页游标、唯一性、分层）
  ↓
integrations/（IO：钉钉 HTTP、OA 只读库）
```

本 app 不改上游 app、不 `ALTER TABLE`，读写都通过声明与 patch 完成。

## 与既有实现的关系

bench 里此前有三套重复的钉钉/OA 代码，本 app 作为唯一入口，逐步替代：

| 既有实现 | 现状 | 本 app 的对应件 |
| --- | --- | --- |
| `draft_notifications/dingtalk_robot.py` | token 用 `frappe.cache()` 缓存 + 机器人发消息 | `integrations/dingtalk_client.py`（token 缓存自动适配 `frappe.cache()`） |
| `overseas_costing/integrations/dingtalk_approval_source.py` | psycopg3 只读直连 OA 库 | `integrations/oa_approval_source.py`（同样的 `read_only` 会话与注入式 `connect`） |
| `oa_purchase_request/...py:729` | 旧版 gettoken + `subprocess psql` 查 OA 库 | 由上面两件替代（**不要再新增 psql 子进程写法**） |

迁移策略：新代码只走本 app；存量调用点按业务优先级逐个切换，切换完再删旧实现。
在切换完成前，两边并存但不互相 import。

## 上游契约与消费方式

数据由 `yueweiit/dingtalk-oa` 拉取并写入 `dingtalk_oa` 库，**它是唯一写方**。
本 app 只消费，不改它的库结构、不写它的表、不重写拉取逻辑。
上游缺能力时**给它提 PR 新增接口**，不在 Python 侧绕过或复制实现。

| 需要的数据 | 走哪 | 原因 |
| --- | --- | --- |
| 审批列表 / 详情 / 附件 | **HTTP** `/api/v1/approvals`、`/attachments`、`/attachment-content` | 上游正式契约，cursor 分页 + `syncUntil` 水位 |
| 部门树 / 部门反查 / 路径快照 | **HTTP** `/api/v1/budget/departments*` | 预算列表展示与审批归属都用它 |
| 预算台账 / 统计 / 明细 / 备注 | **HTTP** `/api/v1/budget/entries*` | 去重与状态口径在 dingtalk-oa 侧复刻 |
| 预算额度配置 | **HTTP** `/api/v1/budget/quota*` | 下拉候选 / 汇总 / 明细分页 |
| 实际支出（报表 + 详情） | **HTTP** `/api/v1/budget/expenses*` | 六大分支口径在 dingtalk-oa 侧复刻 |
| 任务明细、审批效率 | 从 `/api/v1/approvals` 的 `rawPayload.tasks` 解析 | 无独立聚合接口，本地聚合 |

**显式 HTTP 模式下 budget app 不建立任何 PostgreSQL 连接**：所有 SQL 集中在
`dingtalk-oa`，本 app 只做 HTTP 调用。`budget_data_source=postgres` 时才回退到
本仓库原有的 psycopg 直连实现（应急排查用）。

增量水位按上游契约算（`approval-read-api/README.md:150-157`）：

```python
from budget.services.approval_watermark_service import compute_next_watermark

next_updated_after = compute_next_watermark(page.sync_until)  # 自动回退 10 分钟重叠
```

**不要**用 `last_event_time` 当水位——数据字典明确它是「规范化时的本地刷新时间，并非严格的上游事件时间」。
`updated_at` 由触发器维护（源内容不变时保留旧值），才是可用的增量依据。

### 已在 dingtalk-oa 落地的预算接口

| 路由 | 用途 |
| --- | --- |
| `GET /api/v1/budget/entries` | 台账分页（`budgetType` / `startMonth` / `endMonth` / `status` / `keyword` / `page` / `pageSize`），返回 `rows / total / counts / total_amount` |
| `GET /api/v1/budget/entries/stats` | 今日生产 / 今日非生产 / 生产总数 / 非生产总数 |
| `GET /api/v1/budget/entries/{expense-details,production-details,remarks,detail-count}` | 明细、备注、明细行数 |
| `GET /api/v1/budget/quota/{options,summary,entries}` | 额度下拉候选、汇总、明细分页 |
| `GET /api/v1/budget/expenses/{approved,meta,completed,pending,splits,payments}` | 实际支出与报表聚合原料 |
| `GET /api/v1/budget/departments{,/candidates,/snapshots}` | 部门树、业务主体反查、路径快照 |

响应统一 `{success: true, data}`，失败 `{success: false, code}`，限流返回 429 + `Retry-After`。
字段名沿用原 PG 结果集的 snake_case，迁移时业务层不需要改名。

## 仍待上游补充的接口

| 缺口 | 影响 |
| --- | --- |
| 审批列表无 `status` / `createTime` 区间过滤 | 只能按 `updatedAfter` 拉全量再本地筛 |
| 无审批效率 / 聚合接口 | 本地按 `rawPayload.tasks` 聚合 |
| 审批响应无顶层 `tasks` | 需解析 `rawPayload.tasks` |
| 无用户快照名接口 | `snapshot_user_name` 恒为空，用接口原始名称展示 |

## 目录

```
budget/
├─ hooks.py                  # 目前只声明 app 元信息，不挂 doc_events
├─ api/approval.py           # 只读接口：get_ / list_ 前缀（前端可安全重试）
├─ services/                 # 纯逻辑，可脱 Frappe 单测
│  ├─ approval_attribution_service.py
│  ├─ approval_watermark_service.py   # 增量水位（syncUntil 回退重叠窗口）
│  ├─ organization_service.py
│  └─ approval_query_service.py
├─ integrations/
│  ├─ dingtalk_client.py          # token 缓存 + 机器人发送 + 重试 + 三组熔断
│  ├─ oa_read_api_client.py       # 审批只读 HTTP（快照 / cursor 分页）
│  ├─ budget_read_api_client.py   # 预算只读 HTTP（台账 / 额度 / 支出 / 部门树）
│  ├─ budget_factory.py           # 数据源选择：local（默认）| http | postgres
│  ├─ oa_factory.py               # 审批数据源选择（与上面共用开关）
│  ├─ *_api_source.py             # 各数据源的 HTTP 实现（统一签名）
│  ├─ *_source.py                 # 各数据源的 PG 直连实现（应急回退）
│  └─ pg.py                       # 仅 postgres 模式使用的只读连接池
└─ tests/                         # pytest：全部靠注入替身，不需要站点/库/网络
```

## 覆盖的接口与查询

| 清单 | 实现 |
| --- | --- |
| 实例列表 / 增量 | `OAReadAPIClient.iter_approval_pages`（HTTP，上游 cursor） |
| ① token | `DingTalkClient.get_access_token`（企业应用 / 机器人两套凭据） |
| ⑥ 机器人 Markdown | `send_markdown` |
| Q4 / Q5 详情 | `get_instance_row(with_snapshot=False/True)` |
| Q6 部门树 | `list_departments` + `organization_service.build_organization_options` |
| Q7 部门反查 | `list_department_candidates` + `pick_unique_department` |
| Q8 效率报表 | `list_approval_efficiency_rows` |
| Q9 路径快照 | `list_department_snapshot_rows` + `normalize_department_snapshots` |

## 安装与入口

```bash
bench --site <site> install-app budget      # 已装则跳过
bench --site <site> migrate                 # 同步 DocType / Page，并补侧边栏入口
bench --site <site> build --app budget      # 生成 /assets/budget（Page 脚本从这里加载）
bench --site <site> clear-cache
```

新版 CN 启用后，`/desk/budget-dashboard` 和 `/desk/budget-config` 退役为 `/desk/china-budget-execution` 的地址别名，`/desk/china-personnel-report` 转到 `/desk/china-management-reports` 的人事报表。全局导航在原生 Page 渲染前跳转，保留当前 `cn_approval_filters`、其他查询参数和锚点；移除已废弃的 `cn_filters` 参数，替换当前浏览器历史项，避免返回键绕回旧页面。初次打开、缓存 Desk 页签和前进/后退走同一入口。旧页面不再挂载 CN 工作台；`/desk/budget-ledger-entry` 的原生列表、原单详情及独立 Budget 页面不受重定向影响。旧 `budget_data_source` / `budget_oa_source` 不再切换 CN 来源或限制导航。`after_install` / `after_migrate` 调用 `budget.setup.apply_budget_nav` 重放导航，不更改 ERPNext 原生 Budget 菜单及业务数据。更新代码后，已打开的浏览器须完整刷新一次以加载新版全局导航脚本。

旧链接的 `cn_approval_filters` 同时支持直接打开时的对象，以及 Desk 原生点击处理再次序列化后的 JSON 文本；跳转前统一为对象，无效文本不传入工作台。CN 页签迁移监听 `custom_filters` 的 `custom-filters:desk-tabs-restored` 事件，在原生恢复后将三个退役页面并入新版入口；重复目标合并并保留原位置、固定状态和最近访问时间，当前页签标识同步更新。其他模块、原生台账和表单草稿不参与迁移；独立 Budget 模式不启用此处理。该修复须同时更新 Budget 导航脚本与 Custom Filters 页签脚本，已打开页面完整刷新后生效，无数据库迁移。

### 历史预算台账集成（2026-11-19，兼容服务）

以下为保留的原台账/额度服务与旧页面实现，不是 2026-11-20 新 CN 工作台的数据链路；新界面的统一审批读取、权限及统计口径以上文为准。

- `budget/api/cn_workbench.py` 提供本地预算概览、分页明细、核对及最多 10000 行的 XLSX 导出。日期按覆盖的完整预算月份归一；公司取来源原名精确筛选，不与法律公司做模糊合并。预算申请按台账 `COALESCE(total_amount, amount, 0)` 计，真实零值保留。
- 概览与台账按预算月份查询；核对覆盖公司/部门/月份缺失及金额差异（超过 0.01）。公司层级预算允许部门展示名为空，须有相符的组织路径，或由组织映射明确核对为公司层级；不能仅凭非空组织 ID 跳过核对。缺少预算月份的核对记录按申请日期定位，不计入已归期预算金额。概览待核对数采用同一核对查询。
- 预算额度复用 `BudgetQuotaLocalSource`，只计当前配置企业及预算模板的 `effective`，按公司及月份区间读取；下拉候选、统计卡片和导出使用相同范围，其他企业历史额度保留但不混入当前统计；列表、汇总和导出共用部门 ID／名称下钻条件。保留集团、公司、部门、科目汇总以及每单科目明细。额度和预算申请分别展示，不相加，也不混入实际支出。共用 OA 来源的“同步预算数据”使用同一次审批分页结果，在同一事务内更新台账和额度；不再查询独立预算库的 `budget_effective_quota` / `budget_effective_quota_subject`。
- `public/js/cn_budget.js` 为 CN 提供预算适配；品牌 App 负责公共日期、导航及生命周期。原报表和详情分别提取为 `public/js/budget_report.js`、`budget_detail.js`，原生页面和 CN 共用。预算仅用人民币，日期/公司及额度页签随导航保留。
- 额度同步由 `services/oa_budget_quota.py` 复用现有预算表单解析和组织映射，按企业、预算模板和国内执行地区限定来源；同部门、预算月份采用台账的最新申请规则。额度保留原 `COMPLETED/TERMINATED + agree` 兼容口径，台账仍只纳入 `COMPLETED + agree`；表头金额作为额度，科目用 Decimal 按类型和名称汇总，金额差异单独标记。缺失月份、身份冲突或分页不完整会整批失败并回滚；完整空结果保留原数据；完整非空快照确认已无符合条件的台账审批时，按既有快照机制移除失效的派生台账及明细，并核验条数和金额归零，同一事务更新额度，失败则整批回滚。被新申请替代、撤回或不再符合条件的额度标记 `deactivated`，保留历史单据和科目；不删除其他企业的额度。
- `quota_sync.run_quota_sync/enqueue_quota_sync/sync_quotas` 保留兼容入口，共用 OA 模式委托预算同步任务，沿用启用开关、配置版本、来源绑定和同一个互斥锁；手动入口要求管理员 POST。原每日独立额度任务已从 hooks 移除，避免重复扫描；已登记的旧任务也只会走上述兼容入口。非共用来源返回 `shared_oa_required`，不会回退独立预算库。页面和 API 继续读取本地 `Budget Quota` / `Budget Quota Subject`，不新增字段、不自动启用同步。独立安装的显式旧读取适配器保留兼容，不属于 CN 同步链路。
- 报表公司筛选同时约束预算与支出。支出只通过可靠、唯一的部门 ID 归属公司；归属不完整或冲突时，公司执行金额显示不可用，不把全公司金额或零值作为该公司金额。独立旧模式的在途支出和审批效率接口没有可靠公司维度，选择公司时相应指标显示不可用；同年趋势仍显示自然年。
- CN 共用来源的“预算执行分析”通过 `integrations/cn_expense_report_source.py` 读取品牌 App 的本地运营审批快照，沿用原生列表/公司/字段权限，不再调用旧独立支出库和审批库。**已审批支出**按审批完成日期（中国时区）归期，审批中金额按申请日期归期；外币复用 MX 的月初汇率服务折算 CNY，分别采用审批完成月及申请月。只覆盖已接入的国内运营申请，采购尚未纳入；审批通过不代表付款，预算申请也不计入支出。页面卡片及图例明确使用“已审批支出”，口径放入悬停提示。
- 预算组织以相同企业下的组织 ID 精确关联；来源主体与发起人部门不同则不能借用发起人部门 ID。未关联记录仍计入全部公司支出并单列，不能用相似名称冲抵预算。公司筛选通过台账组织归属及显式组织映射约束金额与审批效率。审批效率按区间内发起的申请和 OA 任务时间计算；缺少完成时间、汇率或必要节点信息时返回可翻译的不可用状态，预算部分仍可展示；预算总额为零时整体执行率为 `null`（页面“—”）。
- 品牌 App 同步保存 `workflow_summary.reporting` 的精简时间快照；已有申请在正常同步结束核查时补齐。读取报表不会主动同步审批或生成付款记录，无需新增数据库字段。接口保留 `approvedTotal`、`actualExpense` 等历史字段，新增 `expense_basis=approved` 和 `scope_notice` 区分展示口径，独立旧模式保持兼容。
- 执行状态图按预算、已审批支出和审批中支出的组织并集汇总；没有预算或未关联预算组织的已审批金额仍单列展示。全年趋势读取失败时，重新按当前查询区间计算金额与执行率，返回独立的 `availability.trend_expenses=false`，趋势中的支出值为 `null`，前端只隐藏该趋势系列；本期也无法核实的金额仍显示不可用。OA 来源初始化失败时保留本地预算，显示一条翻译后的配置提示，不回退到旧来源，也不附带服务器错误弹窗。这些读取均不修改同步配置或业务数据。
- 台账、概览、核对、报表要求 `System Manager` / `Accounts Manager`；额度与同步设置仍仅允许 `System Manager`。前端隐藏额度页签之外，后端每次读取/导出均鉴权；未扩大角色授权。全部审批表仍是运营支出合计，预算单独展示。
- CN 预算浏览不触发同步；同步设置及同步执行复用 `BudgetLedgerSync`。未配置连接/企业范围时仍不得启用或同步。
- 同步设置新增 `operating_oa`（共用 CN/MX OA 审批接口），由 `integrations/shared_operating_oa.py` 复用品牌 App 的只读审批客户端与服务器端 URL／企业白名单，按预算模板、审批通过状态和国内执行地区读取。单企业无需重复填写企业 ID，多企业必须明确选择；未配置且无历史台账的 CN 站点默认展示此来源，但不会自动启用或同步。已绑定台账仍禁止直接换企业/来源，已有历史记录不会因读取页面而重写。
- HTTP 同步逐页检查身份、快照、重复记录、游标前进和页数上限，读取完整后才复用原事务、幂等及金额核验写入本地。现有共享接口没有部门树端点，保留已确认的本地组织路径，新组织保留 OA 原名，由管理员在明细／核对行的“核对组织归属”中明确指定公司、部门或公司层级；不猜测公司，不请求不存在的旧预算部门接口。共享接口未提供的审批统计和未关联的旧支出汇总显示不可用，不以零值代替。
- `services/organization_mapping.py` 将组织归属存入 `Budget Settings.organization_mappings`（子表 `Budget Organization Mapping`），按 `corp_id + department_id` 精确匹配，拒绝重复、跨企业、缺失名称及层级冲突。同步设置可集中维护；保存与同步共用锁，原生设置保存也校验并记版本。保存映射在同一事务内更新对应台账的公司／部门及 `reviewed_organization_level`，并更新当前企业、预算模板额度的公司／部门、公司 ID 和组织核对标记；清空台账旧摘要以便后续同步重检，保留 OA 原名、原路径和全部金额。删除映射恢复原路径推导结果；无原路径时使用同单台账保留的 OA 名称，无可核实原名则清空部门展示并回到待核对，不保留旧映射名称。
- 已有台账或额度（含仅有历史额度的站点）通过同步设置“验证并切换 OA 来源”迁移到 `operating_oa`：`services/ledger_source_transition.py` 检查同企业白名单，完整读取国内预算审批，按台账和额度各自的审批条件逐笔匹配实例 ID、单号、组织 ID、月份、类型、模板及金额，并核对额度科目汇总。其他企业及模板额度不参与切换核验，也不改写。预览仅生成绑定当前用户／配置／数据摘要的 10 分钟凭据；确认时在同步锁内重新核对，并锁定现有台账、当前范围额度及科目行。验证失败、超时、企业不符或预览后变化均拒绝切换。仅这条受控路径允许重新绑定指纹，保留台账、额度及组织映射，记录审计说明并停用同步；不会导入、删除数据或自动入队。历史非国内记录、缺少实例 ID 或金额不一致须先人工查明，不能靠清空指纹绕过。
- 发布须先同步 `Budget Settings`、新增子表 `Budget Organization Mapping` 及台账 `reviewed_organization_level` 元数据，再启用新版页面。无需回填或删除历史台账。未迁移站点按实际列存在情况读取旧组织路径，概览、明细、核对与导出仍可用；界面明确提示组织归属核对尚未启用，并阻止映射保存、来源切换及同步写入，返回 `schema_update_required`。结构检查同时核对物理表／列与 Frappe 元数据，避免仅更新代码就查询不存在的列。当前已在未迁移开发站点只读调用概览、明细、核对、导出和设置接口，并通过浏览器验证概览返回 HTTP 200、四张统计卡片正常渲染且无页面脚本异常；台账与同步日志数量保持不变。未运行迁移、真实同步或来源切换。

验证入口：`budget/tests/test_cn_workbench.py`、`test_cn_navigation.py`、`test_organization_mapping.py`、`test_ledger_source_transition.py`、`cn-budget.test.js`、`ledger-sync.test.js`、`dashboard-availability.test.js`；额度来源与事务回归见 `test_oa_budget_quota.py`、`test_shared_operating_oa.py`。开发站点另用事务内合成记录验证公司隔离、跨月、缺失月份核对、分页和导出，完成后回滚；浏览器验证 CN/原入口切换及无自动同步。此验证不等于生产数据核对或部署。

## 配置

### 预算台账同步（2026-11-19）

台账当前默认使用 `local`（见 `budget/integrations/budget_factory.py`），由
`services/oa_budget_sync.py` 按连接配置只读读取 OA PostgreSQL 归档或共用 OA 审批接口，并解析到本地台账。
下面为旧 HTTP 适配器与原台账同步服务；新 CN 工作台直接读取共享审批 API 的缓存投影，见上文。

- 台账页面菜单提供“预算同步设置”，工具栏提供独立“同步数据”。保存只保存配置，
  打开台账只读，不自动排入同步；兼容接口 `ensure_ledger_fresh` 也不再入队。
- 在现有 `Budget Settings` 中配置 `ledger_corp_id`、`ledger_connection_profile`
  和 `ledger_sync_enabled`。未填写企业时兼容服务端原有 `budget_corp_id` 等别名，
  不猜测企业；新开关默认关闭，手动、兼容及定时台账同步均检查完整配置和启用状态。
- `budget` 连接来源复用 `budget_db_*` 及现有别名；`shared_oa` 复用
  `overseas_costing_oa_db_*`。必须显式配置 host/user/password；库名默认 `dingtalk_oa`，
  端口默认 5432。共享来源同时传递已有 SSL 模式。密码只在服务端使用，设置 API 不返回连接凭据。
- 同步、组织树和审批统计读取使用同一企业及连接；同步查询同时限定企业和预算模板。
  未配置时公司/部门选项退回本地配置及台账，返回明确状态，不弹服务器错误。
- 实际支出及审批中支出仍来自独立的历史汇总库，该库没有逐行企业隔离字段。
  本地模式读取前核对原服务端企业配置、主机和端口与台账来源一致；不一致时停止支出查询，
  不猜测新库名，也不把数据缺失当作零支出。详情返回 `expense_available=false`，
  支出和余额为 `null`；看板返回分项 `availability`，保留预算数据，隐藏不可用的支出指标和图表。
  停用同步不影响来源匹配后的只读查询。显式 HTTP/PostgreSQL 模式保留原行为。
- 同步窗口跟踪 RQ 实际状态和持久运行日志，区分排队、运行、成功、失败和跳过，
  展示新增/更新等数量；跳过的已启动任务也保存 `run_id` 和 `reason_code`，RQ 结果过期后
  仍能显示跳过原因。停用状态下的日常定时检查不新增无意义日志。
  关闭窗口停止轮询，打开窗口不会自动提交同步任务。
- 保存设置与同步共用站点锁，包括原生设置表单；原生保存从写库前持锁到事务提交或回滚，
  避免首次导入与来源切换并发。已排队任务执行前复核配置版本。成功同步将来源指纹
  与台账同事务保存，台账已有数据时阻止直接切换企业或数据库；服务端连接变更后指纹
  不匹配也停止同步，避免跨来源覆盖或删除历史数据。

实现入口：`services/ledger_sync_settings.py`、`services/oa_budget_sync.py`、
`public/js/budget_ledger_sync.js`。更新站点需同步 `Budget Settings`、`Budget Ledger Sync Log`
新增字段和清理缓存；
未配置的数据源保持停用，不能把本地界面测试视为 OA 实际同步验收。

显式 `http` 模式只需配置 `dingtalk-oa` 的地址与企业范围，**不需要数据库连接**；本地预算台账同步使用上节独立配置：

```
BUDGET_DATA_SOURCE     # local（默认）| http | postgres
BUDGET_READ_API_URL    # dingtalk-oa 地址，如 https://oa.example.com
BUDGET_READ_API_TIMEOUT# 秒，默认 30
BUDGET_CORP_ID         # 本实例负责的企业，必填
```

`BUDGET_READ_API_URL` 兼容既有的 `BUDGET_OA_API_URL` / `operating_expense_oa_api_url` /
`budget_oa_api_base`，避免同一站点配两处地址。旧开关 `BUDGET_OA_SOURCE` 仍生效，
但优先级低于 `BUDGET_DATA_SOURCE`。

`BUDGET_CORP_ID` 是查询范围，不是可选优化：OA 库多企业共库，`ding_department_tree`
唯一键是 `(corp_id, dept_id)`，`businessId` 也可能跨企业重复。未配置时部门树查询直接
报错，不会退化成读全库。企业范围兼容 `budget_oa_corp_id` / `dingtalk_oa_corp_id` /
`dingtalk_corp_id`。

所有数据源统一由 `integrations/budget_factory.py` 与 `oa_factory.py` 选择：

| 工厂函数 | http（显式配置） | postgres（应急回退） |
| --- | --- | --- |
| `get_ledger_source()` | `BudgetLedgerAPISource` | `BudgetLedgerSource` |
| `get_quota_source()` | `QuotaAPISource` | `QuotaSource` |
| `get_expense_source()` | `ExpenseAPISource` | `ExpenseSource` |
| `get_budget_expense_source()` | `BudgetExpenseAPISource` | `BudgetExpenseSource` |
| `get_oa_source()` | `OAAPISource` | `OAApprovalSource` |

两组实现的方法签名与行字段完全一致，业务层不感知来源。

> 注意：台账同步后落在**站点自己的 MariaDB**（`Budget Ledger Entry`），
> `local` 模式的列表、筛选、排序、导出走站点库；组织选项可从配置完整的 OA 只读库读取，台账同步必须单独启用。

### 应急回退到 PostgreSQL

`BUDGET_DATA_SOURCE=postgres` 时恢复本仓库原有的 psycopg3 直连实现，此时才需要：

```
BUDGET_DB_HOST / BUDGET_DB_PORT / BUDGET_DB_NAME
BUDGET_DB_USER / BUDGET_DB_PASSWORD
```

库名按「数据源自己的键 → `BUDGET_DB_NAME` → 该数据源默认库名」解析：

| 数据源 | 库名键（可选） | 未配置时的默认 |
| --- | --- | --- |
| OA 审批 / 部门树 | —（直接用 `BUDGET_DB_NAME`） | `dingtalk_oa` |
| 预算台账 | `BUDGET_LEDGER_DB_NAME`（旧：`budget_system_db_name`） | `budget_system` |
| 审批支出 | `BUDGET_APPROVAL_DB_NAME`（旧：`dingtalk_approval_db_name`） | `dingtalk_approval` |

兼容既有键名 `budget_oa_db_*` / `dingtalk_oa_db_*` / `oa_mapping_pg_*`；
OA 连接一律 `default_transaction_read_only=on` + `statement_timeout`，账号请只授只读权限。

### dingtalk-oa 侧

新增预算只读接口后，`dingtalk-oa` 需要能读到预算库；主机与账号留空时复用它的主 PG 连接：

```
BUDGET_DB_HOST / BUDGET_DB_PORT / BUDGET_DB_USER / BUDGET_DB_PASSWORD
BUDGET_DB_NAME                  # 默认 budget_system
BUDGET_APPROVAL_DB_NAME         # 默认 dingtalk_approval
BUDGET_API_REQUESTS_PER_MINUTE  # 默认 120
BUDGET_API_STATEMENT_TIMEOUT_MS # 默认 15000
```

钉钉凭据（`frappe.conf` 或环境变量，`DingTalkConfig.from_site_config()`）：

```
dingtalk_app_key / dingtalk_app_secret
dingtalk_robot_app_key / dingtalk_robot_app_secret / dingtalk_robot_code
```

## 台账同步

数据链路：钉钉 OA 归档（PostgreSQL，只读）→ `parse_budget_instance` 解析 → 按
(dept, 预算月份) 去重取最新 → `Budget Ledger Entry` / `Budget Ledger Detail`（本地 MariaDB）。
页面此后只读本地表，外部 PG 只在同步时使用。

### 三个 DocType

| DocType | 作用 |
| --- | --- |
| `Budget Ledger Entry` | 台账主表，`autoname = form_no`；`source_digest` 存该单指纹，增量判定用 |
| `Budget Ledger Detail` | 明细，按 `form_no` 归属；某一单变化时只重建这一单的明细 |
| `Budget Ledger Sync Log` | 每轮一条运行记录（`Running / Success / Failed / Skipped`），只读，30 天后自动清理 |

### 触发方式

| 入口 | 用途 |
| --- | --- |
| `scheduled_sync()` | 外部调度器（cron / ofelia / launchd），`trigger=Scheduled`，失败抛异常以拿到非零退出码 |
| `run_oa_budget_sync(force=0)` | 手动同步；`force=1` 忽略指纹整表重建 |
| `enqueue_oa_budget_sync()` | 入队 long 队列，`job_id + deduplicate` 保证不会重复排队 |
| `ensure_ledger_fresh()` | 台账列表页打开时调用，数据超过 120s 未更新才入队；60s 内试过则退避 |
| `hooks.py` `daily_long` | `sync_budget_entries` 转调 `scheduled_sync`，站点开着 scheduler 时生效 |

站点 `enable_scheduler: 0` 时前四项之外都没人触发，需要外部定时器，例如：

```bash
bench --site <site> execute budget.services.oa_budget_sync.scheduled_sync
```

### 增量与全量

- **默认增量**：每单算 `sha256(最终入库字段 + 明细)`，与本地 `source_digest` 相同就整单跳过
  （不 upsert、不碰明细）。没人改单时每轮 0 写入。
- **每日整表对账**：`scheduled_sync` 发现距上次成功全量满 24h，自动把这一轮切成全量，
  对冲「本地被手工改坏」这类增量发现不了的问题。
- **校验失败自动升级**：增量轮写入后 `_verify` 发现条数/金额/明细对不上，会立刻以全量重跑一次，
  修好才提交；全量仍对不上才判定失败并整批回滚。
- **原子性**：全部写库 → 校验 → 提交；任一步失败 `rollback`，本地保持上一轮完整快照。

### 并发、超时与可观测性

- Redis 锁 `budget-ledger-sync`（`SET NX` + token 释放）串行化多 worker / 定时 / 页面触发；
  拿不到锁的那轮记 `Skipped`，不写库。
- 单轮时间预算默认 300s，站点配置 `budget_ledger_sync_timeout` 可改；超时抛
  `LedgerSyncTimeout` 并整批回滚。
- 最近的同步结果用 `get_ledger_sync_status()` 查（返回 `last` / `recent` / `local` / `lock_held`），
  数据来自 `Budget Ledger Sync Log`，Redis 重启不丢。

## 安装与测试

```bash
# 安装（在容器内）
bench --site dev.localhost install-app budget
bench --site dev.localhost clear-cache

# 测试（不需要站点）
python3 -m pytest -q apps/budget/budget/tests
```

## 阶段规划

- **第一阶段（已完成）**：只读链路 + 纯逻辑归因 + 只读接口 + 单测。
- **第二阶段**：同步账本 DocType（`PENDING / RUNNING / SUCCESS / FAILED / UNCERTAIN / MANUAL_REQUIRED`
  状态机 + `payload_hash` 幂等），`*_ledger_service.py` 负责落库，`hooks.py` 登记 cron，
  `UNCERTAIN` 靠回读 OA 库 reconcile。
- **第三阶段**：业务层（预算额度、表单解析、支出拆分）从两个 Node 项目迁入，
  按 `*_service.py`（纯逻辑）+ `*_ledger_service.py`（IO）成对组织。

### CN 管理视图开发验证（2026-11-20）

覆盖最新预算版本、采购全部占用、待批情景、跨公司分摊权限、未归属/未知类别及筛选后的差额不可比、零/缺失预算、跨月比较、分位折算、停用模板历史隔离与启用状态生命周期、报表与 Excel、报表范围内外汇率异常、异步路由及原生设置编辑。入口：`test_cn_approval_management.py`、`test_cn_approval_history.py`、`cn-approval-management.test.js`，与现有 CN 导航/设置/同步/FX/解析测试一并运行。开发站点已定向同步两个 Page、设置及模板状态元数据，并验证正常审批快照刷新；模板状态新增字段保留已有配置，真实数据库的临时启停验证使用事务回滚。浏览器验证覆盖同页报表切换时的延迟响应、未分类结果和采购导出。生产尚未迁移或部署。公司与类别映射由业务确认，测试不得猜测实际归属。
