"""Refresh MIT-licensed catalog/reference code from a pinned upstream commit.

No LeetCode statements or proprietary test cases are imported. Our prompts and
practice fixtures live separately in app/practice/exercises.py.
"""
import ast
import concurrent.futures
import json
from pathlib import Path
import tempfile
import httpx

COMMIT = "2736633bf3d8a81115b10f299306be91cf822f39"
BASE = f"https://raw.githubusercontent.com/neetcode-gh/leetcode/{COMMIT}/"
DEST = Path(__file__).resolve().parents[1] / "app" / "practice" / "catalog.json"


def read(path):
    response = httpx.get(BASE + path, timeout=30)
    response.raise_for_status()
    return response.text


def convert(item):
    cache = Path(tempfile.gettempdir()) / 'pair-neetcode-sources' / COMMIT
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / (item['code'] + '.py')
    if not cached.exists():
        cached.write_text(read(f"python/{item['code']}.py"))
    code = cached.read_text()
    # Some files contain unfinished later alternatives. Only import a complete
    # parsable prefix, then validate every selected implementation with fixtures.
    lines = code.splitlines()
    while True:
        try:
            tree = ast.parse('\n'.join(lines))
            break
        except SyntaxError:
            lines.pop()
            if not lines:
                raise ValueError(f"No valid implementation: {item['code']}")
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if not classes:
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        tree = ast.Module(body=[ast.ClassDef(name='Solution', bases=[], keywords=[], body=functions, decorator_list=[])], type_ignores=[])
        ast.fix_missing_locations(tree)
        classes = tree.body
    # Keep the first complete implementation; many upstream files repeat the
    # same class for alternative approaches. Preserve helpers/imports before it.
    target = next((n for n in classes if n.name == 'Solution'), None) or next(n for n in classes if n.name not in {"ListNode", "TreeNode", "Node", "TrieNode", "DoublyListNode"})
    seen = set()
    reference_nodes = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            if node.name in seen:
                continue
            seen.add(node.name)
        reference_nodes.append(node)
    reference = '\n\n'.join(ast.unparse(n) for n in reference_nodes) + '\n'
    methods = [n for n in target.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
    method = methods[0].name if target.name == "Solution" else None
    stubs = []
    for n in target.body:
        if not isinstance(n, ast.FunctionDef):
            continue
        if target.name == "Solution" and n.name != method and item['code'] != '0271-encode-and-decode-strings':
            continue
        if n.name.startswith("_") and n.name != "__init__":
            continue
        # Strip annotations: the runner supplies standard typing/node helpers.
        n.returns = None
        for arg in n.args.args:
            arg.annotation = None
        n.body = [ast.Raise(exc=ast.Call(func=ast.Name(id="NotImplementedError", ctx=ast.Load()), args=[], keywords=[]))]
        stubs.append(ast.unparse(n))
    starter = "class " + target.name + ":\n" + "\n\n".join("\n".join("    " + l for l in s.splitlines()) for s in stubs) + "\n"
    return {
        "number": int(item['code'].split('-')[0]),
        "id": item["link"].strip("/"), "title": item["problem"],
        "category": item["pattern"], "difficulty": item["difficulty"].lower(),
        "url": "https://neetcode.io/problems/" + item["link"].strip("/"),
        "class_name": target.name, "method": method, "starter": starter,
        "reference": reference,
    }


if __name__ == "__main__":
    items = [x for x in json.loads(read(".problemSiteData.json")) if x.get("neetcode150")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        catalog = list(pool.map(convert, items))
    DEST.write_text(json.dumps({"source_commit": COMMIT, "problems": catalog}, indent=2) + "\n")
    print(f"Imported {len(catalog)} problems")
