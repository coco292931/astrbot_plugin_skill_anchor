"""
skill_anchor —— Skill 隔离守卫插件

按会话身份隔离注入 skill 清单，替代 AstrBot 原生全局 skill 注入：
- 管理员/姐姐会话：注入完整清单（含敏感 skill，如 astrbot-ops、koko-server-ops）
- 非管理员/陌生人会话：只注入白名单 skill，防止敏感 skill 泄露

注入格式与原生 build_skills_prompt（astrbot/core/skills/skill_manager.py）完全一致：
固定 "## Skills" 引导语 + "### Available skills" 清单 + "### Skill rules" 7 条规则。
注入位置：插到 '[重要工具使用规范]' 标记之前；找不到该标记则回退末尾追加。
"""

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - 测试环境无 astrbot.core
    get_astrbot_data_path = None

# ----------------------------------------------------------------------
# 默认配置（与 _conf_schema.json 保持一致）
# ----------------------------------------------------------------------
DEFAULT_ADMIN_ID = "2111565284"
DEFAULT_SKILL_ROOTS = [
    "/AstrBot/data/skills/",
    "/AstrBot/data/koko/private/",
]
DEFAULT_WHITELIST_SKILLS = ["file-management", "koko-dream-archive"]
DEFAULT_MAX_DESC_CHARS = 200

SKILLS_CONFIG_FILENAME = "skills.json"

_SKILL_NAME_RE = re.compile(r"^[\w.-]+$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1F\x7F]")


# ----------------------------------------------------------------------
# 技能信息与发现（对齐原生 SkillManager.list_skills 的行为）
# ----------------------------------------------------------------------
@dataclass
class SkillInfo:
    name: str
    description: str
    path: str
    active: bool


def _parse_frontmatter_description(text: str) -> str:
    """从 SKILL.md 的 YAML frontmatter 提取 description（与原生一致）。"""
    if not text.startswith("---"):
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return ""
    frontmatter = "\n".join(lines[1:end_idx])
    try:
        payload = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("description", "") or "").strip()


def _find_skill_md(skill_dir: Path) -> Path | None:
    """返回 skill 目录下的 SKILL.md（兼容小写 skill.md）。

    目录不可读（权限不足）时跳过，不中断整体扫描。
    """
    for fname in ("SKILL.md", "skill.md"):
        candidate = skill_dir / fname
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _load_skills_config(config_path: str | None) -> dict:
    """读取 skills.json（active 状态）。缺失/异常时返回空配置（默认全部 active）。"""
    if not config_path:
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("skills"), dict):
            return data
    except Exception as e:
        logger.debug(f"[skill_anchor] 读取 skills.json 失败({config_path}): {e}")
    return {}


def discover_skills(
    skill_roots: list[str],
    skills_config_path: str | None = None,
    active_only: bool = False,
) -> list[SkillInfo]:
    """
    扫描多个 skill 根目录，收集 SKILL.md 技能。

    - name：目录名（与原生 list_skills 一致）
    - description：SKILL.md frontmatter 的 description
    - path：SKILL.md 绝对路径（正斜杠）
    - active：来自 skills.json；active_only=True 时跳过 inactive 技能
    """
    config = _load_skills_config(skills_config_path)
    skill_configs = config.get("skills", {}) if config else {}

    skills_by_name: dict[str, SkillInfo] = {}
    for root_str in skill_roots or []:
        root = Path(root_str)
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError as e:
            logger.debug(f"[skill_anchor] 技能根目录不可读({root}): {e}")
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if not is_dir:
                continue
            name = entry.name
            if not _SKILL_NAME_RE.fullmatch(name):
                continue
            if name in skills_by_name:
                continue  # 多个根目录同名 skill，先发现者优先（同原生去重）
            skill_md = _find_skill_md(entry)
            if skill_md is None:
                continue
            active = bool(skill_configs.get(name, {}).get("active", True))
            if active_only and not active:
                continue
            description = ""
            try:
                content = skill_md.read_text(encoding="utf-8")
                description = _parse_frontmatter_description(content)
            except Exception:
                description = ""
            skills_by_name[name] = SkillInfo(
                name=name,
                description=description,
                path=str(skill_md).replace("\\", "/"),
                active=active,
            )
    return list(skills_by_name.values())


