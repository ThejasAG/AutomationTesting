"""Which flows does a change to the flow engine reach?

Built from the source with `ast`, never by importing it (a half-edited file still
parses or fails cleanly):

  * every FlowRunner method and module function, and what it calls
    (`self._x(...)` and `_x(...)`);
  * every `@token` in FlowRunner._handle_special and the methods its branch calls;
  * every flow's steps (plain-language aliases from _PLAIN_STEP_TOKENS included).

A changed function reaches the tokens whose handlers call it (directly or
through helpers), and so the flows that use those tokens. A function no token
handler reaches is engine core (segment runner, step resolver, session setup):
every flow depends on it, so the answer is "all".
"""
from __future__ import annotations

import ast
import os
from typing import Dict, Iterable, List, Optional, Set

FLOWS_FILE = os.path.join("automation", "scenarios", "cross_app_flows.py")
ALL = "*"


def _calls(fn: ast.AST) -> Set[str]:
    out: Set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self":
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def call_graph(src: str) -> Dict[str, Set[str]]:
    """name -> names it calls, for module functions and FlowRunner methods."""
    tree = ast.parse(src)
    g: Dict[str, Set[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            g[node.name] = _calls(node)
        elif isinstance(node, ast.ClassDef) and node.name == "FlowRunner":
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    g[m.name] = _calls(m)
    return g


def _handle_special(src: str) -> Optional[ast.FunctionDef]:
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == "FlowRunner":
            for m in node.body:
                if isinstance(m, ast.FunctionDef) and m.name == "_handle_special":
                    return m
    return None


def token_handlers(src: str) -> Dict[str, Set[str]]:
    """'@token' (or '@prefix:') -> the functions its branch of _handle_special calls."""
    fn = _handle_special(src)
    out: Dict[str, Set[str]] = {}
    if fn is None:
        return out
    for stmt in fn.body:
        if not isinstance(stmt, ast.If):
            continue
        toks = [c.value for c in ast.walk(stmt.test)
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.startswith("@")]
        if not toks:
            continue
        called: Set[str] = set()
        for b in stmt.body:
            called |= _calls(b)
        for t in toks:
            out.setdefault(t, set()).update(called)
    return out


def _reach(g: Dict[str, Set[str]], start: Iterable[str]) -> Set[str]:
    seen: Set[str] = set()
    todo = list(start)
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        todo.extend(c for c in g.get(n, ()) if c in g)
    return seen


def tokens_reaching(src: str, funcs: Iterable[str]) -> Optional[Set[str]]:
    """The tokens whose handlers reach any of `funcs`; None = engine core (all flows)."""
    g = call_graph(src)
    th = token_handlers(src)
    want = {f for f in funcs if f}
    hit: Set[str] = set()
    reached_any = False
    for tok, called in th.items():
        r = _reach(g, called)
        if r & want:
            hit.add(tok)
            reached_any = True
    known = set(g)
    if not reached_any and (want & known):
        return None
    return hit


def _uses(step: str, tokens: Set[str], aliases: Dict[str, str]) -> bool:
    s = step.strip()
    s = aliases.get(s, s)
    return any(s == t or (t.endswith(":") and s.startswith(t)) for t in tokens)


def flows_for_functions(flows: Dict[str, List[str]], src: str, funcs: Iterable[str],
                        aliases: Optional[Dict[str, str]] = None) -> List[str]:
    """Flow ids ({flow_id: [steps]}) a change to `funcs` reaches; [ALL] when it is
    engine core."""
    toks = tokens_reaching(src, funcs)
    if toks is None:
        return [ALL]
    aliases = aliases or {}
    return [fid for fid, steps in flows.items() if any(_uses(st, toks, aliases) for st in steps)]


def _bodies(src: str) -> Dict[str, str]:
    tree = ast.parse(src)
    out: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.ClassDef) and node.name == "FlowRunner":
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[m.name] = ast.dump(m)
                elif not isinstance(m, ast.Expr):        # class attributes (token maps…)
                    out.setdefault("<FlowRunner attrs>", "")
                    out["<FlowRunner attrs>"] += ast.dump(m)
    return out


def changed_functions(before_src: str, after_src: str) -> Set[str]:
    """Functions whose code differs between two versions of cross_app_flows.py
    (added and removed ones included). Comments and formatting don't count."""
    a, b = _bodies(before_src), _bodies(after_src)
    return {n for n in set(a) | set(b) if a.get(n) != b.get(n) and not n.startswith("<")}
