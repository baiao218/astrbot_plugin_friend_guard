"""好友守卫 —— 被删掉之后，她自己把申请发回去。

**为什么需要代码来做这件事**

人格提示词管的是"怎么说话"，管不了一个人的好友列表。
她被删掉的时候不会有任何消息进来，模型永远等不到那个可以让她反应的事件——
所以"发现被删"和"主动加回来"都只能由插件盯着。

**她能做什么，不能做什么**

QQ 没有「凭一个 QQ 号直接成为好友」的接口，谁都没有。加好友永远是
「A 发申请 → B 同意」。所以这里的"主动"，指的是**由她这一侧发出申请**，
主号那边还是要点一下同意——这一步没人能代劳，也不该代劳。

三条路，从正规到笨拙：

1. **可能认识的人**（首选）。QQ 基于共同群、共同好友、最近联系人维护一份名单，
   主号刚把她删掉时几乎必然在这份名单里——因为他就是"最近联系人"。
   对他发起添加 = 一条真实的好友申请，附言、头像、昵称都正常。
2. **私聊**。已经不是好友了，但 QQ 有时仍允许临时会话。发得出去，
   主号就会看到一条带"加好友"按钮的陌生人消息。
3. **共同群**。前两条都不通时，在第一个共同群里 @ 他一句，附一张"加我"名片。
   最笨，但一定看得见。

**三道防误伤的闸**

- 本次运行里必须先见过他在好友列表里，否则一律不动（挡住配置写错）；
- 单向好友列表里确认有他 = 立刻动手；只是"列表里没有" = 等复核几轮；
- 一天最多试几次、两次之间隔多久，全部写进配置。

**加回来之后**

她不会装作什么都没发生。检测到好友关系恢复，会主动发一句已经在配置里写好、
而且带标点习惯的话——短、克制、不提"系统恢复了"这种词。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .core.napcat import NapCat, candidate_uin
from .core.watch import (
    ACTION_READD,
    ACTION_RESTORED,
    budget_left,
    bump_attempt,
    cooldown_left,
    decide,
)

PLUGIN_NAME = "astrbot_plugin_friend_guard"

# 高于指令闸门之外的一切：这件事发生在消息进模型之前
GUARD_PRIORITY = 99998

DEFAULT_GREET = "你把我删了\n……加回来就好"
DEFAULT_PRIVATE = "你把我删了。加回来"
DEFAULT_GROUP = "好友列表里少了你。加回来"


class FriendGuardPlugin(Star):
    """盯住好友关系，断了就自己发申请。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.settings = config

        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.state = self._load_state()

        self._stop = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._miss = 0  # 连续看不到他的轮数

    # ---------------- 状态 ----------------

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[friend_guard] 状态文件读不出来，按空处理: {e}")
        return {}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning(f"[friend_guard] 状态写入失败: {e}")

    # ---------------- 配置读取 ----------------

    def _owner(self) -> str:
        raw = str(self.settings.get("owner_qq") or "").strip()
        return re.sub(r"\D", "", raw)

    def _text(self, key: str, default: str) -> str:
        raw = str(self.settings.get(key) or "").strip()
        return raw or default

    def _interval(self) -> int:
        try:
            return max(1, int(self.settings.get("check_interval_minutes") or 10))
        except Exception:
            return 10

    # ---------------- 生命周期 ----------------

    @filter.on_astrbot_loaded()
    async def _boot(self) -> None:
        if not bool(self.settings.get("enable", True)):
            logger.info("[friend_guard] 已关闭，不启动。")
            return
        if not self._owner():
            logger.warning("[friend_guard] 没配 owner_qq，盯着也没意义，先不启动。")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[friend_guard] 就绪：每 {self._interval()} 分钟看一眼 {self._owner()} 还在不在。"
        )

    async def terminate(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._save_state()

    async def _loop(self) -> None:
        grace = int(self.settings.get("startup_grace_seconds") or 180)
        await asyncio.sleep(max(0, grace))
        while not self._stop:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[friend_guard] 这一轮出错了：{e}")
            await asyncio.sleep(self._interval() * 60)

    # ---------------- 主循环 ----------------

    async def _tick(self, manual: bool = False) -> str:
        """看一眼现在什么情况。返回一句话说明这一轮做了什么。"""
        owner = self._owner()
        if not owner:
            return "没有 owner_qq"

        nap = NapCat(self.context)
        if nap.client() is None:
            return "NapCat 还没连上"

        try:
            friends = await nap.friend_uins()
        except Exception as e:
            logger.debug(f"[friend_guard] 好友列表拿不到：{e}")
            return "好友列表拿不到"

        one_way = await nap.one_way_uins()

        if owner in friends:
            self._miss = 0
        else:
            self._miss += 1

        ever_present = bool(self.state.get("ever_present"))

        d = decide(
            friends=friends,
            one_way=one_way,
            owner=owner,
            was_present=bool(self.state.get("present")),
            miss_rounds=self._miss,
            confirm_rounds=int(self.settings.get("confirm_rounds") or 2),
        )
        self.state["present"] = owner in friends
        if owner in friends:
            self.state["ever_present"] = True
        self.state["last_check"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        self._save_state()

        if d.action == ACTION_RESTORED:
            if not ever_present:
                # 第一次见到他就在列表里，不是"恢复"，是"刚开始"。
                # 少了这一条，每次重装/删状态文件都会莫名其妙说一句"你把我删了"。
                logger.info("[friend_guard] 首次确认他在好友列表里。")
                return "还在（第一次确认）"
            if not bool(self.settings.get("greet_after_readd", True)):
                self.state["restored_at"] = datetime.now().isoformat(
                    sep=" ", timespec="seconds"
                )
                self._save_state()
                return "恢复"
            if int(self.state.get("greet_fail") or 0) >= 3:
                return "恢复，但招呼一直发不出去，先不试了"
            ok, err = await self._greet(nap, owner)
            if ok:
                self.state["greet_fail"] = 0
                self.state["restored_at"] = datetime.now().isoformat(
                    sep=" ", timespec="seconds"
                )
                logger.info("[friend_guard] 好友关系恢复，打过招呼了")
                self._save_state()
                return "恢复，打过招呼了"
            # 发不出去就把「在」这个结论退回去，下一轮重新走一遍恢复流程。
            # 比另起一套重试机制省事，而且不会误触发「被删」那条路——他还在列表里。
            self.state["greet_fail"] = int(self.state.get("greet_fail") or 0) + 1
            self.state["present"] = False
            self._save_state()
            logger.warning(f"[friend_guard] 恢复后的招呼没发出去：{err}")
            return f"恢复，但招呼没发出去：{err}"

        if d.action != ACTION_READD:
            logger.debug(f"[friend_guard] {d}")
            return str(d)

        if not manual and not bool(self.settings.get("auto_readd", True)):
            return "被删了，但自动加回是关的"

        if not manual:
            left = cooldown_left(self.state, int(self.settings.get("attempt_cooldown_minutes") or 30))
            if left > 0:
                return f"被删了，冷却中，还剩 {left // 60 + 1} 分钟"
            if budget_left(self.state, int(self.settings.get("max_attempts_per_day") or 3)) <= 0:
                return "被删了，今天的次数用完了"

        result = await self._attempt(nap, owner)
        self.state["last_attempt"] = result
        bump_attempt(self.state)
        self._save_state()
        logger.info(f"[friend_guard] {d} → 处理结果：{result}")
        return f"{d} → {self._method_cn(result)}"

    # ---------------- 三条路 ----------------

    async def _attempt(self, nap: NapCat, owner: str) -> dict:
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        notes: list[str] = []

        def done(method: str, detail: str) -> dict:
            # notes 一起带上：走到第三条路才成功的时候，
            # 前两条为什么没通是最重要的信息，不能因为成功就丢掉
            return {
                "ts": now,
                "epoch": datetime.now().timestamp(),  # 冷却要的是数字，不是给人看的字符串
                "method": method,
                "ok": method != "none",
                "detail": detail,
                "notes": list(notes),
            }

        # 1｜可能认识的人 → 发一条真实的好友申请
        try:
            candidates = await nap.doubt_candidates(50)
        except Exception as e:
            candidates = []
            notes.append(f"名单取不到：{e}")

        hit = next(
            (c for c in candidates if candidate_uin(c) == owner), None
        )
        if hit:
            flag = str(hit.get("flag") or "")
            if flag:
                ok, err = await nap.approve_candidate(flag)
                if ok:
                    return done("doubt", "对「可能认识的人」里的他发了申请")
                notes.append(f"申请没发出去：{err}")
            else:
                notes.append("候选里没有 flag")
        else:
            notes.append("他不在「可能认识的人」名单里")

        # 2｜私聊（临时会话）
        if bool(self.settings.get("allow_private", True)):
            await nap.backoff(2)
            ok, err = await nap.send_private(owner, self._text("private_text", DEFAULT_PRIVATE))
            if ok:
                return done("private", "私聊发出去了，他看到就能加")
            notes.append(f"私聊不通：{err}")

        # 3｜共同群 @ 他
        if bool(self.settings.get("allow_group", True)):
            await nap.backoff(2)
            gid = await nap.common_group(owner)
            if gid:
                ark = ""
                if bool(self.settings.get("send_ark_card", True)):
                    ark = await nap.peer_ark(owner)
                ok, err = await nap.send_group(
                    gid, self._text("group_text", DEFAULT_GROUP), at=owner, ark=ark
                )
                if ok:
                    return done("group", f"在群 {gid} 里喊了他")
                notes.append(f"群消息没发出去：{err}")
            else:
                notes.append("没有共同群")

        return done("none", "；".join(notes) or "无路可走")

    async def _greet(self, nap: NapCat, owner: str) -> tuple[bool, str]:
        return await nap.send_private(owner, self._text("greet_text", DEFAULT_GREET))

    @staticmethod
    def _method_cn(result: dict) -> str:
        table = {
            "doubt": "申请已发出（可能认识的人）",
            "private": "私聊已发出",
            "group": "在群里喊了",
            "none": "三条路都不通",
        }
        return table.get(str(result.get("method")), "未知")

    # ---------------- 他主动加回来的时候 ----------------

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE, priority=GUARD_PRIORITY
    )
    async def on_private(self, event: AstrMessageEvent) -> None:
        """好友申请事件没有正文，AstrBot 会把它当私聊事件送进来，这里截住。"""
        if not bool(self.settings.get("enable", True)):
            return
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "request":
            return
        if raw.get("request_type") != "friend":
            return

        uid = str(raw.get("user_id") or "")
        owner = self._owner()
        if not uid or uid != owner:
            return  # 别人的申请照常走人工，不替主人做主

        event.stop_event()
        event.should_call_llm(True)

        if not bool(self.settings.get("auto_approve_request", True)):
            logger.info(f"[friend_guard] 收到 {uid} 的好友申请，但自动通过是关的。")
            return

        flag = str(raw.get("flag") or "")
        nap = NapCat(self.context)
        ok, err = await nap.approve_friend_request(flag)
        if not ok:
            logger.warning(f"[friend_guard] 通过好友申请失败：{err}")
            return

        logger.info(f"[friend_guard] 已通过 {uid} 的好友申请。")
        self.state["present"] = True
        self._miss = 0
        self.state["restored_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        self._save_state()

        if bool(self.settings.get("greet_after_readd", True)):
            await asyncio.sleep(3)  # 等好友关系在两边的列表里都落定
            ok, err = await self._greet(nap, owner)
            if not ok:
                logger.warning(f"[friend_guard] 打招呼失败：{err}")

    # ---------------- 命令 ----------------

    def _status_lines(self) -> list[str]:
        owner = self._owner()
        present = bool(self.state.get("present"))
        left = budget_left(self.state, int(self.settings.get("max_attempts_per_day") or 3))
        cool = cooldown_left(self.state, int(self.settings.get("attempt_cooldown_minutes") or 30))
        last = self.state.get("last_attempt") or {}

        lines = ["【好友】"]
        if present:
            lines.append(f"· {owner} 在列表里")
        else:
            lines.append(f"· {owner} 不在列表里")
        lines.append(f"· 今天还能试 {min(left, 99)} 次" + (f"，冷却还剩 {cool // 60 + 1} 分钟" if cool else ""))
        if last:
            how = self._method_cn(last)
            lines.append(f"· 上次动手：{last.get('ts', '?')}（{how}）")
            if last.get("detail"):
                lines.append(f"　{last['detail']}")
            for note in (last.get("notes") or [])[:3]:
                lines.append(f"　· {note}")
        else:
            lines.append("· 还没动过手")
        lines.append(f"· 状态文件：{self.state_path}")
        return lines

    @filter.command("好友状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """看看好友关系的现状。"""
        nap = NapCat(self.context)
        online = nap.client() is not None
        lines = self._status_lines()
        lines.insert(1, f"· 连接：{'在' if online else '没连上'}")
        yield event.plain_result("\n".join(lines))

    @filter.command("加回我")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_readd(self, event: AstrMessageEvent):
        """立刻走一次加回流程，不看冷却和次数。"""
        if not self._owner():
            yield event.plain_result("没配 owner_qq，不知道要加谁")
            return
        async with self._lock:
            line = await self._tick(manual=True)
        yield event.plain_result(line)

    @filter.command("重加自检")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_probe(self, event: AstrMessageEvent):
        """只探测三条路通不通，不发任何申请。"""
        owner = self._owner()
        if not owner:
            yield event.plain_result("没配 owner_qq")
            return
        nap = NapCat(self.context)
        if nap.client() is None:
            yield event.plain_result("NapCat 还没连上")
            return

        lines = ["【三条路】"]
        try:
            cands = await nap.doubt_candidates(50)
            hit = next((c for c in cands if candidate_uin(c) == owner), None)
            if hit:
                lines.append(f"· 可能认识的人：在名单里（{len(cands)} 个候选），可以直接发申请")
            else:
                lines.append(f"· 可能认识的人：不在名单里（{len(cands)} 个候选），这条路暂时不通")
        except Exception as e:
            lines.append(f"· 可能认识的人：接口不可用（{e}）")

        try:
            gid = await nap.common_group(owner)
        except Exception as e:
            gid = ""
            lines.append(f"· 共同群：查不了（{e}）")
        else:
            lines.append(f"· 共同群：{gid if gid else '没有共同群，群里喊不到他'}")

        try:
            one_way = await nap.one_way_uins()
        except Exception:
            one_way = None
        if one_way is None:
            lines.append("· 单向好友列表：这个 NapCat 版本没有，只能靠列表缺失推断")
        else:
            lines.append(
                "· 单向好友列表："
                + (f"{len(one_way)} 个，{'他在里面' if owner in one_way else '他不在里面'}")
            )

        lines.append(f"· 今天还能试 {min(budget_left(self.state, int(self.settings.get('max_attempts_per_day') or 3)), 99)} 次")
        yield event.plain_result("\n".join(lines))
