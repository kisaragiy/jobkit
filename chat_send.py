"""聊天发送通道 —— 在已登录的求职者聊天页里替前端发一条消息。

为什么必须走 DOM：
BOSS 已把发送端点迁到 WS+Protobuf（boss-agent-cli `recruiter_client.send_message`
的注释：旧 fastReply/sendReplyMsg 必返 121）。求职者端同样没有可用 HTTP 端点，
唯一可行路径是像真人一样在聊天页里输入 + 点发送。

选择器全部来自真机探测（tmp/probe_chat_dom4.py，2026-10-21）：
    #chat-input.chat-input[contenteditable=true]  输入框（可见 752x79）
    button.btn-send                               发送按钮（空输入时带 disabled）
    .user-list-content li                         会话项（40 条）
    ul.im-list li.message-item.item-myself        自己发的消息（验真用）

三条硬约束：
  1. **默认干跑**：send() 的 dry_run 默认 True，只填不发；真发必须显式关闭。
  2. **必须验真**：点完发送不算成功，要看到 im-list 里多出一条且文本匹配。
  3. **不点错人**：会话匹配不到或匹配到多条时拒绝发送，不猜。
"""
from __future__ import annotations

import time

import websocket

from chat_sync import _call, _cdp_target, _eval

CHAT_URL = "https://www.zhipin.com/web/geek/chat"

SEL_LIST = ".user-list-content li"
SEL_INPUT = "#chat-input"
SEL_SEND = "button.btn-send"
SEL_MINE = "ul.im-list li.message-item.item-myself"


class SendError(Exception):
    """发送失败（应中止并报错）。"""


class SendSkip(Exception):
    """不该发（会话找不到/已不可发），不是错误但要如实说。"""


# ── 页面动作（每个都能单独测）──────────────────────────────

def _rows_js() -> str:
    return r"""(() => {
      return [...document.querySelectorAll(".user-list-content li")].map((li, i) => ({
        i,
        text: (li.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
      }));
    })()"""


def pick_row(rows: list[dict], needle: str) -> int:
    """在会话列表里找 needle 所属的行号。

    只接受**唯一命中**：0 条或多条都抛 SendSkip —— 发错人比不发糟得多。
    """
    needle = (needle or "").strip()
    if not needle:
        raise SendSkip("会话标识为空")
    hits = [r for r in rows if needle in (r.get("text") or "")]
    if not hits:
        raise SendSkip(f"会话列表里找不到「{needle}」")
    if len(hits) > 1:
        names = "、".join((h.get("text") or "")[:18] for h in hits[:3])
        raise SendSkip(f"「{needle}」匹配到 {len(hits)} 个会话（{names}…），不敢猜")
    return int(hits[0]["i"])


def _count_mine_js() -> str:
    return f"document.querySelectorAll({SEL_MINE!r}).length"


def _last_mine_js() -> str:
    return r"""(() => {
      const els = document.querySelectorAll("ul.im-list li.message-item.item-myself");
      if (!els.length) return '';
      return (els[els.length - 1].innerText || '').replace(/\s+/g, ' ').trim();
    })()"""


def _tip_js() -> str:
    return r"""(() => {
      const t = document.querySelector('.message-tip-bar');
      return t ? (t.innerText || '').trim() : '';
    })()"""


def _input_text_js() -> str:
    return r"""(() => {
      const el = document.querySelector('#chat-input');
      return el ? (el.innerText || '').trim() : '';
    })()"""


def _send_disabled_js() -> str:
    return r"""(() => {
      const b = document.querySelector('button.btn-send');
      if (!b) return null;
      return b.classList.contains('disabled') || b.disabled === true;
    })()"""


# ── 主流程 ────────────────────────────────────────────────

def _dispatch_click(ws, mid: int, x: float, y: float) -> None:
    """真鼠标事件（el.click() 不触发 Vue 的选中逻辑，已实测）。"""
    for kind in ("mousePressed", "mouseReleased"):
        _call(ws, mid, "Input.dispatchMouseEvent", type=kind, x=x, y=y,
              button="left", clickCount=1)


def _center_of(ws, mid: int, expr: str) -> dict | None:
    return _eval(ws, mid, r"""(() => {
      const el = %s;
      if (!el) return null;
      el.scrollIntoView({block: 'center'});
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return null;
      return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + Math.min(24, r.height / 2))};
    })()""" % expr)


def open_conversation(ws, mid: int, needle: str, *, settle: float = 2.5) -> dict:
    """点击列表里的唯一匹配会话，等右侧渲染出输入框。"""
    rows = _eval(ws, mid, _rows_js()) or []
    idx = pick_row(rows, needle)

    box = _eval(ws, mid, r"""(() => {
      const li = document.querySelectorAll(".user-list-content li")[%d];
      if (!li) return null;
      li.scrollIntoView({block: 'center'});
      const r = li.getBoundingClientRect();
      return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + Math.min(30, r.height / 2))};
    })()""" % idx)
    if not box:
        raise SendError(f"会话项第 {idx} 行不可点（可能被列表虚拟化挤出视口）")
    _dispatch_click(ws, mid, box["x"], box["y"])

    deadline = time.time() + 12
    while time.time() < deadline:
        ok = _eval(ws, mid, r"""(() => {
          const el = document.querySelector('#chat-input');
          if (!el) return false;
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        })()""")
        if ok:
            time.sleep(settle * 0.4)
            return {"index": idx, "row": rows[idx]["text"]}
        time.sleep(0.6)
    raise SendError("点开会话后 12 秒内没等到输入框（右侧可能是「无数据」）")


