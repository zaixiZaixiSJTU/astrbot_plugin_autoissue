# astrbot_plugin_autoissue

回复合并转发消息并 @机器人 输入 `issue`，自动利用 LLM 提取内容并在 GitHub 仓库创建 Issue。

## 功能

- 回复引用消息 + @机器人 + `issue` 关键字触发
- 自动解析合并转发消息内容（支持聊天记录卡片）
- 使用 AstrBot 内置 LLM 整理为结构化 Issue 格式
- 通过 GitHub API 创建 Issue 并返回链接
- 支持多群组分别绑定不同仓库
- 绑定关系持久化存储，重启不丢失
- 自动读取目标仓库的 README，并由 LLM 总结项目用途、功能结构、术语和使用约束
- 按仓库将摘要持久化为本地小型知识库，生成 Issue 时自动作为项目背景使用
- 绑定仓库时预生成知识库；触发 Issue 时若缓存缺失，会自动读取并补建

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_autoissue` 安装，或克隆本仓库到 `data/plugins/` 目录。

## 配置

在 AstrBot 插件配置页面设置以下选项：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `github_token` | GitHub Personal Access Token（需要 `repo` + `issues` 权限） | 空 |
| `trigger_keyword` | 触发关键字 | `issue` |
| `require_at_bot` | 是否必须 @机器人 才触发 | `true` |
| `http_proxy` | HTTP 代理地址，如 `http://127.0.0.1:7890` | 空 |
| `llm_system_prompt` | 自定义 LLM 系统提示词 | 默认提示词 |

## 命令

所有命令仅 AstrBot 管理员可用。

| 命令 | 说明 |
|------|------|
| `/bindrepo owner/repo` | 绑定当前群组到指定 GitHub 仓库 |
| `/unbindrepo` | 解除当前群组的仓库绑定 |
| `/issuestatus` | 查看插件状态和当前群组绑定情况；知识库缺失时自动读取并补建 |

## 使用方法

1. 管理员先绑定仓库：
   ```
   /bindrepo yourname/yourrepo
   ```

2. 在群里长按（引用）一条消息，回复时 @机器人 并输入 `issue`：
   ```
   @机器人 issue
   ```

3. 机器人会分析引用消息内容，调用 LLM 整理后创建 GitHub Issue，并回复 Issue 链接。

首次绑定仓库时，插件还会通过 GitHub API 读取该仓库的 README，并在 AstrBot 插件数据目录生成 `repo_knowledge.json`。知识库按 `owner/repo` 分开保存；若文件被删除或旧绑定尚未生成知识库，下次触发 Issue 时会自动补建。README 读取或分析暂时失败不会阻断 Issue 创建，并会在后续触发时重试。

## 生成的 Issue 格式

```markdown
## Title
[简洁标题]

## Type
[BUG / Feature / Other]

## Description
[详细描述]

## Details
[复现步骤 / 使用场景]
```

## 常见问题

**无法连接 GitHub API**
设置 `http_proxy` 为本地代理地址，如 `http://127.0.0.1:7890`。

**触发后无反应**
确认回复的是合并转发消息（聊天记录卡片），且消息中包含 @机器人 和关键字。

**Token 无效**
确认 Token 具有目标仓库的 `repo` 权限，且仓库已开启 Issues 功能。

## 开发者信息

- **作者**: [zaixiZaixiSJTU](https://github.com/zaixiZaixiSJTU)
- **版本**: v1.0.0
- **依赖**: aiohttp
