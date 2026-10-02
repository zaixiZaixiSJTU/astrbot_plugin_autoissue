"""AstrBot AutoIssue Plugin"""

import json
import re
import asyncio
import base64
import subprocess
import uuid
import tempfile
import os
import shutil
import glob
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import aiohttp

# 压制 openai/httpx/httpcore 的 DEBUG 日志（base64 帧数据太长）
import logging as _logging
for _noisy in ("openai._base_client", "httpcore", "httpx"):
    _logging.getLogger(_noisy).setLevel(_logging.WARNING)

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import At, Image as ImageComp

def _load_bindings(file_path: Path) -> dict:
    try:
        if file_path.exists():
            return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"AutoIssue: failed to load bindings: {e}")
    return {}


def _save_bindings(file_path: Path, bindings: dict) -> None:
    try:
        file_path.write_text(
            json.dumps(bindings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"AutoIssue: failed to save bindings: {e}")


def _load_knowledge_bases(file_path: Path) -> dict:
    """Load repository knowledge bases, ignoring malformed local data."""
    try:
        if file_path.exists():
            data = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"AutoIssue: failed to load knowledge bases: {e}")
    return {}


def _save_knowledge_bases(file_path: Path, knowledge_bases: dict) -> None:
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            json.dumps(knowledge_bases, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"AutoIssue: failed to save knowledge bases: {e}")


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

        self._bindings_file: Path = StarTools.get_data_dir() / "repo_bindings.json"
        self._bindings_lock = asyncio.Lock()
        self.repo_bindings: dict = _load_bindings(self._bindings_file)

        self._knowledge_file: Path = StarTools.get_data_dir() / "repo_knowledge.json"
        self._knowledge_lock = asyncio.Lock()
        self.repo_knowledge: dict = _load_knowledge_bases(self._knowledge_file)

        logger.info(
            f"AutoIssue: init ok | token={'yes' if self.github_token else 'NO'} | "
            f"bindings={len(self.repo_bindings)} | knowledge={len(self.repo_knowledge)}"
        )

    async def initialize(self):
        if not self.github_token:
            logger.warning("AutoIssue: github_token not configured!")

    # ---- main listener ----

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event):
        msg_text: str = event.message_str or ""
        # logger.info(f"AutoIssue: on_message | msg={repr(msg_text)}")
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

        # 绑定旧仓库或知识库文件被删除时，在实际创建 Issue 前自动补建。
        knowledge = await self._ensure_repo_knowledge(repo)

        content, media_urls = await self._extract_quoted_content(event, group_id)
        if not content and not media_urls:
            yield event.plain_result("failed to extract quoted content")
            return

        issue_data = await self._llm_format(content, media_urls, event, knowledge)
        if not issue_data:
            yield event.plain_result("LLM failed to generate issue content")
            return

        result = await self._create_issue(repo, issue_data)
        if result and result.startswith("https://"):
            yield event.plain_result(f"Issue created: {result}")
            # 尝试用 Playwright 截取 issue 页面截图
            screenshot_path = await self._capture_issue_screenshot(result)
            if screenshot_path:
                yield event.chain_result([ImageComp(str(screenshot_path))])
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
        async with self._bindings_lock:
            self.repo_bindings[gid] = repo
            _save_bindings(self._bindings_file, self.repo_bindings)
        logger.info(f"AutoIssue: bind {gid} -> {repo}")
        knowledge = await self._ensure_repo_knowledge(repo)
        knowledge_status = "knowledge base ready" if knowledge else "knowledge base pending (will retry when creating an Issue)"
        yield event.plain_result(f"bound group {gid} -> {repo}\n{knowledge_status}")

    @filter.command("unbindrepo")
    async def cmd_unbind(self, event):
        """unbind group: /unbindrepo"""
        if not self._is_group_admin(event):
            yield event.plain_result("admin/owner only")
            return
        gid = self._extract_group_id(event.session_id)
        if gid and gid in self.repo_bindings:
            async with self._bindings_lock:
                del self.repo_bindings[gid]
                _save_bindings(self._bindings_file, self.repo_bindings)
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
        knowledge_ready = bool(
            bound not in ("none", "?")
            and self.repo_knowledge.get(self._knowledge_key(bound))
        )
        if bound not in ("none", "?") and not knowledge_ready:
            # 状态检查同时负责修复旧绑定或被删除的知识库缓存。
            await self._ensure_repo_knowledge(bound)
            knowledge_ready = bool(
                self.repo_knowledge.get(self._knowledge_key(bound))
            )
        yield event.plain_result(
            f"AutoIssue status\n"
            f"token: {'ok' if self.github_token else 'MISSING'}\n"
            f"keyword: {self.trigger_keyword}\n"
            f"require @bot: {self.require_at_bot}\n"
            f"group({gid}): {bound}\n"
            f"knowledge base: {'ready' if knowledge_ready else 'missing'}\n"
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

    @staticmethod
    def _knowledge_key(repo: str) -> str:
        return repo.strip().lower()

    async def _fetch_repository_readme(
        self, repo: str
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (README text, git sha, error)."""
        owner, name = repo.split("/", 1)
        url = f"https://api.github.com/repos/{owner}/{name}/readme"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "AstrBot-AutoIssue",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=headers, proxy=self.http_proxy
                ) as resp:
                    if resp.status == 404:
                        return None, None, "not_found"
                    if resp.status != 200:
                        return None, None, f"HTTP {resp.status}"
                    data = await resp.json()

                encoded = data.get("content")
                if encoded and data.get("encoding") == "base64":
                    raw = base64.b64decode(encoded)
                    return raw.decode("utf-8", errors="replace"), data.get("sha"), None

                # GitHub may omit inline content for unusually large README files.
                download_url = data.get("download_url")
                if download_url:
                    async with session.get(
                        download_url, headers=headers, proxy=self.http_proxy
                    ) as download_resp:
                        if download_resp.status == 200:
                            return await download_resp.text(errors="replace"), data.get("sha"), None
                        return None, None, f"download HTTP {download_resp.status}"
                return None, None, "README content is empty"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            return None, None, str(e)

    async def _summarize_repository_readme(
        self, repo: str, readme: str
    ) -> Optional[str]:
        """Use the configured AstrBot provider to turn a README into compact context."""
        try:
            provider = self.context.get_using_provider()
            if not provider:
                return None
            prompt = (
                f"请阅读 GitHub 仓库 {repo} 的 README，并整理成供 Issue 分析使用的小型知识库。\n"
                "只依据 README，不要猜测。用简洁中文 Markdown 总结以下内容：\n"
                "1. 项目用途和核心能力；2. 主要功能/模块与结构；3. 关键术语、运行环境和依赖；"
                "4. 常见使用流程；5. 提交 Bug 或功能建议时值得关注的约束。\n"
                "控制在 1200 字以内；README 未提及的项目省略。\n\n"
                f"--- README ---\n{readme[:60000]}"
            )
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider.meta().id,
                    prompt=prompt,
                    system_prompt="你是严谨的软件仓库文档分析助手。",
                ),
                timeout=60,
            )
            summary = response.completion_text.strip()
            return summary[:12000] if summary else None
        except Exception as e:
            logger.warning(f"AutoIssue: README analysis failed for {repo}: {e}")
            return None

    async def _ensure_repo_knowledge(self, repo: str) -> Optional[str]:
        """Return cached repository context, creating it from README when missing."""
        key = self._knowledge_key(repo)
        cached = self.repo_knowledge.get(key)
        if (
            isinstance(cached, dict)
            and cached.get("status") in ("analyzed", "no_readme")
            and isinstance(cached.get("summary"), str)
        ):
            return cached["summary"]

        async with self._knowledge_lock:
            # Another concurrent Issue may have completed the build while waiting.
            cached = self.repo_knowledge.get(key)
            if (
                isinstance(cached, dict)
                and cached.get("status") in ("analyzed", "no_readme")
                and isinstance(cached.get("summary"), str)
            ):
                return cached["summary"]

            logger.info(f"AutoIssue: knowledge base missing, reading README for {repo}")
            readme, readme_sha, error = await self._fetch_repository_readme(repo)
            if error == "not_found":
                summary = "该仓库没有可读取的 README，暂无额外仓库背景信息。"
                status = "no_readme"
            elif error or readme is None:
                logger.warning(f"AutoIssue: failed to read README for {repo}: {error}")
                return None
            else:
                summary = await self._summarize_repository_readme(repo, readme)
                status = "analyzed"
                if not summary:
                    # The Issue flow can still benefit from documentation when README
                    # analysis temporarily fails (for example, provider timeout).
                    summary = "## README 原文摘录\n\n" + readme[:8000]
                    status = "readme_excerpt"

            self.repo_knowledge[key] = {
                "repo": repo,
                "status": status,
                "readme_sha": readme_sha,
                "summary": summary,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_knowledge_bases(self._knowledge_file, self.repo_knowledge)
            logger.info(f"AutoIssue: knowledge base stored for {repo} ({status})")
            return summary

    async def _extract_quoted_content(self, event, group_id: str = "") -> tuple[str, list]:
        """返回 (文本内容, media_urls)，media_urls 为 [(kind, url), ...] 列表。"""
        try:
            reply = self._get_reply_comp(event)
            if reply:
                bot = getattr(event, "bot", None)
                text_lines, media_urls = await self._extract_from_chain(reply.chain, bot=bot, group_id=group_id)
                text = "\n".join(l for l in text_lines if l).strip()
                return text, media_urls
        except Exception as e:
            logger.error(f"AutoIssue: extract error: {e}")
        return "", []

    async def _extract_from_chain(self, chain, bot=None, depth: int = 0, group_id: str = "") -> tuple[list, list]:
        """递归提取消息链中的文本行和媒体URL，支持合并转发。返回 (text_lines, media_urls)，media_urls 为 [(kind, url), ...] 列表。"""
        lines = []
        media_urls = []
        if depth > 5:
            return lines, media_urls
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
                if url and isinstance(url, str) and url.startswith("http"):
                    media_urls.append(("图片", url))
                    lines.append(f"[图片{len(media_urls)}]")
                else:
                    lines.append("[图片]")
            elif ctype == "Video":
                url = getattr(comp, "url", None) or getattr(comp, "file", None)
                if url and isinstance(url, str) and url.startswith("http"):
                    media_urls.append(("视频", url))
                    lines.append(f"[视频{len(media_urls)}]")
                else:
                    lines.append("[视频]")
            elif ctype == "File":
                url = getattr(comp, "url", None)
                file_id = getattr(comp, "file_id", None)
                name = getattr(comp, "name", "") or url or "文件"
                resolved_url = None
                if url and url.startswith("http"):
                    resolved_url = url
                elif file_id and bot and group_id:
                    try:
                        file_info = await bot.call_action("get_group_file_url", group_id=group_id, file_id=file_id)
                        if isinstance(file_info, dict):
                            resolved_url = file_info.get("url") or file_info.get("file") or ""
                        elif isinstance(file_info, str):
                            resolved_url = file_info
                        if not (resolved_url and isinstance(resolved_url, str) and resolved_url.startswith("http")):
                            resolved_url = None
                    except Exception as e:
                        logger.warning(
                            f"AutoIssue: get_group_file_url failed for {file_id}: {e}"
                        )
                if resolved_url:
                    media_urls.append(("视频", resolved_url))
                    lines.append(f"[视频{len(media_urls)}]")
                else:
                    lines.append(f"[文件: {name}]")
            elif ctype in ("Forward", "MergedForward"):
                # 合并转发：先尝试取内嵌节点，无则通过 API 拉取
                raw_nodes = (
                    getattr(comp, "nodes", None)
                    or getattr(comp, "node_list", None)
                    or getattr(comp, "content", None)
                    or getattr(comp, "message", None)
                    or []
                )
                # NapCat embeds nested forwards in data.content. Inner forward
                # ids cannot always be fetched separately, so prefer that data.
                nodes = self._normalize_forward_nodes(raw_nodes)
                if not nodes:
                    nodes = await self._fetch_forward_nodes(comp, bot)
                for node in nodes:
                    sender = (
                        getattr(node, "sender_name", None)
                        or getattr(node, "name", None)
                        or getattr(node, "nickname", None)
                        or (
                            node.get("sender", {}).get("nickname")
                            if isinstance(node, dict)
                            else None
                        )
                        or (
                            str(node.get("sender", {}).get("user_id"))
                            if isinstance(node, dict)
                            and isinstance(node.get("sender"), dict)
                            and node.get("sender", {}).get("user_id")
                            else None
                        )
                        or "unknown"
                    )
                    content = (
                        getattr(node, "content", None)
                        or getattr(node, "chain", None)
                        or (
                            node.get("message") or node.get("content")
                            if isinstance(node, dict)
                            else None
                        )
                        or []
                    )
                    timestamp = (
                        node.get("timestamp") or node.get("time") or node.get("send_time")
                        if isinstance(node, dict)
                        else getattr(node, "timestamp", None) or getattr(node, "time", None)
                    )
                    node_lines, node_media = await self._extract_from_chain(
                        content, bot=bot, depth=depth + 1, group_id=group_id
                    )
                    media_urls.extend(node_media)
                    if node_lines:
                        layer = depth + 1
                        time_label = f" {timestamp}" if timestamp else ""
                        lines.append(
                            f"[转发第{layer}层{time_label}][{sender}]: "
                            + " | ".join(node_lines)
                        )
        return lines, media_urls

    @staticmethod
    def _unwrap_action_data(data):
        """Accept both raw action data and a full OneBot response envelope."""
        if not isinstance(data, dict):
            return {}
        wrapped = data.get("data")
        return wrapped if isinstance(wrapped, dict) else data

    @staticmethod
    def _get_call_action(bot):
        """Resolve call_action across AstrBot/NapCat adapter versions."""
        direct = getattr(bot, "call_action", None)
        if callable(direct):
            return direct
        api = getattr(bot, "api", None)
        nested = getattr(api, "call_action", None)
        return nested if callable(nested) else None

    async def _call_action_with_message_id(self, bot, action: str, message_id):
        """Call a OneBot action with compatible id names and scalar types."""
        call_action = self._get_call_action(bot)
        if not call_action:
            return None

        message_id_str = str(message_id).strip()
        if not message_id_str:
            return None
        attempts = [{"message_id": message_id_str}, {"id": message_id_str}]
        if message_id_str.isdigit():
            numeric_id = int(message_id_str)
            attempts.extend([{"message_id": numeric_id}, {"id": numeric_id}])

        last_error = None
        for params in attempts:
            try:
                return await call_action(action, **params)
            except Exception as exc:
                last_error = exc
                logger.debug(f"AutoIssue: {action} failed with params={params}: {exc}")
        if last_error:
            raise last_error
        return None

    def _normalize_forward_nodes(self, nodes) -> list:
        """Normalize NapCat message records and OneBot node segments."""
        if isinstance(nodes, str):
            try:
                nodes = json.loads(nodes)
            except Exception:
                return []
        if isinstance(nodes, dict):
            # Adapters may return a response envelope or a single message node.
            for key in ("data", "messages", "nodes", "nodeList"):
                value = nodes.get(key)
                if isinstance(value, (dict, list, str)):
                    return self._normalize_forward_nodes(value)
            if nodes.get("type") == "node" or any(
                key in nodes for key in ("sender", "message", "content")
            ):
                nodes = [nodes]
            elif isinstance(nodes.get("message"), list) and nodes["message"] and all(
                isinstance(item, dict) and item.get("type") == "node"
                for item in nodes["message"]
            ):
                return self._normalize_forward_nodes(nodes["message"])
            else:
                return []
        if isinstance(nodes, tuple):
            nodes = list(nodes)
        if not isinstance(nodes, list):
            return []

        # Some OneBot-compatible implementations put a message-segment chain
        # directly in forward.content instead of wrapping it in message records.
        if nodes and all(
            isinstance(item, (str, dict))
            and (isinstance(item, str) or item.get("type") != "node")
            and (
                isinstance(item, str)
                or not any(key in item for key in ("sender", "message", "content"))
            )
            for item in nodes
        ):
            return [
                {
                    "sender": {},
                    "content": self._parse_raw_segments(nodes),
                }
            ]

        result = []
        for node in nodes:
            if not isinstance(node, dict):
                # AstrBot may already have converted inline nodes to components.
                result.append(node)
                continue

            if node.get("type") == "node" and isinstance(node.get("data"), dict):
                node_data = node["data"]
                sender = {
                    "nickname": node_data.get("nickname") or node_data.get("name"),
                    "user_id": node_data.get("user_id") or node_data.get("uin"),
                }
                raw_content = node_data.get("message") or node_data.get("content") or []
                timestamp = node_data.get("time") or node_data.get("timestamp") or node_data.get("send_time")
            else:
                sender = (
                    node.get("sender") if isinstance(node.get("sender"), dict) else {}
                )
                raw_content = node.get("message") or node.get("content") or []
                timestamp = node.get("time") or node.get("timestamp") or node.get("send_time")

            if isinstance(raw_content, str):
                try:
                    raw_content = json.loads(raw_content)
                except Exception:
                    raw_content = [{"type": "text", "data": {"text": raw_content}}]
            if isinstance(raw_content, list) and all(
                isinstance(item, (str, dict)) for item in raw_content
            ):
                parsed = self._parse_raw_segments(raw_content)
            elif isinstance(raw_content, list):
                # Preserve AstrBot components that were already decoded by an adapter.
                parsed = raw_content
            else:
                parsed = []
            result.append({"sender": sender, "content": parsed, "timestamp": timestamp})
        return result

    async def _fetch_forward_nodes(self, comp, bot) -> list:
        """通过 get_forward_msg API 拉取合并转发节点，返回可遍历的 node 列表。"""
        forward_id = getattr(comp, "id", None) or getattr(comp, "forward_id", None)
        if not forward_id or not bot:
            return []
        try:
            data = await self._call_action_with_message_id(
                bot, "get_forward_msg", forward_id
            )
            payload = self._unwrap_action_data(data)
            messages = (
                payload.get("messages")
                or payload.get("message")
                or payload.get("nodes")
                or payload.get("nodeList")
                or []
            )
            result = self._normalize_forward_nodes(messages)
            logger.info(
                f"AutoIssue: fetched {len(result)} nodes from forward {forward_id}"
            )
            logger.debug(
                "AutoIssue: forward raw keys sample: "
                f"{list(messages[0].keys()) if messages and isinstance(messages[0], dict) else []}"
            )
            return result
        except Exception as e:
            logger.error(f"AutoIssue: get_forward_msg error: {e}")
            return []

    @staticmethod
    def _parse_raw_segments(segs: list) -> list:
        """将 OneBot 原始消息段列表转为可被 _extract_from_chain 识别的轻量对象列表。"""
        result = []
        for seg in segs or []:
            if isinstance(seg, str):
                result.append(type("Plain", (), {"text": seg})())
                continue
            if not isinstance(seg, dict):
                continue
            t = seg.get("type", "")
            d = seg.get("data", {})
            if not isinstance(d, dict):
                d = {}
            if t in ("text", "plain"):
                obj = type("Plain", (), {"text": d.get("text", "")})()
                result.append(obj)
            elif t == "image":
                obj = type(
                    "Image", (), {"url": d.get("url", "") or d.get("file", "")}
                )()
                result.append(obj)
            elif t == "video":
                obj = type(
                    "Video", (), {"url": d.get("url", "") or d.get("file", "")}
                )()
                result.append(obj)
            elif t == "file":
                obj = type(
                    "File",
                    (),
                    {
                        "url": d.get("file", ""),
                        "file_id": d.get("file_id", ""),
                        "name": d.get("name", "") or d.get("file", ""),
                    },
                )()
                result.append(obj)
            elif t in ("forward", "forward_msg", "nodes"):
                # Latest NapCat puts complete nested messages in data.content.
                # Fetching an inner id separately may fail by design.
                inline_nodes = (
                    d.get("content") or d.get("message") or d.get("messages") or []
                )
                obj = type(
                    "Forward",
                    (),
                    {
                        "id": d.get("id", "") or d.get("message_id", ""),
                        "content": inline_nodes,
                        "nodes": [],
                    },
                )()
                result.append(obj)
            elif t == "node":
                # Older NapCat versions can expose recursive results as node
                # segments. Reuse the forward walker to retain sender metadata.
                obj = type("Forward", (), {"id": "", "content": [seg], "nodes": []})()
                result.append(obj)
        return result

    async def _extract_video_frames(self, video_url: str, frame_count: int = 6) -> list[str] | None:
        """下载视频并用 ffmpeg 抽帧，返回帧图片路径列表。失败返回 None 并清理临时文件。"""
        tmp_dir = None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="video_frames_")
            video_path = os.path.join(tmp_dir, "video.mp4")
            # 下载
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(video_url) as resp:
                    if resp.status != 200:
                        logger.warning(f"AutoIssue: frame download failed HTTP {resp.status}")
                        shutil.rmtree(tmp_dir)
                        return None
                    with open(video_path, "wb") as f:
                        f.write(await resp.read())
            # 获取时长
            probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", video_path]
            proc = subprocess.run(probe_cmd, capture_output=True, timeout=10)
            duration = float(proc.stdout.decode().strip()) if proc.returncode == 0 and proc.stdout else 0
            if duration <= 0:
                logger.warning("AutoIssue: cannot determine video duration")
                shutil.rmtree(tmp_dir)
                return None
            # ffmpeg 按时间间隔抽帧
            interval = duration / (frame_count + 1)
            frame_pattern = os.path.join(tmp_dir, "frame_%02d.png")
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", video_path,
                "-vf", f"fps=1/{interval:.1f}",
                "-frames:v", str(frame_count),
                frame_pattern,
            ]
            subprocess.run(ffmpeg_cmd, capture_output=True, timeout=30)
            # 收集帧文件
            frames = sorted(glob.glob(os.path.join(tmp_dir, "frame_*.png")))
            if not frames:
                logger.warning("AutoIssue: no frames extracted")
                shutil.rmtree(tmp_dir)
                return None
            os.remove(video_path)  # 删掉视频只留帧
            logger.info(f"AutoIssue: extracted {len(frames)} frames from {duration:.1f}s video")
            return frames
        except Exception as e:
            logger.error(f"AutoIssue: frame extraction error: {e}")
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)
            return None

    async def _llm_format(
        self,
        content: str,
        media_urls: list,
        event,
        repo_knowledge: Optional[str] = None,
    ) -> Optional[dict]:
        """返回 {"title": str, "body": str, "labels": list} 或 None。
        media_urls 为 [(kind, url), ...] 列表，kind 取 \"图片\" 或 \"视频\"。"""
        temp_dirs = []
        try:
            # 使用全局默认 LLM provider，不依赖会话级别配置
            prov = self.context.get_using_provider()
            if not prov:
                logger.warning("AutoIssue: no LLM provider configured")
                return None
            provider_id = prov.meta().id
            # 视频：下载 + ffmpeg 抽帧 → 帧图片传入 image_urls
            image_urls = []
            for kind, url in media_urls:
                if not url.startswith("http"):
                    continue
                if kind == "视频":
                    frame_paths = await self._extract_video_frames(url)
                    if frame_paths:
                        image_urls.extend(frame_paths)
                        temp_dirs.append(os.path.dirname(frame_paths[0]))
                        logger.info(f"AutoIssue: extracted {len(frame_paths)} frames from video")
                    else:
                        logger.warning(f"AutoIssue: frame extraction failed for video")
                else:
                    image_urls.append(url)
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
                "<其他信息，图片用 Markdown 图片格式嵌入，视频可按需描述关键帧/场景，无则省略此节>\n\n"
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
                "<其他信息，图片用 Markdown 图片格式嵌入，视频可按需描述关键帧/场景，无则省略此节>\n\n"
                "注意：聊天内容中标记为[图片N]或[视频N]的媒体已作为附件提供，请根据上下文将它们嵌入到合适的章节，"
                "必须使用下方列出的真实URL，格式为 ![描述](URL)。\n\n"
                + (
                    "以下是目标仓库 README 生成的本地知识库。它只用于理解项目背景、术语和结构；"
                    "若与聊天中明确描述的问题冲突，以聊天内容为准，也不要把知识库全文复制到 Issue。\n\n"
                    f"--- 仓库知识库 ---\n{repo_knowledge}\n--- 仓库知识库结束 ---\n\n"
                    if repo_knowledge else ""
                )
                + (
                    "媒体URL对应关系（直接使用这些URL，不要自行编造链接）：\n"
                    + "\n".join(f"[{kind}{i}] → {url}" for i, (kind, url) in enumerate(media_urls, 1))
                    + "\n\n"
                    if media_urls else ""
                )
                + f"---\n聊天内容：\n{content if content else '（无文字内容，请根据媒体内容分析）'}"
            )
            logger.info(f"AutoIssue: calling LLM with {len(image_urls)} image(s), content_len={len(content)}")
            last_exc = None
            for attempt in range(3):
                try:
                    resp = await asyncio.wait_for(
                        self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=prompt,
                            image_urls=image_urls if image_urls else None,
                            system_prompt=self.llm_system_prompt or None,
                        ),
                        timeout=60,
                    )
                    last_exc = None
                    break
                except (asyncio.TimeoutError, Exception) as e:
                    last_exc = e
                    logger.warning(f"AutoIssue: LLM attempt {attempt + 1}/3 failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(3)
            if last_exc is not None:
                logger.error(f"AutoIssue: LLM all attempts failed: {last_exc}")
                return None
            raw = resp.completion_text.strip()
            logger.info(f"AutoIssue: LLM raw output length={len(raw)}")
            if len(raw) < 30:
                logger.warning(f"AutoIssue: LLM output too short: {repr(raw)}")
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
            labels = labels_map.get(issue_type, ["auto-issue"]) + ["🤖 Agent Generated"]
            title = self._extract_title(body)
            return {"title": title, "body": body, "labels": labels, "media_urls": media_urls}
        except Exception as e:
            logger.error(f"AutoIssue: LLM error: {e}")
            return None
        finally:
            for d in temp_dirs:
                try:
                    shutil.rmtree(d)
                except Exception:
                    pass

    async def _upload_video_to_repo(self, repo: str, video_url: str) -> Optional[str]:
        """下载视频并上传到仓库 .issue-assets/，返回 raw.githubusercontent.com URL。"""
        owner, repo_name = repo.split("/", 1)

        # 1) 下载视频到临时文件
        tmp_path = None
        try:
            suffix = ".mp4"
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(video_url) as resp:
                    if resp.status != 200:
                        logger.warning(f"AutoIssue: download video failed HTTP {resp.status}: {video_url[:80]}")
                        return None
                    with open(tmp_path, "wb") as f:
                        f.write(await resp.read())
            logger.info(f"AutoIssue: downloaded video -> {tmp_path}")

            # 2) 上传到 GitHub
            with open(tmp_path, "rb") as f:
                content_b64 = base64.b64encode(f.read()).decode("ascii")

            filename = f"{uuid.uuid4()}{suffix}"
            upload_url = f"https://api.github.com/repos/{owner}/{repo_name}/contents/.issue-assets/{filename}"
            headers = {
                "Authorization": f"Bearer {self.github_token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "AstrBot-AutoIssue",
            }
            payload = {
                "message": f"upload video attachment",
                "content": content_b64,
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.put(upload_url, headers=headers, json=payload, proxy=self.http_proxy) as resp:
                    if resp.status in (200, 201):
                        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/main/.issue-assets/{filename}"
                        logger.info(f"AutoIssue: uploaded video -> {raw_url}")
                        return raw_url
                    text = await resp.text()
                    logger.error(f"AutoIssue: upload video failed HTTP {resp.status}: {text}")
                    return None
        except Exception as e:
            logger.error(f"AutoIssue: upload video error: {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
                logger.info(f"AutoIssue: cleaned up temp file {tmp_path}")

    async def _capture_issue_screenshot(self, issue_url: str) -> Optional[str]:
        """用 Playwright 截取 issue 页面截图，返回图片路径。失败返回 None。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("AutoIssue: playwright not installed, skip screenshot")
            return None
        wait_until = "domcontentloaded"
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                context = await browser.new_context(
                    viewport={"width": 850, "height": 1200},
                    device_scale_factor=2,
                )
                page = await context.new_page()
                try:
                    await page.goto(issue_url, wait_until=wait_until, timeout=15000)
                    await page.evaluate("""() => {
                        const trash = ['header','footer','.AppHeader','.Layout-sidebar',
                            '#partial-discussion-sidebar','.gh-header-actions','.gh-header-meta',
                            '.js-sticky-header','.Layout-announcement'];
                        trash.forEach(s => document.querySelectorAll(s).forEach(el => el.remove()));
                        const main = document.querySelector('.Layout-main') || document.querySelector('#discussion_bucket');
                        if (main) {
                            let n = main;
                            while (n && n !== document.body) {
                                n.style.setProperty('width','100%','important');
                                n.style.setProperty('max-width','none','important');
                                n.style.setProperty('padding','0','important');
                                n.style.setProperty('margin','0','important');
                                n.style.setProperty('display','block','important');
                                n = n.parentElement;
                            }
                        }
                    }""")
                    await page.wait_for_timeout(1000)
                    total_h = await page.evaluate("document.documentElement.scrollHeight")
                    await page.set_viewport_size({"width": 850, "height": total_h + 200})
                    screenshot_dir = Path(tempfile.mkdtemp(prefix="issue_screenshot_"))
                    screenshot_path = screenshot_dir / "issue.png"
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    logger.info(f"AutoIssue: screenshot saved -> {screenshot_path}")
                    return str(screenshot_path)
                finally:
                    await browser.close()
        except Exception as e:
            logger.warning(f"AutoIssue: screenshot error: {e}")
            return None

    async def _create_issue(self, repo: str, issue_data: dict) -> Optional[str]:
        owner, repo_name = repo.split("/", 1)
        title = issue_data.get("title", "Auto-generated Issue from chat")
        body = issue_data.get("body", "")
        media_urls: list = issue_data.get("media_urls", [])

        # 替换视频 URL：下载 → 上传到 GitHub → 用 raw URL 替换
        for kind, url in media_urls:
            if kind == "视频":
                new_url = await self._upload_video_to_repo(repo, url)
                if new_url:
                    body = body.replace(url, new_url)
                    logger.info(f"AutoIssue: replaced video URL in body: {url[:60]}... -> {new_url}")
                else:
                    logger.warning(f"AutoIssue: failed to upload video, keeping original URL")

        note = ">[!NOTE]\n>\n> 此 issue 由 AI 基于 QQ 群聊天记录总结生成\n\n"
        body = note + body
        labels = issue_data.get("labels", ["auto-issue"])
        url = f"https://api.github.com/repos/{owner}/{repo_name}/issues"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
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
                        401: "Token invalid or expired",
                        403: "Permission denied (403): Token 缺少权限。细粒度 PAT 请在 GitHub 中为该仓库授予 Issues: Read & write 权限；经典 PAT 请确保勾选 repo 或 public_repo scope",
                        404: f"Repo {repo} not found",
                        422: "Validation failed (label 不存在或字段不合法)",
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
            "Authorization": f"Bearer {self.github_token}",
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