def clear_input(ws, mid: int) -> None:
    """清空输入框（选中全部 → delete）。"""
    _eval(ws, mid, r"""(() => {
      const el = document.querySelector('#chat-input');
      if (!el) return false;
      el.focus();
      const r = document.createRange();
      r.selectNodeContents(el);
      const s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
      return true;
    })()""")
    _eval(ws, mid, "document.execCommand('delete', false, null)")


def fill_input(ws, mid: int, text: str) -> str:
    """往 contenteditable 里插入文本，返回实际落进去的内容。

    用 execCommand('insertText') 而非直接改 innerHTML：只有真实输入事件
    才会让 Vue 更新状态、解除发送按钮的 disabled。
    """
    if not (text or "").strip():
        raise SendError("消息内容为空")
    clear_input(ws, mid)
    ok = _eval(ws, mid, "(function(t){ try { return document.execCommand('insertText', false, t); }"
                         " catch (e) { return false; } })(%r)" % text)
    landed = str(_eval(ws, mid, _input_text_js()) or "").strip()
    if not ok and not landed:
        raise SendError("无法向输入框写入文本（execCommand 与 innerText 都为空）")
    return landed


def wait_send_enabled(ws, mid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _eval(ws, mid, _send_disabled_js()) is False:
            return True
        time.sleep(0.3)
    return False


def click_send(ws, mid: int) -> None:
    box = _center_of(ws, mid, "document.querySelector('button.btn-send')")
    if not box:
        raise SendError("找不到可点的发送按钮")
    _dispatch_click(ws, mid, box["x"], box["y"])


def verify_sent(ws, mid: int, text: str, before: int, timeout: float = 10.0) -> dict:
    """验真：im-list 里多出一条自己的消息，且原文对得上；另外盯错误提示条。"""
    expected = (text or "").strip().replace("\r\n", "\n")
    deadline = time.time() + timeout
    last_tip = ""
    while time.time() < deadline:
        tip = str(_eval(ws, mid, _tip_js()) or "").strip()
        if tip:
            last_tip = tip
            if any(signal in tip for signal in ("失败", "频繁", "异常", "验证", "限制", "登录")):
                raise SendError(f"[OUTCOME_UNKNOWN] 发送结果待核对；页面提示：{tip}")
        now = _eval(ws, mid, _count_mine_js()) or 0
        last = str(_eval(ws, mid, _last_mine_js()) or "").strip()
        if now > before and expected and expected == last.replace("\r\n", "\n"):
            return {"ok": False, "count": now, "last": last[:60], "evidence": "local_echo",
                    "outcome": "outcome_unknown", "delivery_confirmed": False,
                    "error": "OUTCOME_UNKNOWN: 已看到本地回显，尚无平台送达确认"}
        time.sleep(0.5)
    tip_hint = f"；页面提示：{last_tip}" if last_tip else ""
    raise SendError(f"[OUTCOME_UNKNOWN] 点了发送但 {timeout:.0f} 秒内未确认完整正文，需核对后再操作{tip_hint}")


def send(needle: str, text: str, *, dry_run: bool = True, settle: float = 2.0,
         clear_after_dry_run: bool = True, keep_text: bool = False) -> dict:
    """打开会话 → 填入 →（可选）发出 → 验真。

    dry_run=True（默认）只在输入框里填好就停，验证通道可用而不打扰任何人。
    keep_text=True 时干跑后**不清空**输入框 —— 这是给人看一遍、自己点发送用的
    （人工确认不替代渠道接入许可）。
    """
    from sandbox import KernelError, require_registered
    try:
        require_registered("boss.chat.fill" if dry_run else "boss.chat.send")
    except KernelError as exc:
        raise SendError(f"{exc.code}: {exc}") from exc
    target = _cdp_target()
    if not target:
        raise SendError("没有找到 zhipin 聊天页（浏览器没开或没登录）")
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60,
                                     suppress_origin=True)
    mid = [0]  # chat_sync._call 用可变计数器，必须传 list
    try:
        opened = open_conversation(ws, mid, needle, settle=settle)
        landed = fill_input(ws, mid, text)
        enabled = wait_send_enabled(ws, mid)
        result = {"ok": True, "dry_run": dry_run, "conversation": opened,
                  "input_text": landed, "send_enabled": enabled}
        if not enabled:
            raise SendError("文本已写入但发送按钮仍是 disabled（页面可能校验未通过）")
        if dry_run:
            if clear_after_dry_run and not keep_text:
                clear_input(ws, mid)  # 清空，避免留半句话在输入框
                result["cleared"] = True
            elif keep_text:
                result["kept"] = True   # 留给用户自己看、自己点发送
            return result

        before = _eval(ws, mid, _count_mine_js()) or 0
        try:
            click_send(ws, mid)
            result.update(verify_sent(ws, mid, text, before))
        except Exception as exc:
            raise SendError(f"[OUTCOME_UNKNOWN] 发送动作已开始，需核对后再操作：{exc}") from exc
        return result
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    """命令行验证通道：默认只填不发。退出码 0=OK / 2=没发（跳过）/ 1=错误。"""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="在聊天页里发一条消息（默认干跑）")
    ap.add_argument("needle", help="会话标识，通常是公司名（必须唯一匹配）")
    ap.add_argument("text", help="要发的内容")
    ap.add_argument("--live", action="store_true", help="真的点发送（默认只填不发）")
    ap.add_argument("--fill", action="store_true",
                    help="填好留在输入框里不清空，你自己看一眼再点发送（最稳）")
    args = ap.parse_args(argv)

    try:
        out = send(args.needle, args.text, dry_run=not args.live,
                   keep_text=args.fill)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        if out.get("kept"):
            print("\n👉 已填入聊天框，请到浏览器里看一眼，确认无误后自己点发送。")
        return 0 if out.get("ok") else 1
    except SendSkip as exc:
        print(f"跳过：{exc}")
        return 2
    except SendError as exc:
        print(f"失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
