# AstrBot AutoIssue 插件

## 概述

AstrBot AutoIssue 插件可以自动从群聊的合并转发消息中提取信息，并使用AI分析后在GitHub仓库中创建Issue。非常适合用于收集用户反馈、BUG报告和功能请求。

## 主要功能

- 🤖 自动检测@机器人 + "issue"关键字
- 📋 智能解析合并转发消息内容
- 🔍 使用LLM提取和整理关键信息
- 🏷️ 自动生成结构化的GitHub Issue
- 🔗 支持多群组与不同仓库的绑定
- 🖼️ 支持包含图片的消息分析

## 安装和配置

### 1. 安装依赖

```bash
pip install aiohttp
```

### 2. 配置GitHub Token

1. 在GitHub设置中生成Personal Access Token
2. 确保Token具有以下权限：
   - `repo` (仓库访问)
   - `issues:write` (创建Issue)

3. 在AstrBot插件配置中设置：
```json
{
  "github_token": "your_github_token_here",
  "trigger_keyword": "issue",
  "require_at_bot": true
}
```

### 3. 绑定群组和仓库

```bash
/bind_repo username/repository-name
```

## 使用方法

### 基本使用流程

1. 在群聊中，回复任意合并转发消息
2. @机器人并输入"issue"
3. 插件自动分析消息内容并创建GitHub Issue

### 可用命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/bind_repo` | 绑定当前群组到GitHub仓库 | `/bind_repo myname/myrepo` |
| `/unbind_repo` | 解除当前群组的仓库绑定 | `/unbind_repo` |
| `/list_bindings` | 查看所有群组绑定 | `/list_bindings` |
| `/issue_status` | 查看插件状态和配置 | `/issue_status` |
| `/issue_help` | 显示帮助信息 | `/issue_help` |

### 配置选项

```json
{
  "github_token": "GitHub Personal Access Token",
  "trigger_keyword": "触发关键字，默认为'issue'",
  "require_at_bot": "是否需要@机器人，默认为true",
  "repo_bindings": {
    "群组ID": "owner/repo"
  },
  "llm_extract_prompt": "LLM提取信息的提示词模板"
}
```

## 使用示例

### 场景1：BUG报告

用户在群里转发了包含BUG描述的消息，管理员回复并@机器人输入"issue"：

```
@机器人 issue
```

插件会自动：
1. 分析转发消息内容
2. 识别这是一个BUG报告
3. 提取关键信息（问题描述、复现步骤等）
4. 在GitHub仓库中创建格式化的Issue

### 场景2：功能请求

用户提出功能建议，经过讨论后需要记录到GitHub：

```
@机器人 issue
```

插件会自动创建功能请求类型的Issue。

## 生成的Issue格式

插件会生成结构化的GitHub Issue，包含：

```markdown
## 问题类型
[BUG报告/功能请求/其他]

## 标题
[简洁明确的标题]

## 描述
[详细描述]

## 详细信息
[具体信息，如复现步骤、使用场景等]

## 其他信息
[任何其他相关信息，包括图片说明]
```

## 注意事项

1. **权限要求**：GitHub Token需要有仓库的Issues写入权限
2. **网络连接**：需要能够访问GitHub API
3. **LLM配置**：需要AstrBot配置了可用的LLM提供商
4. **消息格式**：主要支持QQ群的合并转发消息格式
5. **群组绑定**：每个群组只能绑定一个仓库

## 故障排除

### 常见问题

**Q: 创建Issue失败，提示Token无效**
A: 检查GitHub Token是否正确配置且具有足够权限

**Q: 无法提取消息内容**
A: 确认回复的是合并转发消息，且消息格式被支持

**Q: LLM分析失败**
A: 检查AstrBot的LLM配置是否正常

**Q: 群组无法绑定仓库**
A: 使用 `/issue_status` 检查配置状态

### 调试日志

插件会在日志中记录详细的执行过程，可以通过AstrBot日志查看具体错误信息。

## 开发者信息

- **插件名称**: astrbot_plugin_autoissue
- **版本**: v1.0.0
- **作者**: Claude
- **依赖**: aiohttp

## 许可证

此插件遵循AstrBot的许可证协议。