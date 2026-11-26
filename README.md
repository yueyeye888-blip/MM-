# 迪迦数据库：加密源码备份

2026-11-26 的程序、部署脚本、文档和测试已归入经过身份验证的加密档案。本分支不含真实工作簿、生成报告、历史记录、账号库、API 凭据、环境配置或项目行情映射。恢复的是软件，不是生产数据。

## 如何恢复

1. 下载整个分支，或将本分支克隆到本地。准备备份时另行保存的恢复密钥文件（不要上传或粘贴其内容）。
2. 在可信电脑上的 Python 3 虚拟环境中安装依赖：`python -m pip install cryptography`。
3. 在本目录运行：`python restore.py --key "/path/to/restore.key" --output "restored-workspace"`。输出目录必须尚不存在。恢复成功后可在 `restored-workspace/trade-journal/` 查看使用说明。

档案使用 Fernet 对 ZIP 的全部内容加密并校验完整性。没有对应密钥无法解密；建议将密钥另存至安全的离线位置。
