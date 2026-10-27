"""Pure applicant matching and greeting planning; no browser, network or storage."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re
from string import Formatter

import applicant_job_match
from sandbox import KernelError

PRESET = {
    'keywords': ['Agent', 'Agentic', '智能体', 'AI', '人工智能', '大模型', 'AIGC'],
    'primary_cities': ['广州', '深圳'], 'secondary_cities': ['东莞', '惠州', '佛山', '珠海'],
    'salary_min': 6000, 'daily_greetings': 150, 'daily_replies': 0, 'excluded_terms': [],
    'greeting_template': '您好，对贵司的{job_title}岗位感兴趣，方便进一步了解岗位职责和要求吗？',
}
TEMPLATE_FIELDS = frozenset({'job_title', 'company', 'city', 'education', 'experience', 'skills', 'project_summary'})
EDUCATION = {'高中': 1, '中专': 1, '大专': 2, '专科': 2, '本科': 3, '硕士': 4, '研究生': 4, '博士': 5}


def city(value):
    return value.strip().removesuffix('市')


def validate_plan(value):
    def invalid():
        raise KernelError('INVALID_APPLICANT_PLAN', '请核对岗位关键词、城市、薪资、数量和招呼语模板。', 400)
    if not isinstance(value, dict) or set(value) != set(PRESET):
        invalid()
    for key, minimum, maximum, width in [('keywords', 1, 12, 40), ('primary_cities', 1, 8, 20),
                                       ('secondary_cities', 0, 8, 20), ('excluded_terms', 0, 20, 60)]:
        items = value[key]
        if (not isinstance(items, list) or not minimum <= len(items) <= maximum
                or any(not isinstance(item, str) or not item.strip() or len(item) > width for item in items)):
            invalid()
        normalized = [city(item) if 'cities' in key else item.strip().casefold() for item in items]
        if len(set(normalized)) != len(items):
            invalid()
    if set(map(city, value['primary_cities'])) & set(map(city, value['secondary_cities'])):
        invalid()
    for key, lower, upper in [('salary_min', 0, 200000), ('daily_greetings', 1, 1000), ('daily_replies', 0, 100000)]:
        if type(value[key]) is not int or not lower <= value[key] <= upper:
            invalid()
    template = value['greeting_template']
    if not isinstance(template, str) or not template.strip() or len(template) > 500:
        invalid()
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None and (field not in TEMPLATE_FIELDS or spec or conversion):
                invalid()
    except ValueError:
        invalid()
    return value


def monthly_salary(text):
    """Recognize explicit monthly yuan or ordinary K ranges; no daily/yearly conversion."""
    value = text.strip().replace('－', '-').replace('—', '-').replace('–', '-')
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(?:\s*[-~]\s*(\d+(?:\.\d+)?))?\s*[Kk](?:\s*[·•]\s*(?:1\d|2[0-4])薪)?', value)
    multiplier = 1000
    if match is None:
        match = re.fullmatch(r'(\d+)(?:\s*-\s*(\d+))?\s*元/月', value)
        multiplier = 1
    if match is None:
        return None
    lower = Decimal(match[1]) * multiplier
    upper = Decimal(match[2] or match[1]) * multiplier
    if lower != int(lower) or upper != int(upper) or not 0 < lower <= upper <= 1000000:
        return None
    return int(lower), int(upper)


def years(text, *, requirement=False):
    value = text.strip()
    if value in {'应届', '应届生', '在校/应届', '无经验'} or (requirement and value in {'经验不限', '不限'}):
        return Decimal(0)
    expression = r'(\d+(?:\.\d+)?)(?:\s*[-~—–]\s*(\d+(?:\.\d+)?))?\s*年(以上|以内|以下)?' if requirement else r'(\d+(?:\.\d+)?)(?:年)?'
    match = re.fullmatch(expression, value)
    if requirement and match:
        if match[2] and Decimal(match[2]) < Decimal(match[1]):
            return None
        if match[3] in ('以内', '以下'):
            return Decimal(0) if match[2] is None else None
    return Decimal(match[1]) if match and Decimal(match[1]) <= 60 else None


JOB_REQUIRED = ('job_id', 'title', 'company', 'city', 'salary', 'education', 'experience')
JOB_LIMITS = {'title': 200, 'company': 160, 'city': 40, 'salary': 80, 'education': 40, 'experience': 40}
JOB_EVIDENCE = {'jd': applicant_job_match.MAX_JD, 'source': 300, 'observed_at': 64, 'id_source': 20, 'record_key': 128}
ID_SOURCES = frozenset({'platform', 'unknown'})
TZ = timezone(timedelta(hours=8))     # 观察时间统一按 Asia/Shanghai 表达


def id_source(job):
    """Where the job id came from; a missing id stays labelled 'unknown', never faked."""
    return 'platform' if job.get('job_id') else 'unknown'


def record_key(job):
    """Stable local key for de-duplication; the platform id when there is one."""
    if job.get('job_id'):
        return job['job_id']
    supplied = job.get('record_key')
    if isinstance(supplied, str) and re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}', supplied):
        return supplied
    digest = hashlib.sha256('|'.join(str(job.get(key) or '') for key in
                                     ('title', 'company', 'city', 'salary', 'jd')).encode('utf-8')).hexdigest()
    return 'local-' + digest[:32]     # 本地去重键，不是平台岗位标识


def record(job):
    """Canonical copy of one record; missing evidence fields default to honest blanks."""
    copied = {key: (job.get(key) if key == 'job_id' else str(job.get(key) or ''))
              for key in JOB_REQUIRED}
    copied['jd'] = job.get('jd') if isinstance(job.get('jd'), str) else ''
    source = job.get('source')
    copied['source'] = source if isinstance(source, str) and source.strip() else 'unknown'
    for key in ('observed_at', 'id_source', 'record_key'):
        if key in job:
            copied[key] = job[key]
    return copied


def observed_iso(value):
    """Epoch or ISO input -> ISO8601 at UTC+8; unknown stays None instead of a guess."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=TZ).isoformat(timespec='seconds')
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=TZ)
        return moment.astimezone(TZ).isoformat(timespec='seconds')
    return None


