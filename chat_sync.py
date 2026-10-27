"""BOSS 聊天页会话同步：抓会话列表 → 分类 → 入库。

设计要点
--------
1. **只读**：只通过 CDP 的 Runtime.evaluate 读 DOM，不点按钮、不发消息、不改状态。
2. **分类**：把「对方最后说话」再细分，否则系统消息（「您的附件简历已发送给对方」）
   会冒充成「有新回复」，用户白高兴一场。分类顺序 = 拒信 > 面试 > 系统 > 普通。
3. **去重**：conv_key = hr|company。msg_hash 判断最后一条消息有没有变，
   变了才算「新消息」，用来做增量提醒。
4. **不可达要能说清**：浏览器没开 / 页面没登录 / DOM 改版，分别抛不同异常，
   不许静默返回空列表（空列表会被上层当成「今天没人理我」）。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request

import websocket

CHAT_URL = "https://www.zhipin.com/web/geek/chat"
CDP_PORT = 9222

# 分类顺序有意义：拒信里也可能出现「面试」字样，先判负面。
REJECT_PATTERNS = ("很遗憾", "不能与您共事", "不太合适", "暂不匹配",
                   "祝您找到", "不再考虑", "职位已关闭", "已招满")
INTERVIEW_PATTERNS = ("面试", "方便的时候", "聊聊", "沟通一下", "约个时间",
                      "打电话", "电话沟通", "视频面", "什么时候有空", "简历通过")
SYSTEM_PATTERNS = ("已发送给对方", "对方已同意", "附件简历", "已查看",
                   "已接收你的简历", "交换了联系方式", "请求交换")

# 光靠「面试」这个词会漏掉最常见的情况：HR 直接把问题甩过来，
# 比如「1. 展示一条你制作的代表性AIGC视频…2. 你如何排查…」。这类消息
# 用「多个问号 + 提问词」或「编号清单」来识别。
QUESTION_HINTS = ("你如何", "如何排查", "请讲", "讲清", "有哪些", "是否",
                  "能不能", "会不会", "介绍一下", "说说", "举例")
_ATTACHMENT_RE = re.compile(r"^[\w\u4e00-\u9fa5\-_.\s]{1,60}\.(pdf|docx?|jpe?g|png|zip)$",
                            re.I)


def _looks_like_questions(t: str) -> bool:
    marks = t.count("?") + t.count("？")
    if marks >= 2 and any(h in t for h in QUESTION_HINTS):
        return True
    # 「1. ... 2. ... 3. ...」这种编号清单，通常是 HR 列的面试题
    numbered = len(re.findall(r"(?:^|\s)\d{1,2}[.、]", t))
    return numbered >= 2 and marks >= 1


def classify(msg: str) -> str:
    """把一条消息归到 rejected / interview / system / resume_sent / normal / empty。

    判定顺序：拒信 → 明确的面试词 → 附件卡片 → 问句形态 → 系统词。
    拒信优先，因为「很遗憾，面试取消」这种也会命中面试词。
    """
    t = (msg or "").strip()
    if not t:
        return "empty"
    for p in REJECT_PATTERNS:
        if p in t:
            return "rejected"
    for p in INTERVIEW_PATTERNS:
        if p in t:
            return "interview"
    if _ATTACHMENT_RE.match(t):
        return "resume_sent"
    if _looks_like_questions(t):
        return "interview"
    for p in SYSTEM_PATTERNS:
        if p in t:
            return "system"
    return "normal"

GRAB_JS = r"""
(() => {
  const items = [...document.querySelectorAll('.user-list-content li[role="listitem"]')];
  return items.map(li => {
    const q = s => li.querySelector(s);
    const nameEl = q('.name-text');
    const spans = [...li.querySelectorAll('.name-box span')]
                    .map(x => x.textContent.trim()).filter(Boolean);
    const msgEl = q('.last-msg-text');
    const statusEl = q('.message-status');
    const timeEl = q('.time');
    const imgEl = q('img.image-circle');
    return {
      hr: nameEl ? nameEl.textContent.trim() : (spans[0] || ''),
      company: spans.length > 1 ? spans[1] : '',
      position: spans.length > 2 ? spans[2] : '',
      last_msg: msgEl ? msgEl.innerText.replace(/\s+/g, ' ').trim().slice(0, 500) : '',
      status: statusEl ? statusEl.textContent.trim() : '',
      time: timeEl ? timeEl.textContent.trim() : '',
      avatar: imgEl ? imgEl.getAttribute('src') : '',
    };
  });
})()
"""


class ChatUnavailable(RuntimeError):
    """聊天页不可用（浏览器没开 / 没登录 / DOM 改版）。"""


def conv_key(hr: str, company: str) -> str:
    return f"{(hr or '').strip()}|{(company or '').strip()}"


def msg_hash(msg: str) -> str:
    return hashlib.sha1((msg or "").encode("utf-8")).hexdigest()[:16]


# ---------- CDP ----------

def _cdp_target():
    try:
        raw = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list",
                                     timeout=5).read()
        targets = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise ChatUnavailable(f"连不上浏览器 CDP({CDP_PORT})：{exc}") from exc
    page = next((t for t in targets if t.get("type") == "page"
                 and "/web/geek/chat" in (t.get("url") or "")), None)
    if page is None:
        page = next((t for t in targets if t.get("type") == "page"
                     and "zhipin.com" in (t.get("url") or "")), None)
    if page is None:
        raise ChatUnavailable("没有找到 zhipin 标签页")
    return page


def _call(ws, mid, method, **params):
    mid[0] += 1
    ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid[0]:
            return m.get("result", {})


def _eval(ws, mid, expr):
    r = _call(ws, mid, "Runtime.evaluate",
              expression=expr, returnByValue=True)
    return r.get("result", {}).get("value")


def fetch_conversations(url: str = CHAT_URL, wait: float = 25.0) -> list[dict]:
    """导航到聊天页并抓取全部会话。DOM 没就绪时轮询等待，超时报 ChatUnavailable。"""
    page = _cdp_target()
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60,
                                     suppress_origin=True)
    mid = [0]
    try:
        _call(ws, mid, "Page.enable")
        if "/web/geek/chat" not in (page.get("url") or ""):
            _call(ws, mid, "Page.navigate", url=url)
        elif url != CHAT_URL:
            _call(ws, mid, "Page.navigate", url=url)

        deadline = time.time() + wait
        convs = []
        while time.time() < deadline:
            time.sleep(1.5)
            if _eval(ws, mid, "!!document.querySelector('.user-list-content')") is not True:
                continue
            convs = _eval(ws, mid, GRAB_JS) or []
            if convs:
                break
        if not convs:
            logged_out = _eval(ws, mid,
                               "!!document.querySelector('.login-btn, .sign-in')")
            if logged_out:
                raise ChatUnavailable("聊天页未登录")
            raise ChatUnavailable(
                f"{int(wait)} 秒内没抓到会话（可能页面加载慢或 DOM 改版）")
        return convs
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def normalize(convs: list[dict]) -> list[dict]:
    """补上派生字段：conv_key / category / from_me / is_pending / hash / hr_read。"""
    out = []
    for c in convs:
        msg = c.get("last_msg") or ""
        status = (c.get("status") or "").strip()
        from_me = bool(status)
        out.append({
            **c,
            "conv_key": conv_key(c.get("hr", ""), c.get("company", "")),
            "from_me": from_me,
            "pending": not from_me,          # 对方最后说话 = 待处理
            "category": classify(msg),
            "hash": msg_hash(msg),
            # 对方是否已读我的消息：DOM 里是 `<i class="message-status">[已读]</i>`
            "hr_read": 1 if "已读" in status else 0,
        })
    return out


def summarize(rows: list[dict]) -> dict:
    """给界面/CLI 用的统计。"""
    pending = [r for r in rows if r["pending"]]
    by_cat: dict[str, int] = {}
    for r in pending:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    return {
        "total": len(rows),
        "pending": len(pending),
        "by_category": by_cat,
        "interview": [r for r in pending if r["category"] == "interview"],
        "rejected": [r for r in pending if r["category"] == "rejected"],
        "system": [r for r in pending if r["category"] == "system"],
        "resume_sent": [r for r in pending if r["category"] == "resume_sent"],
        "normal": [r for r in pending if r["category"] == "normal"],
    }


def sync(now: float | None = None) -> dict:
    """抓一次聊天页并写库。返回 stats + summary + 原始行。

    抓不到时抛 ChatUnavailable——上层必须区分「今天没人理我」和「我没抓到」。
    """
    import store

    rows = normalize(fetch_conversations())
    c = store.connect()
    try:
        stats = store.upsert_conversations(c, rows, now=now)
    finally:
        c.close()
    return {**stats, "summary": summarize(rows), "rows": rows}


if __name__ == "__main__":
    rows = normalize(fetch_conversations())
    s = summarize(rows)
    print(f"会话 {s['total']} 条，待处理 {s['pending']} 条 {s['by_category']}")
    for label, key in (("面试/邀约", "interview"), ("拒信", "rejected"),
                       ("附件/简历卡片", "resume_sent"), ("系统消息", "system"),
                       ("普通消息", "normal")):
        items = s[key]
        if not items:
            continue
        print(f"\n=== {label} ({len(items)}) ===")
        for r in items:
            print(f"  [{r['time']}] {r['hr']} / {r['company']} / {r['position']}")
            print(f"      {r['last_msg'][:90]}")
