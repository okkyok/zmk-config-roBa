#!/usr/bin/env python3
"""roBa.keymap の「隙間」(コンボ切れ+hold未確定のタイミング窓)を検出する。

## 背景 — このリポジトリで3回発生した同型バグ

ホールドタップ(例: Z=Shift/z)の修飾+相手キーを即時確定する「加速コンボ」
(例: shift_p = Z+P→大文字P)は、コンボの timeout-ms がホールド側の
tapping-term-ms と一致していないと隙間が生まれる:

  - キー間隔 0〜timeout        → コンボが発火 ✓
  - キー間隔 timeout〜term     → コンボ切れ、かつholdも未確定 ✗ ← 隙間
  - キー間隔 term〜            → hold確定済みの修飾が効く ✓

この隙間に落ちると「zp」「ag」等の文字化けになる(shift_p / raycast_ag /
half_space で実際に発生・修正済み)。

## このスクリプトが強制する不変条件

keymap を解析して「加速コンボ」を自動検出し(コンボの出力が、メンバーの
ホールド側修飾を相手キーのタップに適用したもの、またはメンバーのホールド側
レイヤーにおける相手キーの割り当てと一致するもの)、以下を検証する:

  R1: timeout-ms がホールド側の tapping-term-ms と同値であること
  R2: layers 指定があること(全レイヤーで誤発火しないため)

## 意図的な例外 — gap-ok マーカー

2キーの並びがローマ字に実在する組(例: raycast_ag の a→g「あが」)は、
timeout を term まで広げると通常タイピングを食って誤発する(実際に発生)。
その場合は隙間ゼロを諦めて timeout を小さくし、中間域を文字入力(安全側)に
倒す。この判断をした箇所は keymap 内のコメントに

    // gap-ok(コンボ名): 理由...

と書くと R1 を免除する(理由の併記必須。マーカーだけの免除は運用で禁止)。

使い方: python3 scripts/check_keymap_gaps.py  (終了コード0=OK / 1=違反あり)
CI (build.yml) とローカルの pre-commit hook から自動実行される。
"""

import re
import sys
from pathlib import Path

KEYMAP = Path(__file__).resolve().parent.parent / "config" / "roBa.keymap"

# ZMKの既定値
DEFAULT_TAPPING_TERM = 200
DEFAULT_COMBO_TIMEOUT = 50

# ホールド側パラメータ(単独モディファイア)→ キーコードラッパーの対応
MOD_WRAP = {
    "LEFT_SHIFT": "LS", "LSHFT": "LS", "LSHIFT": "LS",
    "LEFT_CONTROL": "LC", "LCTRL": "LC",
    "LEFT_GUI": "LG", "LGUI": "LG", "LEFT_COMMAND": "LG",
    "LEFT_ALT": "LA", "LALT": "LA",
    "RIGHT_SHIFT": "RS", "RSHFT": "RS", "RSHIFT": "RS",
    "RIGHT_CONTROL": "RC", "RCTRL": "RC",
    "RIGHT_GUI": "RG", "RGUI": "RG", "RIGHT_COMMAND": "RG",
    "RIGHT_ALT": "RA", "RALT": "RA",
}


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def block_body(text: str, brace_idx: int):
    """text[brace_idx] == '{' として、対応する閉じ括弧までの中身と終了位置を返す。"""
    depth = 0
    for i in range(brace_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_idx + 1:i], i
    raise ValueError("unbalanced braces")


def child_nodes(body: str):
    """ノード本体直下の `name { ... }` 子ノードを順に (name, body) で返す。"""
    out = []
    i = 0
    while True:
        m = re.compile(r"([\w-]+)\s*\{").search(body, i)
        if not m:
            return out
        inner, end = block_body(body, m.end() - 1)
        out.append((m.group(1), inner))
        i = end + 1


def prop_int(body: str, name: str, default):
    m = re.search(rf"{name}\s*=\s*<\s*(\d+)\s*>", body)
    return int(m.group(1)) if m else default


def prop_list(body: str, name: str):
    m = re.search(rf"(?<![\w-]){name}\s*=\s*<([^>]*)>", body)
    return m.group(1).split() if m else None


def bindings_entries(body: str):
    """bindings = <...> を '&' 区切りのエントリ列(トークンのリスト)へ。"""
    m = re.search(r"(?<![\w-])bindings\s*=\s*<(.*?)>\s*;", body, flags=re.DOTALL)
    if not m:
        return []
    entries = []
    for chunk in m.group(1).split("&"):
        toks = chunk.split()
        if toks:
            entries.append(toks)
    return entries


