import re
from typing import Optional
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import At
from astrbot.core.config import AstrBotConfig


@register("astrbot_plugin_autoissue", "Claude", "自动从合并转发消息中提取信息并创建GitHub Issue", "1.0.0")
class AutoIssuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 插件配置直接从 config 读取（AstrBot 已按 _conf_schema.json 加载）
        self.github_token: str = config.get('github_token', '')
        self.trigger_keyword: str = config.get('trigger_keyword', 'issue')
        self.require_at_bot: bool = config.get('require_at_bot', True)
        self.llm_extract_prompt: str = config.get(
            'llm_extract_prompt',
            "请分析以下消息内容，提取出BUG报告或功能请求的关键信息，"
            "包括：标题、详细描述、复现步骤（如果是BUG）、期望行为等。请用markdown格式输出。"
        )

        # repo_bindings 在 schema 中是 list，格式: ["groupid=owner/repo", ...]
        # 加载时解析成 dict 方便查询，运行时修改也存在内存 dict 中
        raw_bindings: list = config.get('repo_bindings', [])
        self.repo_bindings: dict = self._parse_bindings_list(raw_bindings)

        logger.info(f"AutoIssue插件初始化完成，绑定了 {len(self.repo_bindings)} 个群组")

    async def initialize(self):
        if not self.github_token:
            logger.warning("GitHub Token未配置，请在插件配置中设置 github_token")

    # ------------------------------------------------------------------ #
    #  主消息处理                                                           #
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_message(self, event, *args, **kwargs):
        """检测回复合并转发消息时触发 issue 创建"""
        # 检查触发关键字
        if self.trigger_keyword not in event.message_str:
            return

        # 检查是否@机器人
        if self.require_at_bot and not self._has_at_bot(event):
            return

        # 获取群组ID
        group_id = self._extract_group_id(event.session_id)
        if not group_id:
            logger.warning("无法确定群组ID")
            return

        # 检查群组绑定
        repo = self.repo_bindings.get(str(group_id))
        if not repo:
            yield event.plain_result(
                f"当前群组未绑定 GitHub 仓库，请联系管理员使用 /bind_repo 配置。\n群组ID: {group_id}"
            )
            return

        # 检查是否回复了消息
        if not self._is_reply_message(event):
            yield event.plain_result("请在回复合并转发消息时使用此功能")
            return

        # 提取被引用的消息内容
        yield event.plain_result("⏳ 正在分析消息内容，请稍等...")

        quoted_content = self._extract_quoted_content(event)
        if not quoted_content:
            yield event.plain_result("❌ 未能提取到被回复的消息内容")
            return

        # 用LLM整理成 issue 格式
        issue_content = await self._extract_issue_info(quoted_content, event)
        if not issue_content:
            yield event.plain_result("❌ LLM未能从消息中整理出有效的 issue 信息")
            return

        # 调用 GitHub API 创建 issue
        result = await self._create_github_issue(repo, issue_content)
        if result and result.startswith("https://"):
            yield event.plain_result(f"✅ 成功创建 Issue：{result}")
        else:
            yield event.plain_result(f"❌ 创建 Issue 失败：{result or '未知错误'}")

    # ------------------------------------------------------------------ #
    #  辅助方法                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_bindings_list(raw: list) -> dict:
        """将 ["groupid=owner/repo", ...] 解析为 {groupid: "owner/repo"}"""
        result = {}
        for entry in raw:
            if isinstance(entry, str) and '=' in entry:
                gid, repo = entry.split('=', 1)
                result[gid.strip()] = repo.strip()
        return result

    def _has_at_bot(self, event: AstrMessageEvent) -> bool:
        try:
            for component in event.message_obj.message:
                if isinstance(component, At):
                    if str(component.target) == str(event.message_obj.self_id):
                        return True
        except Exception:
            pass
        return False

    def _extract_group_id(self, session_id: str) -> Optional[str]:
        """session_id 通常格式为 platform_groupid_userid"""
        try:
            parts = session_id.split('_')
            return parts[1] if len(parts) >= 2 else session_id
        except Exception:
            return None

    def _is_reply_message(self, event: AstrMessageEvent) -> bool:
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, dict):
                return 'source' in raw or 'reply' in raw
        except Exception:
            pass
        return False

    def _extract_quoted_content(self, event: AstrMessageEvent) -> Optional[str]:
        """从 raw_message 中提取被引用/转发的文字内容"""
        try:
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                return None

            quoted_text = ""
            image_count = 0

            # QQ 回复消息：source / reply 字段
            for key in ('source', 'reply'):
                if key in raw:
                    block = raw[key]
                    if isinstance(block, dict):
                        quoted_text = str(block.get('message', block.get('content', '')))
                        image_count += len(block.get('images', []))
                    break

            # 合并转发消息：forward / multiforward / merge_forward 字段
            forward_data = raw.get('forward') or raw.get('multiforward') or raw.get('merge_forward')
            if forward_data:
                if isinstance(forward_data, list):
                    lines = []
                    for item in forward_data:
                        if not isinstance(item, dict):
                            continue
                        content = str(
                            item.get('message') or item.get('content') or item.get('text', '')
                        )
                        sender_info = item.get('sender', {})
                        if isinstance(sender_info, dict):
                            sender = sender_info.get('nickname') or sender_info.get('username', '')
                        else:
                            sender = str(sender_info)
                        nickname = sender or item.get('nickname', '') or item.get('username', '')
                        lines.append(f"[{nickname}]: {content}" if nickname else content)
                        image_count += len(item.get('images', []))
                    quoted_text = "\n".join(lines)
                elif isinstance(forward_data, dict):
                    quoted_text = str(forward_data.get('message', ''))

            image_count += len(raw.get('images', []))
            if image_count:
                quoted_text += f"\n\n[包含 {image_count} 张图片]"

            return quoted_text.strip() or None

        except Exception as e:
            logger.error(f"提取引用内容出错: {e}")
            return None

    async def _extract_issue_info(self, content: str, event: AstrMessageEvent) -> Optional[str]:
        """调用 bot 默认 LLM 将消息整理成结构化 issue 内容"""
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                logger.warning("未找到可用的 LLM 提供商")
                return None

            prompt = f"""{self.llm_extract_prompt}

请特别注意：
1. 如果内容提到了图片，请在描述中说明"相关截图已包含在原消息中"
2. 尝试识别这是 BUG 报告、功能请求还是一般问题
3. BUG 报告：重点提取问题描述、复现步骤、预期结果、实际结果
4. 功能请求：重点提取功能描述、使用场景、预期效果
5. 使用清晰的 markdown 格式

待分析的消息内容：
{content}

请按以下格式输出：

## 问题类型
[BUG报告/功能请求/其他]

## 标题
[简洁明确的标题]

## 描述
[详细描述]

## 详细信息
[复现步骤或使用场景等]

## 其他信息
[任何其他相关信息]"""

            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=(
                    "你是一个专业的 GitHub Issue 整理助手，"
                    "负责将聊天记录中的问题或需求整理为结构清晰的 Issue 内容。"
                ),
            )

            result = resp.completion_text.strip()
            if len(result) < 50:
                logger.warning("LLM 生成内容过短，视为无效")
                return None
            return result

        except Exception as e:
            logger.error(f"LLM 提取信息出错: {e}")
            return None

    async def _create_github_issue(self, repo: str, content: str) -> Optional[str]:
        """调用 GitHub API 创建 issue，成功返回 URL，失败返回错误说明"""
        if not self.github_token:
            return "GitHub Token 未配置"
        if '/' not in repo:
            return f"仓库格式错误: {repo}"

        owner, repo_name = repo.split('/', 1)
        title, body = self._parse_issue_content(content)

        url = f"https://api.github.com/repos/{owner}/{repo_name}/issues"
        headers = {
            'Authorization': f'token {self.github_token}',
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json',
            'User-Agent': 'AstrBot-AutoIssue-Plugin',
        }
        data = {
            'title': title[:250],
            'body': body,
            'labels': ['auto-generated', 'from-chat'],
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=data) as response:
                    if response.status == 201:
                        result = await response.json()
                        logger.info(f"成功创建 issue: {result['html_url']}")
                        return result['html_url']
                    error_text = await response.text()
                    logger.error(f"GitHub API {response.status}: {error_text}")
                    if response.status == 401:
                        return "GitHub Token 无效或过期"
                    if response.status == 404:
                        return f"仓库 {repo} 不存在或无权限访问"
                    if response.status == 422:
                        return "请求数据格式错误（仓库可能未开启 Issues）"
                    return f"GitHub API 错误 {response.status}"
        except aiohttp.ClientError as e:
            logger.error(f"网络请求出错: {e}")
            return f"网络请求失败: {e}"

    def _parse_issue_content(self, content: str) -> tuple:
        """从 LLM 输出中提取标题，其余作为正文"""
        try:
            lines = content.strip().split('\n')
            title = "从聊天消息自动生成的Issue"

            for i, line in enumerate(lines):
                stripped = line.strip()
                if '标题' in stripped and stripped.startswith('#'):
                    # 标题在下一行
                    for j in range(i + 1, len(lines)):
                        candidate = lines[j].strip()
                        if candidate and not candidate.startswith('#'):
                            title = candidate
                            break
                    break

            # 清理标题中的 markdown 标记和方括号
            title = re.sub(r'^#+\s*', '', title)
            title = re.sub(r'\[.*?\]', '', title).strip()
            if len(title) < 3:
                title = "从聊天消息自动生成的Issue"

            return title, content
        except Exception as e:
            logger.error(f"解析 issue 内容出错: {e}")
            return "自动生成的Issue", content

    # ------------------------------------------------------------------ #
    #  管理命令                                                             #
    # ------------------------------------------------------------------ #

    @filter.command("bind_repo")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bind_repo(self, event, *args, **kwargs):
        """绑定当前群组到 GitHub 仓库：/bind_repo owner/repo"""
        args_str = ' '.join(args).strip()
        if not args_str:
            yield event.plain_result("使用方法: /bind_repo owner/repo\n例如: /bind_repo username/my-project")
            return
        if '/' not in args_str:
            yield event.plain_result("仓库格式错误，正确格式: owner/repo")
            return
        if not self.github_token:
            yield event.plain_result("❌ GitHub Token 未配置，请先在插件配置中设置 github_token")
            return

        yield event.plain_result("🔍 正在验证 GitHub Token 和仓库访问权限...")

        is_valid, msg = await self._validate_repo(self.github_token, args_str)
        if not is_valid:
            yield event.plain_result(f"❌ 验证失败: {msg}")
            return

        group_id = self._extract_group_id(event.session_id)
        if not group_id:
            yield event.plain_result("❌ 无法确定群组ID")
            return

        self.repo_bindings[str(group_id)] = args_str
        yield event.plain_result(f"✅ 群组 {group_id} 已绑定到仓库 {args_str}")

    @filter.command("unbind_repo")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unbind_repo(self, event, *args, **kwargs):
        """解除当前群组的仓库绑定"""
        group_id = self._extract_group_id(event.session_id)
        if not group_id:
            yield event.plain_result("❌ 无法确定群组ID")
            return
        if str(group_id) in self.repo_bindings:
            del self.repo_bindings[str(group_id)]
            yield event.plain_result(f"✅ 已解除群组 {group_id} 的仓库绑定")
        else:
            yield event.plain_result(f"群组 {group_id} 没有绑定任何仓库")

    @filter.command("list_bindings")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_bindings(self, event, *args, **kwargs):
        """列出所有群组仓库绑定"""
        if not self.repo_bindings:
            yield event.plain_result("当前没有任何群组仓库绑定")
            return
        text = "当前群组仓库绑定：\n" + "\n".join(
            f"群组 {gid}: {repo}" for gid, repo in self.repo_bindings.items()
        )
        yield event.plain_result(text)

    @filter.command("issue_status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def issue_status(self, event, *args, **kwargs):
        """查看插件状态"""
        group_id = self._extract_group_id(event.session_id)
        current_repo = self.repo_bindings.get(str(group_id), "未绑定") if group_id else "无法确定群组"
        yield event.plain_result(
            f"🔧 AutoIssue 插件状态\n\n"
            f"GitHub Token: {'✅ 已配置' if self.github_token else '❌ 未配置'}\n"
            f"触发关键字: {self.trigger_keyword}\n"
            f"需要@机器人: {'是' if self.require_at_bot else '否'}\n"
            f"当前群组: {group_id}\n"
            f"当前群组绑定: {current_repo}\n"
            f"总绑定数量: {len(self.repo_bindings)}\n\n"
            f"💡 使用 /issue_help 查看使用说明"
        )

    @filter.command("issue_help")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def issue_help(self, event, *args, **kwargs):
        """显示帮助信息"""
        yield event.plain_result(
            "📋 AutoIssue 插件使用说明\n\n"
            "🎯 使用方法：\n"
            "回复合并转发消息，@机器人 并输入 issue\n\n"
            "⚙️ 管理命令：\n"
            "• /bind_repo owner/repo — 绑定当前群组到仓库\n"
            "• /unbind_repo — 解除当前群组绑定\n"
            "• /list_bindings — 查看所有绑定\n"
            "• /issue_status — 查看插件状态\n"
            "• /issue_help — 显示此帮助\n\n"
            "⚠️ 前提条件：\n"
            "1. 在插件配置中设置 github_token\n"
            "2. 使用 /bind_repo 绑定当前群组"
        )

    # ------------------------------------------------------------------ #
    #  GitHub 验证                                                          #
    # ------------------------------------------------------------------ #

    async def _validate_repo(self, token: str, repo: str) -> tuple:
        owner, repo_name = repo.split('/', 1)
        url = f"https://api.github.com/repos/{owner}/{repo_name}"
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'AstrBot-AutoIssue-Plugin',
        }
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not data.get('has_issues', False):
                            return False, "仓库未开启 Issues 功能"
                        return True, "验证成功"
                    if resp.status == 401:
                        return False, "Token 无效或过期"
                    if resp.status == 404:
                        return False, "仓库不存在或无访问权限"
                    return False, f"HTTP {resp.status}"
        except aiohttp.ClientError as e:
            return False, f"请求失败: {e}"

    async def terminate(self):
        logger.info("AutoIssue 插件已卸载")
