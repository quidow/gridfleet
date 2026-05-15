"""Smoke test for the diff parser used by the Ollama review workflow.

Run with `python .github/scripts/test_parse_diff.py`. Exits non-zero on
failure; intended to be quick enough to run in pre-commit without pytest.
"""

from __future__ import annotations

from ollama_review import parse_diff_positions

SAMPLE = """\
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 keep
-removed
+added one
+added two
 tail
"""


def main() -> None:
    eligible = parse_diff_positions(SAMPLE)
    assert "foo.py" in eligible, eligible
    # Lines 2 and 3 are the two '+' lines; line 1 ("keep") and 4 ("tail") are context.
    assert eligible["foo.py"] == {2, 3}, eligible["foo.py"]
    print("ok")


if __name__ == "__main__":
    main()
