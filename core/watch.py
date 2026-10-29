"""判断「是不是真的被删了」，以及这一轮该不该动手。

单独抽出来是因为这部分最容易出错，而它不该跟网络请求、定时器缠在一起：
输入是三个集合，输出是一个结论，可以完全离线地跑测试。

**为什么不能只看好友列表**

`get_friend_list` 里没有主号，有三种可能：

1. 他真的把她删了；
2. NapCat 刚重连，列表还没拉全；
3. `owner_qq` 配错了。

第 1 种要动手，第 2、3 种一动手就是事故——去加一个根本没删你的人，
或者对着一个错的号反复发申请。所以设了三道闸：

- **必须先在本次运行里见过他一次**。没见过的，一律不动。
  这条挡住了「配置写错」和「启动瞬间列表为空」。
- **单向好友列表优先信**。QQ 自己记着「谁把我删了」，
  他在这张表里 = 确定被删，立刻动手，不等复核。
- **不确定就等下一轮**。列表里没有、单向表里也没有，
  那就再等 `confirm_rounds` 轮；连续几轮都看不到才认。

最后一道是"判错也得能收场"：一天最多试几次、两次之间隔多久，
都在配置里，不写在代码里。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

ACTION_NONE = "none"
ACTION_RESTORED = "restored"
ACTION_READD = "readd"

WHY_NOT_SEEN = "还没在列表里见过他，先不动"
WHY_NO_LIST = "好友列表取不到"
WHY_PRESENT = "还在"
WHY_WAIT = "列表里没有，等下一轮复核"
WHY_ABSENT = "被删了"


@dataclass
class Decision:
    action: str
    why: str
    confident: bool = False  # True = 单向好友列表确认，False = 靠连续缺失推断

    def __str__(self) -> str:
        tag = "确定" if self.confident else "推断"
        return f"{self.action}（{tag}）：{self.why}"


def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def decide(
    *,
    friends: set[str] | None,
    one_way: set[str] | None,
    owner: str,
    was_present: bool,
    miss_rounds: int,
    confirm_rounds: int,
) -> Decision:
    """给这一轮一个结论。

    Args:
        friends: 好友列表里的 QQ 号；None 表示这一轮没拿到，不做任何判断。
        one_way: 单向好友列表；None 表示接口不可用（不等于"没被删"）。
        owner: 主号 QQ 号。
        was_present: 上一轮的结论是不是"在"。
        miss_rounds: 到这一轮为止，连续看不到他的轮数（调用方维护）。
        confirm_rounds: 连续多少轮看不到才认。
    """
    if friends is None:
        return Decision(ACTION_NONE, WHY_NO_LIST)

    if owner in friends:
        if was_present:
            return Decision(ACTION_NONE, WHY_PRESENT)
        return Decision(ACTION_RESTORED, "回来了", confident=True)

    # 下面都是「好友列表里没有」
    if one_way is not None and owner in one_way:
        return Decision(ACTION_READD, WHY_ABSENT, confident=True)

    if not was_present:
        # 从没见过他在列表里：可能是配错了号，也可能是启动时列表没拉全
        return Decision(ACTION_NONE, WHY_NOT_SEEN)

    if miss_rounds < max(1, confirm_rounds):
        return Decision(ACTION_NONE, WHY_WAIT)

    return Decision(ACTION_READD, WHY_ABSENT, confident=False)


def budget_left(state: dict, per_day: int) -> int:
    """今天还能试几次。"""
    if per_day <= 0:
        return 10**6
    bucket = state.get("attempts") or {}
    if bucket.get("date") != today_key():
        return per_day
    return max(0, per_day - int(bucket.get("count") or 0))


def bump_attempt(state: dict) -> None:
    key = today_key()
    bucket = state.get("attempts") or {}
    if bucket.get("date") != key:
        bucket = {"date": key, "count": 0}
    bucket["count"] = int(bucket.get("count") or 0) + 1
    state["attempts"] = bucket


def cooldown_left(state: dict, minutes: int) -> int:
    """还要等多少秒。0 表示可以动手了。

    优先读 `epoch`（真时间戳）。老状态文件里只有 `ts`（可读字符串），
    这里补一次解析，免得升级插件之后读到一个字符串直接抛 ValueError。
    """
    if minutes <= 0:
        return 0
    last = state.get("last_attempt") or {}
    raw = last.get("epoch")
    if raw is None:
        raw = last.get("ts")
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        # 老状态文件里 `ts` 是可读字符串，补一次解析，别让升级当场抛异常
        try:
            ts = datetime.fromisoformat(str(raw or "")).timestamp()
        except Exception:
            return 0
    if ts <= 0:
        return 0
    left = minutes * 60 - (datetime.now().timestamp() - ts)
    return int(left) if left > 0 else 0