# ----------------------------------------------------------------------
# 注入块构建：与原生 build_skills_prompt 输出完全一致
# ----------------------------------------------------------------------
def _sanitize_prompt_description(description: str) -> str:
    description = description.replace("`", "")
    description = _CONTROL_CHARS_RE.sub(" ", description)
    description = " ".join(description.split())
    return description


def _sanitize_skill_display_name(name: str) -> str:
    if _SKILL_NAME_RE.fullmatch(name):
        return name
    return "<invalid_skill_name>"


def _build_skill_read_command_example(path: str) -> str:
    if path == "<skills_root>/<skill_name>/SKILL.md":
        return f"cat {path}"
    command = "cat"
    path_arg = shlex.quote(path)
    return f"{command} {path_arg}"


def build_skills_prompt(
    skills: list[SkillInfo],
    max_desc_chars: int | None = None,
) -> str:
    """
    生成与原生 build_skills_prompt 完全一致的 skill 注入块：
    "## Skills" 固定开头 + "### Available skills" 清单 + "### Skill rules" 7 条规则。
    max_desc_chars：description 超长截断并追加省略号（…）。
    """
    skills_lines: list[str] = []
    example_path = ""
    for skill in skills:
        display_name = _sanitize_skill_display_name(skill.name)

        description = skill.description or "No description"
        description = _sanitize_prompt_description(description)
        if not description:
            description = "Read SKILL.md for details."
        if max_desc_chars and len(description) > max_desc_chars:
            description = description[:max_desc_chars] + "…"

        rendered_path = skill.path or "<skills_root>/<skill_name>/SKILL.md"
        skills_lines.append(
            f"- **{display_name}**: {description}\n  File: `{rendered_path}`"
        )
        if not example_path:
            example_path = rendered_path

    skills_block = "\n".join(skills_lines)

    if example_path == "<skills_root>/<skill_name>/SKILL.md":
        example_path = "<skills_root>/<skill_name>/SKILL.md"
    else:
        example_path = example_path or "<skills_root>/<skill_name>/SKILL.md"
    example_command = _build_skill_read_command_example(example_path)

    # 以下引导语与规则文本逐字照抄原生 build_skills_prompt
    return (
        "## Skills\n\n"
        "You have specialized skills — reusable instruction bundles stored "
        "in `SKILL.md` files. Each skill has a **name** and a **description** "
        "that tells you what it does and when to use it.\n\n"
        "### Available skills\n\n"
        f"{skills_block}\n\n"
        "### Skill rules\n\n"
        "1. **Discovery** — The list above is the complete skill inventory "
        "for this session. Full instructions are in the referenced "
        "`SKILL.md` file.\n"
        "2. **When to trigger** — Use a skill if the user names it "
        "explicitly, or if the task clearly matches the skill's description. "
        "*Never silently skip a matching skill* — either use it or briefly "
        "explain why you chose not to.\n"
        "3. **Mandatory grounding** — Before executing any skill you MUST "
        "first read its `SKILL.md` by running a shell command compatible "
        "with the current runtime shell and using the **absolute path** "
        f"shown above (e.g. `{example_command}`). "
        "Never rely on memory or assumptions about a skill's content.\n"
        "4. **Progressive disclosure** — Load only what is directly "
        "referenced from `SKILL.md`:\n"
        "   - If `scripts/` exist, prefer running or patching them over "
        "rewriting code from scratch.\n"
        "   - If `assets/` or templates exist, reuse them.\n"
        "   - Do NOT bulk-load every file in the skill directory.\n"
        "5. **Coordination** — When multiple skills apply, pick the minimal "
        "set needed. Announce which skill(s) you are using and why "
        "(one short line). Prefer `astrbot_*` tools when running skill "
        "scripts.\n"
        "6. **Context hygiene** — Avoid deep reference chasing; open only "
        "files that are directly linked from `SKILL.md`.\n"
        "7. **Failure handling** — If a skill cannot be applied, state the "
        "issue clearly and continue with the best alternative.\n"
    )


