"""联答求解器测试：可达集合、无解、未完成、分支排除、{0,2} 关键情形与暴力交叉验证。

运行：python -I tests/test_joint.py
依赖仅标准库（直接调用 app.joint / app.services，不启动 Flask）。
"""
from __future__ import annotations

import itertools
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_extra = os.environ.get("EXTRA_PYTHONPATH")
if _extra:
    for p in _extra.split(os.path.pathsep):
        sys.path.insert(0, p)

from app import joint, services  # noqa: E402
from app.db import connect, init_db  # noqa: E402

P = F = 0


def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print("  ✓", name)
    else:
        F += 1
        print("  ✗", name, detail)


def fresh_db():
    conn = connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    init_db(conn)
    return conn


# ----------------------------------------------------------- 构造 {0,2} 实例
def build_012_instance(conn):
    """4 张缺码卡，联答 (Q7,A)+(Q8,A) 的可达集合恰为 {0,2}（经独立暴力验证）。

    四题位 P1~P4，两种卷型下均可换算：
      甲卷 P1/P3 的命中选项产出 Q7/Q8 的 A 单元；乙卷 P2/P4 产出；
      其余作答一律落到 Q7/Z、Q8/Z 单元（不命中）。
    卡片分支标记（甲c1,甲c2 / 乙c1,乙c2）：
      U1=(0,0)/(0,1)  U2=(0,1)/(1,1)  U3=(1,0)/(0,0)  U4=(1,1)/(1,0)
    目标 Q7/A=2、Q8/A=2，缺码卡恰 2 张甲。可行分配仅两组，联答分别为 0、2。
    """
    # 每个 (卷型, 题位, 原始选项) 都可换算，避免“无法换算”误伤；每个题位在每种
    # 卷型下映射到独立题库题号，杜绝同卡同题重复。命中关系（产出 Q7/Q8 的 A 单元）：
    #   甲卷 P1 答 A -> Q7/A，甲卷 P3 答 A -> Q8/A；
    #   乙卷 P2 答 B -> Q7/A，乙卷 P4 答 B -> Q8/A；其余作答落到各自题位的 Z 单元。
    cell_of = {
        ("A", "P1"): "Q7", ("A", "P2"): "Q11", ("A", "P3"): "Q8", ("A", "P4"): "Q12",
        ("B", "P1"): "Q13", ("B", "P2"): "Q7", ("B", "P3"): "Q14", ("B", "P4"): "Q8",
    }
    hit_opt = {("A", "P1"): "A", ("A", "P3"): "A",
               ("B", "P2"): "B", ("B", "P4"): "B"}
    mapping = []
    for paper in ("A", "B"):
        for pos in ("P1", "P2", "P3", "P4"):
            qid = cell_of[(paper, pos)]
            for opt in ("A", "B", "Z", "Y"):
                is_hit = hit_opt.get((paper, pos)) == opt
                mapping.append((paper, pos, opt, qid, "A" if is_hit else "Z"))
    conn.executemany(
        "INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES(?,?,?,?,?)",
        mapping,
    )
    marks = {
        "U1": {"A": (0, 0), "B": (0, 1)},
        "U2": {"A": (0, 1), "B": (1, 1)},
        "U3": {"A": (1, 0), "B": (0, 0)},
        "U4": {"A": (1, 1), "B": (1, 0)},
    }
    for card, mm in marks.items():
        a1, a2 = mm["A"]
        b1, b2 = mm["B"]
        opts = {
            "P1": "A" if a1 else "Z",
            "P2": "B" if b1 else "Y",
            "P3": "A" if a2 else "Z",
            "P4": "B" if b2 else "Y",
        }
        conn.execute("INSERT INTO cards(card_no,paper,is_known) VALUES(?,NULL,0)", (card,))
        for pos, opt in opts.items():
            conn.execute("INSERT INTO answers_raw VALUES(?,?,?)", (card, pos, opt))
    # 两个可行分配的完整单元人数一致：Q7/Q8 的 A=2、Z=2；Q11~Q14 各 Z=2。
    for qid in ("Q7", "Q8"):
        conn.execute("INSERT INTO supply VALUES(?,?,?)", (qid, "A", 2))
        conn.execute("INSERT INTO supply VALUES(?,?,?)", (qid, "Z", 2))
    for qid in ("Q11", "Q12", "Q13", "Q14"):
        conn.execute("INSERT INTO supply VALUES(?,?,?)", (qid, "Z", 2))
    services.set_received_a(conn, "2")  # 无已知卡，缺码卡恰 2 张甲


print("[1] 关键情形：联答可达集合恰为 {0, 2}")
conn = fresh_db()
build_012_instance(conn)
snap = joint.load_snapshot(conn)
r = joint.enumerate_joint(snap, "Q7", "A", "Q8", "A")
check("枚举完整", r["feasible"] and r["complete"], str(r)[:200])
check("可达集合恰为 {0,2}", r.get("joint_values") == [0, 2], str(r.get("joint_values")))
check("恰两个可行分配", r.get("solutions") == 2, str(r.get("solutions")))

