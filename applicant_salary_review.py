"""Pure validation of immutable, explicitly confirmed salary evidence."""
import hashlib
import json
import re
from sandbox import KernelError
import applicant_jobs

JOB_FIELDS = ('job_id', 'title', 'company', 'city', 'salary', 'education', 'experience')
BINDING_FIELDS = ('scope_id', 'job_observation_task_id', 'salary_observation_task_id', 'job',
                  'salary_text', 'monthly_salary', 'image_sha256', 'profile_revision', 'plan_revision')


def identity(binding):
    return hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def checked(document, scope_id):
    try:
        data = document['content']
        binding = {key: data[key] for key in BINDING_FIELDS}
        applicant_jobs.validate_jobs([binding['job']])
        if not isinstance(binding['salary_text'], str) or not 1 <= len(binding['salary_text']) <= 80:
            raise ValueError('invalid salary text')
        monthly = applicant_jobs.monthly_salary(binding['salary_text'])
        valid = (type(document['revision']) is int and document['revision'] == 1 and set(data) == set(BINDING_FIELDS) | {'confirmation_id', 'confirmed', 'sending_enabled'}
                 and data['scope_id'] == scope_id and data['confirmed'] is True and data['sending_enabled'] is False
                 and data['confirmation_id'] == identity(binding)
                 and monthly is not None and list(monthly) == data['monthly_salary']
                 and isinstance(data['monthly_salary'], list) and all(type(v) is int for v in data['monthly_salary'])
                 and all(type(data[k]) is int and data[k] > 0 for k in ('profile_revision', 'plan_revision'))
                 and all(isinstance(data[k], str) and re.fullmatch(r'[0-9a-f]{32}', data[k])
                         for k in ('job_observation_task_id', 'salary_observation_task_id'))
                 and isinstance(data['image_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', data['image_sha256']))
        if valid:
            return data
    except (TypeError, KeyError, ValueError, KernelError):
        pass
    raise KernelError('INVALID_SALARY_CONFIRMATION', '薪资确认记录无法核对，未用于匹配。')