def main() -> int:
    raw = KEYMAP.read_text()
    # コメント除去前に gap-ok(name) マーカーを収集(意図的な例外宣言)
    gap_ok = set(re.findall(r"gap-ok\((\w+)\)", raw))
    text = strip_comments(raw)

    defines = {m.group(1): int(m.group(2))
               for m in re.finditer(r"#define\s+(\w+)\s+(\d+)", text)}

    # --- behaviors: 名前 → (種別 'mod'|'layer', tapping-term) ---
    behaviors = {}
    for m in re.finditer(r"(\w+):\s*[\w-]+\s*\{", text):
        body, _ = block_body(text, m.end() - 1)
        if "zmk,behavior-hold-tap" not in body:
            continue
        first = re.search(r"bindings\s*=\s*<\s*&(\w+)", body)
        kind = "layer" if first and first.group(1) == "mo" else "mod"
        behaviors[m.group(1)] = (kind, prop_int(body, "tapping-term-ms",
                                                DEFAULT_TAPPING_TERM))
    # 組み込みの &mt / &lt (トップレベルの上書きブロックから term を拾う)
    for name, kind in (("mt", "mod"), ("lt", "layer")):
        term = DEFAULT_TAPPING_TERM
        m = re.search(rf"&{name}\s*\{{", text)
        if m:
            body, _ = block_body(text, m.end() - 1)
            term = prop_int(body, "tapping-term-ms", DEFAULT_TAPPING_TERM)
        behaviors.setdefault(name, (kind, term))

    # --- keymap: レイヤー番号 → 位置 → バインディング ---
    km = re.search(r"keymap\s*\{", text)
    km_body, _ = block_body(text, km.end() - 1)
    layers = []
    for _, layer_body in child_nodes(km_body):
        entries = bindings_entries(layer_body)
        if entries:
            layers.append(entries)

    def resolve(layer_idx: int, pos: int):
        """&trans はBASEへフォールバックして実バインディングを得る。"""
        entry = layers[layer_idx][pos]
        if entry[0] == "trans" and layer_idx != 0:
            return layers[0][pos]
        return entry

    def tap_key(entry):
        if entry[0] == "kp":
            return entry[1]
        if entry[0] in behaviors and len(entry) >= 3:
            return entry[2]  # ホールドタップのタップ側
        return None

    # --- combos を検査 ---
    combos = re.search(r"combos\s*\{", text)
    combos_body, _ = block_body(text, combos.end() - 1)
    errors = []
    verified = []
    waived = []
    for name, body in child_nodes(combos_body):
        positions = prop_list(body, "key-positions")
        binding = bindings_entries(body)
        if not positions or len(positions) != 2 or len(binding) != 1:
            continue
        p0, p1 = (int(p) for p in positions)
        combo_out = " ".join(binding[0])
        timeout = prop_int(body, "timeout-ms", DEFAULT_COMBO_TIMEOUT)
        layer_prop = prop_list(body, "layers")
        combo_layers = ([defines.get(tok, None) for tok in layer_prop]
                        if layer_prop else [0])

        for layer_idx in combo_layers:
            if layer_idx is None or layer_idx >= len(layers):
                continue
            for holder, tapped in ((p0, p1), (p1, p0)):
                h = resolve(layer_idx, holder)
                if h[0] not in behaviors:
                    continue
                kind, term = behaviors[h[0]]
                expected = None
                if kind == "mod":
                    wrap = MOD_WRAP.get(h[1])
                    tk = tap_key(resolve(layer_idx, tapped))
                    if wrap and tk:
                        expected = f"kp {wrap}({tk})"
                else:  # layer
                    hl = defines.get(h[1], None)
                    if hl is not None and hl < len(layers):
                        expected = " ".join(resolve(hl, tapped))
                if expected != combo_out:
                    continue
                # 加速コンボと判定
                where = (f"combo '{name}' (pos {holder}+{tapped}, "
                         f"holder=&{h[0]}, 出力 {combo_out})")
                if timeout != term and name in gap_ok:
                    waived.append(
                        f"{where}: timeout-ms={timeout} != term={term} だが "
                        f"gap-ok 宣言あり(間隔{timeout}〜{term}msは文字入力に"
                        f"倒す設計)。")
                elif timeout != term:
                    errors.append(
                        f"{where}: timeout-ms={timeout} だがホールド側 "
                        f"&{h[0]} の tapping-term-ms={term}。間隔が "
                        f"{timeout}〜{term}ms のとき隙間に落ちる。"
                        f"timeout-ms = <{term}> に揃えるか、ローマ字隣接等で"
                        f"それが不可能な場合は理由付きで gap-ok({name}) を"
                        f"コメントに書くこと。")
                elif layer_prop is None:
                    errors.append(
                        f"{where}: layers 指定がない。全レイヤーで誤発火"
                        f"し得るため layers = <...> を明示すること。")
                else:
                    verified.append(where)

    if verified:
        print("検証済みの加速コンボ (timeout==term / layers指定あり):")
        for v in sorted(set(verified)):
            print(f"  OK {v}")
    if waived:
        print("意図的な例外 (gap-ok宣言):")
        for w in sorted(set(waived)):
            print(f"  許容 {w}")
    if errors:
        print("\nNG 隙間の可能性を検出:", file=sys.stderr)
        for e in sorted(set(errors)):
            print(f"  {e}", file=sys.stderr)
        return 1
    print("隙間なし。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
