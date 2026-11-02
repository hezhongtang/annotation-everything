"""浏览器场景注册表与选择调度（issue #53，无浏览器依赖）。

`e2e_browser.py` 模块级导入 playwright，`python3 -m unittest discover tests`
导不进来，所以调度所需的这一半放这里：场景注册表（稳定 ID、行为、fixture、
隔离、旧参数别名）、参数解析与选择校验、执行计划与清单。注册表是唯一清单，
`--list`、执行计划和结构化摘要都从它生成，不另维护副本。

新增浏览器场景的接入规则：先在这里登记（ID 用小写加连字符，写清行为、
fixture 与隔离），再到 `e2e_browser.py` 的 `SCENARIO_RUNNERS` 挂执行函数；
两边的完整性由入口启动时的一致性检查把关，缺一边都直接报错退出，
不会静默少跑。登记后全量模式自动纳入（全量必选场景按注册顺序执行）。
"""
from __future__ import annotations

import dataclasses
from typing import Optional


@dataclasses.dataclass(frozen=True)
class Scenario:
    id: str
    title: str                 # 行为说明：这个场景验证什么用户行为
    fixture: str               # 声明的准备步骤：合成材料与目录，只承担本场景需要的状态
    isolation: str             # 隔离边界：浏览器上下文、临时审阅目录与收尾
    full_required: bool = True  # 是否全量模式必选
    aliases: tuple[str, ...] = ()   # 旧局部参数（兼容别名）
    parallel_safe: bool = False  # 审查过的进程 / HOME / 台账隔离；新场景默认串行