# ----------------------------------------------------------------------
# 插件主体
# ----------------------------------------------------------------------
@register(
    "astrbot_plugin_skill_anchor",
    "coco",
    "Skill 隔离守卫：按会话身份隔离注入 skill 清单，防敏感 skill 泄露",
    "1.0.0",
    "https://github.com/coco292931/astrbot_plugin_skill_anchor",
)
class SkillGuardPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        if value is None:
            return default
        return value

    def _skills_config_path(self) -> str | None:
        """skills.json 路径：配置显式指定优先，否则用 AstrBot data 目录。"""
        explicit = self.config.get("_skills_config_path")
        if explicit:
            return str(explicit)
        if get_astrbot_data_path is not None:
            try:
                return str(Path(get_astrbot_data_path()) / SKILLS_CONFIG_FILENAME)
            except Exception as e:
                logger.debug(f"[skill_anchor] 定位 skills.json 失败: {e}")
        return None

    # ------------------------------------------------------------------
    # 身份判定：is_admin 优先，异常/不可用时回退 admin_id 比对
    # ------------------------------------------------------------------
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if callable(getattr(event, "is_admin", None)):
                return bool(event.is_admin())
        except Exception as e:
            logger.debug(f"[skill_anchor] is_admin 判定异常，回退 admin_id: {e}")

        # 回退：admin_id 配置比对
        try:
            user_id = str(event.get_sender_id() or "").strip()
        except Exception as e:
            logger.debug(f"[skill_anchor] 获取 sender_id 失败: {e}")
            return False
        admin_id = str(self._cfg("admin_id", DEFAULT_ADMIN_ID) or "").strip()
        return bool(user_id) and (user_id == admin_id)

    # ------------------------------------------------------------------
    # LLM 请求钩子：按身份注入隔离后的 skill 清单
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, request: ProviderRequest, *args, **kwargs
    ) -> None:
        """在请求发给 LLM 之前，按会话身份注入 skill 清单（只注入 system，末尾追加）。"""
        try:
            if not bool(self._cfg("enable", True)):
                return

            is_admin = self._is_admin(event)
            active_only = not bool(self._cfg("inject_enabled_override", True))
            skills_config_path = self._skills_config_path()

            if is_admin:
                skills = discover_skills(
                    self._cfg("skill_roots", DEFAULT_SKILL_ROOTS),
                    skills_config_path=skills_config_path,
                    active_only=active_only,
                )
            else:
                all_skills = discover_skills(
                    self._cfg("skill_roots", DEFAULT_SKILL_ROOTS),
                    skills_config_path=skills_config_path,
                    active_only=active_only,
                )
                whitelist = set(self._cfg("whitelist_skills", DEFAULT_WHITELIST_SKILLS))
                skills = [s for s in all_skills if s.name in whitelist]

            if not skills:
                return

            block = build_skills_prompt(
                skills,
                max_desc_chars=int(self._cfg("max_desc_chars", DEFAULT_MAX_DESC_CHARS) or 0)
                or None,
            )

            if not hasattr(request, "system_prompt"):
                return
            sp = request.system_prompt or ""
            marker = "重要工具使用规范"
            pos = sp.find(marker)
            if pos != -1:
                # 找到标记行首（标记可能不在行首，退回到行首）
                line_start = sp.rfind("\n", 0, pos)
                insert_at = line_start + 1 if line_start != -1 else pos
                request.system_prompt = sp[:insert_at] + f"{block}\n" + sp[insert_at:]
            elif sp:
                request.system_prompt = sp + f"\n{block}\n"
            else:
                request.system_prompt = block + "\n"

            logger.debug(
                f"[skill_anchor] 已注入(system) admin={is_admin} "
                f"skills={[s.name for s in skills]}"
            )
        except Exception as e:
            logger.warning(f"[skill_anchor] 注入失败: {e}")
