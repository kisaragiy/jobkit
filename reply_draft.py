"""按 HR 的实际问法生成一句可直接回的草稿。

只回答明确覆盖且有本人确认依据的问题；不支持的问题转人工。
当前不会把未覆盖问题静默交给模型，也不自动承诺面试、录用或薪资让步。

**铁律：不知道就不说。** 城市、在职状态、到岗时间这类事实必须来自
`reply_profile.json`（或用户当场填）；缺了就返回 needs 让用户决定，
绝不替用户拍板 —— 一句「我在深圳」编错了，面试当场就穿帮。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROFILE_PATH = Path(__file__).with_name("reply_profile.json")

PROFILE_FIELDS = {
    "city": "你现在在哪个城市（例：深圳）",
    "current_status": "在职状态（离职 / 在职 / 应届）",
    "available": "最快到岗（例：随时 / 一周内）",
    "salary_target": "期望薪资（例：8-9K）",
    "onsite_availability": "已确认的现场面试安排（例：周三下午可到场）",
    "education": "已确认的最高学历（例：本科）",
    "experience": "已确认的工作年限（例：1年；应届填写应届）",
    "skills": "已确认的技术与技能",
    "project_summary": "允许向招聘者介绍的项目经历",
}

DEFAULT_PROFILE = {k: "" for k in PROFILE_FIELDS}


def _city(p: dict) -> str:
    return str(p.get("city") or "").strip()


def _status_phrase(p: dict) -> str:
    s = str(p.get("current_status") or "").strip()
    return f"目前是{s}状态"


def _available_phrase(p: dict) -> str:
    a = str(p.get("available") or "").strip()
    if a in ("随时", "随时可以", "立即"):
        return "随时可以到岗"
    return f"{a}可以到岗"


# 每条：(id, 触发词, 需要的事实, 生成短语, 人话说明)
PATTERNS = [
    ("city", ("在深圳", "深圳吗", "在深圳么", "人在深圳", "在深圳对吧", "哪个城市", "现在在哪", "目前在哪",
              "人在哪", "居住城市", "住哪里", "住在哪", "目前所在地", "在东莞", "在惠州", "在佛山", "在珠海"),
     ("city",), lambda p: f"我在{_city(p)}", "回答居住城市"),
    ("city_gz", ("在广州", "广州吗", "在广州么"),
     ("city",), lambda p: f"我在{_city(p)}", "回答居住城市"),
    ("onsite", ("现场面试", "过来面试", "能来面试", "当面聊", "到公司面试", "面对面"),
     ("onsite_availability",), lambda p: f"现场面试安排：{str(p['onsite_availability']).strip()}",
     "只说明已确认的面试安排"),
    ("status", ("离职", "在职", "还在上班", "已经不看机会", "目前状态", "就业状态", "现在还在上班"),
     ("current_status",), _status_phrase, "回答在职状态"),
    ("available", ("什么时候能到岗", "多久能到岗", "到岗时间", "什么时候入职", "几时可以入职", "什么时候可以到岗", "何时能到岗", "何时到岗", "什么时候到岗", "几时到岗", "入职时间"),
     ("available",), _available_phrase, "回答到岗时间"),
    ("salary", ("期望薪资", "薪资要求", "期望多少", "多少钱", "待遇要求", "预算", "期望工资", "薪资期望", "预期薪资", "薪酬预期"),
     ("salary_target",), lambda p: f"期望薪资：{str(p.get('salary_target') or '').strip()}",
     "回答薪资"),
    ("resume", ("发份简历", "发简历", "简历发我", "简历给我", "发一份简历", "发一下简历", "提供简历", "简历方便发"),
     (), lambda p: "我确认简历版本后再回复您", "确认简历版本"),
    ("outsource", ("外派", "内网开发", "驻场"),
     (), lambda p: "想先确认一下这次驻场的地点和周期，还有项目的性质，"
                  "了解清楚之后我再答复您是否合适",
     "外派：只问限制，不替用户表「能接受」的态"),
    ("consider", ("考虑吗", "有兴趣吗", "方便聊聊吗", "是否考虑"),
     (), lambda p: "想先了解下岗位的具体职责和技术栈",
     "询问岗位信息"),
    ("education", ("最高学历", "什么学历", "你的学历", "学历情况", "学历是"),
     ("education",), lambda p: f"已确认学历：{p['education'].strip()}", "回答本人确认学历"),
    ("experience", ("工作年限", "几年经验", "多少年经验", "多少年工作", "工作几年", "工作经验多久"),
     ("experience",), lambda p: f"已确认工作年限：{p['experience'].strip()}", "回答本人确认工作年限"),
    ("skills", ("熟悉哪些技术", "掌握哪些技术", "擅长什么技术", "你的技术栈", "技术栈是什么", "主要技术栈"),
     ("skills",), lambda p: f"技术与技能：{p['skills'].strip()}", "回答本人确认技能"),
    ("projects", ("做过什么项目", "介绍下项目", "介绍一下项目", "介绍项目经历", "项目经历介绍", "项目经验介绍"),
     ("project_summary",), lambda p: f"项目经历：{p['project_summary'].strip()}", "只使用允许介绍的项目内容"),
]

MANUAL_TERMS = ('身份证', '银行卡', '验证码', '押金', '培训费', '征信', '人脸', '婚育', '怀孕', '是否已婚',
    '当前薪资', '目前薪资', '现在工资', '现在的工资', '目前工资', '上一份薪资', '现薪', '工资流水', '离职原因', '为什么离职', '最低薪资', '可以降薪',
    'offer', '签合同', '背调', '客户名单', '源代码', '保密项目', '什么专业', '哪个专业', '毕业时间',
    '毕业院校', '全日制', '英语', '英文', '粤语', '日语', '驾照', '证书', '性别', '年龄', '具体住址', '哪个区')

# 我主动收尾（不用回，是我自己说的）
MY_CLOSING = ("不适合我", "不太适合我", "祝您早日找到", "祝早日找到合适")
# HR 拒我（也不用回，但原因完全不同——别混着说成「我自己婉拒过」）
HR_REJECT = ("很遗憾不能与您共事", "找到更匹配的工作机会", "暂不合适", "岗位已招满")
SYSTEM_HINTS = ("附件简历已发送", "对方已同意", "已查看", "简历已投递",
                "已发送给对方", "您的简历")
SYSTEM_SUFFIXES = (".pdf", ".doc", ".docx")


def load_profile(path=None) -> dict:
    p = Path(path or PROFILE_PATH)
    if not p.exists():
        return dict(DEFAULT_PROFILE)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {**DEFAULT_PROFILE, **(data if isinstance(data, dict) else {})}
    except (OSError, ValueError):
        return dict(DEFAULT_PROFILE)


def save_profile(profile: dict, path=None) -> None:
    Path(path or PROFILE_PATH).write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def detect(msg: str) -> list[tuple]:
    """命中哪些问法（一条消息里可能同时问两三件事）。"""
    text = str(msg or "")
    hits = []
    for pid, keys, needs, say, note in PATTERNS:
        if any(k in text for k in keys):
            hits.append((pid, needs, say, note))
    # city 和 city_gz 是同一个信息的两个问法，只留一个
    ids = [h[0] for h in hits]
    if "city" in ids and "city_gz" in ids:
        hits = [h for h in hits if h[0] != "city_gz"]
    # 外派那条已经说了「了解清楚再答复」，再来一句「我有兴趣」就自相矛盾
    if "outsource" in ids and "consider" in ids:
        hits = [h for h in hits if h[0] != "consider"]
    # 去重：同一个 needs 只问一次
    seen, out = set(), []
    for h in hits:
        key = h[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def draft(msg: str, profile: dict | None = None) -> dict:
    """返回 {text, needs, hits, skip, reason}。

    needs 非空 = 缺事实，**不给文本**（调用方负责让用户填，不许编）。
    """
    text = str(msg or "").strip()
    if not text:
        return {"text": "", "needs": [], "hits": [], "skip": True, "reason": "消息为空"}
    if any(h in text for h in MY_CLOSING):
        return {"text": "", "needs": [], "hits": [], "skip": True, "reason": "我自己已经婉拒过"}
    if any(h in text for h in HR_REJECT):
        return {"text": "", "needs": [], "hits": [], "skip": True,
                "reason": "HR 已婉拒，这条线结束了"}
    if any(h in text for h in SYSTEM_HINTS) and not detect(text):
        return {"text": "", "needs": [], "hits": [], "skip": True, "reason": "系统消息，不用回"}
    # HR 回头发来一个简历文件名（.pdf）也是系统回执，不是提问
    if text.lower().endswith(SYSTEM_SUFFIXES):
        return {"text": "", "needs": [], "hits": [], "skip": True,
                "reason": "HR 回执/附件名，不是提问"}

    p = dict(DEFAULT_PROFILE, **(profile or {}))
    if any(term in text.casefold() for term in MANUAL_TERMS):
        return {"text": "", "needs": [], "hits": [], "skip": False,
                "reason": "涉及隐私、承诺、专项条件或未确认信息，请人工核对。"}
    hits = detect(text)
    if not hits:
        return {"text": "", "needs": [], "hits": [], "skip": False,
                "reason": "没认出来问的是什么，先手动看原文"}

    clauses = re.split(r'(?<=[，,；;。?？\n])', text)
    if any(re.search(r'[?？]|吗|么|多少|什么|哪[里个些]|多久|能否|是否|提供|介绍|说明', part)
           and not detect(part) for part in clauses if part.strip()):
        return {"text": "", "needs": [], "hits": [h[0] for h in hits], "skip": False,
                "reason": "消息还包含未覆盖的问题，请人工完整答复。"}

    needs = []
    for _pid, need_fields, _say, _note in hits:
        for f in need_fields:
            if not str(p.get(f) or "").strip() and f not in needs:
                needs.append(f)
    if needs:
        return {"text": "", "needs": needs, "hits": [h[0] for h in hits], "skip": False,
                "reason": "缺事实，先补 " + "、".join(PROFILE_FIELDS.get(n, n) for n in needs)}

    phrases = []
    for _pid, _needs, say, _note in hits:
        try:
            s = say(p).strip()
        except Exception:
            return {"text": "", "needs": [], "hits": [h[0] for h in hits], "skip": False,
                    "reason": "回复模板处理失败，请人工处理"}
        if s and s not in phrases:
            phrases.append(s)
    body = "，".join(phrases)
    text_out = body + "。" if body else ""
    if len(text_out) > 4000:
        return {"text": "", "needs": [], "hits": [h[0] for h in hits], "skip": False,
                "reason": "合并答复过长，请人工拆分核对后回复。"}
    return {"text": text_out, "needs": [], "hits": [h[0] for h in hits], "skip": False,
            "reason": ""}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按 HR 的问法生成一句回复草稿")
    ap.add_argument("message", nargs="?", default="", help="HR 的消息原文")
    ap.add_argument("--init", action="store_true", help="生成 reply_profile.json 模板")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.init:
        save_profile(load_profile())
        print(f"✅ 已生成 {PROFILE_PATH}")
        for k, desc in PROFILE_FIELDS.items():
            print(f"   {k}: {desc}")
        return 0

    if not args.message:
        ap.print_help()
        return 1

    res = draft(args.message, load_profile())
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    elif res["skip"]:
        print(f"⏭  不用回：{res['reason']}")
    elif res["needs"]:
        print(f"⚠️  {res['reason']}")
        print(f"   跑 python reply_draft.py --init 然后把这几项填上：")
        for n in res["needs"]:
            print(f"     · {n} — {PROFILE_FIELDS.get(n, '')}")
    else:
        print(res["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
