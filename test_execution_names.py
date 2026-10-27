"""内核集成层的名字对齐（不是 mock 层）。

2026-10-27 live 实测踩到：search.step 调 read_current_jobs() 时没传 execution，
函数内部默认走 'runtime.applicant.jobs.read'，与 capability 的工具名不匹配，
require_registered 抛 KERNEL_REQUIRED —— 而单测把 read_current_jobs 整个 mock 掉了，
所以从没暴露。这里锁死两条：名字必须对齐、失败必须显式。
"""
import inspect
import unittest

import runtime_tools


class ExecutionNameAlignmentTest(unittest.TestCase):
    def test_search_step_passes_its_own_execution_name(self):
        source = inspect.getsource(runtime_tools.applicant_search_step)
        self.assertIn("execution='runtime.applicant.jobs.search.step'", source,
                      'search.step 必须把自己的工具名传给 read_current_jobs，'
                      '否则会落到 jobs.read 上抛 KERNEL_REQUIRED')

    def test_jobs_detail_passes_its_own_execution_name(self):
        import applicant_browser
        signature = inspect.signature(applicant_browser.detail_open)
        self.assertEqual(signature.parameters['execution'].default,
                         'runtime.applicant.jobs.detail',
                         'detail_open 的默认执行名必须等于 jobs.detail 的注册名')

    def test_bulk_details_tool_cannot_call_itself_recursively(self):
        """批量补读按条调用，但不得调用自己的工具名（否则 capability 串层）。"""
        source = inspect.getsource(runtime_tools.applicant_search_details)
        self.assertIn("execution=DETAILS_TOOL", source)


if __name__ == '__main__':
    unittest.main()