SCENARIOS: list[Scenario] = [
    Scenario(
        parallel_safe=True,
        id="receiver-binding",
        title="接收方身份：harness、会话和等待／处理／未等待状态常显，双语与旧服务提示一致",
        fixture="fixtures/sample 合成审阅；显式等待会话绑定；notes.py wait/claim/done 真实子进程",
        isolation="独立审阅目录、端口与浏览器上下文；等待者与服务全部登记回收",
    ),
    Scenario(
        parallel_safe=True,
        id="generated-draft-loop",
        title="默认范式初稿闭环：安装副本上的核验入口放行初稿、对照页吃进带基础样式的产物、"
              "批注落盘后页序调整再重建，批注按稳定页面键找回且审阅标识不变",
        fixture="ae.py install 装进合成 HOME 的临时副本；按范式合成的三页报告（内联 report-base.css）、"
                "讲稿与 ae-report-record/1 核验记录；独立 AE_HOME，不碰真实台账",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
    ),
    Scenario(
        parallel_safe=True,
        id="report-verdict-loop",
        title="结构与视觉核验的可交付判定：真实渲染逐页测得溢出与无溢出并作为视觉证据写进记录，"
              "正确产物支持「报告可交付」，故障产物（如实登记的溢出、缺页覆盖）不能通过整体交付验收；"
              "判定离线完成不落批注，报告仍在对照页可审阅",
        fixture="ae.py install 装进合成 HOME 的临时副本；正确与故障两套合成产物（内联 report-base.css 的"
                "三页报告、讲稿、合成材料与 ae-report-record/1 记录，故障报告第 3 页带 1600px 宽块）；"
                "独立 AE_HOME，不碰真实台账",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
    ),
    Scenario(
        parallel_safe=True,
        id="revision-update-loop",
        title="改稿更新核验：可交付初态经改数字、来源变动、只改样式与页序调整后旧判定失效，"
              "维护记录（更新哈希、受影响引用与计算、revision 重新核验声明）后恢复，"
              "无法确认影响范围时摘要如实披露待核验范围与原因，页序重排按稳定页面键保持关联",
        fixture="ae.py install 装进合成 HOME 的临时副本；带完整核验记录的合成三页报告（内联 "
                "report-base.css）、讲稿与合成材料；独立 AE_HOME，不碰真实台账",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
    ),
    Scenario(
        parallel_safe=True,
        id="legacy-report-compat",
        title="旧报告无核验记录仍直接审阅：批注落盘、页面提交（已初改·待复核）、接收初改、重建，"
              "批次停在待复核且报告质量状态独立；整体核验请求下按完整清单建立记录恢复可交付，"
              "原始依据不可得时缺口登记不伪称核验完成",
        fixture="无核验记录的合成三页报告与讲稿（部分用例带合成材料、部分材料缺席）；"
                "独立 AE_HOME，不碰真实台账",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
    ),
    Scenario(
        parallel_safe=True,
        id="external-method-loop",
        title="外部生成技能共用验收：明确指定的合成外部技能被实际调用（运行其脚本，"
              "可观察的调用效果与调用说明写进记录），产物走同一核验入口与记录契约、"
              "打开现有对照页渲染并批注；缺依据的外部产物与换成内置方法声明的孪生"
              "记录判定一致，外部方法不放宽验收",
        fixture="ae.py install 装进合成 HOME 的临时副本；fixtures/external-report-skill 与 "
                "external-report-skill-lite（受控合成外部技能）复制进同一合成技能目录；"
                "按技能脚本合成的两页简报、讲稿与 ae-report-record/1 记录、合成材料；"
                "独立 AE_HOME，不碰真实台账",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
    ),
    Scenario(
        parallel_safe=True,
        id="installed-delivery",
        title="从唯一的临时安装副本完成英文、中文、中英混合三种语言组合的真实审阅流程",
        fixture="ae.py install 装进合成 HOME 的临时副本；fixtures/sample 合成报告与讲稿；"
                "独立 AE_HOME，不碰真实台账",
        isolation="每种语言组合独立临时审阅目录、独立端口与浏览器上下文；CLI 子进程全部登记回收",
        aliases=("--delivery-only",),
    ),
    Scenario(
        parallel_safe=True,
        id="locale-live-controls",
        title="同一页切换界面语言只改系统标签：原文、选区、待提交标注与弹窗留在原位，auto 在仅报告模式下先解析成具体语言",
        fixture="内嵌词典的合成单文件（含仅报告、无讲稿前言的 auto 变体）",
        isolation="独立临时审阅目录与浏览器上下文",
    ),
    Scenario(
        parallel_safe=True,
        id="skill-search",
        title="技能输入栏的搜索与选择：方向键、光标处替换、输入法组合、常驻技能编辑，文件与服务两种模式一致",
        fixture="合成 skills.json（含恶意说明与插件技能），经真实 build_viewer 生成页面",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
        aliases=("--skills-only",),
    ),
    Scenario(
        parallel_safe=True,
        id="skill-recommendations",
        title="技能推荐：批注内容、材料文件名与当前页标题影响候选，可浏览与搜索完整目录，编辑草稿互不干扰",
        fixture="合成 skills.json 大目录与中性标题报告，经真实 build_viewer 生成页面",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
        aliases=("--skills-only",),
    ),
    Scenario(
        parallel_safe=True,
        id="skill-i18n",
        title="技能选择与常驻技能的中英界面：范围与调用名的存储值不变、搜索覆盖原文、页面与 CLI 输出一致",
        fixture="205 条合成技能目录（含插件命名空间与特殊字符）与四类范围的常驻技能",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
        aliases=("--skills-only",),
    ),
    Scenario(
        parallel_safe=True,
        id="full-check",
        title="全量勾稽提交：页面勾选与附言落盘、等待中的 Agent 收到要求、断线暂存、旧服务不静默丢弃并提示升级",
        fixture="fixtures/sample 合成材料生成的审阅；notes.py claim/wait 真实子进程",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
        aliases=("--full-check-only",),
    ),
    Scenario(
        parallel_safe=True,
        id="locale-loop",
        title="语言最小闭环：内嵌词典、0/1/2 计数、切换不写批注不改审阅标识，file:// 与服务模式都能加批注，auto 跟随浏览器语言",
        fixture="英文与 auto 两种 locale 的合成单文件；服务模式读落盘 notes.json",
        isolation="独立临时审阅目录、独立端口与浏览器上下文（auto 逐语言另开），服务用后即停",
    ),
    Scenario(
        parallel_safe=True,
        id="english-script",
        title="英文讲稿在两种界面语言下逐页浏览：栏目标签跟界面、正文和时长跟讲稿；缺页时 --lenient 显示空讲稿",
        fixture="fixtures/sample 的英文讲稿与其截去后段的缺页变体（--lenient 构建）",
        isolation="独立临时审阅目录与浏览器上下文",
    ),
    Scenario(
        parallel_safe=True,
        id="handoff-i18n",
        title="提交、复核与处理记录在中英两种语言下同一含义：结构化选项不随语言改写、断线说清暂存、服务不匹配给出恢复方式、旧服务提示升级",
        fixture="英文 locale 的合成审阅；notes.py wait/resolve/done 真实子进程；路由模拟旧服务响应",
        isolation="独立临时审阅目录、独立端口与浏览器上下文，服务用后即停",
        aliases=("--handoff-only",),
    ),
    Scenario(
        parallel_safe=True,
        id="current-task-controls",
        title="当前任务详情的展开与收起、双语与轮询保持状态，以及安全停止本审阅服务并保留数据",
        fixture="fixtures/sample 合成材料生成的审阅；独立批次、待补传改动与服务重启",
        isolation="独立临时审阅目录、独立端口与浏览器上下文；所有服务和 CLI 子进程用后回收",
    ),
    Scenario(
        parallel_safe=True,
        id="export-import",
        title="双语导出与跨语言导入：中英文 Markdown 往返、页面与 CLI 互导、解析按格式标记选文法、旧中文格式不退步、认不出的文字有明确提示",
        fixture="E2E_NOTES 合成批注（含删除 / 改写、标注、风格、对应句与技能）；CLI 侧独立审阅目录",
        isolation="独立临时审阅目录与浏览器上下文；CLI 审阅目录单独构建",
    ),
    Scenario(
        parallel_safe=True,
        id="file-mode",
        title="file:// 退回模式：批注存 localStorage、刷新仍在、两个标签页互不覆盖、CSP 拦截内联事件、发现并导入旧版批注、导出 Markdown、「交给 Agent」退回为复制",
        fixture="合成单文件与旧版 v7viewer 批注（注入用字符串均为合成数据）",
        isolation="独立临时审阅目录与浏览器上下文（同上下文的第二个标签页是场景自身行为）",
    ),
    Scenario(
        parallel_safe=True,
        id="css-fidelity",
        title="报告样式保真：报告 :root 变量在 Shadow DOM 里生效、外壳同名变量不渗入、body 布局不把幻灯片挤偏、rem 按报告根字号",
        fixture="自带样式的合成报告页（内联 :root、body flex 与 rem 用例）",
        isolation="独立临时审阅目录与浏览器上下文",
    ),
    Scenario(
        parallel_safe=True,
        id="server-mode",
        title="服务模式全流程：落盘与编辑、标点 / 区域交互、快捷删除与改写、风格优化、常驻技能、轮询不打断草稿、交给 Agent 唤醒等待命令，随后断线补传、写入失败、服务卡死、同一端口换审阅、恶意网页与恶意导入",
        fixture="fixtures/sample 合成材料生成的审阅；导入用的 Markdown 由真实 CLI（resolve 标已处理后 "
                "notes.py export --all）从独立合成审阅导出；恶意页面与批注均为合成载荷",
        isolation="独立临时审阅目录与端口；一个跨阶段的浏览器上下文承载同一次审阅会话，"
                  "服务进程全部经登记表启动与回收，场景结束即停",
    ),
]

