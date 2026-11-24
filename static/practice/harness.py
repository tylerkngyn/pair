"""Executed only in the isolated browser runner (or trusted fixture tests).

Never import/execute user submissions in the web application process.
"""
import collections
import __future__
import contextlib
import copy
import io
import json
import math
import statistics
import time
import traceback


class ListNode:
    def __init__(self, val=0, next=None):
        self.val, self.next = val, next


class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val, self.left, self.right = val, left, right


class Node:
    def __init__(self, val=0, next=None, random=None, neighbors=None):
        self.val, self.next, self.random = val, next, random
        self.neighbors = neighbors or []


def adapt(value):
    if isinstance(value, list):
        return [adapt(x) for x in value]
    if not isinstance(value, dict):
        return value
    if "linked" in value:
        nodes = [ListNode(x) for x in value["linked"]]
        for a, b in zip(nodes, nodes[1:]):
            a.next = b
        if nodes and value.get("pos", -1) >= 0:
            nodes[-1].next = nodes[value["pos"]]
        return nodes[0] if nodes else None
    if "tree" in value:
        values = value["tree"]
        if not values or values[0] is None:
            return None
        root = TreeNode(values[0])
        queue, index = collections.deque([root]), 1
        while queue and index < len(values):
            node = queue.popleft()
            for side in ("left", "right"):
                if index < len(values) and values[index] is not None:
                    child = TreeNode(values[index])
                    setattr(node, side, child)
                    queue.append(child)
                index += 1
        return root
    if "random" in value:
        nodes = [Node(x[0]) for x in value["random"]]
        for i, (_, random) in enumerate(value["random"]):
            nodes[i].next = nodes[i + 1] if i + 1 < len(nodes) else None
            nodes[i].random = nodes[random] if random is not None else None
        return nodes[0] if nodes else None
    if "graph" in value:
        nodes = [Node(i + 1) for i in range(len(value["graph"]))]
        for node, neighbors in zip(nodes, value["graph"]):
            node.neighbors = [nodes[i - 1] for i in neighbors]
        return nodes[0] if nodes else None
    return copy.deepcopy(value)


def flatten(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "left") and hasattr(value, "right"):
        result, queue, seen = [], collections.deque([value]), set()
        while queue:
            node = queue.popleft()
            if node is None:
                result.append(None)
                continue
            if id(node) in seen:
                raise ValueError("Output tree contains a cycle/shared node")
            seen.add(id(node))
            result.append(node.val)
            queue.extend([node.left, node.right])
        while result and result[-1] is None:
            result.pop()
        return result
    if hasattr(value, "next"):
        result, seen = [], set()
        while value is not None:
            if id(value) in seen:
                raise ValueError("Output list contains a cycle")
            seen.add(id(value))
            result.append(value.val)
            value = value.next
        return result
    if isinstance(value, dict):
        return {str(k): flatten(v) for k, v in value.items()}
    return [flatten(x) for x in value]


PRELUDE = """from __future__ import annotations
from typing import *
from collections import *
from heapq import *
from functools import *
from itertools import *
from math import *
import collections, heapq, math, bisect, functools, itertools, random
"""


def namespace(source):
    scope = {"ListNode": ListNode, "TreeNode": TreeNode, "Node": Node}
    exec(PRELUDE, scope)
    exec(compile(source, "<solution>", "exec", flags=__future__.annotations.compiler_flag), scope)
    return scope


def prepare(problem, scope, case):
    args = adapt(case["args"])
    mode = problem["mode"]
    cls = scope[problem["class_name"]]
    if mode == "lca":
        root = args[0]
        def find(node, val):
            while node and node.val != val:
                node = node.left if val < node.val else node.right
            return node
        args = [root, find(root, args[1]), find(root, args[2])]
    def run():
        if mode == "design":
            operations = args[0]
            obj = cls(*operations["init"])
            return [getattr(obj, name)(*values) for name, values in operations["calls"]]
        obj = cls()
        if mode == "strings_codec":
            return obj.decode(obj.encode(args[0]))
        if mode == "tree_codec":
            return obj.deserialize(obj.serialize(args[0]))
        return getattr(obj, problem["method"])(*args)
    return args, run


