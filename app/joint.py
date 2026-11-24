"""联答人数求解：缺码卡卷型未查实时，两道题指定选项的“同时选中”可达人数。

规则（与核对求解器一致并额外收紧）：
  * 仅卷型码缺失的卡参与分配，每张缺码卡整张卡共用一个假定卷型（甲 A / 乙 B）；
  * 已知卷型卡照用原码，其实收甲卷份数作为硬约束（甲卷总数 == 实收份数）；
  * 某卡某分支下存在对照表无法换算的作答、或同一张卡在同一题库题号出现
    多条换算结果（同卡同题重复）时，该分支不可用，对应分配整体排除；
  * 只保留能同时精确复现供数表全部单元人数、且甲卷总数等于实收份数的分配；
  * 列出这些分配下联答人数（同时命中两道题各自指定标准选项的卡数）的全部
    确实可达取值。搜索受节点上限保护，超限返回“未完成”而非不全的结果。

整个模块对调用时传入的数据库快照做纯内存计算，不写库；调用方负责在出结果前
按最新已提交状态复核（停供或计算依据变化则只提示重查）。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .db import META_RECEIVED_A, META_STATUS, get_meta, get_status

JOINT_NODE_LIMIT = 500_000


class JointError(Exception):
    """联答查询的可向页面回显的业务错误。"""


# ---------------------------------------------------------------- 快照与指纹
def load_snapshot(conn) -> dict:
    """在单个只读事务内取走计算所需的全部已提交数据。"""
    conn.execute("BEGIN")
    try:
        snap = {
            "status": get_status(conn),
            "received_a_raw": get_meta(conn, META_RECEIVED_A),
            "cards": [
                tuple(r) for r in conn.execute(
                    "SELECT card_no, paper, is_known FROM cards ORDER BY card_no"
                )
            ],
            "raw": [
                tuple(r) for r in conn.execute(
                    "SELECT card_no, pos, option FROM answers_raw "
                    "ORDER BY card_no, pos"
                )
            ],
            "mapping": [
                tuple(r) for r in conn.execute(
                    "SELECT paper, pos, option, qid, std_option FROM mapping "
                    "ORDER BY paper, pos, option"
                )
            ],
            "supply": [
                tuple(r) for r in conn.execute(
                    "SELECT qid, std_option, cnt FROM supply ORDER BY qid, std_option"
                )
            ],
        }
    finally:
        # 纯读取，立即结束事务，避免长时间占着读锁阻塞更正提交
        conn.execute("COMMIT")
    snap["fingerprint"] = fingerprint(snap)
    return snap


def fingerprint(snap: dict) -> str:
    """计算依据指纹：原始作答、卷型码、对照表、实收份数、供数表任一变化即变。"""
    payload = json.dumps(
        {
            "cards": snap["cards"],
            "raw": snap["raw"],
            "mapping": snap["mapping"],
            "supply": snap["supply"],
            "received_a": snap["received_a_raw"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 分支建模
def _build_model(snap: dict) -> dict:
    """把快照整理成求解结构，并完成已知卡/分支可用性校验。"""
    if not snap["cards"]:
        raise JointError("尚未导入作答 CSV")
    if not snap["mapping"]:
        raise JointError("尚未导入甲乙卷对照表")
    if not snap["supply"]:
        raise JointError("尚未导入待供数 CSV")
    if snap["received_a_raw"] is None:
        raise JointError("尚未登记实收甲卷份数")
    received_a = int(snap["received_a_raw"])

    mapping = {(p, pos, opt): (qid, so) for p, pos, opt, qid, so in snap["mapping"]}
    target = {(qid, so): cnt for qid, so, cnt in snap["supply"]}
    raw_by_card: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for card, pos, opt in snap["raw"]:
        raw_by_card[card].append((pos, opt))

    def branch_vector(card: str, paper: str):
        """返回 (贡献向量, 各题号命中的标准选项)；不可用返回 None。

        不可用的两种情形：
          1) 某条作答在该卷型对照表里找不到映射（无法换算）；
          2) 同一张卡对同一题库题号换算出两个不同的标准选项（同卡同题重复，
             无法确定该卡此题到底选了哪个标准选项）。
        同题号同标准选项的多条作答只计人数，不算重复。
        """
        vec: dict[tuple[str, str], int] = defaultdict(int)
        qid_options: dict[str, set[str]] = defaultdict(set)
        for pos, opt in raw_by_card.get(card, ()):
            hit = mapping.get((paper, pos, opt))
            if hit is None:
                return None
            qid, std_option = hit
            vec[hit] += 1
            qid_options[qid].add(std_option)
        if any(len(opts) > 1 for opts in qid_options.values()):
            return None
        return dict(vec), {qid: next(iter(opts)) for qid, opts in qid_options.items()}

    base: dict[tuple[str, str], int] = defaultdict(int)
    known_a = 0
    unknown = []
    for card, paper, is_known in snap["cards"]:
        if is_known:
            if paper == "A":
                known_a += 1
            built = branch_vector(card, paper)
            if built is None:
                raise JointError(
                    f"已知{paper}卷卡 {card} 存在无法换算的作答或同卡同题重复，"
                    "不存在可接受的分配"
                )
            for cell, n in built[0].items():
                base[cell] += n
        else:
            unknown.append(card)

    need_a = received_a - known_a
    return {
        "mapping": mapping,
        "target": target,
        "base": dict(base),
        "known_a": known_a,
        "received_a": received_a,
        "need_a": need_a,
        "unknown": unknown,
        "branch_vector": branch_vector,
    }


def _freeze(vec: dict | None):
    return tuple(sorted(vec.items())) if vec is not None else None


# ---------------------------------------------------------------- 枚举求解
def enumerate_joint(snap: dict, qid1: str, option1: str, qid2: str, option2: str) -> dict:
    """枚举全部可行分配下联答人数的可达取值集合。

    返回：
      {feasible: True, values: [升序取值], complete: True/False, nodes, groups...}
      资料不全/分支全不可用等 -> JointError；无解 -> {feasible: False, reason}
      搜索未完成 -> complete=False，values 为已确认可达的子集。
    """
    qid1 = (qid1 or "").strip()
    qid2 = (qid2 or "").strip()
    option1 = (option1 or "").strip()
    option2 = (option2 or "").strip()
    if not qid1 or not qid2 or not option1 or not option2:
        raise JointError("请完整选择两道不同的题及各自的标准选项")
    if qid1 == qid2:
        raise JointError("联答查询必须选择两道不同的题")
    cell1, cell2 = (qid1, option1), (qid2, option2)

    model = _build_model(snap)
    target, base = model["target"], model["base"]
    unknown, need_a = model["unknown"], model["need_a"]
    branch_vector = model["branch_vector"]

    # 缺码卡两个分支的贡献向量与目标单元命中标记；不可用分支记 None
    card_branches: dict[str, tuple] = {}
    for card in unknown:
        va = branch_vector(card, "A")
        vb = branch_vector(card, "B")
        if va is None and vb is None:
            return {"feasible": False,
                    "reason": f"卡 {card} 在甲、乙卷下均存在无法换算的作答或同卡同题重复，无可用分配"}
        card_branches[card] = (va, vb)

    # 已知卡对联答的固定贡献
    joint_base = 0
    for card, paper, is_known in snap["cards"]:
        if not is_known:
            continue
        built = branch_vector(card, paper)
        opts = built[1]
        if opts.get(qid1) == option1 and opts.get(qid2) == option2:
            joint_base += 1

    # 同构分组：两个分支的贡献向量完全相同的卡，组内只决定甲卷数量 k
    groups_map: dict[tuple, list[str]] = defaultdict(list)
    for card in unknown:
        va, vb = card_branches[card]
        groups_map[((_freeze(va[0]) if va else None),
                    (_freeze(vb[0]) if vb else None))].append(card)

    groups = []
    for (fa, fb), cards in groups_map.items():
        va = dict(fa) if fa else None
        vb = dict(fb) if fb else None
        n = len(cards)
        k_min, k_max = (n, n) if vb is None else ((0, 0) if va is None else (0, n))
        diff, low, high = {}, {}, {}
        for cell in set(va or ()) | set(vb or ()):
            d = (va or {}).get(cell, 0) - (vb or {}).get(cell, 0)
            if d:
                diff[cell] = d
                low[cell] = n * min(d, 0)
                high[cell] = n * max(d, 0)
        # 联答标记：同组卡同构，命中情况一致，只需算单卡再乘张数
        rep = cards[0]
        rep_a, rep_b = card_branches[rep]
        hit_a = bool(rep_a and rep_a[1].get(qid1) == option1
                     and rep_a[1].get(qid2) == option2)
        hit_b = bool(rep_b and rep_b[1].get(qid1) == option1
                     and rep_b[1].get(qid2) == option2)
        groups.append({
            "cards": cards, "n": n, "va": va or {}, "vb": vb or {},
            "diff": diff, "low": low, "high": high,
            "free": not diff, "k_min": k_min, "k_max": k_max,
            "hit_a": hit_a, "hit_b": hit_b,
        })
    groups.sort(key=lambda g: (g["free"], -len(g["diff"]), -g["n"]))

    universe = set(target) | set(base)
    for g in groups:
        universe.update(g["va"])
        universe.update(g["vb"])
    for cell in universe:
        if base.get(cell, 0) > target.get(cell, 0):
            return {"feasible": False,
                    "reason": f"已知卷型卡已使 {cell[0]}/{cell[1]} 人数 "
                              f"{base[cell]} 超过供数目标 {target.get(cell, 0)}"}

    min_unknown_a = sum(g["k_min"] for g in groups)
    max_unknown_a = sum(g["k_max"] for g in groups)
    if not (min_unknown_a <= need_a <= max_unknown_a):
        return {"feasible": False,
                "reason": f"缺失卡 {len(unknown)} 张中需 {need_a} 张甲卷，"
                          f"可行范围为 {min_unknown_a}~{max_unknown_a}"}

    if not unknown:
        if need_a != 0:
            return {"feasible": False,
                    "reason": f"无缺失卷型卡，但甲卷总数 {model['known_a']} "
                              f"≠ 实收 {model['received_a']}"}
        if all(base.get(c, 0) == target.get(c, 0) for c in universe):
            return {"feasible": True, "joint_values": [joint_base], "complete": True,
                    "nodes": 0, "solutions": 1}
        return {"feasible": False, "reason": "整表人数无法逐单元复现供数表"}

    # 搜索起点：每组取 k_min
    cur = dict(base)
    joint_cur = joint_base
    for g in groups:
        start_vec = g["vb"] if g["k_min"] == 0 else g["va"]
        for cell, per in start_vec.items():
            cur[cell] = cur.get(cell, 0) + per * g["n"]
        joint_cur += g["n"] * (g["hit_a"] if g["k_min"] == g["n"] else g["hit_b"])

    remaining_low: dict = defaultdict(int)
    remaining_high: dict = defaultdict(int)
    for g in groups:
        span = g["k_max"] - g["k_min"]
        for cell, d in g["diff"].items():
            if d < 0:
                remaining_low[cell] += span * d
            else:
                remaining_high[cell] += span * d

    values: set[int] = set()
    nodes = 0
    solutions = 0
    incomplete = False

    def max_a_after(i: int) -> int:
        return sum(g["k_max"] for g in groups[i + 1:])

    def dfs(i: int, cur: dict, rem_low: dict, rem_high: dict,
            a_left: int, joint_now: int) -> None:
        nonlocal nodes, solutions, incomplete
        nodes += 1
        if nodes > JOINT_NODE_LIMIT:
            incomplete = True
            return
        g = groups[i]
        n = g["n"]
        k_lo = max(g["k_min"], a_left - max_a_after(i))
        k_hi = min(g["k_max"], a_left)
        span = g["k_max"] - g["k_min"]
        for k in range(k_lo, k_hi + 1):
            nxt = dict(cur)
            for cell, d in g["diff"].items():
                nxt[cell] = nxt.get(cell, 0) + (k - g["k_min"]) * d
            nl, nh = {}, {}
            for c in universe:
                d = g["diff"].get(c, 0)
                nl[c] = rem_low.get(c, 0) - min(d, 0) * span
                nh[c] = rem_high.get(c, 0) - max(d, 0) * span
            ok = True
            for cell in universe:
                lo = nxt.get(cell, 0) + nl.get(cell, 0)
                hi = nxt.get(cell, 0) + nh.get(cell, 0)
                t = target.get(cell, 0)
                if lo > t or hi < t:
                    ok = False
                    break
            if not ok:
                continue
            # joint_now 已含搜索起点（各组 k_min）的联答；本组只加相对增量，
            # 不能再用 k*hit_a+(n-k)*hit_b 绝对公式，否则与起点重复计数。
            start_hits = g["n"] * (g["hit_a"] if g["k_min"] == g["n"] else g["hit_b"])
            joint_next = joint_now + k * g["hit_a"] + (n - k) * g["hit_b"] - start_hits
            if i + 1 == len(groups):
                if a_left - k == 0 and all(
                    nxt.get(c, 0) == target.get(c, 0) for c in universe
                ):
                    values.add(joint_next)
                    solutions += 1
                continue
            dfs(i + 1, nxt, nl, nh, a_left - k, joint_next)
            if incomplete:
                return

    dfs(0, cur, dict(remaining_low), dict(remaining_high), need_a, joint_cur)

    if incomplete:
        return {"feasible": True, "joint_values": sorted(values), "complete": False,
                "nodes": nodes, "solutions": solutions,
                "reason": f"搜索节点超过上限 {JOINT_NODE_LIMIT}，未能枚举全部可行分配"}
    if solutions == 0:
        return {"feasible": False,
                "reason": "不存在能同时复现供数表全部人数且满足实收甲卷份数的卷型分配"}
    return {"feasible": True, "joint_values": sorted(values), "complete": True,
            "nodes": nodes, "solutions": solutions}


# ---------------------------------------------------------------- 出结果前复核
def run_joint_query(conn, qid1: str, option1: str, qid2: str, option2: str) -> dict:
    """供数期间联答查询：计算前后各取一次快照，按最新已提交状态复核。

    若期间发生停供或任一计算依据变化（即便供数表人数未变），只提示重查。
    """
    before = load_snapshot(conn)
    result = enumerate_joint(before, qid1, option1, qid2, option2)
    after = load_snapshot(conn)

    stale = after["fingerprint"] != before["fingerprint"]
    paused = after["status"] != "SUPPLYING"
    result["stale"] = stale
    result["paused"] = paused
    result["status_now"] = after["status"]
    if paused:
        result["ok"] = False
        result["recheck_reason"] = (
            "当前不是供数中状态"
            + (f"（{after['status']}，供数已暂停或尚未发布）" if after["status"] != "SUPPLYING" else "")
            + "，请重新核对恢复供数后再查"
        )
    elif stale:
        result["ok"] = False
        result["recheck_reason"] = "计算期间原始作答、卷型码、对照表、实收份数或供数表发生了更正，请重新查询"
    else:
        result["ok"] = True
    result["fingerprint"] = before["fingerprint"]
    return result
