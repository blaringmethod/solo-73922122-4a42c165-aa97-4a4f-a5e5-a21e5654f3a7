"""真实 HTTP 冒烟测试（与 Flask test_client 不同，走完整 WSGI/HTTP 栈）。"""
import os, sys, pathlib, urllib.request, urllib.parse, urllib.error, http.cookiejar, re

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5057")
SAMPLES = pathlib.Path("/workspace/samples")
ok = fail = 0

def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print("  ✓", name)
    else: fail += 1; print("  ✗", name, detail)

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def multipart(url, fields, files):
    boundary = "----bnd" + "x" * 12
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, data) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                 f"filename=\"{fname}\"\r\nContent-Type: text/csv\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return opener.open(req).read().decode()

def post(url, fields=None):
    data = urllib.parse.urlencode(fields or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    return opener.open(req).read().decode()

def get(url):
    return opener.open(url).read().decode()

def status_badge(html):
    m = re.search(r'badge s-(IMPORTED|VERIFIED|SUPPLYING|PAUSED)"', html)
    return m.group(1) if m else "?"

def flashes(html):
    return re.findall(r'flash (ok|error|warn)">([^<]+)', html)

# 1 全新流程
multipart(BASE + "/import/answers", {}, {"answer_file": ("answers.csv", (SAMPLES/"answers.csv").read_bytes())})
multipart(BASE + "/import/mapping", {}, {"mapping_file": ("mapping.csv", (SAMPLES/"mapping.csv").read_bytes())})
multipart(BASE + "/import/supply", {}, {"supply_file": ("supply.csv", (SAMPLES/"supply.csv").read_bytes())})
post(BASE + "/received", {"received_a": "6"})
check("初始 IMPORTED", status_badge(get(BASE+"/check")) == "IMPORTED")

html = post(BASE + "/check/run")
f = flashes(html)
check("核对通过", any("核对通过" in m for _, m in f), str(f))
check("状态 VERIFIED", status_badge(html) == "VERIFIED")
check("15 个单元全部一致", html.count("✓ 一致") == 15, str(html.count("✓ 一致")))
check("页面列出可行分配", "C009" in html and "C010" in html)

html = post(BASE + "/publish")
check("发布成功", any("已发布供数" in m for _, m in flashes(html)))
check("状态 SUPPLYING", status_badge(html) == "SUPPLYING")

# 2 无害更正 -> 继续供数
html = post(BASE + "/correct/paper", {"card_no": "C001", "paper": "A"})
check("无害更正继续供数", any("继续供数" in m for _, m in flashes(html)))
check("仍 SUPPLYING", status_badge(html) == "SUPPLYING")

# 3 破坏式更正 -> 立即暂停
html = post(BASE + "/correct/paper", {"card_no": "C006", "paper": "A"})
check("破坏式更正提示暂停", any("立即暂停" in m for _, m in flashes(html)))
check_page = get(BASE + "/check")
check("状态 PAUSED", status_badge(check_page) == "PAUSED")
check("实测出现偏差", "偏差" in check_page)

# 4 暂停期间重新核对仍无解
html = post(BASE + "/check/run")
check("无解核对提示原因", any("核对未通过" in m for _, m in flashes(html)))
check("保持 PAUSED", status_badge(html) == "PAUSED")

# 5 暂停期间更正（改回），仍保持暂停，需显式恢复
html = post(BASE + "/correct/paper", {"card_no": "C006", "paper": "B"})
check("暂停中更正不自动恢复", status_badge(html) == "PAUSED")
html = post(BASE + "/resume")
check("恢复供数", any("已恢复供数" in m for _, m in flashes(html)))
check("状态回到 SUPPLYING", status_badge(html) == "SUPPLYING")

# 6 实收份数更正 -> 暂停 -> 恢复
html = post(BASE + "/correct/received", {"received_a": "7"})
check("份数不符暂停", any("立即暂停" in m for _, m in flashes(html)))
check("状态 PAUSED", status_badge(html) == "PAUSED")
post(BASE + "/correct/received", {"received_a": "6"})
html = post(BASE + "/resume")
check("改回后恢复", any("已恢复供数" in m for _, m in flashes(html)))
check("状态 SUPPLYING", status_badge(html) == "SUPPLYING")

# 7 非法输入保存失败，沿用已提交状态
html = post(BASE + "/correct/paper", {"card_no": "C001", "paper": "丙"})
check("非法卷型被拒", any("更正失败" in m for _, m in flashes(html)))
check("状态未变 SUPPLYING", status_badge(html) == "SUPPLYING")
html = post(BASE + "/correct/received", {"received_a": "-1"})
check("负份数被拒", any("更正失败" in m for _, m in flashes(html)))
check("状态仍 SUPPLYING", status_badge(html) == "SUPPLYING")

# 8 篡改人数表无解
multipart(BASE + "/import/supply", {}, {"supply_file": ("b.csv", (SAMPLES/"supply_bad_counts.csv").read_bytes())})
html = post(BASE + "/check/run")
check("篡改人数核对不通过", any("不存在" in m or "核对未通过" in m for _, m in flashes(html)))

# 9 联答查询：恢复供数后用 {0,2} 专用样例走完整 HTTP 流程
multipart(BASE + "/import/supply", {}, {"supply_file": ("s.csv", (SAMPLES/"supply.csv").read_bytes())})
post(BASE + "/resume")
check("恢复供数 SUPPLYING", status_badge(get(BASE+"/check")) == "SUPPLYING")

multipart(BASE + "/import/answers", {}, {"answer_file": ("ja.csv", (SAMPLES/"joint_answers.csv").read_bytes())})
multipart(BASE + "/import/mapping", {}, {"mapping_file": ("jm.csv", (SAMPLES/"joint_mapping.csv").read_bytes())})
multipart(BASE + "/import/supply", {}, {"supply_file": ("js.csv", (SAMPLES/"joint_supply.csv").read_bytes())})
post(BASE + "/received", {"received_a": "2"})
check("联答样例导入后待核对", status_badge(get(BASE+"/check")) == "IMPORTED")

# 未发布前联答被守卫拦截
html = post(BASE + "/joint/query", {"q1": "Q7|A", "q2": "Q8|A"})
check("非供数中联答被拦", "仅在供数中" in html)

post(BASE + "/check/run")
post(BASE + "/publish")
check("联答样例发布供数", status_badge(get(BASE+"/joint")) == "SUPPLYING")

html = get(BASE + "/joint")
check("联答页候选含 Q7/A 与 Q8/A", "Q7|A" in html and "Q8|A" in html)
html = post(BASE + "/joint/query", {"q1": "Q7|A", "q2": "Q8|A"})
check("联答完整枚举提示", "完整枚举完成" in html, html[:200])
check("页面只列 0 人和 2 人", ("0 人" in html) and ("2 人" in html) and ("1 人" not in html), html[:200])

# 同题被拒
html = post(BASE + "/joint/query", {"q1": "Q7|A", "q2": "Q7|Z"})
check("同题查询被拒", "两道不同的题" in html)

# 无解：把实收份数改成不可能的值并恢复供数流程，先停供验证重查提示
post(BASE + "/correct/received", {"received_a": "4"})
check("份数更正触发停供", status_badge(get(BASE+"/joint")) == "PAUSED")
html = post(BASE + "/joint/query", {"q1": "Q7|A", "q2": "Q8|A"})
check("停供后联答只提示重查/被拦", "仅在供数中" in html)
# 恢复
post(BASE + "/correct/received", {"received_a": "2"})
post(BASE + "/resume")
check("恢复后可再查", "完整枚举完成" in post(BASE + "/joint/query", {"q1": "Q7|A", "q2": "Q8|A"}))

print(f"\nHTTP 冒烟结果：通过 {ok}，失败 {fail}")
sys.exit(1 if fail else 0)
