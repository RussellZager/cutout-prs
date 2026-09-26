#!/usr/bin/env python3
"""Run the conformance suite against one server.

  python3 tests/run_conformance.py --target python     # reference server
  python3 tests/run_conformance.py --target supabase   # edge function
  python3 tests/run_conformance.py --target python -k receipt

The python target needs only Python 3.9+. The supabase target also needs
deno, initdb, pg_ctl and psql on PATH; it runs supabase/index.ts against a
throwaway local PostgreSQL cluster (no Docker, nothing hosted).

Tests marked @known_bug fail today because of a bug that an open fix
removes. They count as expected failures. When a fix lands, its tests
report "unexpected success" and the run fails until the marker is removed.

Exit codes: 0 passed; 1 failures, unexpected successes, or a known bug that
failed for a reason other than an assertion; 2 the target could not start
(missing tool, server crash) or no tests were collected. A target is never
skipped.
"""

import argparse
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "conformance")


def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _label(test):
    return "%s.%s" % (type(test).__name__, test._testMethodName)


def _known_bug(test):
    fn = getattr(type(test), test._testMethodName, None)
    return getattr(fn, "known_bug", None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", choices=["python", "supabase"],
                    default="python")
    ap.add_argument("-k", dest="patterns", action="append", default=None,
                    help="only run tests whose name contains this substring")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    os.environ["CUTOUT_TARGET"] = args.target
    sys.dont_write_bytecode = True  # keep tests/conformance clean
    sys.path.insert(0, CONF)
    import harness  # noqa: E402

    loader = unittest.TestLoader()
    if args.patterns:
        loader.testNamePatterns = ["*%s*" % p for p in args.patterns]
    suite = loader.discover(CONF, pattern="test_*.py", top_level_dir=CONF)
    tests = list(_iter_tests(suite))
    broken = [t for t in tests if not hasattr(t, "_testMethodName")
              or type(t).__name__ in ("_FailedTest", "ModuleImportFailure")]
    if broken:
        print("IMPORT_ERROR: %s" % broken, file=sys.stderr)
        return 2
    if not tests:
        print("NO_TESTS_COLLECTED (patterns=%r)" % args.patterns,
              file=sys.stderr)
        return 2

    try:
        target = harness.get_target()
    except (harness.HarnessError, harness.PgUnavailable, OSError,
            RuntimeError) as exc:
        print("HARNESS_ERROR target=%s: %s" % (args.target, exc),
              file=sys.stderr)
        return 2
    print("conformance target=%s base_url=%s tests=%d"
          % (target.name, target.base_url, len(tests)), flush=True)

    result = unittest.TextTestRunner(
        verbosity=1 if args.quiet else 2).run(suite)

    # A known bug must fail on an assertion. A crash, timeout or harness
    # error would otherwise hide behind the expectedFailure marker.
    wrong_reason = [(t, tb) for t, tb in result.expectedFailures
                    if not any(line.startswith("AssertionError")
                               for line in tb.splitlines())]
    print("\nknown bugs (expected failures): %d"
          % len(result.expectedFailures))
    for t, _ in result.expectedFailures:
        print("  %-60s %s" % (_label(t), (_known_bug(t) or ("", "?"))[1]))
    for t in result.unexpectedSuccesses:
        print("UNEXPECTED_SUCCESS %s: the fix for %s is in this tree; "
              "remove its @known_bug marker"
              % (_label(t), (_known_bug(t) or ("", "?"))[1]))
    for t, tb in wrong_reason:
        print("KNOWN_BUG_WRONG_REASON %s:\n%s" % (_label(t), tb))
    ok = result.wasSuccessful() and not wrong_reason
    print("RESULT target=%s run=%d failures=%d errors=%d expected_failures=%d"
          " unexpected_successes=%d wrong_reason=%d -> %s"
          % (args.target, result.testsRun, len(result.failures),
             len(result.errors), len(result.expectedFailures),
             len(result.unexpectedSuccesses), len(wrong_reason),
             "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
