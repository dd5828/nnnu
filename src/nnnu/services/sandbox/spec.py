"""沙箱执行协议（§11.2）：子进程 + 资源限制。

限制项：
- 超时默认 30s（上限 120s）；
- 输出上限 1MB（stdout+stderr 合并口径，超限杀进程并截断）；
- 工作目录锁定 data/user/workspace/（cwd_relative 归一化后必须落在其内；§8.1 目录树）；
- 内存限制：没做（§11.2 写的是"平台可用时"）——Windows 无 rlimit，POSIX 侧也没接
  setrlimit；失控进程靠超时杀进程 + 输出上限兜住；
- 环境变量白名单：系统级几个变量 + 显式传入；代理/凭据变量默认剔除
  （子进程沙箱的"网络关闭"即代理变量剥离——真隔离需 Docker，方案 §4 列为后续可选）；
- 配额：并发上限 + 每分钟次数（进程内，单用户单进程部署足够）。
"""

from dataclasses import dataclass, field

DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 120
OUTPUT_LIMIT_BYTES = 1 * 1024 * 1024
MAX_CONCURRENT = 2
MAX_PER_MINUTE = 30


@dataclass(slots=True)
class ExecRequest:
    """code（Python 源码）与 command（shell 命令）二选一。"""

    code: str | None = None
    command: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    allow_network: bool = False
    cwd_relative: str = ""  # 相对 data/workspace/ 的子目录
    env: dict[str, str] = field(default_factory=dict)  # 白名单之外的显式附加
    turn_id: str = ""  # 审计留痕用（哪个回合跑的）
    session_id: str = ""  # 审计留痕用（哪次会话跑的）


@dataclass(slots=True)
class ExecResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None  # 沙箱层错误（超时/输出超限/配额/路径非法）
    duration_s: float = 0.0
    truncated: bool = False  # 输出是否因超限被截断