def invoke(problem, scope, case):
    args, run = prepare(problem, scope, case)
    mode = problem["mode"]
    started = time.perf_counter()
    output = run()
    elapsed = (time.perf_counter() - started) * 1000
    if mode == "inplace":
        output = args[0]
    if mode == "lca":
        output = output.val if output else None
    if mode == "random":
        originals, current = set(), args[0]
        while current:
            originals.add(id(current))
            current = current.next
        nodes, seen = [], set()
        while output:
            if id(output) in seen or id(output) in originals:
                raise ValueError("Return a deep copy with no original nodes or next cycles")
            seen.add(id(output))
            nodes.append(output)
            output = output.next
        positions = {id(n): i for i, n in enumerate(nodes)}
        output = [[n.val, positions[id(n.random)] if n.random else None] for n in nodes]
    elif mode == "graph":
        def visit(root):
            found, queue = {}, collections.deque([root] if root else [])
            while queue:
                node = queue.popleft()
                if node.val in found:
                    continue
                found[node.val] = node
                queue.extend(node.neighbors)
            return found
        original, clone = visit(args[0]), visit(output)
        if {id(n) for n in original.values()} & {id(n) for n in clone.values()}:
            raise ValueError("Return a deep graph copy")
        output = [sorted(n.val for n in clone[v].neighbors) for v in sorted(clone)]
    elif output is None and case["expected"] == []:
        output = []  # Empty linked lists/trees are represented by [].
    return flatten(output), elapsed


def benchmark(problem, scope, case):
    # Time batches rather than individual microsecond calls: browser clocks
    # have reduced resolution. Each call gets a fresh input and instance.
    count = 8 if len(repr(case["args"])) > 10000 else 128
    samples = []
    for _ in range(3):
        calls = [prepare(problem, scope, case)[1] for _ in range(count)]
        started = time.perf_counter()
        for run in calls:
            run()
        samples.append((time.perf_counter() - started) * 1000 / count)
    return statistics.median(samples)


def matches(problem, case, actual):
    expected, args = case["expected"], case["args"]
    compare = problem["compare"]
    if compare in ("unordered", "groups"):
        def key(values):
            if compare == "groups":
                values = [sorted(v) for v in values]
            return sorted(json.dumps(v, sort_keys=True) for v in values)
        return key(actual) == key(expected)
    if compare == "indices":
        return sorted(actual) == sorted(expected)
    if compare == "float":
        return math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-8)
    if compare == "palindrome":
        return isinstance(actual, str) and actual in args[0] and actual == actual[::-1] and len(actual) == len(expected)
    if compare == "courses":
        if not expected:
            return actual == []
        return sorted(actual) == list(range(args[0])) and all(actual.index(pre) < actual.index(course) for course, pre in args[1])
    if compare == "alien":
        if not expected:
            return actual == ""
        words = args[0]
        if len(actual) != len(set("".join(words))) or set(actual) != set("".join(words)):
            return False
        rank = {c: i for i, c in enumerate(actual)}
        return all([rank[c] for c in a] <= [rank[c] for c in b] for a, b in zip(words, words[1:]))
    # bools must not pass as integer answers (Python normally equates them).
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual == expected
    return actual == expected and not (isinstance(actual, bool) and not isinstance(expected, bool))


class LimitedOutput(io.StringIO):
    def write(self, text):
        remaining = 8000 - self.tell()
        if remaining > 0:
            super().write(text[:remaining])
        return len(text)


def evaluate(problem, source, full=True):
    cases = problem["cases"] if full else problem["cases"][:2]
    results, durations, reference_durations = [], [], []
    output = LimitedOutput()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            scope = namespace(source)
        except BaseException:
            return {"passed": 0, "total": len(cases), "cases": [], "runtime_ms": None, "reference_ms": None, "error": traceback.format_exc(limit=3)[-3000:], "stdout": output.getvalue()}
        for case in cases:
            try:
                actual, _ = invoke(problem, scope, case)
                passed = matches(problem, case, actual)
                results.append({"passed": passed, "actual": repr(actual)[:1000], "expected": repr(case["expected"])[:1000]})
            except BaseException:
                results.append({"passed": False, "error": traceback.format_exc(limit=3)[-2000:]})
        # Repeated invocation samples reduce one-off timer noise. This is a
        # device-local comparison on this suite, not a complexity proof or rank.
        if all(r["passed"] for r in results):
            try:
                reference = namespace(problem["reference"])
                for case in cases:
                    durations.append(benchmark(problem, scope, case))
                    reference_durations.append(benchmark(problem, reference, case))
            except BaseException:
                durations, reference_durations = [], []
                print("Timing could not complete; correctness results are retained.")
    return {"passed": sum(r["passed"] for r in results), "total": len(cases), "cases": results, "runtime_ms": sum(durations) if durations else None, "reference_ms": sum(reference_durations) if reference_durations else None, "stdout": output.getvalue(), "error": None}
