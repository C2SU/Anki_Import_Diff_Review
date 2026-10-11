import subprocess
import sys

# Get list of tests from pytest --collect-only
res = subprocess.run(
    [sys.executable, "-m", "pytest", "test_import_diff.py", "--collect-only", "-q"],
    capture_output=True,
    text=True
)

tests = []
for line in res.stdout.splitlines():
    if "test_import_diff.py::" in line:
        tests.append(line.strip())

print(f"Collected {len(tests)} tests from test_import_diff.py")

failures = []
for test in tests:
    print(f"Running {test}...", end="", flush=True)
    # Run test in a separate python process
    p = subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "--tb=short"],
        capture_output=True,
        text=True
    )
    if p.returncode == 0:
        print(" PASSED")
    else:
        print(" FAILED")
        print(p.stdout)
        print(p.stderr)
        failures.append(test)

if failures:
    print(f"\n{len(failures)} tests failed:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("\nAll tests passed successfully!")
    sys.exit(0)