SCENARIO_IDS: tuple[str, ...] = tuple(scenario.id for scenario in SCENARIOS)

# 旧局部参数保留为兼容别名：原定目标行为不变，只修正「判断参数前先跑
# 安装交付与语言控件」的问题。别名不得夹带目标之外的场景。
LEGACY_FLAGS: dict[str, tuple[str, ...]] = {
    "--delivery-only": ("installed-delivery",),
    "--full-check-only": ("full-check",),
    "--handoff-only": ("handoff-i18n",),
    "--skills-only": ("skill-search", "skill-recommendations", "skill-i18n"),
}

SELECTION_FLAGS = ("--only",)
LISTING_FLAG = "--list"


class UsageError(Exception):
    """浏览器场景参数用错：入口以非零退出码结束，不启动浏览器。"""


@dataclasses.dataclass(frozen=True)
class Request:
    mode: str                    # "full" | "partial"
    plan: tuple[str, ...]        # 实际执行的场景，按注册表顺序去重
    requested: tuple[str, ...]   # 用户原始选择（去重前，便于记录解释）
    source: str                  # "default" | "--only" | 旧参数名
    listing: bool = False        # 只要求打印场景清单


def scenario_by_id(scenario_id: str) -> Scenario:
    for scenario in SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(scenario_id)


def _unknown(names: list[str]) -> str:
    return "未知的浏览器场景：%s（可选：%s）\n用 --list 查看场景清单与行为说明。" % (
        "、".join(names), "、".join(SCENARIO_IDS))


def _split_ids(raw: str) -> list[str]:
    return [token.strip() for token in raw.split(",") if token.strip()]


