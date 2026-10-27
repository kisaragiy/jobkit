"""多进程并发探针的回归：额度占位在真实子进程竞争下不超限、不丢额度。

真跑子进程（不是 mock），因为要证的就是「两个进程各自读到 used=N 会双双放行」这件事。
参数取小值以控制 CI 时长：2 进程 × 2 次，上限 3 → 必须恰好 3 成功、1 被拒。
"""
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent


class ConcurrencyProbeTest(unittest.TestCase):
    def run_probe(self, *args):
        return subprocess.run([sys.executable, 'scripts/concurrency_probe.py', *args],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=240)

    def test_parallel_reservations_never_exceed_limit(self):
        done = self.run_probe('--workers', '2', '--per-worker', '2', '--limit', '3')
        self.assertEqual(done.returncode, 0, f'{done.stdout[-1200:]}\n{done.stderr[-800:]}')
        payload = [json.loads(line) for line in done.stdout.splitlines()
                   if line.strip().startswith('{')]
        verdict = [p for p in payload if p.get('step') == 'concurrency']
        self.assertEqual(len(verdict), 1, done.stdout[-800:])
        result = verdict[0]
        self.assertEqual(result['ok'], 3)                 # 不多不少
        self.assertEqual(result['requested'], 4)
        self.assertEqual(result['limited'], 1)
        self.assertEqual(result['locked'], 0)             # 没有 database is locked
        self.assertEqual(result['stored']['attempts'], 3)  # 落库不双计
        self.assertEqual(result['stored']['reserved'], 3)
        self.assertEqual(result['verdict'], 'ok')

    def test_rejects_absurd_parameters(self):
        done = self.run_probe('--workers', '99')
        self.assertEqual(done.returncode, 1)              # ERROR，不是 ALERT


if __name__ == '__main__':
    unittest.main()
