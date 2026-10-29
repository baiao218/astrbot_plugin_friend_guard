"""NapCat 侧的动作封装。

**只碰这几个接口**

| 动作 | 作用 | 备注 |
|---|---|---|
| `get_friend_list` | 好友列表 | 判断主号还在不在 |
| `get_unidirectional_friend_list` | 单向好友列表 | QQ 自己记着「谁把我删了」，这是最准的信号 |
| `get_doubt_friends_add_request` | 「可能认识的人」候选 | 返回里带 `flag`，是下一步的钥匙 |
| `set_doubt_friends_add_request` | 对候选人发好友申请 | 参数只有 `flag`；`approve` 没有语义，强制 true |
| `send_private_msg` | 私聊 | 已非好友时大概率失败，但有时还能走临时会话 |
| `send_group_msg` | 群消息 | 没有任何私聊通路时的最后一级 |
| `ArkSharePeer` | 生成「加好友」名片 ark | 配合群消息用，点一下就能加 |

**为什么是「可能认识的人」而不是「加好友」**

QQ 没有「凭 QQ 号直接成为一个陌生人的好友」的接口，谁都没有——
加好友永远是「A 发申请 → B 同意」。所以能做的只有「让她把申请发出去」。

`set_doubt_friends_add_request` 干的就是这件事：QQ 会基于共同群、共同好友、
最近联系人维护一份「可能认识的人」，对一个候选点「添加」，就是由她这一侧
发出真实的好友申请。主号刚把她删掉时，几乎必然落在这份名单里——
因为他就是「最近联系人」。这条路径最正规，也不需要在群里公开喊话。

**返回值形态**

aiocqhttp 的 `call_action` 成功时直接给 `data`，失败时抛异常；
但不同 NapCat 版本偶尔会把 `{status, retcode, data}` 整个吐回来。
两种都认，免得因为一个包装层就整条链路失灵。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

ACTION_FRIENDS = "get_friend_list"
ACTION_ONE_WAY = "get_unidirectional_friend_list"
ACTION_DOUBT_LIST = "get_doubt_friends_add_request"
ACTION_DOUBT_ADD = "set_doubt_friends_add_request"
ACTION_SEND_PRIVATE = "send_private_msg"
ACTION_SEND_GROUP = "send_group_msg"
ACTION_ARK_PEER = "ArkSharePeer"
ACTION_MEMBER_INFO = "get_group_member_info"
ACTION_GROUP_LIST = "get_group_list"


def candidate_uin(item: dict) -> str:
    """从一条候选里抠出 QQ 号。不同版本字段名不一样，挨个试。"""
    for key in ("uin", "user_id", "qq"):
        v = item.get(key)
        if v:
            return str(v)
    return ""


class NapCatError(RuntimeError):
    """NapCat 没连上、或者动作明确失败。"""


def _unwrap(data):
    """把 `{status, retcode, data}` 这种信封拆掉，不是信封就原样返回。"""
    if isinstance(data, dict) and "data" in data and (
        "retcode" in data or "status" in data
    ):
        return data["data"]
    return data


def _as_list(data) -> list:
    """接口返回的东西不一定真是 list，尽量捞出一个 list 来。"""
    data = _unwrap(data)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "doubtList", "doubt_list", "data"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


class NapCat:
    """一个薄壳，把「跟 NapCat 说话」这件事收在一处。"""

    def __init__(self, context) -> None:
        self.context = context
        self._client = None

    # ---------------- 连接 ----------------

    def client(self):
        """拿到 aiocqhttp 的底层 client；拿不到返回 None（此时不该做任何判断）。"""
        if self._client is not None:
            return self._client

        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return None

        try:
            insts = manager.get_insts()
        except Exception:
            insts = getattr(manager, "platform_insts", None) or []

        for inst in insts:
            try:
                if inst.meta().name != "aiocqhttp":
                    continue
            except Exception:
                continue
            try:
                client = inst.get_client()
            except Exception as e:
                logger.debug(f"[friend_guard] 取 client 失败: {e}")
                continue
            if client is not None:
                self._client = client
                return client
        return None

    async def call(self, action: str, **params):
        client = self.client()
        if client is None:
            raise NapCatError("NapCat 还没连上")
        ret = await client.call_action(action, **params)
        if isinstance(ret, dict) and ret.get("status") == "failed":
            raise NapCatError(
                f"{action} 失败：{ret.get('wording') or ret.get('message') or ret.get('retcode')}"
            )
        return _unwrap(ret)

    # ---------------- 查询 ----------------

    async def login_uin(self) -> str:
        data = await self.call("get_login_info")
        return str((data or {}).get("user_id") or "")

    async def friend_uins(self) -> set[str]:
        data = await self.call(ACTION_FRIENDS)
        if not isinstance(data, list):
            raise NapCatError("好友列表格式不对")
        return {str(f.get("user_id") or "") for f in data if f.get("user_id")}

    async def one_way_uins(self) -> set[str] | None:
        """谁把我删了。接口不存在时返回 None，不能因此判定「没被删」。"""
        try:
            data = await self.call(ACTION_ONE_WAY)
        except Exception as e:
            logger.debug(f"[friend_guard] 单向好友列表不可用: {e}")
            return None
        out = set()
        for item in _as_list(data):
            uin = item.get("uin") or item.get("user_id")
            if uin:
                out.add(str(uin))
        return out

    async def doubt_candidates(self, count: int = 50) -> list[dict]:
        try:
            data = await self.call(ACTION_DOUBT_LIST, count=count)
        except Exception as e:
            logger.debug(f"[friend_guard] 「可能认识的人」不可用: {e}")
            return []
        return [x for x in _as_list(data) if isinstance(x, dict)]

    @staticmethod
    def candidate_uin(item: dict) -> str:
        return candidate_uin(item)

    async def common_group(self, owner: str, limit: int = 12) -> str:
        """找主号还在的第一个群。没有共同群就返回空。"""
        try:
            groups = _as_list(await self.call(ACTION_GROUP_LIST))
        except Exception as e:
            logger.debug(f"[friend_guard] 群列表取不到: {e}")
            return ""
        for g in groups[:limit]:
            gid = str(g.get("group_id") or "")
            if not gid:
                continue
            try:
                info = await self.call(
                    ACTION_MEMBER_INFO, group_id=gid, user_id=owner, no_cache=True
                )
            except Exception:
                continue
            if isinstance(info, dict) and str(info.get("user_id") or "") == owner:
                return gid
        return ""

    async def peer_ark(self, owner: str) -> str:
        """生成一张「加我」名片（ark json 字符串），失败返回空串。"""
        try:
            data = await self.call(ACTION_ARK_PEER, user_id=owner)
        except Exception as e:
            logger.debug(f"[friend_guard] 名片生成失败: {e}")
            return ""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return str(data.get("ark") or data.get("data") or "")
        return ""

    # ---------------- 动作 ----------------

    async def approve_candidate(self, flag: str) -> tuple[bool, str]:
        """对「可能认识的人」里的一条发起好友申请。"""
        try:
            await self.call(ACTION_DOUBT_ADD, flag=str(flag), approve=True)
        except Exception as e:
            return False, str(e)
        return True, ""

    async def send_private(self, owner: str, text: str) -> tuple[bool, str]:
        try:
            await self.call(
                ACTION_SEND_PRIVATE,
                user_id=owner,
                message=[{"type": "text", "data": {"text": text}}],
            )
        except Exception as e:
            return False, str(e)
        return True, ""

    async def send_group(
        self, group_id: str, text: str, at: str = "", ark: str = ""
    ) -> tuple[bool, str]:
        chain: list[dict] = []
        if at:
            chain.append({"type": "at", "data": {"qq": at}})
            chain.append({"type": "text", "data": {"text": " "}})
        chain.append({"type": "text", "data": {"text": text}})
        if ark:
            chain.append({"type": "ark", "data": {"data": ark}})
        try:
            await self.call(ACTION_SEND_GROUP, group_id=group_id, message=chain)
        except Exception as e:
            return False, str(e)
        return True, ""

    async def approve_friend_request(self, flag: str, remark: str = "") -> tuple[bool, str]:
        params = {"flag": str(flag), "approve": True}
        if remark:
            params["remark"] = remark
        try:
            await self.call("set_friend_add_request", **params)
        except Exception as e:
            return False, str(e)
        return True, ""

    async def backoff(self, seconds: float) -> None:
        """两次动作之间留点空隙，别让 QQ 觉得是脚本在刷。"""
        await asyncio.sleep(max(0.0, seconds))
