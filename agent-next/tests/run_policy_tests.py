

import sys, os, traceback
from tests import test_policy_validated_propagation as t

tests = [name for name in dir(t) if name.startswith("test_")]
passed, failed = 0, 0
for name in tests:
    fn = getattr(t, name)
    try:
        fn()
        print(f"PASS  {name}")
        passed += 1
    except AssertionError as e:
        print(f"FAIL  {name}  {e}")
        failed += 1
    except Exception:
        print(f"ERROR {name}")
        traceback.print_exc()
        failed += 1

print(f"\n{passed} passed, {failed} failed out of {len(tests)}")
sys.exit(1 if failed else 0)