def _job_id_ok(job):
    value = job['job_id']
    if value is None:
        return job.get('id_source') == 'unknown'
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}', value) is not None


def _evidence_ok(job):
    for key, width in JOB_EVIDENCE.items():
        if key not in job:
            continue
        value = job[key]
        if key == 'observed_at':
            if value is None:
                continue
            if not (isinstance(value, str) or (isinstance(value, (int, float)) and not isinstance(value, bool))):
                return False
            if isinstance(value, str) and len(value) > width:
                return False
        elif key == 'id_source':
            if not isinstance(value, str) or value not in ID_SOURCES:
                return False
        elif key == 'record_key':
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_~\-]{1,128}', value):
                return False
        elif not isinstance(value, str) or len(value) > width:
            return False
    return True


def validate_jobs(jobs):
    if not isinstance(jobs, list) or len(jobs) > 50:
        raise KernelError('INVALID_JOB_FEED', '每次最多预检 50 条具有明确岗位标识的记录。', 400)
    seen = set()
    for job in jobs:
        if (not isinstance(job, dict) or not set(JOB_REQUIRED) <= set(job)
                or set(job) - set(JOB_REQUIRED) - set(JOB_EVIDENCE)
                or any(not isinstance(job[key], str) or len(job[key]) > width for key, width in JOB_LIMITS.items())
                or not _evidence_ok(job) or not _job_id_ok(job)
                or record_key(job) in seen):
            raise KernelError('INVALID_JOB_FEED', '岗位字段或唯一标识不完整，未生成发送候选。', 400)
        seen.add(record_key(job))