# 暴力枚举交叉验证：直接枚举 2^4 全部卷型组合
mapping = {(p, pos, o): (q, so) for p, pos, o, q, so in snap["mapping"]}
raw = {}
for card, pos, opt in snap["raw"]:
    raw.setdefault(card, []).append((pos, opt))
target = {(q, so): c for q, so, c in snap["supply"]}
brute = set()
brute_solutions = 0
for bits in itertools.product(("A", "B"), repeat=4):
    if bits.count("A") != 2:
        continue
    totals = {}
    joint_n = 0
    for card, paper in zip(sorted(raw), bits):
        cells = [mapping[(paper, pos, opt)] for pos, opt in raw[card]]
        for cell in cells:
            totals[cell] = totals.get(cell, 0) + 1
        if ("Q7", "A") in cells and ("Q8", "A") in cells:
            joint_n += 1
    if all(totals.get(k, 0) == v for k, v in target.items()):
        brute.add(joint_n)
        brute_solutions += 1
check("暴力枚举集合一致", brute == {0, 2}, str(brute))
check("暴力枚举解数一致", brute_solutions == r["solutions"])
conn.close()

print("[2] 无缺失卡：唯一联答值，且份数/人数不符时无解")
conn = fresh_db()
conn.executemany(
    "INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES(?,?,?,?,?)",
    [("A", "P1", "1", "Q1", "A"), ("A", "P2", "1", "Q2", "A"),
     ("B", "P1", "1", "Q3", "A"), ("B", "P2", "1", "Q4", "A")],
)
conn.execute("INSERT INTO cards VALUES('K1','A',1)")
conn.executemany("INSERT INTO answers_raw VALUES(?,?,'1')", [("K1", "P1"), ("K1", "P2")])
conn.execute("INSERT INTO supply VALUES('Q1','A',1)")
conn.execute("INSERT INTO supply VALUES('Q2','A',1)")
services.set_received_a(conn, "1")
r = joint.enumerate_joint(joint.load_snapshot(conn), "Q1", "A", "Q2", "A")
check("无缺码卡单值 [1]", r["feasible"] and r["complete"] and r["joint_values"] == [1], str(r))
services.set_received_a(conn, "2")
r = joint.enumerate_joint(joint.load_snapshot(conn), "Q1", "A", "Q2", "A")
check("份数不符无解", not r["feasible"], str(r))
conn.close()

print("[3] 无法换算的分支被排除，整卡两分支都不可用则无解")
conn = fresh_db()
conn.execute("INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES('A','P1','1','Q1','A')")
conn.execute("INSERT INTO cards VALUES('X',NULL,0)")
conn.execute("INSERT INTO answers_raw VALUES('X','P1','1')")
conn.execute("INSERT INTO supply VALUES('Q1','A',1)")
services.set_received_a(conn, "1")
r = joint.enumerate_joint(joint.load_snapshot(conn), "Q1", "A", "Q2", "A")
check("乙分支无映射->强制甲可解", r["feasible"] and r["joint_values"] == [0], str(r)[:160])
# 再制造乙分支也无映射的题位作答
conn.execute("INSERT INTO answers_raw VALUES('X','P2','9')")
conn.commit()
r = joint.enumerate_joint(joint.load_snapshot(conn), "Q1", "A", "Q2", "A")
check("两分支都不可用判无解", not r["feasible"], str(r))
conn.close()

print("[4] 同卡同题重复：分支被排除")
conn = fresh_db()
# 甲卷 P1->Q1/A、P2->Q1/C（同一题号两个标准选项）；乙卷两题位映射到不同题号，不重复
conn.executemany(
    "INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES(?,?,?,?,?)",
    [("A", "P1", "x", "Q1", "A"), ("A", "P2", "x", "Q1", "C"),
     ("B", "P1", "x", "Q2", "A"), ("B", "P2", "x", "Q3", "A")],
)
conn.execute("INSERT INTO cards VALUES('D1',NULL,0)")
conn.executemany("INSERT INTO answers_raw VALUES(?,?,'x')", [("D1", "P1"), ("D1", "P2")])
conn.execute("INSERT INTO supply VALUES('Q2','A',1)")
conn.execute("INSERT INTO supply VALUES('Q3','A',1)")
conn.commit()
services.set_received_a(conn, "0")  # 甲分支同卡同题重复不可用 -> 只能全乙
r = joint.enumerate_joint(joint.load_snapshot(conn), "Q2", "A", "Q3", "A")
check("甲分支同卡同题重复被排除，乙可解", r["feasible"] and r["joint_values"] == [1], str(r)[:200])
# 让乙分支也重复 -> 两分支都不可用 -> 无解
conn.execute("UPDATE mapping SET qid='Q2' WHERE paper='B' AND pos='P2'")
conn.commit()
r = joint.enumerate_joint(joint.load_snapshot(conn), "Q2", "A", "Q3", "A")
check("两分支都重复判无解", not r["feasible"], str(r)[:160])
conn.close()

