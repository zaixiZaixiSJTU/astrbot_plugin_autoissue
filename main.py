"""AstrBot AutoIssue Plugin"""

import json
import re
import asyncio
from pathlib import Path
from typing import Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import At

_BINDINGS_FILE = Path(__file__).parent / "repo_bindings.json"


def _load_bindings() -> dict:
    try:
        if _BINDINGS_FILE.exists():
            return json.loads(_BINDINGS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"AutoIssue: failed to load bindings: {e}")
    return {}


def _save_bindings(bindings: dict) -> None:
    try:
        _BINDINGS_FILE.write_text(
            json.dumps(bindings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"AutoIssue: failed to save bindings: {e}")


@register(
    "astrbot_plugin_autoissue",
    "zaixiZaixiSJTU",
    "auto create GitHub Issue from forwarded messages",
    "1.0.0",
)
class AutoIssuePlugin(Star):

    def __init__(self, context: Context, config):
        super().__init__(context)
        self.github_token: str = config.get("github_token", "")
        self.trigger_keyword: str = config.get("trigger_keyword", "issue")
        self.require_at_bot: bool = config.get("require_at_bot", True)
        self.llm_system_prompt: str = config.get("llm_system_prompt", "")
        self.http_proxy: str = config.get("http_proxy", "") or None

        self.repo_bindings: dict = _load_bindings()

        logger.info(
            f"AutoIssue: init ok | token={'yes' if self.github_token else 'NO'} | "
            f"bindings={len(self.repo_bindings)}"
        )

    async def initialize(self):
        if not self.github_token:
            logger.warning("AutoIssue: github_token not configured!")

    # ---- main listener ----

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event):
        msg_text: str = event.message_str or ""
        logger.info(f"AutoIssue: on_message | msg={repr(msg_text)}")
        if self.trigger_keyword not in msg_text:
            return
        if self.require_at_bot:
            at_bot = False
            try:
                self_id = str(event.message_obj.self_id)
                for comp in event.message_obj.message:
                    if isinstance(comp, At) and str(comp.qq) == self_id:
                        at_bot = True
                        break
                if not at_bot:
                    raw = event.message_obj.raw_message or ""
                    if isinstance(raw, str) and f"qq={self_id}" in raw:
                        at_bot = True
            except Exception as e:
                logger.warning(f"AutoIssue: at check error: {e}")
            logger.info(f"AutoIssue: at_bot={at_bot} self_id={getattr(event.message_obj, 'self_id', '?')} comps={[(type(c).__name__, vars(c)) for c in event.message_obj.message]}")
            if not at_bot:
                return

        group_id = self._extract_group_id(event.session_id)
        logger.info(f"AutoIssue: group_id={group_id} bindings={self.repo_bindings}")
        if not group_id:
            return

        repo = self.repo_bindings.get(group_id)
        if not repo:
            yield event.plain_result(
                f"group not bound. use /bindrepo owner/repo\ngroup_id: {group_id}"
            )
            return

        is_reply = self._is_reply(event)
        logger.info(f"AutoIssue: is_reply={is_reply}")
        if not is_reply:
            yield event.plain_result("please reply to a (forwarded) message first")
            return

        yield event.plain_result("analyzing...")

        content = await self._extract_quoted_content(event)
        if not content:
            yield event.plain_result("failed to extract quoted content")
            return

        issue_data = await self._llm_format(content, event)
        if not issue_data:
            yield event.plain_result("LLM failed to generate issue content")
            return

        result = await self._create_issue(repo, issue_data)
        if result and result.startswith("https://"):
            yield event.plain_result(f"Issue created: {result}")
        else:
            yield event.plain_result(f"failed: {result or 'unknown'}")

    # ---- commands (use AstrBot param injection) ----

    @filter.command("bindrepo")
    async def cmd_bind(self, event, repo: str):
        """bind group to repo: /bindrepo owner/repo"""
        if not self._is_group_admin(event):
            yield event.plain_result("admin/owner only")
            return
        if "/" not in repo:
            yield event.plain_result("format: /bindrepo owner/repo")
            return
        if not self.github_token:
            yield event.plain_result("github_token not configured in plugin settings")
            return
        ok, msg = await self._verify_repo(repo)
        if not ok:
            yield event.plain_result(f"verify failed: {msg}")
            return
        gid = self._extract_group_id(event.session_id)
        if not gid:
            yield event.plain_result("cannot determine group id")
            return
        self.repo_bindings[gid] = repo
        _save_bindings(self.repo_bindings)
        logger.info(f"AutoIssue: bind {gid} -> {repo}")
        yield event.plain_result(f"bound group {gid} -> {repo}")

    @filter.command("unbindrepo")
    async def cmd_unbind(self, event):
        """unbind group: /unbindrepo"""
        if not self._is_group_admin(event):
            yield event.plain_result("admin/owner only")
            return
        gid = self._extract_group_id(event.session_id)
        if gid and gid in self.repo_bindings:
            del self.repo_bindings[gid]
            _save_bindings(self.repo_bindings)
            yield event.plain_result(f"unbound group {gid}")
        else:
            yield event.plain_result("group not bound")

    @filter.command("issuestatus")
    async def cmd_status(self, event):
        """show plugin status: /issuestatus"""
        if not self._is_group_admin(event):
            yield event.plain_result("admin/owner only")
            return
        gid = self._extract_group_id(event.session_id)
        bound = self.repo_bindings.get(gid, "none") if gid else "?"
        yield event.plain_result(
            f"AutoIssue status\n"
            f"token: {'ok' if self.github_token else 'MISSING'}\n"
            f"keyword: {self.trigger_keyword}\n"
            f"require @bot: {self.require_at_bot}\n"
            f"group({gid}): {bound}\n"
            f"total bindings: {len(self.repo_bindings)}"
        )

    # ---- internals ----

    @staticmethod
    def _is_group_admin(event) -> bool:
        return getattr(event, "role", "member") in ("admin", "owner")

    @staticmethod
    def _extract_group_id(session_id: str) -> Optional[str]:
        parts = session_id.split("_")
        return parts[1] if len(parts) >= 2 else session_id

    @staticmethod
    def _get_reply_comp(event):
        """Return the Reply component if present, else None."""
        try:
            for comp in event.message_obj.message:
                if type(comp).__name__ == "Reply":
                    return comp
        except Exception:
            pass
        return None

    def _is_reply(self, event) -> bool:
        if self._get_reply_comp(event):
            return True
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, str):
                return "[CQ:reply" in raw
        except Exception:
            pass
        return False

    async def _extract_quoted_content(self, event) -> Optional[str]:
        try:
            reply = self._get_reply_comp(event)
            if reply:
                bot = getattr(event, "bot", None)
                lines = await self._extract_from_chain(reply.chain, bot=bot)
                text = "\n".join(l for l in lines if l)
                return text.strip() or None
        except Exception as e:
            logger.error(f"AutoIssue: extract error: {e}")
        return None

    async def _extract_from_chain(self, chain, bot=None, depth: int = 0) -> list:
        """递归提取消息链中的文本和图片内容，支持合并转发。"""
        lines = []
        if depth > 5:
            return lines
        for comp in (chain or []):
            ctype = type(comp).__name__
            if ctype == "Json":
                # comp.data 可能是 dict 或 JSON 字符串
                raw = comp.data
                if isinstance(raw, str):
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {}
                elif isinstance(raw, dict):
                    data = raw
                else:
                    data = {}
                news = data.get("meta", {}).get("detail", {}).get("news", [])
                json_lines = []
                for item in news:
                    if isinstance(item, dict) and item.get("text"):
                        json_lines.append(item["text"])
                if json_lines:
                    lines.extend(json_lines)
                else:
                    fallback = data.get("desc") or data.get("prompt") or ""
                    if fallback:
                        lines.append(fallback)
            elif ctype == "Plain":
                t = getattr(comp, "text", "").strip()
                if t:
                    lines.append(t)
            elif ctype in ("Image", "Img"):
                url = getattr(comp, "url", None) or getattr(comp, "file", None)
                if url:
                    lines.append(f"![图片]({url})")
                else:
                    lines.append("[图片]")
            elif ctype in ("Forward", "MergedForward"):
                # 合并转发：先尝试取内嵌节点，无则通过 API 拉取
                nodes = (
                    getattr(comp, "nodes", None)
                    or getattr(comp, "node_list", None)
                    or []
                )
                if not nodes:
                    nodes = await self._fetch_forward_nodes(comp, bot)
                for node in nodes:
                    sender = (
                        getattr(node, "sender_name", None)
                        or getattr(node, "name", None)
                        or getattr(node, "nickname", None)
                        or (node.get("sender", {}).get("nickname") if isinstance(node, dict) else None)
                        or "unknown"
                    )
                    content = (
                        getattr(node, "content", None)
                        or getattr(node, "chain", None)
                        or (node.get("content") if isinstance(node, dict) else None)
                        or []
                    )
                    node_lines = await self._extract_from_chain(content, bot=bot, depth=depth + 1)
                    if node_lines:
                        lines.append(f"[{sender}]: " + " | ".join(node_lines))
        return lines

    async def _fetch_forward_nodes(self, comp, bot) -> list:
        """通过 get_forward_msg API 拉取合并转发节点，返回可遍历的 node 列表。"""
        forward_id = getattr(comp, "id", None) or getattr(comp, "forward_id", None)
        if not forward_id or not bot:
            return []
        try:
            data = await bot.call_action("get_forward_msg", message_id=forward_id)
            messages = data.get("messages") or data.get("message") or []
            # 将原始 dict 节点转换为统一结构，使 _extract_from_chain 能处理
            result = []
            for msg in messages:
                sender = msg.get("sender", {}).get("nickname") or str(msg.get("sender", {}).get("user_id", "unknown"))
                # content 是 OneBot 消息段列表，转成轻量 dict 节点
                content_segs = msg.get("content", [])
                # 若 content 是字符串（部分实现），尝试 JSON 解析
                if isinstance(content_segs, str):
                    try:
                        content_segs = json.loads(content_segs)
                    except Exception:
                        content_segs = [{"type": "text", "data": {"text": content_segs}}]
                # 构造轻量 dict node，用 sender 和已解析的段列表
                parsed = self._parse_raw_segments(content_segs)
                result.append({"sender": {"nickname": sender}, "content": parsed})
            logger.info(f"AutoIssue: fetched {len(result)} nodes from forward {forward_id}")
            return result
        except Exception as e:
            logger.error(f"AutoIssue: get_forward_msg error: {e}")
            return []

    @staticmethod
    def _parse_raw_segments(segs: list) -> list:
        """将 OneBot 原始消息段列表转为可被 _extract_from_chain 识别的轻量对象列表。"""
        class _Seg:
            def __init__(self, ctype, **kwargs):
                self.__class__.__name__ = ctype
                for k, v in kwargs.items():
                    setattr(self, k, v)
        result = []
        for seg in (segs or []):
            t = seg.get("type", "")
            d = seg.get("data", {})
            if t == "text":
                o = _Seg.__new__(_Seg)
                o.__class__ = type("Plain", (), {"__name__": "Plain"})
                o.text = d.get("text", "")
                result.append(o)
            elif t == "image":
                o = _Seg.__new__(_Seg)
                o.__class__ = type("Image", (), {"__name__": "Image"})
                o.url = d.get("url", "") or d.get("file", "")
                result.append(o)
            elif t == "forward":
                o = _Seg.__new__(_Seg)
                o.__class__ = type("Forward", (), {"__name__": "Forward"})
                o.id = d.get("id", "")
                o.nodes = []
                result.append(o)
        return result

    async def _llm_format(self, content: str, event) -> Optional[dict]:
        """返回 {"title": str, "body": str, "labels": list} 或 None"""
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                logger.warning("AutoIssue: no LLM provider")
                return None
            prompt = (
                "根据以下聊天内容，创建一个 GitHub Issue，严格遵循如下规则：\n\n"
                "第一行必须输出类型标记（仅此一行，不加任何其他内容）：\n"
                "  - BUG 报告输出：TYPE: BUG\n"
                "  - 功能建议输出：TYPE: FEATURE\n"
                "  - 其他输出：TYPE: OTHER\n\n"
                "第二行起根据类型按对应模板输出中文 Markdown 正文：\n\n"
                "【BUG 模板】\n"
                "## 标题\n"
                "[Bug] <简洁标题>\n\n"
                "## 问题描述\n"
                "<简要描述 bug 的具体表现>\n\n"
                "## 操作系统\n"
                "<从聊天内容提取，未提及则填\"未知\">\n\n"
                "## 复现步骤\n"
                "<详细的复现步骤>\n\n"
                "## 预期行为\n"
                "<预期的正确行为>\n\n"
                "## 环境信息（可选）\n"
                "<相关配置或环境信息，无则省略此节>\n\n"
                "## 补充信息（可选）\n"
                "<其他信息，图片用 Markdown 图片格式嵌入，无则省略此节>\n\n"
                "【功能建议模板】\n"
                "## 标题\n"
                "[Feature] <简洁标题>\n\n"
                "## 相关问题（可选）\n"
                "<功能建议相关的问题，无则省略此节>\n\n"
                "## 解决方案\n"
                "<希望实现的功能>\n\n"
                "## 替代方案（可选）\n"
                "<考虑过的替代方案，无则省略此节>\n\n"
                "## 补充信息（可选）\n"
                "<其他信息，图片用 Markdown 图片格式嵌入，无则省略此节>\n\n"
                "注意：聊天内容中的图片（格式为 ![图片](url)）应根据上下文嵌入到合适的章节，不要单独罗列。\n\n"
                f"---\n聊天内容：\n{content}"
            )
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=self.llm_system_prompt or None,
                ),
                timeout=60,
            )
            raw = resp.completion_text.strip()
            if len(raw) < 30:
                logger.warning("AutoIssue: LLM output too short")
                return None
            # --- 解析类型标记 ---
            lines = raw.splitlines()
            issue_type = "OTHER"
            body_start = 0
            for i, line in enumerate(lines):
                s = line.strip().upper()
                if s.startswith("TYPE:"):
                    tag = s.split(":", 1)[1].strip()
                    if "BUG" in tag:
                        issue_type = "BUG"
                    elif "FEATURE" in tag:
                        issue_type = "FEATURE"
                    body_start = i + 1
                    break
            body = "\n".join(lines[body_start:]).strip()
            # --- 标签映射 ---
            labels_map = {
                "BUG": ["📝 BUG Report"],
                "FEATURE": ["💡 Feature Request"],
                "OTHER": ["auto-issue"],
            }
            labels = labels_map.get(issue_type, ["auto-issue"])
            title = self._extract_title(body)
            return {"title": title, "body": body, "labels": labels}
        except asyncio.TimeoutError:
            logger.error("AutoIssue: LLM timeout")
            return None
        except Exception as e:
            logger.error(f"AutoIssue: LLM error: {e}")
            return None

    async def _create_issue(self, repo: str, issue_data: dict) -> Optional[str]:
        owner, repo_name = repo.split("/", 1)
        title = issue_data.get("title", "Auto-generated Issue from chat")
        body = issue_data.get("body", "")
        labels = issue_data.get("labels", ["auto-issue"])
        url = f"https://api.github.com/repos/{owner}/{repo_name}/issues"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AstrBot-AutoIssue",
        }
        payload = {"title": title[:256], "body": body, "labels": labels}
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload, proxy=self.http_proxy) as resp:
                    if resp.status == 201:
                        data = await resp.json()
                        logger.info(f"AutoIssue: created {data['html_url']}")
                        return data["html_url"]
                    text = await resp.text()
                    logger.error(f"AutoIssue: GitHub {resp.status}: {text}")
                    return {
                        401: "Token invalid",
                        403: "Forbidden",
                        404: f"Repo {repo} not found",
                        422: "Validation failed",
                    }.get(resp.status, f"HTTP {resp.status}")
        except aiohttp.ClientError as e:
            logger.error(f"AutoIssue: net error: {e}")
            return f"network error: {e}"

    @staticmethod
    def _extract_title(md: str) -> str:
        found_header = False
        for line in md.split("\n"):
            s = line.strip()
            # 匹配 ## 标题 / ## Title 节
            if s.startswith("#") and ("标题" in s or "Title" in s or "title" in s):
                found_header = True
                continue
            if found_header and s and not s.startswith("#"):
                # 保留 [Bug] / [Feature] 前缀，直接使用该行
                return s
        return "Auto-generated Issue from chat"

    async def _verify_repo(self, repo: str) -> tuple:
        owner, name = repo.split("/", 1)
        url = f"https://api.github.com/repos/{owner}/{name}"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AstrBot-AutoIssue",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, proxy=self.http_proxy) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not data.get("has_issues"):
                            return False, "Issues not enabled"
                        return True, "OK"
                    return False, {
                        401: "Token invalid",
                        404: "Repo not found",
                    }.get(resp.status, f"HTTP {resp.status}")
        except aiohttp.ClientError as e:
            return False, str(e)

    async def terminate(self):
        logger.info("AutoIssue: terminated")
