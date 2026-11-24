"""The detection rules `codeorbit audit` runs.

Deliberately regex-over-source rather than AST-matched. Two reasons: a rule
stays one readable line so the table below IS the documentation, and a rule can
see things the graph does not model (a string literal's shape, a keyword
argument's value). The graph's job comes after: it says which symbol a hit
lives in and how much code depends on it, which is what turns a list of hits
into a ranked list of risks.

Every rule carries `why` - what actually goes wrong - because a finding a
reader cannot act on is noise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .langs import family

HIGH, MEDIUM, LOW = "high", "medium", "low"


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    why: str
    pattern: re.Pattern
    langs: tuple[str, ...]          # language FAMILIES, or ANY
    skip_tests: bool = False        # don't report inside test files


def _r(pat: str) -> re.Pattern:
    return re.compile(pat)


# Families, not language ids. A rule written for "javascript" has to fire on
# TypeScript, on a Vue component and on a `.jsx` too - they are the same
# language with different endings, and before this the audit silently skipped
# every one of them because `rules_for("typescript")` matched nothing.
ANY = ("*",)
PY = ("py",)
JS = ("js",)
BOTH = ("py", "js")
GO = ("go",)
JAVA = ("java", "kotlin", "scala")
RUBY = ("rb",)
PHP = ("php",)
C = ("c",)
RUST = ("rust",)

RULES: list[Rule] = [
    # ---------------- code execution ----------------
    Rule("py-eval", HIGH, "eval() on runtime data",
         "Any attacker-influenced string becomes code. Use ast.literal_eval or a parser.",
         _r(r"(?<![\w.])eval\s*\("), PY),
    Rule("py-exec", HIGH, "exec() on runtime data",
         "Same as eval: arbitrary code execution. Restructure so code is not built at runtime.",
         _r(r"(?<![\w.])exec\s*\("), PY),
    Rule("py-os-system", HIGH, "os.system() shell call",
         "Runs through a shell, so input containing ; or | executes extra commands. "
         "Use subprocess.run([...]) with a list.",
         _r(r"os\.system\s*\("), PY),
    Rule("py-shell-true", HIGH, "subprocess with shell=True",
         "Reintroduces shell metacharacter injection. Pass a list of arguments instead.",
         _r(r"shell\s*=\s*True"), PY),
    Rule("js-eval", HIGH, "eval() on runtime data",
         "Arbitrary code execution. Use JSON.parse for data.",
         _r(r"(?<![\w.])eval\s*\("), JS),
    Rule("js-child-exec", HIGH, "child_process.exec()",
         "Runs a shell string. Use execFile/spawn with an argument array.",
         _r(r"child_process\s*\.\s*exec\s*\(|(?<![\w.])exec\s*\(\s*[`\"']"), JS),
    Rule("js-new-function", HIGH, "new Function() from a string",
         "Compiles a string as code, the same risk as eval.",
         _r(r"new\s+Function\s*\("), JS),

    # ---------------- deserialization ----------------
    Rule("py-pickle", HIGH, "pickle load of untrusted data",
         "Unpickling executes arbitrary code by design. Use JSON for anything crossing a trust boundary.",
         _r(r"pickle\s*\.\s*loads?\s*\("), PY),
    Rule("py-yaml-load", HIGH, "yaml.load() without a safe loader",
         "The default loader can construct arbitrary Python objects. Use yaml.safe_load.",
         _r(r"yaml\s*\.\s*load\s*\((?![^)]*Safe)"), PY),

    # ---------------- injection ----------------
    Rule("py-sql-fstring", HIGH, "SQL built by string interpolation",
         "Concatenated SQL is injectable. Pass parameters with ? or %s placeholders instead.",
         _r(r"(?:execute|executemany)\s*\(\s*(?:f[\"']|[\"'][^\"']*[\"']\s*%|[\"'][^\"']*[\"']\s*\+)"), PY),
    Rule("js-sql-template", HIGH, "SQL built by template literal",
         "Interpolated SQL is injectable. Use parameterised queries.",
         _r(r"\.\s*query\s*\(\s*`[^`]*\$\{"), JS),
    Rule("js-innerhtml", MEDIUM, "assignment to innerHTML",
         "Writes unescaped markup, so any user text becomes DOM. Use textContent, or sanitise.",
         _r(r"\.\s*innerHTML\s*="), JS),
    Rule("js-dangerously", MEDIUM, "dangerouslySetInnerHTML",
         "Bypasses React's escaping. Sanitise the HTML before it reaches this prop.",
         _r(r"dangerouslySetInnerHTML"), JS),
    Rule("js-doc-write", MEDIUM, "document.write()",
         "Injects unescaped markup and blocks parsing. Build nodes instead.",
         _r(r"document\s*\.\s*write\s*\("), JS),

    # ---------------- secrets ----------------
    Rule("secret-assign", HIGH, "credential assigned in source",
         "A secret in the repository is in its history forever. Read it from the environment "
         "and rotate this one.",
         _r(r"(?i)\b(?:password|passwd|secret|api_?key|access_?token|auth_?token|private_?key)"
            r"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"), BOTH, skip_tests=True),
    Rule("secret-aws", HIGH, "AWS access key id in source",
         "A live AWS key. Revoke it and load credentials from the environment or a profile.",
         _r(r"\bAKIA[0-9A-Z]{16}\b"), BOTH),
    Rule("secret-pem", HIGH, "private key block in source",
         "A private key committed to the repository. Remove it and rotate the key.",
         _r(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), BOTH),

    # ---------------- weak crypto / transport ----------------
    Rule("py-verify-false", HIGH, "TLS verification disabled",
         "verify=False accepts any certificate, so the connection can be intercepted.",
         _r(r"verify\s*=\s*False"), PY),
    Rule("js-tls-reject", HIGH, "TLS verification disabled",
         "rejectUnauthorized:false accepts any certificate.",
         _r(r"rejectUnauthorized\s*:\s*false"), JS),
    Rule("py-weak-hash", MEDIUM, "MD5/SHA1 used for hashing",
         "Both are broken for security use. Use SHA-256, or bcrypt/argon2 for passwords.",
         _r(r"hashlib\s*\.\s*(?:md5|sha1)\s*\("), PY),
    Rule("py-weak-random", MEDIUM, "random module used for a secret",
         "random is predictable from observed output. Use the secrets module for "
         "tokens, passwords, salts and nonces.",
         _r(r"(?i)\b(?:token|secret|password|passwd|salt|nonce|key|otp)\w*\s*=\s*random\s*\."),
         PY),
    Rule("py-mktemp", MEDIUM, "tempfile.mktemp() race",
         "The name can be claimed between the call and the open. Use NamedTemporaryFile or mkstemp.",
         _r(r"tempfile\s*\.\s*mktemp\s*\("), PY),

    # ---------------- error handling ----------------
    Rule("py-bare-except", MEDIUM, "bare except swallows everything",
         "Catches KeyboardInterrupt and SystemExit too, and hides real failures. "
         "Catch the exception you expect.",
         _r(r"except\s*:"), PY),
    Rule("py-except-pass", MEDIUM, "exception silently discarded",
         "A failure here leaves no trace. Log it, or let it propagate.",
         _r(r"except[^:]*:\s*(?:#.*)?\n\s*pass\b"), PY),
    Rule("py-assert-check", MEDIUM, "assert used as a runtime check",
         "python -O strips asserts, so this validation vanishes in optimised runs. Raise instead.",
         _r(r"^\s*assert\s+"), PY, skip_tests=True),
    Rule("js-empty-catch", MEDIUM, "empty catch block",
         "A failure here leaves no trace. Log it, or rethrow.",
         _r(r"catch\s*\([^)]*\)\s*\{\s*\}"), JS),

    # ---------------- leftovers ----------------
    Rule("py-debugger", HIGH, "debugger breakpoint left in code",
         "Halts the process in production. Remove it.",
         _r(r"(?<![\w.])(?:breakpoint\s*\(|pdb\s*\.\s*set_trace\s*\()"), PY),
    Rule("js-debugger", HIGH, "debugger statement left in code",
         "Halts execution when devtools are open. Remove it.",
         _r(r"(?<![\w.])debugger\s*;?\s*$"), JS),
    Rule("todo", LOW, "unresolved TODO/FIXME",
         "A known gap recorded in a comment. Track it, or fix it.",
         _r(r"(?://|#|--)\s*(?:TODO|FIXME|XXX|HACK)\b"), ANY),

    # ---------------- Go ----------------
    Rule("go-ignored-error", MEDIUM, "error assigned to _",
         "The failure is discarded and the code carries on with a zero value. "
         "Handle it, or say why in a comment.",
         _r(r"(?m)^\s*_\s*(?:,\s*\w+\s*)?[:=]=[^=\n]*\berr(?:or)?\b|,\s*_\s*:?=\s*\w+\("), GO),
    Rule("go-exec-command", HIGH, "exec.Command with a shell",
         "sh -c runs a shell string, so input containing ; or | executes extra "
         "commands. Pass the program and its arguments directly.",
         _r(r"exec\.Command(?:Context)?\s*\(\s*[\"'`](?:/bin/)?(?:ba|z|k)?sh[\"'`]"), GO),
    Rule("go-sql-sprintf", HIGH, "SQL built with Sprintf",
         "Interpolated SQL is injectable. Use placeholders and pass arguments.",
         _r(r"(?:Query|Exec|QueryRow)\w*\s*\(\s*(?:\w+\s*,\s*)?fmt\.Sprintf\s*\("), GO),
    Rule("go-insecure-tls", HIGH, "TLS verification disabled",
         "InsecureSkipVerify accepts any certificate, so the connection can be "
         "intercepted.",
         _r(r"InsecureSkipVerify\s*:\s*true"), GO),
    Rule("go-panic", MEDIUM, "panic() in library code",
         "A panic takes the whole process down. Return an error and let the "
         "caller decide.",
         _r(r"(?<![\w.])panic\s*\("), GO, skip_tests=True),

    # ---------------- JVM ----------------
    Rule("jvm-runtime-exec", HIGH, "Runtime.exec() on a built string",
         "Runs a command assembled at runtime. Use ProcessBuilder with an "
         "argument list.",
         _r(r"Runtime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*exec\s*\("), JAVA),
    Rule("jvm-sql-concat", HIGH, "SQL built by concatenation",
         "Concatenated SQL is injectable. Use a PreparedStatement with "
         "placeholders.",
         _r(r"(?:executeQuery|executeUpdate|execute|createQuery)\s*\(\s*[\"'][^\"']*[\"']\s*\+"),
         JAVA),
    Rule("jvm-deserialize", HIGH, "ObjectInputStream.readObject()",
         "Java deserialization of untrusted bytes executes code. Use a data "
         "format that does not construct arbitrary types.",
         _r(r"ObjectInputStream\s*\([^)]*\)[\s\S]{0,80}?readObject\s*\(|\.readObject\s*\(\s*\)"),
         JAVA),
    Rule("jvm-empty-catch", MEDIUM, "empty catch block",
         "A failure here leaves no trace. Log it, or rethrow.",
         _r(r"catch\s*\([^)]*\)\s*\{\s*\}"), JAVA),
    Rule("jvm-printstacktrace", LOW, "printStackTrace() as error handling",
         "Writes to stderr and continues as if nothing happened. Log it "
         "through the application's logger, or rethrow.",
         _r(r"\.printStackTrace\s*\(\s*\)"), JAVA),

    # ---------------- Ruby ----------------
    Rule("rb-eval", HIGH, "eval / instance_eval on runtime data",
         "Any attacker-influenced string becomes code.",
         _r(r"(?<![\w.])(?:eval|instance_eval|class_eval|module_eval)\s*[\(\s][\"'#]"), RUBY),
    Rule("rb-backtick", HIGH, "shell command in backticks or %x",
         "Runs through a shell, so interpolated input can execute extra "
         "commands. Use system with an argument list.",
         _r(r"`[^`\n]*#\{|%x\s*[\(\[\{][^\n]*#\{"), RUBY),
    Rule("rb-sql-interp", HIGH, "SQL built by interpolation",
         "Interpolated SQL is injectable. Use parameter binding: "
         "where(\"a = ?\", x).",
         # `[^"]*` rather than `[^"']*`: the interpolation is nearly always
         # inside quotes of the other kind - where("name = '#{term}'") - and
         # excluding both quote characters made the rule miss the common case.
         _r(r"(?:where|find_by_sql|execute|order|group)\s*\(\s*"
            r"(?:\"[^\"]*#\{|'[^']*#\{)"), RUBY),
    Rule("rb-yaml-load", HIGH, "YAML.load of untrusted data",
         "The default loader constructs arbitrary Ruby objects. Use "
         "YAML.safe_load.",
         _r(r"YAML\s*\.\s*load\s*\("), RUBY),
    Rule("rb-rescue-nil", MEDIUM, "exception silently swallowed",
         "A failure here leaves no trace. Log it, or let it propagate.",
         _r(r"rescue\s*(?:=>\s*\w+\s*)?$\s*\n\s*(?:nil|end)\b"), RUBY),

    # ---------------- PHP ----------------
    Rule("php-eval", HIGH, "eval() on runtime data",
         "Arbitrary code execution.",
         _r(r"(?<![\w.])eval\s*\("), PHP),
    Rule("php-shell", HIGH, "shell execution on a built string",
         "system/exec/shell_exec run through a shell. Use escapeshellarg, or "
         "restructure so no shell is involved.",
         _r(r"(?<![\w.])(?:system|shell_exec|passthru|popen|proc_open)\s*\("), PHP),
    Rule("php-sql-interp", HIGH, "SQL built by interpolation",
         "Interpolated SQL is injectable. Use a prepared statement.",
         _r(r"(?:query|exec|prepare)\s*\(\s*[\"'][^\"']*\$"), PHP),
    Rule("php-unserialize", HIGH, "unserialize() of untrusted data",
         "Constructs arbitrary objects and can trigger magic methods. Use "
         "json_decode.",
         _r(r"(?<![\w.])unserialize\s*\("), PHP),
    Rule("php-echo-raw", MEDIUM, "request data echoed without escaping",
         "Reflected user input becomes markup. Escape with htmlspecialchars.",
         _r(r"echo\s+\$_(?:GET|POST|REQUEST|COOKIE)\b"), PHP),

    # ---------------- C / C++ ----------------
    Rule("c-strcpy", HIGH, "unbounded string copy",
         "strcpy/strcat/sprintf write past the end of a buffer whose size they "
         "never see. Use the n- or _s variants with a length.",
         _r(r"(?<![\w.])(?:strcpy|strcat|sprintf|vsprintf)\s*\("), C),
    Rule("c-gets", HIGH, "gets() reads an unbounded line",
         "gets cannot be used safely and was removed from C11. Use fgets.",
         _r(r"(?<![\w.])gets\s*\("), C),
    Rule("c-system", HIGH, "system() shell call",
         "Runs through a shell. Use exec-family calls with an argument array.",
         _r(r"(?<![\w.])system\s*\("), C),
    Rule("c-malloc-unchecked", MEDIUM, "allocation result used unchecked",
         "malloc returns NULL under pressure and the next dereference is a "
         "crash. Check before use.",
         _r(r"=\s*(?:\([^)]*\)\s*)?(?:m|c|re)alloc\s*\([^;]*\);\s*\n\s*(?!.*(?:if|assert|NULL))"),
         C),

    # ---------------- Rust ----------------
    Rule("rs-unwrap", MEDIUM, "unwrap() / expect() on a fallible value",
         "Panics on the error path, which takes the thread down. Propagate "
         "with ? or handle the error.",
         _r(r"\.\s*(?:unwrap|expect)\s*\("), RUST, skip_tests=True),
    Rule("rs-unsafe", MEDIUM, "unsafe block",
         "The compiler's guarantees stop here. Keep it small and say what "
         "invariant makes it sound.",
         _r(r"(?<![\w])unsafe\s*\{"), RUST),
    Rule("rs-panic", MEDIUM, "panic!/todo!/unimplemented! in library code",
         "Aborts rather than returning. Return a Result instead.",
         _r(r"(?<![\w])(?:panic|todo|unimplemented)!\s*[\(\[]"), RUST, skip_tests=True),
]

RULES_BY_ID = {r.id: r for r in RULES}

SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}


def rules_for(lang: str) -> list[Rule]:
    """Rules that apply to a language, matched by family.

    A rule declares the families it is about, so one entry covers `.js`, `.ts`,
    `.tsx`, `.vue` and `.svelte` at once, and `ANY` covers everything - which is
    what a committed private key or a TODO needs.
    """
    fam = family(lang)
    return [r for r in RULES if "*" in r.langs or fam in r.langs]


# Test-file conventions, per ecosystem: `test_x.py`, `x_test.go`, `XTest.java`,
# `x.spec.ts`, `x_spec.rb`, `XTests.cs`. A rule with skip_tests=True is one
# whose whole point is that production code should not do this, and firing it on
# every assertion in a test suite is what makes people stop reading the output.
_TEST_DIRS = ("test", "tests", "spec", "specs", "__tests__", "fixtures",
              "testdata", "e2e")
_TEST_PREFIXES = ("test_", "tests_", "spec_")
# A separator is required. Matching a bare "test" at the end of a stem is what
# made `Latest.java` a test file, `contest.py` a test file, and every rule with
# skip_tests=True silently stop firing in real code.
_TEST_SUFFIXES = ("_test", "_tests", "_spec", "_specs", "-test", "-spec")
_TEST_WHOLE = ("test", "tests", "spec", "specs", "conftest")
# camelCase, so case-sensitive on purpose: `FooTest` is a test, `Latest` is not.
_TEST_CAMEL = ("Test", "Tests", "Spec", "Specs", "TestCase", "Tests")


def is_test_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if any(part.lower() in _TEST_DIRS for part in parts[:-1]):
        return True

    name = parts[-1]
    stem = name.split(".")[0]
    lowered = stem.lower()
    return (
        lowered in _TEST_WHOLE
        or lowered.startswith(_TEST_PREFIXES)
        or lowered.endswith(_TEST_SUFFIXES)
        or stem.endswith(_TEST_CAMEL)
        or ".test." in name.lower() or ".spec." in name.lower()
    )
