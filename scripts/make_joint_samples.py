"""生成联答查询演示用 CSV（独立于 make_samples.py 的 10 卡主流程数据）。

产出 samples/joint_*.csv，构造一个“联答 (Q7,A)+(Q8,A) 可达集合恰为 {0,2}”
的小型实例（4 张缺码卡、无已知卡，实收甲卷 2 份），并经暴力枚举校验。
"""
from __future__ import annotations

import csv
import itertools
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "samples")
os.makedirs(OUT, exist_ok=True)

# 题位 P1~P4，两种卷型下均可换算；每个 (卷型,题位) 映射到独立题库题号。
# 命中关系（产出 Q7/Q8 的 A 单元）：
#   甲卷 P1 答 A -> Q7/A，甲卷 P3 答 A -> Q8/A；
#   乙卷 P2 答 B -> Q7/A，乙卷 P4 答 B -> Q8/A；其余作答落到该题位的 Z 单元。
CELL_OF = {
    ("甲", "P1"): "Q7", ("甲", "P2"): "Q11", ("甲", "P3"): "Q8", ("甲", "P4"): "Q12",
    ("乙", "P1"): "Q13", ("乙", "P2"): "Q7", ("乙", "P3"): "Q14", ("乙", "P4"): "Q8",
}
HIT_OPT = {("甲", "P1"): "A", ("甲", "P3"): "A",
           ("乙", "P2"): "B", ("乙", "P4"): "B"}
ALL_OPTS = ("A", "B", "Z", "Y")

# 每张缺码卡的分支命中标记（甲 c1,c2 / 乙 c1,c2），c1=Q7/A, c2=Q8/A
MARKS = {
    "J01": {"甲": (0, 0), "乙": (0, 1)},
    "J02": {"甲": (0, 1), "乙": (1, 1)},
    "J03": {"甲": (1, 0), "乙": (0, 0)},
    "J04": {"甲": (1, 1), "乙": (1, 0)},
}


def build_mapping() -> list[tuple[str, str, str, str, str]]:
    rows = []
    for paper_cn in ("甲", "乙"):
        for pos in ("P1", "P2", "P3", "P4"):
            qid = CELL_OF[(paper_cn, pos)]
            for opt in ALL_OPTS:
                is_hit = HIT_OPT.get((paper_cn, pos)) == opt
                rows.append((paper_cn, pos, opt, qid, "A" if is_hit else "Z"))
    return rows


def build_answers() -> tuple[list[tuple[str, str, str, str]], dict[str, dict[str, str]]]:
    rows = []
    raw_by_card: dict[str, dict[str, str]] = {}
    for card, mm in MARKS.items():
        a1, a2 = mm["甲"]
        b1, b2 = mm["乙"]
        opts = {
            "P1": "A" if a1 else "Z",
            "P2": "B" if b1 else "Y",
            "P3": "A" if a2 else "Z",
            "P4": "B" if b2 else "Y",
        }
        raw_by_card[card] = opts
        for pos, opt in opts.items():
            rows.append((card, "", pos, opt))  # 卷型码留空 = 缺码卡
    return rows, raw_by_card


def write_csv(path: str, header: list[str], rows: list[tuple]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def brute_force(raw_by_card: dict[str, dict[str, str]]) -> tuple[set[int], dict]:
    mapping = {(p, pos, o): (q, so) for p, pos, o, q, so in build_mapping()}
    all_totals: dict[tuple, tuple[dict, int]] = {}
    for bits in itertools.product(("甲", "乙"), repeat=len(raw_by_card)):
        if bits.count("甲") != 2:  # 实收甲卷 2 份
            continue
        totals: dict[tuple[str, str], int] = {}
        joint_n = 0
        for card, paper in zip(sorted(raw_by_card), bits):
            cells = [mapping[(paper, pos, opt)] for pos, opt in raw_by_card[card].items()]
            for cell in cells:
                totals[cell] = totals.get(cell, 0) + 1
            if ("Q7", "A") in cells and ("Q8", "A") in cells:
                joint_n += 1
        all_totals[bits] = (totals, joint_n)
    # 目标的两个可行分配（联答分别为 0、2），其完整单元人数一致，取作供数表
    target_bits = [b for b, (_, j) in all_totals.items()
                   if j in (0, 2) and all_totals[b][0].get(("Q7", "A")) == 2]
    target_totals = all_totals[target_bits[0]][0]
    assert all(all_totals[b][0] == target_totals for b in target_bits), "目标分配人数不一致"
    vals: set[int] = set()
    totals_by_sol: dict[tuple, dict] = {}
    for bits, (totals, joint_n) in all_totals.items():
        if totals == target_totals:  # 能复现整表全部单元人数
            vals.add(joint_n)
            totals_by_sol[bits] = totals
    return vals, totals_by_sol


def main() -> None:
    mapping_rows = build_mapping()
    answer_rows, raw_by_card = build_answers()
    write_csv(os.path.join(OUT, "joint_mapping.csv"),
              ["卷型码", "题位", "所选项", "题号", "标准选项"], mapping_rows)
    write_csv(os.path.join(OUT, "joint_answers.csv"),
              ["卡号", "卷型码", "题位", "所选项"], answer_rows)

    vals, totals_by_sol = brute_force(raw_by_card)
    assert vals == {0, 2}, vals
    # 两个可行分配的完整单元人数一致，取任一作为供数表
    target = next(iter(totals_by_sol.values()))
    supply_rows = [(qid, so, cnt) for (qid, so), cnt in sorted(target.items())]
    write_csv(os.path.join(OUT, "joint_supply.csv"),
              ["题号", "标准选项", "人数"], supply_rows)
    with open(os.path.join(OUT, "joint_received.txt"), "w", encoding="utf-8") as f:
        f.write("2\n")

    print("联答样例已生成于", os.path.abspath(OUT))
    print("暴力枚举联答可达集合：", sorted(vals), "（期望 {0, 2}）")
    print("可行分配与联答：")
    for bits, totals in totals_by_sol.items():
        joint_n = 0
        for card, paper in zip(sorted(raw_by_card), bits):
            cells = [
                dict(((p, pos, o), (q, so))
                     for p, pos, o, q, so in mapping_rows)[(paper, pos, opt)]
                for pos, opt in raw_by_card[card].items()
            ]
            if ("Q7", "A") in cells and ("Q8", "A") in cells:
                joint_n += 1
        print(" ", bits, "联答 =", joint_n)
    print("实收甲卷份数：2；供数单元：", len(supply_rows))


if __name__ == "__main__":
    main()
