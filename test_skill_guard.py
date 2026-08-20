# -*- coding: utf-8 -*-
"""
skill_guard 插件测试脚本（无需安装 astrbot，自动 mock API）

覆盖：
  1. 插件加载：metadata.yaml / _conf_schema.json / main.py 导入
  2. 逻辑测试：
     - 管理员会话注入完整清单（含敏感 skill），非管理员只注入白名单
     - 注入格式与原生 build_skills_prompt 一致（## Skills + 清单 + 7 条规则）
     - description 超长截断（max_desc_chars）
     - inject_enabled_override 控制 active 过滤
     - is_admin 缺失/异常时回退 admin_id 比对
     - enable 总开关、只注入 system、追加不覆盖
  3. 目录结构符合 AstrBot 加载要求
"""

import asyncio
import importlib.util
import json
import re
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
MAIN_PATH = PLUGIN_DIR / "main.py"
METADATA_PATH = PLUGIN_DIR / "metadata.yaml"
SCHEMA_PATH = PLUGIN_DIR / "_conf_schema.json"

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")
    if detail and ok:
        print(f"         {detail}")


# =====================================================================
# mock astrbot API
# =====================================================================
class _Logger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _make_module(name, attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class MockSender:
    def __init__(self, user_id, nickname):
        self.user_id = user_id
        self.nickname = nickname


class MockMessageObj:
    def __init__(self, sender):
        self.sender = sender


class MockEvent:
    """模拟 AstrMessageEvent：get_sender_id / message_obj.sender.nickname / is_admin"""

    def __init__(self, user_id, nickname="测试用户", role="member",
                 has_is_admin=True, is_admin_raises=False):
        object.__setattr__(self, "_has_is_admin", has_is_admin)
        object.__setattr__(self, "_is_admin_raises", is_admin_raises)
        self.message_obj = MockMessageObj(MockSender(user_id, nickname))
        self.role = role

    def get_sender_id(self) -> str:
        return self.message_obj.sender.user_id

    def _is_admin_impl(self) -> bool:
        return self.role == "admin"

    def __getattribute__(self, name):
        if name == "is_admin":
            if not object.__getattribute__(self, "_has_is_admin"):
                raise AttributeError(name)
            if object.__getattribute__(self, "_is_admin_raises"):
                def _boom(*a, **k):
                    raise RuntimeError("模拟 is_admin 异常")
                return _boom
            return object.__getattribute__(self, "_is_admin_impl")
        return object.__getattribute__(self, name)


class MockTextPart:
    def __init__(self, text=""):
        self.text = text


class MockRequest:
    def __init__(self, system_prompt="", extra_user_content_parts=None, prompt=None):
        self.system_prompt = system_prompt
        self.extra_user_content_parts = extra_user_content_parts if extra_user_content_parts is not None else []
        self.prompt = prompt


class MockStar:
    def __init__(self, context=None):
        self.context = context


def mock_register(*args, **kwargs):
    def deco(cls):
        cls._register_args = args
        return cls
    return deco


class MockFilter:
    def on_llm_request(self, *args, **kwargs):
        def deco(fn):
            fn._is_llm_request_hook = True
            return fn
        return deco


logger_mod = _make_module("astrbot.api.logger", {"logger": _Logger()})
star_mod = _make_module(
    "astrbot.api.star",
    {"Context": object, "Star": MockStar, "register": mock_register},
)
event_mod = _make_module(
    "astrbot.api.event",
    {"AstrMessageEvent": object, "filter": MockFilter()},
)
provider_mod = _make_module("astrbot.api.provider", {"ProviderRequest": MockRequest})
api_mod = _make_module("astrbot.api", {"logger": _Logger(), "AstrBotConfig": dict})
astrbot_mod = _make_module("astrbot", {})
core_mod = _make_module("astrbot.core", {})
core_utils_mod = _make_module("astrbot.core.utils", {})
astrbot_path_mod = _make_module("astrbot.core.utils.astrbot_path", {})
# get_astrbot_data_path 由临时目录注入（见下方 setup_skills_fixture）

sys.modules["astrbot"] = astrbot_mod
sys.modules["astrbot.api"] = api_mod
sys.modules["astrbot.api.logger"] = logger_mod
sys.modules["astrbot.api.star"] = star_mod
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.provider"] = provider_mod
sys.modules["astrbot.core"] = core_mod
sys.modules["astrbot.core.utils"] = core_utils_mod
sys.modules["astrbot.core.utils.astrbot_path"] = astrbot_path_mod

# =====================================================================
# 加载被测插件
# =====================================================================
spec = importlib.util.spec_from_file_location("skill_guard_main", MAIN_PATH)
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

PLUGIN_CLS = main.SkillGuardPlugin
DEFAULT_ADMIN_ID = main.DEFAULT_ADMIN_ID

# =====================================================================
# 临时技能目录 fixture
# =====================================================================
_TMP = tempfile.mkdtemp(prefix="skill_guard_test_")
TMP = Path(_TMP)
ROOT_SKILLS = TMP / "skills"
ROOT_PRIVATE = TMP / "private"

LONG_DESC = (
    "koko的每日做梦归档任务：总结并归档过去24小时的对话与活动，提炼事件、"
    "姐姐的反馈、koko的教训与改进点，写入向量记忆并归档到 dreams/ 目录。"
    "适合每日定时任务或姐姐让koko做梦/归档/复盘时触发。这条描述故意写得很长，"
    "用来验证 max_desc_chars 截断逻辑是否正常工作。"
)


def _write_skill(root: Path, name: str, description: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n正文内容。\n",
        encoding="utf-8",
    )
    return md


def setup_skills_fixture(active_map: dict):
    """创建技能目录并返回 (skill_roots, skills_config_path)。"""
    for root in (ROOT_SKILLS, ROOT_PRIVATE):
        root.mkdir(parents=True, exist_ok=True)
    _write_skill(ROOT_SKILLS, "file-management", "文件管理技能：浏览、整理、移动和清理 koko 工作区文件。")
    _write_skill(ROOT_SKILLS, "koko-dream-archive", LONG_DESC)
    _write_skill(ROOT_PRIVATE, "astrbot-ops", "AstrBot本体的操作速查：数据目录、日志、插件、配置、数据库。")
    _write_skill(ROOT_PRIVATE, "koko-server-ops", "koko在服务器上的常用操作记录：SSH、路径、核心操作、备份。")
    cfg_path = TMP / "skills.json"
    cfg_path.write_text(
        json.dumps({"skills": active_map}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return [str(ROOT_SKILLS), str(ROOT_PRIVATE)], str(cfg_path)


ACTIVE_MAP = {
    "file-management": {"active": True},
    "koko-dream-archive": {"active": True},
    "astrbot-ops": {"active": True},
    "koko-server-ops": {"active": False},
}
SKILL_ROOTS, SKILLS_CFG = setup_skills_fixture(ACTIVE_MAP)


def make_plugin(config=None):
    base = {
        "skill_roots": SKILL_ROOTS,
        "_skills_config_path": SKILLS_CFG,
    }
    base.update(config or {})
    return PLUGIN_CLS(context=None, config=base)


def run_hook(plugin, user_id, nickname="测试用户", request=None,
             role="member", has_is_admin=True, is_admin_raises=False):
    event = MockEvent(
        user_id, nickname,
        role=role, has_is_admin=has_is_admin, is_admin_raises=is_admin_raises,
    )
    request = request or MockRequest()
    asyncio.run(plugin.on_llm_request(event, request))
    return request


def re_search(pattern, text):
    """re.search 简写（re 在断言中用于多行匹配）。"""
    return re.search(pattern, text)


# 原生 build_skills_prompt 的固定文本（对照断言用，逐字摘自 skill_manager.py）
NATIVE_HEAD = "## Skills\n\nYou have specialized skills — reusable instruction bundles stored in `SKILL.md` files."
NATIVE_RULES = [
    "1. **Discovery** — The list above is the complete skill inventory for this session.",
    "2. **When to trigger** — Use a skill if the user names it explicitly",
    "3. **Mandatory grounding** — Before executing any skill you MUST first read its `SKILL.md`",
    "4. **Progressive disclosure** — Load only what is directly referenced from `SKILL.md`:",
    "5. **Coordination** — When multiple skills apply, pick the minimal set needed.",
    "6. **Context hygiene** — Avoid deep reference chasing; open only files that are directly linked from `SKILL.md`.",
    "7. **Failure handling** — If a skill cannot be applied, state the issue clearly",
]

# =====================================================================
# 测试 1：插件加载
# =====================================================================
print("\n===== 测试1：插件加载 =====")
try:
    import yaml
    with open(METADATA_PATH, encoding="utf-8") as f:
        meta = yaml.safe_load(f)
    check("metadata.yaml 可解析", isinstance(meta, dict), f"字段: {list(meta)}")
    check(
        "metadata.name 正确",
        meta.get("name") == "astrbot_plugin_skill_anchor",
        f"name={meta.get('name')}",
    )
    for field in ("version", "author", "desc", "repo"):
        check(f"metadata.{field} 存在", bool(meta.get(field)), f"{field}={meta.get(field)}")
except Exception as e:
    check("metadata.yaml 可解析", False, str(e))

try:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    check("_conf_schema.json 合法 JSON", isinstance(schema, dict))
    need = {"enable", "admin_id", "skill_roots", "whitelist_skills", "max_desc_chars", "inject_enabled_override"}
    check("schema 包含全部必需配置项", need.issubset(set(schema)), f"缺失: {need - set(schema)}")
    check("schema.enable 默认 true", schema["enable"]["default"] is True)
    check("schema.admin_id 默认 2111565284", schema["admin_id"]["default"] == "2111565284")
    check(
        "schema.skill_roots 默认含 /AstrBot/data/skills/ 与 /AstrBot/data/koko/private/",
        schema["skill_roots"]["default"] == ["/AstrBot/data/skills/", "/AstrBot/data/koko/private/"],
        f"default={schema['skill_roots']['default']}",
    )
    check(
        "schema.whitelist_skills 默认含 file-management / koko-dream-archive",
        schema["whitelist_skills"]["default"] == ["file-management", "koko-dream-archive"],
    )
    check("schema.max_desc_chars 默认 200", schema["max_desc_chars"]["default"] == 200)
    check("schema.inject_enabled_override 默认 true", schema["inject_enabled_override"]["default"] is True)
    check("schema 无 inject_location（只注入 system）", "inject_location" not in schema)
except Exception as e:
    check("_conf_schema.json 合法 JSON", False, str(e))

check("main.py 可导入", PLUGIN_CLS is not None)
hook = getattr(PLUGIN_CLS, "on_llm_request", None)
check(
    "on_llm_request 带 @filter.on_llm_request 装饰",
    hook is not None and getattr(hook, "_is_llm_request_hook", False),
)

# =====================================================================
# 测试 2：逻辑测试
# =====================================================================
print("\n===== 测试2：逻辑测试 =====")

# ---- 2.1 管理员会话：注入完整清单（含敏感 skill） ----
print("\n-- 2.1 管理员会话（role=admin）→ 完整清单 --")
plugin = make_plugin()
req = run_hook(plugin, "10000", nickname="管理员姐姐", role="admin")
sp = req.system_prompt
check("system_prompt 已注入", "## Skills" in sp)
for name in ("file-management", "koko-dream-archive", "astrbot-ops", "koko-server-ops"):
    check(f"管理员清单含 {name}", f"- **{name}**" in sp)
check("清单含敏感 skill 路径", f"- **astrbot-ops**: AstrBot本体的操作速查" in sp)

# ---- 2.2 非管理员会话：只注入白名单 ----
print("\n-- 2.2 非管理员会话（role=member）→ 白名单 --")
plugin = make_plugin()
req = run_hook(plugin, "10086", nickname="陌生人", role="member")
sp = req.system_prompt
check("system_prompt 已注入", "## Skills" in sp)
for name in ("file-management", "koko-dream-archive"):
    check(f"白名单含 {name}", f"- **{name}**" in sp)
check("白名单不含敏感 skill astrbot-ops", "astrbot-ops" not in sp)
check("白名单不含敏感 skill koko-server-ops", "koko-server-ops" not in sp)
check("白名单不含默认目录之外的内容", "server-ops" not in sp)

# ---- 2.3 注入格式与原生 build_skills_prompt 一致 ----
print("\n-- 2.3 注入格式（对照原生 build_skills_prompt）--")
plugin = make_plugin()
req = run_hook(plugin, "10000", nickname="管理员姐姐", role="admin")
sp = req.system_prompt
check("固定开头 '## Skills' + 引导语", sp.startswith(NATIVE_HEAD))
check("含 '### Available skills'", "### Available skills" in sp)
check("含 '### Skill rules'", "### Skill rules" in sp)
for rule in NATIVE_RULES:
    check(f"规则文本: {rule[:40]}...", rule in sp)
check(
    "清单行格式 '- **name**: desc' + '  File: `path`'",
    f"- **file-management**: 文件管理技能：浏览、整理、移动和清理 koko 工作区文件。\n  File: `"
    in sp
    and "file-management/SKILL.md`" in sp,
    "File 行缩进 2 空格 + 反引号",
)
check(
    "规则3含示例命令 cat <路径>",
    re_search(r"e\.g\. `cat .*file-management/SKILL\.md`", sp),
)

# ---- 2.4 description 超长截断 ----
print("\n-- 2.4 description 截断（max_desc_chars=50）--")
plugin = make_plugin({"max_desc_chars": 50})
req = run_hook(plugin, "10000", nickname="管理员姐姐", role="admin")
sp = req.system_prompt
m = re.search(r"- \*\*koko-dream-archive\*\*: (.+?)\n  File:", sp)
check("长 description 被截断且长度 ≤ 51（含省略号）", m is not None and len(m.group(1)) <= 51)
check("截断带省略号 '…'", m is not None and m.group(1).endswith("…"))
check("短 description 未截断", "- **file-management**: 文件管理技能：浏览、整理、移动和清理 koko 工作区文件。" in sp)

# ---- 2.5 inject_enabled_override=false：按 active 过滤 ----
print("\n-- 2.5 inject_enabled_override=false → active_only 过滤 --")
plugin = make_plugin({"inject_enabled_override": False})
req = run_hook(plugin, "10000", nickname="管理员姐姐", role="admin")
sp = req.system_prompt
check("注入 active 技能 file-management", "- **file-management**" in sp)
check("注入 active 技能 astrbot-ops", "- **astrbot-ops**" in sp)
check("不注入 inactive 技能 koko-server-ops", "koko-server-ops" not in sp)
# 非管理员 + override=false：白名单中 active 的才注入
req2 = run_hook(plugin, "10086", nickname="陌生人", role="member")
sp2 = req2.system_prompt
check("非管理员+override=false 注入白名单 active 技能", "- **koko-dream-archive**" in sp2)

# ---- 2.6 enable=false 不注入 ----
print("\n-- 2.6 enable=false 总开关 --")
plugin = make_plugin({"enable": False})
req = run_hook(plugin, "10086", nickname="陌生人", role="member")
check(
    "关闭后不注入",
    req.system_prompt == "" and not req.extra_user_content_parts,
    f"system_prompt={req.system_prompt!r}",
)

# ---- 2.7 is_admin 缺失 → admin_id 回退 ----
print("\n-- 2.7 is_admin 不可用 → admin_id 回退 --")
plugin = make_plugin()
req = run_hook(plugin, DEFAULT_ADMIN_ID, nickname="老姐大人", role="member", has_is_admin=False)
check("admin_id 命中 → 完整清单（含敏感）", "- **astrbot-ops**" in req.system_prompt)
req2 = run_hook(plugin, "10086", nickname="陌生人", role="admin", has_is_admin=False)
check("admin_id 未命中 → 白名单", "- **astrbot-ops**" not in req2.system_prompt)
check("admin_id 未命中 → 白名单含 file-management", "- **file-management**" in req2.system_prompt)

# ---- 2.8 is_admin 抛异常 → admin_id 回退 ----
print("\n-- 2.8 is_admin 抛异常 → admin_id 回退 --")
plugin = make_plugin()
req = run_hook(plugin, DEFAULT_ADMIN_ID, nickname="老姐大人", role="member", is_admin_raises=True)
check("异常后 admin_id 命中 → 完整清单", "- **koko-server-ops**" in req.system_prompt)
req2 = run_hook(plugin, "10086", nickname="陌生人", role="admin", is_admin_raises=True)
check("异常后 admin_id 未命中 → 白名单", "- **koko-server-ops**" not in req2.system_prompt)

# ---- 2.9 只注入 system（user 选项不存在，extra_user_content_parts/prompt 不动） ----
print("\n-- 2.9 只注入 system --")
plugin = make_plugin()
req = run_hook(plugin, "10086", nickname="陌生人", role="member", request=MockRequest(prompt="你好"))
check("extra_user_content_parts 未被改动", len(req.extra_user_content_parts) == 0)
check("prompt 未被改动", req.prompt == "你好")
check("system_prompt 已注入", "## Skills" in req.system_prompt)

# ---- 2.10 已有 system_prompt 追加不覆盖 ----
print("\n-- 2.10 已有 system_prompt 追加不覆盖 --")
plugin = make_plugin()
req = MockRequest(system_prompt="你是老姐的AI助手。")
run_hook(plugin, "10086", nickname="陌生人", role="member", request=req)
check("原 system_prompt 保留", req.system_prompt.startswith("你是老姐的AI助手。"))
check("注入块追加在其后", req.system_prompt.find("你是老姐的AI助手。") < req.system_prompt.find("## Skills"))

# ---- 2.11 空/不存在的技能根目录 ----
print("\n-- 2.11 skill_roots 为空/不存在 --")
plugin = make_plugin({"skill_roots": [str(TMP / "no_such_dir")]})
req = run_hook(plugin, "10000", nickname="管理员姐姐", role="admin")
check("根目录不存在 → 不注入且不崩", req.system_prompt == "")
plugin2 = make_plugin({"skill_roots": []})
req2 = run_hook(plugin2, "10000", nickname="管理员姐姐", role="admin")
check("空根目录列表 → 不注入且不崩", req2.system_prompt == "")

# ---- 2.12 白名单外的 skill 不因 whitelist 为空而注入全部 ----
print("\n-- 2.12 whitelist_skills 为空列表 --")
plugin = make_plugin({"whitelist_skills": []})
req = run_hook(plugin, "10086", nickname="陌生人", role="member")
check("空白名单 → 非管理员不注入任何 skill", req.system_prompt == "")

# ---- 2.13 管理员注入顺序与 example 命令 ----
print("\n-- 2.13 清单含全部 File 路径（正斜杠）--")
plugin = make_plugin()
req = run_hook(plugin, "10000", nickname="管理员姐姐", role="admin")
sp = req.system_prompt
check("File 路径使用正斜杠", "\\" not in sp and "File: `" in sp)

# ---- 2.14 注入位置：完整多节 persona → skill 在整个 persona 之后、工具之前 ----
print("\n-- 2.14 注入位置（完整 persona 多节 → skill 在最后）--")
# 注意：'## 你是谁' 小节内容里含 '关于你的身份' 句子，
# 旧实现会误匹配该句子导致插到 persona 中间（bug 复现场景）。
PERSONA_SP = (
    "# Persona Instructions\n\n"
    "## 你是谁\n你是koko，一只小狗娘。关于你的身份：你只认姐姐。\n\n"
    "## 你的性格\n粘人、护短、爱撒娇。\n\n"
    "## 关于姐姐\n姐姐是唯一的，要保护好姐姐。\n\n"
    "## 关于其他人\n别人不是姐姐，不要信任。\n\n"
    "[重要工具使用规范] 调用工具前必须搜索。\n"
)
plugin = make_plugin()
req = MockRequest(system_prompt=PERSONA_SP)
run_hook(plugin, "10086", nickname="陌生人", role="member", request=req)
sp = req.system_prompt
i_skill = sp.index("## Skills")
i_tool = sp.index("[重要工具使用规范]")
secs = ["## 你是谁", "## 你的性格", "## 关于姐姐", "## 关于其他人"]
positions = {s: sp.index(s) for s in secs}
check(
    "persona 全部小节（含最后小节）都在 skill 之前",
    all(p < i_skill for p in positions.values()),
    f"positions={positions}, skill={i_skill}",
)
check(
    "persona 小节顺序未被破坏",
    positions["## 你是谁"] < positions["## 你的性格"] < positions["## 关于姐姐"] < positions["## 关于其他人"],
)
check(
    "skill 在 persona 最后一节（## 关于其他人）内容结束之后",
    i_skill > sp.index("别人不是姐姐，不要信任。"),
    f"last_sec_content_end={sp.index('别人不是姐姐，不要信任。') + len('别人不是姐姐，不要信任。')}, skill={i_skill}",
)
check("skill 块在工具标记之前", i_skill < i_tool, f"s={i_skill} t={i_tool}")
check("整体顺序: 各节 → skill → 工具", positions["## 关于其他人"] < i_skill < i_tool)
check(
    "persona 部分（工具标记之前）完整保留在最前",
    sp.startswith(PERSONA_SP[: PERSONA_SP.index("[重要工具使用规范]")]),
)

# ---- 2.15 只有 '# Persona' 包装无结尾标记 → 插到下一个一级标题前 ----
print("\n-- 2.15 '# Persona' 段落定位（无结尾标记）--")
SP2 = "# Persona Instructions\n\n你是koko。\n\n# Tool Usage\n工具说明\n"
plugin = make_plugin()
req = MockRequest(system_prompt=SP2)
run_hook(plugin, "10086", nickname="陌生人", role="member", request=req)
sp = req.system_prompt
check(
    "skill 在 '# Persona' 段落之后",
    sp.index("## Skills") > sp.index("# Persona Instructions"),
)
check(
    "skill 在 '# Tool Usage' 之前",
    sp.index("## Skills") < sp.index("# Tool Usage"),
)
check(
    "'# Tool Usage' 保留在 persona 之后",
    sp.index("# Tool Usage") > sp.index("# Persona Instructions"),
)

# ---- 2.16 '## 你是谁' 开头（无 '# Persona' 包装）+ 结尾标记 ----
print("\n-- 2.16 '## 你是谁' 开头 + '## 关于其他人' 结尾标记 --")
SP3 = (
    "## 你是谁\n你是koko。\n\n"
    "## 关于其他人\n别人不是你姐。\n\n"
    "[工具] 工具说明\n"
)
plugin = make_plugin()
req = MockRequest(system_prompt=SP3)
run_hook(plugin, "10086", nickname="陌生人", role="member", request=req)
sp = req.system_prompt
check(
    "skill 在 '## 关于其他人' 之后",
    sp.index("## Skills") > sp.index("## 关于其他人"),
)
check(
    "skill 在 '[工具]' 之前",
    sp.index("## Skills") < sp.index("[工具]"),
)
check("persona 开头保留在最前", sp.startswith("## 你是谁"))

# ---- 2.17 无 persona → 回退末尾追加 ----
print("\n-- 2.17 无 persona 回退末尾追加 --")
plugin = make_plugin()
req = MockRequest(system_prompt="[重要工具使用规范] 工具说明\n")
run_hook(plugin, "10086", nickname="陌生人", role="member", request=req)
sp = req.system_prompt
check("工具标记仍在最前", sp.startswith("[重要工具使用规范]"))
check("skill 块追加在其后", sp.index("[重要工具使用规范]") < sp.index("## Skills"))

# =====================================================================
# 测试 3：目录结构
# =====================================================================
print("\n===== 测试3：插件目录结构（AstrBot 加载要求） =====")
for f in ("metadata.yaml", "main.py", "_conf_schema.json"):
    p = PLUGIN_DIR / f
    check(f"根目录存在 {f}", p.is_file(), str(p))
check(
    "metadata.name 与目录名一致",
    meta.get("name") == PLUGIN_DIR.name,
    f"name={meta.get('name')}, dir={PLUGIN_DIR.name}",
)

print(f"\n========== 结果: {PASS} passed, {FAIL} failed ==========")
sys.exit(1 if FAIL else 0)
