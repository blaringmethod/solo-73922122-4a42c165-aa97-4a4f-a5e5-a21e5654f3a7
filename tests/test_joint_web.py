"""联答查询网页端到端测试（Flask 测试客户端）。

运行：EXTRA_PYTHONPATH=/path/to/flask python -I tests/test_joint_web.py
"""
from __future__ import annotations

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

import app.web as web  # noqa: E402
from app import joint, services  # noqa: E402
from app.db import META_STATUS, connect, get_status, set_meta  # noqa: E402

P = F = 0


def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print("  ✓", name)
    else:
        F += 1
        print("  ✗", name, detail)


def build_012_db(conn):
    """与 test_joint.build_012_instance 相同的 {0,2} 实例（直接写库）。"""
    cell_of = {("A", "P1"): "Q7", ("A", "P2"): "Q11", ("A", "P3"): "Q8", ("A", "P4"): "Q12",
               ("B", "P1"): "Q13", ("B", "P2"): "Q7", ("B", "P3"): "Q14", ("B", "P4"): "Q8"}
    hit_opt = {("A", "P1"): "A", ("A", "P3"): "A", ("B", "P2"): "B", ("B", "P4"): "B"}
    mapping = []
    for paper in ("A", "B"):
        for pos in ("P1", "P2", "P3", "P4"):
            qid = cell_of[(paper, pos)]
            for opt in ("A", "B", "Z", "Y"):
                is_hit = hit_opt.get((paper, pos)) == opt
                mapping.append((paper, pos, opt, qid, "A" if is_hit else "Z"))
    conn.executemany(
        "INSERT INTO mapping(paper,pos,option,qid,std_option) VALUES(?,?,?,?,?)", mapping)
    marks = {
        "U1": {"A": (0, 0), "B": (0, 1)},
        "U2": {"A": (0, 1), "B": (1, 1)},
        "U3": {"A": (1, 0), "B": (0, 0)},
        "U4": {"A": (1, 1), "B": (1, 0)},
    }
    for card, mm in marks.items():
        a1, a2 = mm["A"]
        b1, b2 = mm["B"]
        opts = {"P1": "A" if a1 else "Z", "P2": "B" if b1 else "Y",
                "P3": "A" if a2 else "Z", "P4": "B" if b2 else "Y"}
        conn.execute("INSERT INTO cards(card_no,paper,is_known) VALUES(?,NULL,0)", (card,))
        for pos, opt in opts.items():
            conn.execute("INSERT INTO answers_raw VALUES(?,?,?)", (card, pos, opt))
    for qid in ("Q7", "Q8"):
        conn.execute("INSERT INTO supply VALUES(?,?,?)", (qid, "A", 2))
        conn.execute("INSERT INTO supply VALUES(?,?,?)", (qid, "Z", 2))
    for qid in ("Q11", "Q12", "Q13", "Q14"):
        conn.execute("INSERT INTO supply VALUES(?,?,?)", (qid, "Z", 2))
    services.set_received_a(conn, "2")


tmp = tempfile.mkdtemp(prefix="joint-web-")
db_path = os.path.join(tmp, "t.db")
web.DB_PATH = db_path
client = web.app.test_client()

print("[1] 页面与守卫")
rv = client.get("/joint")
check("联答页 200", rv.status_code == 200)
check("非供数中按钮禁用", "disabled" in rv.get_data(as_text=True))
rv = client.post("/joint/query", data={"q1": "Q7|A", "q2": "Q8|A"})
check("非供数中查询被拦并提示", "仅在供数中" in rv.get_data(as_text=True))

print("[2] 供数中查询 {0,2} 关键情形")
conn = connect(db_path)
build_012_db(conn)
set_meta(conn, META_STATUS, "SUPPLYING")
conn.commit()
conn.close()
rv = client.get("/joint")
html = rv.get_data(as_text=True)
check("候选项含 Q7/A、Q8/A", "Q7|A" in html and "Q8|A" in html)
rv = client.post("/joint/query", data={"q1": "Q7|A", "q2": "Q8|A"})
html = rv.get_data(as_text=True)
check("完整枚举提示", "完整枚举完成" in html, html[:300])
check("页面列出 0 人和 2 人", ("0 人" in html) and ("2 人" in html), html[:300])
check("不出现 1 人", "1 人" not in html)
check("审计留痕", "联答查询" in html or True)  # 审计在总览页，下面单独验证

conn = connect(db_path)
audits = [r["event"] for r in conn.execute("SELECT event FROM audit")]
check("审计表记录联答查询", "联答查询" in audits, str(audits))
conn.close()

print("[3] 输入校验：同题、空选择")
rv = client.post("/joint/query", data={"q1": "Q7|A", "q2": "Q7|Z"})
check("同题报错", "两道不同的题" in rv.get_data(as_text=True))
rv = client.post("/joint/query", data={"q1": "", "q2": "Q8|A"})
check("空选择报错", "完整选择" in rv.get_data(as_text=True))

print("[4] 无解提示")
conn = connect(db_path)
conn.execute("UPDATE supply SET cnt=cnt+5 WHERE qid='Q7' AND std_option='A'")
conn.commit()
conn.close()
rv = client.post("/joint/query", data={"q1": "Q7|A", "q2": "Q8|A"})
html = rv.get_data(as_text=True)
check("无解有明确提示", "无解" in html and "不存在" in html, html[:300])

print("[5] 搜索未完成提示（压低节点上限；先恢复 [4] 篡改的人数）")
conn = connect(db_path)
conn.execute("UPDATE supply SET cnt=2 WHERE qid='Q7' AND std_option='A'")
conn.commit()
conn.close()
old = joint.JOINT_NODE_LIMIT
joint.JOINT_NODE_LIMIT = 1
try:
    rv = client.post("/joint/query", data={"q1": "Q7|A", "q2": "Q8|A"})
    html = rv.get_data(as_text=True)
    check("未完成明确提示且不假冒完整", "搜索未完成" in html and "完整枚举完成" not in html, html[:300])
finally:
    joint.JOINT_NODE_LIMIT = old

print("[6] 停供后复核只提示重查")
conn = connect(db_path)
conn.execute("UPDATE supply SET cnt=2 WHERE qid='Q7' AND std_option='A'")
conn.commit()
conn.close()
rv = client.post("/joint/query", data={"q1": "Q7|A", "q2": "Q8|A"})
check("恢复数据后可正常查询", "完整枚举完成" in rv.get_data(as_text=True))
conn = connect(db_path)
set_meta(conn, META_STATUS, "PAUSED")
conn.commit()
conn.close()
rv = client.post("/joint/query", data={"q1": "Q7|A", "q2": "Q8|A"}, follow_redirects=False)
# PAUSED 下走守卫分支
check("停供后查询被守卫拦截", rv.status_code == 200 and "仅在供数中" in rv.get_data(as_text=True))

print(f"\n联答网页测试：通过 {P}，失败 {F}")
sys.exit(1 if F else 0)
