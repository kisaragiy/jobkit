#!/usr/bin/env python3
"""绕过 patchright，用纯标准库 CDP WebSocket 抓 BOSS 登录态 → 写 session.enc。

为什么重写：
  boss-agent-cli 的 `login_via_cdp` 依赖 patchright 的 Node driver，在本机
  Edge 上稳定崩溃：
      BrowserType.connect_over_cdp: Connection closed while reading from the driver
      CRNetworkManager.setCacheDisabled / type:'closed'
  加 --disable-extensions 重启也无改善（driver 遍历 CDP session 时遇到异常 target 即崩）。
  但读 cookie 这件事本身只需要一个 CDP 命令 Storage.getCookies —— 用标准库
  socket 手搓 WebSocket 即可，零依赖、可控、不会崩。

写入格式与 boss-agent-cli 的 TokenStore 完全一致：
  session.enc = Fernet(urlsafe_b64(PBKDF2-HMAC-SHA256(machine_id, salt, 480000))) 加密的 JSON

用法：
    python boss_session.py            # 抓取并写入
    python boss_session.py --show     # 只看当前 session.enc 里有什么（不抓取）
    python boss_session.py --dry-run  # 只从浏览器读 cookie 并打印概况，不写入
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import struct
import subprocess
import sys
import time
from base64 import urlsafe_b64encode
from pathlib import Path
from urllib.parse import urlparse

import urllib.request

CDP_BASE = "http://localhost:9222"
AUTH_DIR = Path(r"C:\Users\<user>\.boss-agent\auth")
SESSION = AUTH_DIR / "session.enc"
SALT = AUTH_DIR / "salt"


# ══════════════════════════════════════════════════════════════
# 最小 WebSocket 客户端（RFC6455，仅客户端必需的部分）
# ══════════════════════════════════════════════════════════════
class WS:
    def __init__(self, url: str, timeout: float = 15.0):
        u = urlparse(url)
        host, port = u.hostname, u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        import socket
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败：连接被对方关闭")
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        self.buf = rest
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise RuntimeError(f"WebSocket 握手失败：{status}")

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        mask = os.urandom(4)
        n = len(data)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(hdr) + masked)

    def recv_frame(self) -> tuple[int, bytes]:
        b1 = self._read(1)[0]
        b2 = self._read(1)[0]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        mk = self._read(4) if masked else None
        payload = self._read(ln) if ln else b""
        if mk:
            payload = bytes(c ^ mk[i % 4] for i, c in enumerate(payload))
        return opcode, payload

    def call(self, method: str, params: dict | None = None, msg_id: int = 1,
             deadline: float = 20.0) -> dict:
        self.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        end = time.time() + deadline
        while time.time() < end:
            opcode, payload = self.recv_frame()
            if opcode == 0x9:  # ping → pong（0x8A）
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0x8:
                raise RuntimeError("CDP 主动关闭了 WebSocket")
            if opcode != 0x1:
                continue
            msg = json.loads(payload.decode("utf-8", "replace"))
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP 错误: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP 命令超时: {method}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════
# TokenStore 兼容的加密（复刻 boss_agent_cli/auth/token_store.py）
# ══════════════════════════════════════════════════════════════
def machine_id() -> str:
    if override := os.getenv("BOSS_AGENT_MACHINE_ID"):
        return override
    try:
        out = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout
        for line in out.splitlines():
            if "MachineGuid" in line:
                return line.split()[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    fp = "|".join([platform.node() or "unknown-node", platform.system() or "",
                   platform.machine() or "unknown-machine"])
    return hashlib.sha256(fp.encode()).hexdigest()


def derive_key(salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480000)
    return urlsafe_b64encode(kdf.derive(machine_id().encode()))


def read_salt(create: bool = False) -> bytes | None:
    if SALT.exists():
        return SALT.read_bytes()
    if not create:
        return None
    SALT.parent.mkdir(parents=True, exist_ok=True)
    s = os.urandom(16)
    SALT.write_bytes(s)
    return s


def save_token(token: dict) -> None:
    from cryptography.fernet import Fernet
    salt = read_salt(create=True)
    fernet = Fernet(derive_key(salt))
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    SESSION.write_bytes(fernet.encrypt(json.dumps(token, ensure_ascii=False).encode()))


def load_token() -> dict | None:
    from cryptography.fernet import Fernet, InvalidToken
    salt = read_salt()
    # 注意：salt 可能是空字节串 b""（历史上被清空过），空串是 falsy，
    # 不能写 `if not salt`，否则会把可解密的状态误判为不可读
    if salt is None or not SESSION.exists():
        return None
    try:
        return json.loads(Fernet(derive_key(salt)).decrypt(SESSION.read_bytes()))
    except (InvalidToken, ValueError, TypeError):
        return None


# ══════════════════════════════════════════════════════════════
def fetch_from_browser() -> dict:
    """从 CDP 浏览器读 zhipin cookie + UA。"""
    with urllib.request.urlopen(f"{CDP_BASE}/json/version", timeout=5) as r:
        ver = json.loads(r.read().decode())
    ws_url = ver.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("拿不到 webSocketDebuggerUrl（CDP 未就绪？）")

    ws = WS(ws_url)
    try:
        res = ws.call("Storage.getCookies", {}, msg_id=1)
        all_cookies = res.get("cookies", [])
        cookies = {c["name"]: c["value"] for c in all_cookies
                   if "zhipin" in (c.get("domain") or "")}
        try:
            ua = (ws.call("Browser.getVersion", {}, msg_id=2).get("userAgent") or "")
        except Exception:
            ua = ""
    finally:
        ws.close()

    return {
        "cookies": cookies,
        "stoken": cookies.get("__zp_stoken__", ""),
        "user_agent": ua,
        "_browser": ver.get("Browser", "?"),
        "_total_cookies": len(all_cookies),
    }


def save_from_cookie_list(cookie_list: list[dict], user_agent: str = "") -> dict:
    """从「cookie 对象列表」写入 session.enc。

    与 fetch_from_browser() 的区别：不自己去连 CDP，而是接收调用方已经读到的 cookie。
    Electron 壳子用 session.fromPartition('persist:boss').cookies.get() 读自己的分区，
    登录在壳子里完成 → 这份 token 直接供 CLI 投递使用，用户不用登录两次。
    """
    cookies: dict[str, str] = {}
    for c in cookie_list or []:
        name = (c or {}).get("name")
        dom = (c or {}).get("domain") or ""
        if not name or "zhipin" not in dom:
            continue
        cookies[name] = c.get("value", "")

    if not cookies.get("wt2"):
        raise ValueError(
            "没拿到 wt2 —— 壳子里还没有登录 BOSS 直聘。"
            "请先点「去登录」在右侧窗口完成登录，回到职位页后再同步。"
        )

    token = {
        "cookies": cookies,
        "stoken": cookies.get("__zp_stoken__", ""),
        "user_agent": user_agent or "",
    }
    if SESSION.exists():
        SESSION.with_suffix(".enc.bak").write_bytes(SESSION.read_bytes())
    save_token(token)
    return token


def main() -> int:
    ap = argparse.ArgumentParser(description="纯标准库 CDP 抓 BOSS 登录态")
    ap.add_argument("--show", action="store_true", help="只看当前 session.enc，不抓取")
    ap.add_argument("--dry-run", action="store_true", help="只读不写")
    args = ap.parse_args()

    if args.show:
        t = load_token()
        if not t:
            print("❌ 无法读取 session.enc（不存在或解密失败）")
            return 1
        ck = t.get("cookies", {})
        print(f"cookies: {len(ck)} 个")
        print("名单:", sorted(ck))
        for k in ("wt2", "__zp_stoken__", "zp_at", "wbg"):
            print(f"  {k:16s} {'✅ len=%d' % len(ck[k]) if ck.get(k) else '❌ 缺失'}")
        print("user_agent:", (t.get("user_agent") or "(空)")[:70])
        print("stoken:", "有" if t.get("stoken") else "无")
        return 0

    print("═" * 58)
    print("从 CDP 浏览器抓取 BOSS 登录态（纯标准库，不走 patchright）")
    print("═" * 58)

    try:
        data = fetch_from_browser()
    except Exception as e:
        print(f"❌ 抓取失败: {type(e).__name__}: {e}")
        return 2

    ck = data["cookies"]
    print(f"\n浏览器: {data['_browser']}")
    print(f"全部 cookie {data['_total_cookies']} 个，其中 zhipin {len(ck)} 个")
    print("名单:", sorted(ck)[:20])
    for k in ("wt2", "__zp_stoken__", "zp_at", "wbg"):
        print(f"  {k:16s} {'✅ len=%d' % len(ck[k]) if ck.get(k) else '❌ 缺失'}")

    if not ck.get("wt2"):
        print("\n❌ 没有 wt2 —— 浏览器里没有登录 BOSS 直聘")
        return 1
    if not data["stoken"]:
        print("\n⚠️ 有 wt2 但没有 __zp_stoken__（部分登录态）")
        print("   建议：在那个浏览器里打开 https://www.zhipin.com/ 逛一下，等页面跑完 JS 再重跑")

    if args.dry_run:
        print("\n（--dry-run，未写入）")
        return 0

    token = {"cookies": ck, "stoken": data["stoken"], "user_agent": data["user_agent"]}
    if SESSION.exists():
        bak = SESSION.with_suffix(".enc.bak")
        bak.write_bytes(SESSION.read_bytes())
        print(f"\n已备份旧 session → {bak.name}")
    save_token(token)
    print(f"✅ 已写入 {SESSION}")

    back = load_token()
    ok = bool(back and back.get("cookies", {}).get("wt2"))
    print(f"读回校验：{'✅ 通过（可解密，wt2 在）' if ok else '❌ 失败'}"
          f"  cookies={len(back.get('cookies', {})) if back else 0}"
          f"  stoken={'有' if back and back.get('stoken') else '无'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