def resolve_request(argv: list[str]) -> Request:
    """解析浏览器入口参数。空选择、未知场景与互相冲突的组合都抛 UsageError。"""
    listing = False
    requested: list[str] = []
    source: Optional[str] = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == LISTING_FLAG:
            listing = True
        elif token in SELECTION_FLAGS:
            index += 1
            if index >= len(argv):
                raise UsageError("%s 需要场景 ID，例如 --only skill-i18n；用 --list 查看清单。" % token)
            if source not in (None, "--only"):
                raise UsageError("冲突的选择：%s 不能与旧参数 %s 混用，请只用一种选择方式。"
                                 % (token, source))
            source = "--only"
            requested.extend(_split_ids(argv[index]))
        elif token in LEGACY_FLAGS:
            if source is not None:
                partner = ("旧参数 " + source) if source in LEGACY_FLAGS else source
                raise UsageError("冲突的选择：%s 不能与 %s 混用，请只用一种选择方式。"
                                 % (token, partner))
            source = token
            requested.extend(LEGACY_FLAGS[token])
        elif token.startswith("-"):
            raise UsageError("未知的参数：%s\n用 --list 查看场景清单；"
                             "旧参数 --delivery-only / --skills-only / --full-check-only / "
                             "--handoff-only 仍可用。" % token)
        else:
            raise UsageError("未知的参数：%s（场景选择用 --only，清单用 --list）" % token)
        index += 1

    if listing and source is not None:
        raise UsageError("冲突的选择：--list 只打印清单，不能与 %s 的场景选择同时使用。" % source)
    if listing:
        return Request(mode="full", plan=SCENARIO_IDS, requested=(), source="default", listing=True)
    if source is None:
        return Request(mode="full", plan=SCENARIO_IDS, requested=(), source="default")
    if not requested:
        raise UsageError("空的选择：没有选中任何浏览器场景；用 --only 场景1,场景2 或 --list 查看清单。")
    unknown = [name for name in requested if name not in SCENARIO_IDS]
    if unknown:
        raise UsageError(_unknown(unknown))
    plan = tuple(sid for sid in SCENARIO_IDS if sid in set(requested))   # 去重并按注册顺序执行
    return Request(mode="partial", plan=plan, requested=tuple(requested), source=source)


def format_listing() -> str:
    lines = ["浏览器场景清单（%d 个；无参数时全部按此顺序执行）：\n" % len(SCENARIOS)]
    for scenario in SCENARIOS:
        marks = ["全量必选" if scenario.full_required else "全量可选"]
        if scenario.aliases:
            marks.append("旧参数 %s" % " / ".join(scenario.aliases))
        lines.append("%-22s %s" % (scenario.id, "（%s）" % "；".join(marks)))
        lines.append("  行为：%s" % scenario.title)
        lines.append("  fixture：%s" % scenario.fixture)
        lines.append("  隔离：%s\n" % scenario.isolation)
    lines.append("选择：--only 场景1,场景2（去重，按上面顺序执行）；"
                 "未知 / 空 / 冲突的选择在启动浏览器前报错退出。")
    return "\n".join(lines)


def format_plan(request: Request) -> str:
    scope = "全部 %d 个注册场景" % len(SCENARIO_IDS) if request.mode == "full" else "部分场景"
    lines = ["本次执行计划（%s）：" % scope]
    for position, sid in enumerate(request.plan, start=1):
        scenario = scenario_by_id(sid)
        lines.append("%2d. %-22s %s" % (position, sid, scenario.title))
    return "\n".join(lines)


def ensure_runners(runner_ids) -> Optional[str]:
    """注册表与执行函数的一致性检查；两边不一致时返回错误说明。"""
    runner_ids = set(runner_ids)
    registered = set(SCENARIO_IDS)
    missing = [sid for sid in SCENARIO_IDS if sid not in runner_ids]
    unregistered = sorted(runner_ids - registered)
    if not missing and not unregistered:
        return None
    parts = []
    if missing:
        parts.append("注册表里有场景但 e2e_browser.py 没有执行函数：%s" % "、".join(missing))
    if unregistered:
        parts.append("有执行函数但没在注册表登记：%s" % "、".join(unregistered))
    parts.append("两侧必须一致：先在 tests/e2e_scenarios.py 登记，再到 SCENARIO_RUNNERS 挂函数。")
    return "；".join(parts)
