"""企业微信待办 -> 飞书任务（Task v2）同步工具。

数据流：
    官方 @wecom/cli 的 todo 服务  ->  本地 SQLite 归档  ->  飞书任务 v2

本包只依赖 Python 标准库（3.8+），不要求安装 requests / openai 等第三方库。
"""

__version__ = "0.1.0"