print("[5] 输入校验：同题、空选项、资料不全")
conn = fresh_db()
build_012_instance(conn)
try:
    joint.enumerate_joint(joint.load_snapshot(conn), "Q7", "A", "Q7", "B")
    check("同题应报错", False)
except joint.JointError:
    check("两道题相同报 JointError", True)
try:
    joint.enumerate_joint(joint.load_snapshot(conn), "Q7", "", "Q8", "A")
    check("空选项应报错", False)
except joint.JointError:
    check("选项为空报 JointError", True)
conn2 = fresh_db()
try:
    joint.enumerate_joint(joint.load_snapshot(conn2), "Q1", "A", "Q2", "A")
    check("空库应报错", False)
except joint.JointError:
    check("未导入作答报 JointError", True)
conn.close()
conn2.close()

print("[6] 指纹：任一依据变化即变；仅状态变化不影响指纹")
conn = fresh_db()
build_012_instance(conn)
s1 = joint.load_snapshot(conn)
s2 = joint.load_snapshot(conn)
check("相同数据指纹稳定", s1["fingerprint"] == s2["fingerprint"])
services.set_received_a(conn, "3")
s3 = joint.load_snapshot(conn)
check("实收份数变化指纹变", s3["fingerprint"] != s1["fingerprint"])
services.set_received_a(conn, "2")
conn.execute("UPDATE supply SET cnt=cnt+1 WHERE qid='Q11'")
conn.commit()
s4 = joint.load_snapshot(conn)
check("供数表变化指纹变（即便其他单元人数未变）", s4["fingerprint"] != s1["fingerprint"])
conn.close()

print("[7] 出结果前复核：停供/依据变化只提示重查")
conn = fresh_db()
build_012_instance(conn)
# 标记为供数中
from app.db import META_STATUS, set_meta  # noqa: E402
set_meta(conn, META_STATUS, "SUPPLYING")
conn.commit()
before = joint.load_snapshot(conn)
r = joint.enumerate_joint(before, "Q7", "A", "Q8", "A")
after_ok = joint.load_snapshot(conn)
check("无变化时复核通过", after_ok["fingerprint"] == before["fingerprint"])
# 模拟计算期间停供
set_meta(conn, META_STATUS, "PAUSED")
conn.commit()
qr = joint.run_joint_query(conn, "Q7", "A", "Q8", "A")
check("停供后 ok=False 且提示重查", (not qr["ok"]) and qr["paused"] and "重新" in qr["recheck_reason"],
      str(qr.get("recheck_reason")))
# 恢复供数。stale 分支模拟“计算期间数据被更正”：monkeypatch 枚举函数，
# 在 before 快照取完后、after 复核前改实收份数。
set_meta(conn, META_STATUS, "SUPPLYING")
conn.commit()
real_enum = joint.enumerate_joint


def slow_enum(snap, q1, o1, q2, o2):
    services.set_received_a(conn, "1")  # 模拟计算期间的更正提交
    return real_enum(snap, q1, o1, q2, o2)


joint.enumerate_joint = slow_enum
try:
    qr = joint.run_joint_query(conn, "Q7", "A", "Q8", "A")
finally:
    joint.enumerate_joint = real_enum
check("计算期间依据变化 -> stale 且只提示重查",
      (not qr["ok"]) and qr["stale"] and (not qr["paused"])
      and "重新查询" in qr["recheck_reason"], str(qr.get("recheck_reason")))
# 数据恢复后正常查询可达 {0,2}
services.set_received_a(conn, "2")
qr2 = joint.run_joint_query(conn, "Q7", "A", "Q8", "A")
check("依据稳定时正常出结果 {0,2}", qr2["ok"] and qr2["joint_values"] == [0, 2], str(qr2)[:160])
conn.close()

print("[8] 搜索未完成：节点上限触保护，complete=False 且不返回假的完整集合")
conn = fresh_db()
old_limit = joint.JOINT_NODE_LIMIT
joint.JOINT_NODE_LIMIT = 1  # 人为压到 1 个节点
try:
    build_012_instance(conn)
    r = joint.enumerate_joint(joint.load_snapshot(conn), "Q7", "A", "Q8", "A")
    check("超限时 complete=False", r["feasible"] and not r["complete"], str(r)[:160])
    check("返回已确认子集而非报错", isinstance(r["joint_values"], list))
finally:
    joint.JOINT_NODE_LIMIT = old_limit
conn.close()

print(f"\n联答求解器测试：通过 {P}，失败 {F}")
sys.exit(1 if F else 0)
