# -*- coding: utf-8 -*-
"""
국가법령정보 Open API로 감시 대상 법령의 공포일·시행일을 조회해
law-data.json(현재 상태)과 law-history.json(바뀐 기록)을 만듭니다.

- GitHub Actions가 매일 실행합니다. 직접 고칠 일은 거의 없습니다.
- 인증값(OC)은 코드에 쓰지 않고 GitHub 저장소의 Secret(LAW_OC)에서 읽습니다.
- 표준 라이브러리만 씁니다(추가 설치 없음).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.strftime("%Y%m%d")
BASE = os.environ.get("LAW_API_BASE", "https://www.law.go.kr/DRF/lawSearch.do")
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "laws-config.json")
DATA = os.path.join(HERE, "law-data.json")
HISTORY = os.path.join(HERE, "law-history.json")

OC = os.environ.get("LAW_OC", "").strip()
REFERER = os.environ.get("LAW_REFERER", "").strip()


def norm(name):
    """법령명 비교용: 띄어쓰기와 가운뎃점 차이를 없앱니다."""
    return "".join(name.split()).replace("ㆍ", "·").replace("・", "·")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def call(params):
    q = dict(params)
    q["OC"] = OC
    q["type"] = "XML"
    url = BASE + "?" + urllib.parse.urlencode(q)
    headers = {"User-Agent": "Mozilla/5.0 (law-monitor; GitHub Actions)"}
    if REFERER:
        headers["Referer"] = REFERER
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as res:
        body = res.read().decode("utf-8", errors="replace")
    if "검증에 실패" in body or "인증" in body[:300] and "<law" not in body:
        raise RuntimeError("인증값(OC) 확인 필요: " + " ".join(body.split())[:160])
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise RuntimeError("응답을 읽지 못함: " + " ".join(body.split())[:160])
    return root


def text(el, *tags):
    for t in tags:
        v = el.findtext(t)
        if v:
            return v.strip()
    return ""


def rows(root):
    out = []
    for el in root.iter("law"):
        out.append({
            "name": text(el, "법령명한글", "법령명"),
            "mst": text(el, "법령일련번호", "MST"),
            "lawId": text(el, "법령ID"),
            "ancYd": text(el, "공포일자"),
            "ancNo": text(el, "공포번호"),
            "efYd": text(el, "시행일자"),
            "kind": text(el, "제개정구분명", "제개정구분"),
            "dept": text(el, "소관부처명"),
            "lawType": text(el, "법령구분명"),
            "status": text(el, "현행연혁코드"),
        })
    return out


def find_current(name):
    root = call({"target": "law", "query": name, "display": 100})
    want = norm(name)
    hits = [r for r in rows(root) if norm(r["name"]) == want]
    if not hits:
        raise RuntimeError("법령명을 찾지 못함 (이름이 바뀌었거나 폐지되었을 수 있음)")
    hits.sort(key=lambda r: (r["ancYd"], r["efYd"]), reverse=True)
    return hits[0]


def find_upcoming(name):
    """시행일이 오늘 이후인 개정(시행예정). 조회에 실패해도 전체 작업은 계속합니다."""
    try:
        root = call({"target": "eflaw", "query": name, "display": 100, "nw": "2"})
    except Exception:
        return []
    want = norm(name)
    seen, out = set(), []
    for r in rows(root):
        if norm(r["name"]) == want and r["efYd"] > TODAY and (r["ancYd"], r["efYd"]) not in seen:
            seen.add((r["ancYd"], r["efYd"]))
            out.append({k: r[k] for k in ("ancYd", "ancNo", "efYd", "kind")})
    out.sort(key=lambda r: r["efYd"])
    return out


def main():
    cfg = load_json(CONFIG, None)
    if not cfg:
        print("laws-config.json을 읽지 못했습니다.")
        sys.exit(1)
    prev = load_json(DATA, {})
    prev_laws = prev.get("laws", {})
    history = load_json(HISTORY, [])

    names = [n for g in cfg["groups"] for n in g["laws"]]
    result = {
        "fetchedAt": NOW.isoformat(timespec="seconds"),
        "ok": True,
        "message": "",
        "laws": {},
        "errors": {},
    }

    if not OC:
        result.update(ok=False, message="인증값(OC)이 없습니다. 저장소 Settings → Secrets and variables → Actions에 LAW_OC를 등록하세요.")
        result["laws"] = prev_laws
        write(result, history)
        print(result["message"])
        sys.exit(1)

    for name in names:
        try:
            cur = find_current(name)
            cur["upcoming"] = find_upcoming(name)
            old = prev_laws.get(name)
            if old and (old.get("ancYd"), old.get("mst")) != (cur["ancYd"], cur["mst"]):
                cur["changedAt"] = NOW.strftime("%Y-%m-%d")
                history.insert(0, {
                    "detectedAt": NOW.strftime("%Y-%m-%d"),
                    "name": name,
                    "fromAncYd": old.get("ancYd", ""),
                    "toAncYd": cur["ancYd"],
                    "ancNo": cur["ancNo"],
                    "efYd": cur["efYd"],
                    "kind": cur["kind"],
                })
            elif old and old.get("changedAt"):
                cur["changedAt"] = old["changedAt"]
            result["laws"][name] = cur
            print("확인:", name, cur["ancYd"], cur["kind"], "시행", cur["efYd"])
        except Exception as e:  # 한 법령이 실패해도 나머지는 계속
            result["errors"][name] = str(e)
            if name in prev_laws:
                result["laws"][name] = prev_laws[name]
            print("실패:", name, "-", e)
        time.sleep(0.4)

    if result["errors"]:
        n = len(result["errors"])
        result["ok"] = n < len(names)
        result["message"] = f"{len(names)}개 중 {n}개 조회 실패" if result["ok"] else "모든 법령 조회에 실패했습니다. 인증값과 API 신청 정보를 확인하세요."
    write(result, history[:300])
    print(result["message"] or "모든 법령 조회 완료")
    if not result["ok"]:
        sys.exit(1)


def write(result, history):
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
