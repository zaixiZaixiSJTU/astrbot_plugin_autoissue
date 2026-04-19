#!/bin/bash

# AstrBot AutoIssue 插件安装脚本

echo "🚀 开始安装 AstrBot AutoIssue 插件..."

# 检查Python环境
if ! command -v python &> /dev/null; then
    echo "❌ 未找到Python环境，请先安装Python"
    exit 1
fi

# 安装依赖
echo "📦 安装依赖包..."
pip install -r requirements.txt

if [ $? -eq 0 ]; then
    echo "✅ 依赖安装成功"
else
    echo "❌ 依赖安装失败"
    exit 1
fi

echo "📝 配置说明："
echo "1. 在GitHub生成Personal Access Token"
echo "2. 在AstrBot插件配置中设置github_token"
echo "3. 使用 /bind_repo 命令绑定群组和仓库"
echo "4. 参考 config_example.json 进行配置"

echo "🎉 插件安装完成！使用 /issue_help 查看帮助信息"