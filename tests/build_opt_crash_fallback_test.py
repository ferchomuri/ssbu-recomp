"""Clang crash → whole-file -O0 fallback in tools/lifter/build_opt.py (no real clang).

    python tests/build_opt_crash_fallback_test.py

Reproduces the Nobara / Clang 19 failure mode: the driver exits 1 while the log says the frontend
failed with exit code 134 (and often starts with std::bad_alloc under prlimit). The previous check
only matched returncode 134 / -SIGABRT, so the -O0 fallback never ran.
"""
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
LIFTER = os.path.join(HERE, '..', 'tools', 'lifter')

CRASH_OUT = (
    "terminate called after throwing an instance of 'std::bad_alloc'\n"
    "  what():  std::bad_alloc\n"
    + ("#0 0xdeadbeef in somewhere\n" * 80)
    + "clang: error: clang frontend command failed with exit code 134 (use -v to see invocation)\n"
    "PLEASE ATTACH THE FOLLOWING FILES TO THE BUG REPORT:\n"
)


def main():
    os.environ['RECOMP_TARGET'] = 'linux'
    sys.modules['build'] = types.ModuleType('build')
    sys.modules['build_nro'] = types.ModuleType('build_nro')
    sys.path.insert(0, LIFTER)
    import build_opt as B

    bad = []
    # Detection helpers (full log vs truncated tail as kept for FAILED lines)
    if not B.clang_crashed(1, CRASH_OUT[-2000:]):
        bad.append('clang_crashed should match driver exit 1 + frontend 134 trailer')
    if B.clang_crashed(0, CRASH_OUT):
        bad.append('clang_crashed must ignore exit 0')
    if B.clang_crashed(1, 'error: unknown type name \'foo\'\n'):
        bad.append('clang_crashed must ignore ordinary compile errors')
    # Truncated tail alone often loses bad_alloc (the bug we hit in production)
    B.MEM_LIMIT = 8 << 30
    if not B.out_of_memory(1, CRASH_OUT):
        bad.append('out_of_memory should see bad_alloc in the full log')
    if B.out_of_memory(1, CRASH_OUT[-2000:]) and 'bad_alloc' not in CRASH_OUT[-2000:]:
        bad.append('unexpected: truncated tail still has bad_alloc')
    if not B.clang_crashed(1, CRASH_OUT[-2000:]):
        bad.append('truncated crash trailer must still be recoverable via clang_crashed')

    # compile_file: fake -O1 crash (exit 1 + trailer, no bad_alloc left after our OOM path is bypassed)
    # by making run_limited return the crash the way a non-OOM abort looks after classification.
    calls = []

    def fake_run_limited(cmd, limit, mem=0):
        calls.append(('opt', cmd[cmd.index('-c') + 1] if '-c' in cmd else '?'))
        return 1, CRASH_OUT[-2000:], 0.05

    class Done:
        def __init__(self, code=0):
            self.returncode = code
            self.stdout = ''
            self.stderr = ''

    def fake_run(cmd, **kw):
        calls.append(('O0', cmd[cmd.index('-c') + 1] if '-c' in cmd else '?'))
        open(cmd[-1], 'w').close()
        return Done(0)

    B.MEM_BUDGET = 0          # no reservations: exercise crash→O0, not the OOM raise path
    B.MEM_LIMIT = 0
    B.run_limited = fake_run_limited
    B.subprocess = types.SimpleNamespace(run=fake_run, Popen=subprocess.Popen, TimeoutExpired=subprocess.TimeoutExpired)

    d = tempfile.mkdtemp()
    src = os.path.join(d, 'c_crash.c')
    obj = os.path.join(d, 'c_crash.obj')
    with open(src, 'w') as f:
        f.write('void f0(Ctx *c) {\n' + 'x;\n' * 25000 + '}\n')
    code, out, secs, note = B.compile_file(src, obj, [])
    if code != 0:
        bad.append('compile_file should recover: %r' % ((code, note, out[:200]),))
    if not any(c[0] == 'O0' for c in calls):
        bad.append('expected a whole-file -O0 attempt, calls=%r' % calls)
    if 'WHOLE FILE at -O0' not in note or 'crashed' not in note.lower():
        bad.append('note should describe crash→O0: %r' % note)
    if not os.path.isfile(obj):
        bad.append('object not written')

    # Ordinary non-zero exit must NOT fall back to -O0
    calls.clear()

    def fake_err(cmd, limit, mem=0):
        calls.append(('opt', 'err'))
        return 1, 'error: unknown type name \'nope\'\n', 0.01

    B.run_limited = fake_err
    src2 = os.path.join(d, 'c_err.c')
    obj2 = os.path.join(d, 'c_err.obj')
    with open(src2, 'w') as f:
        f.write('void f0(Ctx *c) {\nx;\n}\n')
    code2, _, _, note2 = B.compile_file(src2, obj2, [])
    if code2 == 0 or any(c[0] == 'O0' for c in calls):
        bad.append('ordinary errors must not fall back to -O0: %r %r' % (code2, calls))
    if 'WHOLE FILE' in (note2 or ''):
        bad.append('ordinary error note should not be WHOLE FILE: %r' % note2)

    import shutil
    shutil.rmtree(d, ignore_errors=True)
    print('crash_fallback %s' % ('ok' if not bad else 'FAILED: ' + '; '.join(bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
