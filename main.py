"""AstrBot AutoIssue Plugin"""

import re
import asyncio
from typing import Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import At


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

        raw: list = config.get("repo_bindings", [])
        self.repo_bindings: dict = {}
        for entry in raw:
            if isinstance(entry, str) and "=" in entry:
                gid, repo = entry.split("=", 1)
                self.repo_bindings[gid.strip()] = repo.strip()

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
        if self.trigger_keyword not in msg_text:
            return
        if self.require_at_bot:
            at_bot = False
            try:
                for comp in event.message_obj.message:
                    if isinstance(comp, At) and str(comp.target) == str(event.message_obj.self_id):
                        at_bot = True
                        break
            except Exception:
                pass
            if not at_bot:
                return

        group_id = self._extract_group_id(event.session_id)
        if not group_id:
            return

        repo = self.repo_bindings.get(group_id)
        if not repo:
            yield event.plain_result(
                f"group not bound. use /bindrepo owner/repo\ngroup_id: {group_id}"
            )
            return

        if not self._is_reply(event):
            yield event.plain_result("please reply to a (forwarded) message first")
            return

        yield event.plain_result("analyzing...")

        content = self._extract_quoted_content(event)
        if not content:
            yield event.plain_result("failed to extract quoted content")
            return

        issue_md = await self._llm_format(content, event)
        if not issue_md:
            yield event.plain_result("LLM failed to generate issue content")
            return

        result = await self._create_issue(repo, issue_md)
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
    def _is_reply(event) -> bool:
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, dict):
                return "source" in raw or "reply" in raw
        except Exception:
            pass
        return False

    @staticmethod
    def _extract_quoted_content(event) -> Optional[str]:
        try:
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                return None
            text = ""
            img_count = 0
            for key in ("source", "reply"):
                if key in raw and isinstance(raw[key], dict):
                    block = raw[key]
                    text = str(block.get("message") or block.get("content") or "")
                    img_count += len(block.get("images", []))
                    break
            fwd = (
                raw.get("forward")
                or raw.get("multiforward")
                or raw.get("merge_forward")
            )
            if isinstance(fwd, list):
                lines = []
                for item in fwd:
                    if not isinstance(item, dict):
                        continue
                    body = str(
                        item.get("message")
                        or item.get("content")
                        or item.get("text")
                        or ""
                    )
                    si = item.get("sender")
                    sender = ""
                    if isinstance(si, dict):
                        sender = si.get("nickname") or si.get("username") or ""
                    elif si:
                        sender = str(si)
                    sender = (
                        sender
                        or item.get("nickname")
                        or item.get("username")
                        or ""
                    )
                    lines.append(f"[{sender}] {body}" if sender else body)
                    img_count += len(item.get("images", []))
                text = "\n".join(lines)
            elif isinstance(fwd, dict):
                text = str(fwd.get("message", ""))
            img_count += len(raw.get("images", []))
            if img_count:
                text += f"\n\n[{img_count} images]"
            return text.strip() or None
        except Exception as e:
            logger.error(f"AutoIssue: extract error: {e}")
            return None

    async def _llm_format(self, content: str, event) -> Optional[str]:
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                logger.warning("AutoIssue: no LLM provider")
                return None
            prompt = (
                "Analyze the following chat messages and create a GitHub Issue.\n"
                "Requirements:\n"
                "1. Determine if this is a BUG report, feature request, or other\n"
                "2. For BUGs: extract description, reproduction steps, expected/actual results\n"
                "3. For features: extract feature description, use cases\n"
                "4. If images are mentioned, note them\n"
                "5. Output in Chinese markdown\n\n"
                "Output format:\n"
                "## Title\n[concise title]\n\n"
                "## Type\n[BUG/Feature/Other]\n\n"
                "## Description\n[detailed description]\n\n"
                "## Details\n[reproduction steps or use cases]\n\n"
                f"---\nContent:\n{content}"
            )
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=self.llm_system_prompt or None,
                ),
                timeout=60,
            )
            result = resp.completion_text.strip()
            if len(result) < 30:
                logger.warning("AutoIssue: LLM output too short")
                return None
            return result
        except asyncio.TimeoutError:
            logger.error("AutoIssue: LLM timeout")
            return None
        except Exception as e:
            logger.error(f"AutoIssue: LLM error: {e}")
            return None

    async def _create_issue(self, repo: str, body: str) -> Optional[str]:
        owner, repo_name = repo.split("/", 1)
        title = self._extract_title(body)
        url = f"https://api.github.com/repos/{owner}/{repo_name}/issues"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AstrBot-AutoIssue",
        }
        payload = {"title": title[:256], "body": body, "labels": ["auto-issue"]}
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
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
            if s.startswith("#") and ("Title" in s or "title" in s):
                found_header = True
                continue
            if found_header and s and not s.startswith("#"):
                cleaned = re.sub(r"\[.*?\]", "", s).strip()
                if len(cleaned) >= 3:
                    return cleaned
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
                async with session.get(url, headers=headers) as resp:
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
