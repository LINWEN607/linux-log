#!/bin/bash
# 容器健康检查脚本

# 检查Python进程 - 使用pgrep替代pidof（更通用）
if ! pgrep -f "python.*src/main.py" > /dev/null; then
    echo "Python进程不存在"
    exit 1
fi

# 进程存在即视为健康（数据库轮询无文件心跳依赖）

echo "健康检查通过"
exit 0
