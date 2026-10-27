"""HR 高频问题应答库。

为什么单独做一块
----------------
面试和 HR 沟通里，真正让人栽跟头的不是技术题，是这几句话：
「期望薪资多少」「什么时候能到岗」「为什么离职/空窗这么久」「能接受加班吗」。
答错一句，前面所有技术表现清零。

设计原则
--------
1. **每个问题给两版**：安全版（不出错）+ 进攻版（争取更好条件）。
   只给一版等于替用户做决定，而这两版适用的场景完全不同。
2. **给结构不给台词**：所有答案都留了占位/填空提示，逼用户填自己的事实。
   编好的台词背出来，追问两句就露馅（这条和面试题库同一个理由）。
3. **雷区单列**：说错话的代价远大于说得好听，先把雷排掉。
4. **薪资给算术**：不给「8K-10K 比较合适」这种废话，给「怎么根据目标倒推、
   怎么说区间、被压价时怎么接」。
"""
from __future__ import annotations

TOPICS = {
    "salary": {
        "q": "期望薪资多少？",
        "why": "这是最容易一句话亏几千的问题。HR 问这个有两个目的：筛掉超预算的、"
               "以及探你的底线。你先报数，就失去了信息优势。",
        "safe": "我了解到这个岗位的范围大概是 X-Y（可以问 HR：方便说一下这个岗位的预算范围吗）。"
                "我个人期望在 {target} 左右，具体看整体包——五险一金基数、几薪、"
                "加班和调薪机制，这些加起来差别比月薪大。",
        "aggressive": "我先问一下，这个岗位的预算区间是多少？（等对方先说）\n"
                      "如果对方坚持让你先说：我这边同时在聊的几家在 {target_high} 上下，"
                      "不过我更看重团队做的事——如果这边方向对，{target} 我也能接受。",
        "pitfalls": [
            "别说「按公司规定」「多少都行」——直接暴露你对自己没定价，后面必被压",
            "别报跨度太大的区间（7-15K），HR 只会记住下限 7",
            "第一次沟通别问加班费/年终奖细节，显得只关心钱",
            "别虚报上一份薪资——背景调查/社保记录能查出来",
        ],
        "fill": ["{target}=你的目标月薪（广州 7-9K 岗位建议报 8-9K）",
                 "{target_high}=比 target 高 1-2K 的数"],
    },
    "onboard": {
        "q": "什么时候能到岗？",
        "why": "HR 关心的是「这个人能不能马上来填坑」。拖太久会被跳过，"
               "但说「随时」会显得你没得选。",
        "safe": "随时可以到岗，我这边没有在职交接的问题。",
        "aggressive": "一周内可以到岗。如果团队急着用人，这周内我也能安排。",
        "pitfalls": [
            "别说「看你什么时候要」（把决定权推回去，显得不积极）",
            "别报「一个月」除非你真有在职工作要交接",
            "别为了显得抢手编「手上还有别的 offer」——被追问细节会崩",
        ],
        "fill": [],
    },
    "leave": {
        "q": "为什么离开上一份工作？/ 为什么毕业这么久没工作？",
        "why": "这题考的不是原因，是你**怎么讲原因**。抱怨前公司 = 直接淘汰，"
               "因为 HR 会想「你以后也会这么说我们」。",
        "safe": "我毕业后主要在补技术栈和做项目（具体见简历里的 XX 项目），"
                "现在方向明确了，想找一个能长期做下去的团队。",
        "aggressive": "我花了一段时间想清楚要做什么——期间做了 XX 项目、"
                      "把 XX 技术跑通了。现在我很确定方向是 XX，所以出来找对口的岗位。",
        "pitfalls": [
            "别说「找不到工作」「行情不好」——承认被动，气势直接垮",
            "别抱怨前公司/前领导（如果有）",
            "别说「想休息一段时间」「想提升自己」（等于没回答）",
            "别道歉——空窗不是错，解释清楚就行",
        ],
        "fill": ["XX 项目 = 你真实做过的项目名（灵枢 / AIGC 管线等）"],
        "ref": "gap",
    },
    "overtime": {
        "q": "能接受加班吗？",
        "why": "这是筛「能不能扛」的题，不是考你爱不爱加班。真正危险的不是加班，"
               "是「996 但没产出」的团队——所以回答里要留一个反向筛选的钩子。",
        "safe": "能配合项目节奏。我理解赶版本、上线前需要集中投入，这个我能接受。",
        "aggressive": "能。我做 XX 项目时为了上线连续加过班，这个不是问题。"
                      "不过我比较在意加班是不是因为排期不合理——如果是流程上的问题，"
                      "我愿意帮忙一起改。",
        "pitfalls": [
            "别直接问「有加班费吗」（第一次沟通就问钱，容易出局）",
            "别说「我不加班」（这个行业里等于自我淘汰）",
            "别撒谎说「我很喜欢加班」（假得刺眼）",
        ],
        "fill": ["XX 项目 = 你真加过班的项目"],
    },
    "why_us": {
        "q": "为什么选择我们公司？",
        "why": "考你有没有做功课。「公司发展好、平台大」这种答案等于没答，"
               "因为套在任何公司上都成立。",
        "safe": "我看了 JD 里提到的 XX 技术/业务，和我之前做的 XX 有重合，"
                "我想在这个方向上继续深入。",
        "aggressive": "我看了你们做的 XX（具体产品/业务），其中 XX 这块我正好做过类似的，"
                      "知道难点在哪。我想把这块经验用上，同时补上 XX（你缺的能力）。",
        "pitfalls": [
            "别说「平台大/发展好/离家近」——套话，谁都这么说",
            "别说「因为你们招人」（最差答案）",
            "别吹过头——说「我很崇拜贵司」会显得不真实",
        ],
        "fill": ["XX = 从 JD 或公司官网抄的具体业务/技术，必须是这家独有的"],
    },
    "questions": {
        "q": "你还有什么想问我们的？",
        "why": "说「没有了」= 你对这个岗位没兴趣。这题也是你唯一能反向筛选公司的机会。",
        "safe": "想了解一下这个岗位入职后前三个月主要做什么？团队现在几个人？",
        "aggressive": "三个问题：① 这个岗位现在最急要解决的问题是什么？"
                      "② 团队的技术栈和协作方式？③ 如果我进来，您希望我三个月内交付什么？",
        "pitfalls": [
            "别说「没有了」（最常见的自杀式回答）",
            "别第一轮就问薪资福利（等 HR 主动提）",
            "别问能在官网查到的信息（暴露你没做功课）",
        ],
        "fill": [],
    },
}