def preview(plan, profile, jobs):
    validate_plan(plan)
    validate_jobs(jobs)
    cities = [city(name) for name in plan['primary_cities'] + plan['secondary_cities']]
    results = []
    for job in jobs:
        missing = []
        gaps = []          # 待核对项的机器可读代码，与 needs 的人工说明一一对应
        title = job['title'].strip()
        # 方案第 119 行：标题与职责正文一起核对；JD 缺失只登记待核对，不硬判匹配。
        matched = applicant_job_match.assess(plan, profile, job)
        location = city(job['city'])
        salary = monthly_salary(job['salary'])
        rejected = []
        if not title or not job['company'].strip():
            missing.append('岗位名称或公司')
            gaps.append('record_incomplete')
        if not matched['matched_keywords']:
            rejected.append(applicant_job_match.KEYWORD_MISMATCH)
        if location not in cities:
            rejected.append('CITY_MISMATCH')
        haystack = ' '.join(filter(None, (title, job['company'], job.get('jd') or ''))).casefold()
        if any(term.strip().casefold() in haystack for term in plan['excluded_terms']):
            rejected.append('EXCLUDED_TERM')
        if salary is None:
            missing.append('可确认的月薪范围')
            gaps.append('salary_unknown')
        elif salary[1] < plan['salary_min']:
            rejected.append('SALARY_TOO_LOW')
        elif salary[0] < plan['salary_min']:
            missing.append('薪资下限是否达到要求')
            gaps.append('salary_floor_unverified')
        degree = job['education'].strip()
        if degree not in {'学历不限', '不限'}:
            required, actual = EDUCATION.get(degree), EDUCATION.get(profile.get('education', '').strip())
            if required is None or actual is None:
                missing.append('已确认学历或岗位学历要求')
                gaps.append('education_unknown')
            elif actual < required:
                rejected.append('EDUCATION_MISMATCH')
        required_years = years(job['experience'], requirement=True)
        actual_years = years(profile.get('experience', ''))
        if required_years is None or (required_years > 0 and actual_years is None):
            missing.append('已确认工作年限或岗位经验要求')
            gaps.append('experience_unknown')
        elif required_years > 0 and actual_years < required_years:
            rejected.append('EXPERIENCE_MISMATCH')
        review_codes = [code for code in matched['review_codes'] if code not in rejected]
        if applicant_job_match.JD_MISSING in review_codes:
            missing.append('岗位职责描述（JD）缺失，需人工核对')
        review_codes += [code for code in gaps if code not in review_codes and code not in rejected]
        greeting = ''
        if not rejected and not missing:
            values = {'job_title': title, 'company': job['company'].strip(), 'city': location,
                      **{key: profile.get(key, '').strip() for key in TEMPLATE_FIELDS - {'job_title', 'company', 'city'}}}
            fields = [field for _, field, _, _ in Formatter().parse(plan['greeting_template']) if field is not None]
            absent = [field for field in fields if not values[field]]
            if absent:
                missing.extend(absent)
                gaps.append('greeting_facts_missing')
            else:
                greeting = plan['greeting_template'].format_map(values).strip()
                if len(greeting) > 2000:
                    missing.append('招呼语过长，需人工调整')
                    gaps.append('greeting_too_long')
                    greeting = ''
        decision = 'rejected' if rejected else 'needs_review' if missing else 'eligible'
        stamp = job.get('observed_at')
        source = job.get('source')
        results.append({**job, 'job_id': job['job_id'], 'id_source': id_source(job),
            'record_key': record_key(job),
            'source': source if isinstance(source, str) and source.strip() else 'unknown',
            'observed_at': observed_iso(stamp),
            'observed_at_epoch': stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None,
            'decision': decision, 'reason_codes': rejected, 'review_codes': review_codes,
            'blocking_reason_code': (rejected + review_codes or [None])[0], 'needs': list(dict.fromkeys(missing)),
            'matched_keywords': matched['matched_keywords'], 'matched_in': matched['matched_in'],
            'skill_matches': matched['skill_matches'], 'jd_status': matched['jd_status'],
            'evidence': matched['evidence'],
            'city_priority': cities.index(location) if location in cities else None,
            'monthly_salary': list(salary) if salary else None, 'greeting': greeting,
            'platform_identity_verified': False, 'sent': False})
    results.sort(key=lambda job: ({'eligible': 0, 'needs_review': 1, 'rejected': 2}[job['decision']],
                                job['city_priority'] if job['city_priority'] is not None else 999,
                                job['job_id'] or job['record_key']))
    return {'candidates': results, 'counts': {state: sum(item['decision'] == state for item in results)
            for state in ('eligible', 'needs_review', 'rejected')}, 'automatic_execution': False,
            'ready_for_platform_execution': False, 'source_type': 'provided_job_records',
            'daily_greetings': plan['daily_greetings'], 'daily_replies': plan['daily_replies'],
            'jd_policy': 'jd_required_for_eligible'}
