#!/usr/bin/env python3
"""外部渠道供岗导入 —— 把 agent 定时搜索到的岗位喂进 jobkit 台账。

为什么存在
----------
BOSS 通道被风控卡死时，岗位池只能靠外部渠道（猎聘/智联/51job/公司官网等）
续命。每天晚上由 cron 触发 agent 用搜索工具搜一批 AI 岗，把原始结果
dump 成 JSON（见下方 RAW 格式），本脚本负责解析/清洗/去重/落库。

与 BOSS 收集器的关系
---------------------
- 不碰 collector / dom_channel 的任何逻辑（外部来源边界见 docs/ARCHITECTURE.md）
- 落库复用 store.upsert_job（platform='web'，job_id=md5(url)）
- 外部岗没有 security_id，投递管线会天然跳过它们，它们只作为候选清单存在
- 每条岗位带 url，用户可点开原文

输入 RAW 格式（agent 每晚 dump 到 data/external_feed_raw.json）
----------------------------------------------------------------
{
  "feed_time": "2026-10-21T22:30:00",
  "queries": [
    {"query": "AI应用开发 招聘 广州", "city_hint": "广州",
     "results": [
       {"title": "...", "host": "智联招聘", "url": "https://...",
        "snippet": "...", "publish_time": "..."}
     ]}
  ]
}

用法
----
    python feed_external.py                # 读默认 data/external_feed_raw.json 并落库
    python feed_external.py --in x.json    # 指定输入
    python feed_external.py --dry-run       # 只解析统计，不写库
    python feed_external.py --dump-json     # 打印解析后的 job 结构（调试用）

测试：python -m unittest test_feed_external
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import store  # noqa: E402

DEFAULT_IN = HERE / "data" / "external_feed_raw.json"
SOURCE_TAG = "external_feed"

# ══════════════════════════════════════════════════════════
# 岗位名关键词表 —— 与 run_collect.py 的 12 个关键词对齐
# 匹配顺序按长度降序，取最长命中（避免「AI应用开发」先吞掉「AI Agent开发」）
# ══════════════════════════════════════════════════════════
JOB_NAMES = [
    "大模型应用开发工程师", "大模型应用开发", "AI应用后台开发工程师",
    "AI应用开发副总监", "AI应用开发工程师", "AI应用开发实习生", "AI应用开发",
    "AI Agent开发工程师", "AI Agent研发工程师", "AI agent开发工程师",
    "AI Agent开发", "AI agent开发", "智能体开发工程师", "智能体开发",
    "LLM应用开发工程师", "LLM应用开发", "RAG开发工程师", "RAG开发",
    "AI后端开发工程师", "Python后端开发工程师", "Python后端",
    "AIGC应用开发工程师", "AIGC应用开发", "LangChain开发工程师",
    "AI 全栈开发工程师", "AI全栈开发工程师", "大模型算法工程师",
    "算法工程师", "AI开发工程师", "AI 应用开发", "Python开发工程师",
]
_JOB_NAMES_BY_LEN = sorted(JOB_NAMES, key=len, reverse=True)
_JOB_NAME_PAT = re.compile("|".join(re.escape(n) for n in _JOB_NAMES_BY_LEN))

# ══════════════════════════════════════════════════════════
# 字段提取正则
# ══════════════════════════════════════════════════════════
CITIES = ("广州", "深圳", "东莞", "佛山", "惠州", "珠海")
_CITY_PAT = re.compile("|".join(CITIES))
# 只认带行政区后缀的区名（天河区/松山湖园/东坑镇），避免把标题垃圾当区名
_DISTRICT_PAT = re.compile(
    r"(广州|深圳|东莞|佛山|惠州|珠海)\s*[-—–]\s*([\u4e00-\u9fff]{2,4}(?:区|镇|县|园|街道|新区))"
)
_SALARY_PAT = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-~至—–—]\s*(\d+(?:\.\d+)?)\s*([Kk万]|k/月)"
    r"|(\d+(?:\.\d+)?)\s*[-~至—–—]\s*(\d+(?:\.\d+)?)\s*元/月"
)
# 经验正则：必须排除 19xx/20xx 年份（「2026年」不是经验）
_EXP_PAT = re.compile(
    r"(经验不限|在校生/应届生|在校生|应届生|应届|"
    r"\d+(?:\.\d+)?\s*[-~—–]\s*\d+\s*年|"
    r"(?<!\d)(?![12]\d{3})\d+\s*年(?:以上|及以上|以内|以下)?)"
)
_EDU_PAT = re.compile(r"(本科及以上|统招本科|本科|硕士|博士|学历不限|大专)")
_SKILL_WORDS = (
    "Python", "Java", "LangChain", "RAG", "Agent", "智能体", "大模型", "LLM",
    "Dify", "vLLM", "Docker", "Kubernetes", "ComfyUI", "FastAPI", "Flask",
    "Django", "MySQL", "Redis", "Milvus", "Node", "React", "Vue", "MCP",
)
_SKILL_PAT = re.compile("|".join(sorted(_SKILL_WORDS, key=len, reverse=True)))

# 平台 host → platform 归一化
_HOST_MAP = {
    "猎聘": "猎聘", "liepin": "猎聘",
    "智联招聘": "智联", "zhaopin": "智联",
    "前程无忧": "51job", "51job": "51job",
    "BOSS直聘": "BOSS", "zhipin": "BOSS",
}
_DEFAULT_PLATFORM = "web"


def _find_longest_job_name(text: str) -> str:
    """在文本里找最长的岗位名命中；找不到返回 ''。

    注意 re.search 是最左匹配不是最长匹配（"AI应用开发工程师（大模型…）"
    会先命中更靠左的短词），所以按长度降序遍历候选，返回第一个命中。
    """
    for name in _JOB_NAMES_BY_LEN:
        if re.search(name, text or ""):
            return name
    return ""


def clean_title(raw: str) -> str:
    """从平台拼接标题里抠出岗位名。

    优先级：① 关键词表最长命中 → ② 通用清理兜底。
    通用清理：去【】外壳、去「_公司招聘」后缀、去「- 平台名」尾巴、
    去「- 城市/区」尾巴、去括号编号。
    """
    if not raw:
        return ""
    hit = _find_longest_job_name(raw)
    if hit:
        return hit
    t = raw
    t = re.sub(r"【([^】]*)】", r"\1", t)
    t = re.split(r"_招聘|招聘_|招聘 -|招聘-", t)[0]
    t = re.sub(r"\s*[-—–]\s*(智联招聘|前程无忧|猎聘|51job|BOSS直聘).*$", "", t)
    t = re.sub(r"[-—–]\s*(广州|深圳|东莞|佛山|惠州|珠海).*$", "", t)
    t = re.sub(r"[\(（][^\)）]*[\)）]", "", t)
    t = re.sub(r"(招聘网|招聘信息)", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -—–【】")
    return t


def extract_company(text: str) -> str:
    """从 title/snippet 里提公司名；提取不到返回 ''。"""
    if not text:
        return ""
    # 「XXX有限公司招聘」 / 「XXX科技有限公司招聘」
    m = re.search(r"([\u4e00-\u9fff]{2,}(?:有限公司|科技|网络|软件|信息|智能|数据|数字|互联网)[^\s，。;；、|]*?)(?:招聘|招才|诚聘)", text)
    if m:
        return m.group(1).strip()
    # 「某中型互联网公司」「某自动化设备公司」
    m = re.search(r"(某[\u4e00-\u9fff]{2,}公司)", text)
    if m:
        return m.group(1)
    return ""


def extract_salary(text: str) -> str:
    """提取薪资区间，保留原文格式；找不到返回 ''。"""
    if not text:
        return ""
    m = _SALARY_PAT.search(text)
    if not m:
        return ""
    if m.group(1) and m.group(3):
        return f"{m.group(1)}-{m.group(2)}{m.group(3)}"
    if m.group(4) and m.group(5):
        return f"{m.group(4)}-{m.group(5)}元/月"
    return ""


def extract_city(text: str, city_hint: str = "") -> tuple[str, str]:
    """返回 (city, district)。snippet 里的「广州-天河区」优先，其次 city_hint。"""
    if not text:
        return city_hint or "", ""
    m = _DISTRICT_PAT.search(text)
    if m:
        return m.group(1), m.group(2)
    m = _CITY_PAT.search(text)
    if m:
        return m.group(0), ""
    return city_hint or "", ""


def extract_experience(text: str) -> str:
    m = _EXP_PAT.search(text or "")
    return m.group(0) if m else ""


def extract_education(text: str) -> str:
    m = _EDU_PAT.search(text or "")
    return m.group(0) if m else ""


def extract_skills(text: str) -> str:
    if not text:
        return ""
    hits = []
    for w in _SKILL_WORDS:
        if re.search(w, text, flags=re.IGNORECASE):
            hits.append(w)
    # 保持技能表顺序，去重
    seen, out = set(), []
    for w in _SKILL_WORDS:
        if w in hits and w not in seen:
            seen.add(w)
            out.append(w)
    return ",".join(out)


def platform_from_host(host: str) -> str:
    if not host:
        return _DEFAULT_PLATFORM
    for k, v in _HOST_MAP.items():
        if k.lower() in host.lower():
            return v
    return _DEFAULT_PLATFORM


def job_id_from_url(url: str) -> str:
    """外部岗没有 BOSS job_id，用 url 的 md5 前 32 位做稳定主键。"""
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:32]


def parse_record(rec: dict, city_hint: str = "") -> dict | None:
    """把一条搜索结果解析成 jobs 表字段 dict；无 url 返回 None。"""
    url = (rec.get("url") or "").strip()
    if not url:
        return None
    title = (rec.get("title") or "").strip()
    snippet = (rec.get("snippet") or "").strip()
    full = f"{title}\n{snippet}"
    clean = clean_title(title)
    city, district = extract_city(full, city_hint)
    return {
        "job_id": job_id_from_url(url),
        "security_id": "",                       # 外部岗无 BOSS 凭证，投递跳过
        "title": clean or title[:60],
        "company": extract_company(full),
        "salary": extract_salary(full),
        "city": city,
        "district": district,
        "experience": extract_experience(full),
        "education": extract_education(full),
        "skills": extract_skills(full),
        "industry": "",
        "scale": "",
        "boss_name": "",
        "boss_title": "",
        "boss_active": "",
        "match_score": None,
        "platform": platform_from_host(rec.get("host") or ""),
        "url": url,
        "job_description": snippet[:2000],
    }


def load_raw(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"找不到输入文件: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"输入 JSON 解析失败: {e}") from e
    if not isinstance(data.get("queries"), list):
        raise ValueError("RAW 格式错误：顶层需有 queries 数组，见脚本头注释")
    return data


def parse_all(data: dict) -> tuple[list[dict], list[str]]:
    """解析全部记录，返回 (jobs, 失败原因列表)。"""
    jobs, fails = [], []
    for q in data.get("queries") or []:
        hint = q.get("city_hint") or ""
        for rec in q.get("results") or []:
            job = parse_record(rec, hint)
            if job is None:
                fails.append(f"无 url: {str(rec.get('title'))[:40]}")
            else:
                jobs.append(job)
    return jobs, fails


def dedupe_against_db(c: sqlite3.Connection, jobs: list[dict]) -> tuple[list[dict], int]:
    """跟库内已存在的 url（job_id）比对，剔除重复。返回 (新增候选, 跳过数)。"""
    out, skipped = [], 0
    existing = {r["job_id"] for r in c.execute("SELECT job_id FROM jobs")}
    for j in jobs:
        if j["job_id"] in existing:
            skipped += 1
        else:
            out.append(j)
    return out, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="外部渠道供岗导入（agent 搜索 dump → jobkit 台账）")
    ap.add_argument("--in", dest="infile", default=str(DEFAULT_IN), help="输入 RAW JSON 路径")
    ap.add_argument("--dry-run", action="store_true", help="只解析统计，不写库")
    ap.add_argument("--dump-json", action="store_true", help="打印解析结果 JSON 后退出")
    a = ap.parse_args()

    try:
        data = load_raw(Path(a.infile))
    except (FileNotFoundError, ValueError) as e:
        print(f"✗ {e}")
        return 1

    jobs, fails = parse_all(data)
    if a.dump_json:
        print(json.dumps(jobs, ensure_ascii=False, indent=2))
        return 0

    if not a.dry_run:
        c = store.connect()
        try:
            jobs, skipped = dedupe_against_db(c, jobs)
        finally:
            c.close()
    else:
        skipped = 0

    new = 0
    if not a.dry_run:
        c = store.connect()
        try:
            now = __import__("time").time()
            for j in jobs:
                if store.upsert_job(c, j, SOURCE_TAG, now):
                    new += 1
            c.commit()
        finally:
            c.close()
    else:
        new = len(jobs)

    by_city: dict[str, int] = {}
    for j in jobs:
        by_city[j["city"] or "未知"] = by_city.get(j["city"] or "未知", 0) + 1
    city_txt = " / ".join(f"{k} {v}" for k, v in sorted(by_city.items(), key=lambda x: -x[1]))

    print(f"✓ 输入 {len(jobs) + skipped} 条，解析成功 {len(jobs)} 条，失败 {len(fails)} 条")
    if skipped:
        print(f"· 跳过已在库 {skipped} 条（url 重复）")
    print(f"· 本次{'(dry-run 未写库) ' if a.dry_run else '新增入库 '}{new} 条 | 城市分布：{city_txt or '无'}")
    if fails:
        print("· 失败明细（前 5）：")
        for f in fails[:5]:
            print(f"    {f}")
    if not a.dry_run and new:
        print("· 新岗位示例：")
        for j in jobs[:5]:
            print(f"    [{j['platform']}] {j['title']} | {j['company'] or '公司未知'} | {j['salary'] or '薪资未知'} | {j['city'] or '城市未知'} | {j['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