ORDER = ["salary", "onboard", "leave", "overtime", "why_us", "questions"]


def get(topic: str) -> dict:
    """取单个话题。未知话题抛 KeyError，不返回空 dict。"""
    return TOPICS[topic]


def all_topics() -> list[str]:
    return list(ORDER)


def render_one(topic: str, target: str = "8-9K", target_high: str = "10-11K") -> str:
    t = get(topic)
    lines = [f"【{t['q']}】", "", f"为什么难：{t['why']}", ""]
    lines.append(f"安全答法（不出错）：\n  {_fill(t['safe'], target, target_high)}")
    lines.append("")
    lines.append(f"进攻答法（争取条件）：\n  " +
                 _fill(t["aggressive"], target, target_high).replace("\n", "\n  "))
    if t.get("pitfalls"):
        lines.append("")
        lines.append("⚠ 雷区（说错代价最大）：")
        lines.extend(f"  · {p}" for p in t["pitfalls"])
    if t.get("fill"):
        lines.append("")
        lines.append("要填的地方：")
        for f in t["fill"]:
            k, _, v = f.partition("=")
            lines.append(f"  {k} → {v}")
    if t.get("ref"):
        lines.append("")
        lines.append(f"（展开版见：python jobkit.py {t['ref']}）")
    return "\n".join(lines)


def _fill(text: str, target: str, target_high: str) -> str:
    return (text.replace("{target_high}", target_high)
                .replace("{target}", target))


def render(target: str = "8-9K", target_high: str = "10-11K") -> str:
    out = ["HR 高频问题 · 应答库", "=" * 60, ""]
    for i, k in enumerate(ORDER, 1):
        t = TOPICS[k]
        out.append(f"{i}. {t['q']}")
    out.append("")
    out.append("看某一条：python jobkit.py hr --topic salary")
    out.append("（topic 可选：" + " / ".join(ORDER) + "）")
    out.append("")
    out.append("提示：所有答案都留了要填的地方——按你真实情况改，")
    out.append("      背现成台词在追问第二句就会崩。")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(render_one(sys.argv[1]))
    else:
        print(render())
