"""Which lines a diff changed - the input to everything `review` reports."""
from codeorbit.review import _changed_lines, changed_ranges, signature_change

DIFF = """diff --git a/pkg/utils.py b/pkg/utils.py
index 4d648bc..ae10a6a 100644
--- a/pkg/utils.py
+++ b/pkg/utils.py
@@ -404,7 +404,7 @@ def unquote_unreserved(uri):
     return ''.join(parts)


-def requote_uri(uri):
+def requote_uri(uri, safe):
     \"\"\"Re-quote the given URI.

     This function passes the given URI through an unquote/quote cycle to
@@ -500,6 +500,8 @@ def other():
     a = 1
     b = 2
     c = 3
+    d = 4
+    e = 5
     return a


@@ -600,7 +602,6 @@ def third():
     x = 1
     y = 2
     z = 3
-    w = 4
     return x


diff --git a/pkg/gone.py b/pkg/gone.py
deleted file mode 100644
--- a/pkg/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def f():
-    pass
diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,2 @@
+def g():
+    pass
"""


def test_only_added_and_removed_lines_count_not_context():
    ranges = changed_ranges(DIFF)
    # The def line moved from 407 to 407; the three context lines above it
    # (404-406, the end of the previous function) must not be included.
    assert ranges["pkg/utils.py"][0] == (407, 407)


def test_additions_are_numbered_on_the_new_side():
    assert (503, 504) in changed_ranges(DIFF)["pkg/utils.py"]


def test_a_removed_line_is_pinned_to_the_line_above_it():
    # After the earlier hunk added two lines, `z = 3` sits at 604 in the new file.
    assert (604, 604) in changed_ranges(DIFF)["pkg/utils.py"]


def test_deleted_files_have_nothing_on_the_new_side_and_new_files_do():
    ranges = changed_ranges(DIFF)
    assert "pkg/gone.py" not in ranges
    assert ranges["pkg/new.py"] == [(1, 2)]


def test_a_changed_definition_line_is_a_signature_change():
    lines = _changed_lines(DIFF)["pkg/utils.py"]
    assert signature_change(lines, "requote_uri") == (
        "def requote_uri(uri):", "def requote_uri(uri, safe):")


def test_body_edits_and_other_names_are_not_signature_changes():
    lines = _changed_lines(DIFF)["pkg/utils.py"]
    assert signature_change(lines, "other") is None      # only its body changed
    assert signature_change(lines, "uri") is None        # a parameter, not a def


def test_brace_language_definitions_are_recognised():
    lines = {"-": ["public int total(int a) {"], "+": ["public int total(int a, int b) {"]}
    assert signature_change(lines, "total")
    calls = {"-": ["x = total(1);"], "+": ["x = total(1, 2);"]}
    assert signature_change(calls, "total") is None      # a call, not a definition
