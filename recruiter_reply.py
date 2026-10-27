"""Pure planning: replies cite approved facts, never send or access storage."""
TOPICS = {
    "location": ("地点", "地址", "哪里", "在哪", "办公"),
    "salary": ("工资", "薪资", "待遇", "多少钱", "薪酬"),
    "hours": ("时间", "班次", "几点", "双休", "加班"),
    "duties": ("职责", "做什么", "工作内容", "负责"),
    "process": ("流程", "面试", "简历", "下一步"),
}
SENSITIVE = ("录用", "offer", "歧视", "投诉", "身份证", "银行卡", "怀孕", "结婚", "保证", "谈薪", "涨薪")


def draft(message, facts):
    if any(word in message.lower() for word in SENSITIVE):
        return {"state": "needs_review", "text": "", "citations": [],
                "reason": "涉及承诺、隐私或敏感问题，请人工处理。"}
    topics = [key for key, words in TOPICS.items() if any(word in message for word in words)]
    missing = [key for key in topics if not str(facts.get(key) or "").strip()]
    if not topics or missing:
        return {"state": "needs_review", "text": "", "citations": [],
                "reason": "岗位资料不足或问题未覆盖，请核对后人工处理。", "needs": missing}
    return {"state": "drafted", "text": "\n".join(str(facts[key]).strip() for key in topics),
            "citations": topics, "reason": "仅使用已确认岗位资料；草稿尚未发送。"}
