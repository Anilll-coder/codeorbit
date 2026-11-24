from tree_sitter_language_pack import get_parser

from codeorbit.extract import javascript_ex as js


def symbols(code: str) -> dict[str, str]:
    src = code.encode()
    ex = js.extract(get_parser("javascript").parse(src), src, "m")
    return {s.qname: s.kind for s in ex.symbols}


def test_commonjs_assignments_are_functions():
    s = symbols("module.exports = function httpAdapter(config) {\n"
                "  function done() {}\n};\n"
                "exports.helper = (a, b) => a + b;\n"
                "module.exports.anon = function () {};\n")
    assert s["m.httpAdapter"] == "function"
    assert s["m.httpAdapter.done"] == "function"      # nested under it, not at module level
    assert s["m.helper"] == "function"
    assert s["m.anon"] == "function"


def test_prototype_assignment_is_a_method_of_its_owner():
    s = symbols("function Foo() {}\nFoo.prototype.bar = function () { return 1; };\n")
    assert s["m.Foo.bar"] == "method"


def test_plain_value_assignment_is_not_a_symbol():
    assert set(symbols("module.exports = { a: 1 };\nx.y = 3;\n")) == {"m"}


def test_a_named_function_expression_anywhere_is_a_symbol():
    # axios lib/adapters/http.js: the whole adapter is an expression operand.
    got = symbols('''
export default isSupported && function httpAdapter(config) {
  return wrapAsync(async function dispatchHttpRequest(resolve) {
    function abort() {}
    req.on("error", function handleRequestError(err) {});
  });
};
[1].map(function (x) { return x; });
''')
    assert got["m.httpAdapter"] == "function"
    assert "m.httpAdapter.dispatchHttpRequest" in got
    assert "m.httpAdapter.dispatchHttpRequest.abort" in got
    assert "m.httpAdapter.dispatchHttpRequest.handleRequestError" in got
