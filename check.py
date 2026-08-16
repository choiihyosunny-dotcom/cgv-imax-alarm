"""
CGV 용산아이파크몰 IMAX관 - 특정 영화 새 예매 날짜 감지 스크립트

실제 사이트 구조 (Playwright로 직접 접속해서 확인함, 2026-08-16):
- 페이지 상단에 "오늘 16 / 월 17 / 화 18 ..." 형태의 날짜 탭(button.dayScroll_scrollItem)이
  가로로 나열되어 있고, 하나를 클릭해야 그 날짜의 상영시간표가 화면에 그려지는 SPA 구조.
  영화 이름 옆에 날짜 텍스트가 붙어있는 구조가 아님 (기존 버전의 잘못된 가정).
- 날짜 탭 중 일부는 disabled 클래스(dayScroll_disabled)가 붙어 클릭 불가 (해당 극장 휴관일 등).
- 날짜 탭에는 ISO 날짜 값이 DOM에 없고 "오늘/내일/월/화.." + 숫자만 있다. 숫자는 보통 일(day)
  이지만 달이 바뀌는 지점에서는 "9.1"처럼 월.일 형태로 나온다.
  ★ 중요: CGV의 "오늘"은 시스템 날짜와 다를 수 있다. 심야 상영이 24:00~27:32처럼 표기되는 데서
  보듯 CGV 영업일은 자정을 넘겨 이어지므로, 한국시간 8/17 00:50에도 첫 탭이 아직 "오늘 16"이다.
  그래서 시스템 날짜를 0번 탭으로 가정하면 전체 날짜가 하루씩 밀린다(실제로 이 버그로 25일
  오픈을 26일이라고 잘못 알림 보낸 적 있음). 반드시 탭에 적힌 숫자를 읽어 날짜를 확정한다.
- 영화 하나당 상영시간표 전체가 [class*="accordion_container"] 안에 들어있고, 그 안에
  [class*="screenInfo_contentWrap"] 가 상영관 종류(IMAX관/4DX관/2D 등)별로 나뉘어 있다.
  각 회차는 button[class*="screenInfo_timeLink"]이고, 아직 예매가 열리지 않았거나 매진/종료된
  회차는 aria-disabled="true"가 붙는다. 좌석수가 보이며 클릭 가능한 회차만 aria-disabled="false".

동작 방식:
1. Playwright로 페이지를 열고, 비활성화되지 않은 날짜 탭을 순서대로 클릭한다.
2. 각 날짜에서 MOVIE_KEYWORD(오디세이)의 SCREEN_KEYWORD(IMAX관) 섹션 안에 실제로 예매
   버튼이 활성화된(aria-disabled="false") 회차가 하나라도 있는지 DOM에서 직접 확인한다.
   (단순히 "IMAX관" 텍스트가 보이는 것만으로는 True로 치지 않음 — 매진/예매종료/오픈 전
   회차만 나열된 경우는 False로 취급한다.)
3. state.json에는 "이미 예매 가능하다고 한 번이라도 확인/알림한 날짜" 목록만 누적 저장한다.
   이번 실행에서 True인데 그 목록에 없는 날짜만 "새로 열린 날짜"로 보고 알림을 보낸 뒤
   목록에 추가한다. 이미 목록에 있는 날짜(예: 오늘처럼 원래도 예매 가능했던 날짜)는
   매진→취소표 발생→재예매 가능처럼 상태가 왔다갔다해도 다시 알림을 보내지 않는다.
   (바로 직전 실행과만 비교하면 이런 매진/재오픈 반복 때마다 오탐 알림이 가기 때문에,
   "한 번이라도 True였던 날짜"를 영구히 기억하는 방식으로 바꿨다.)
   첫 실행(state.json이 아예 없을 때)은 현재 열려있는 날짜들을 조용히 기준선으로만
   저장하고 알림은 보내지 않는다.
4. 갱신된 목록을 state.json에 다시 저장한다 (GitHub Actions가 커밋해줌).

주의:
- CGV가 화면 구조(클래스명 등)를 바꾸면 DAY_TAB_SELECTOR나 아래 JS 안의 클래스 매칭을
  다시 맞춰야 한다. 클래스는 CSS 모듈 해시(__xxxxx)가 붙어 있어서 정확한 전체 클래스명 대신
  [class*="..."] 부분일치로 매칭해 해시가 바뀌어도 어느 정도 버티도록 했다.
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

URL = "https://cgv.co.kr/cnm/movieBook/cinema?siteNo=0013&siteNm=CGV%EC%9A%A9%EC%82%B0"
MOVIE_KEYWORD = os.environ.get("MOVIE_KEYWORD", "오디세이")
SCREEN_KEYWORD = os.environ.get("SCREEN_KEYWORD", "IMAX관")
STATE_FILE = "state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

DAY_TAB_SELECTOR = "button.dayScroll_scrollItem__IZ35T"
KST = timezone(timedelta(hours=9))

# 현재 선택된 날짜에서, MOVIE_KEYWORD 영화의 SCREEN_KEYWORD 상영관에 예매 버튼이
# 활성화된(클릭 가능한) 회차가 하나라도 있는지 DOM에서 직접 판단하는 스크립트.
HAS_BOOKABLE_SESSION_JS = """
([movieKeyword, screenKeyword]) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node, movieTitleNode = null;
  while ((node = walker.nextNode())) {
    if (node.textContent.includes(movieKeyword)) { movieTitleNode = node; break; }
  }
  if (!movieTitleNode) return false;

  let container = movieTitleNode.parentElement;
  while (container && !(container.className && container.className.toString().includes('accordion_container'))) {
    container = container.parentElement;
  }
  if (!container) return false;

  const contentWraps = Array.from(container.querySelectorAll('[class*="screenInfo_contentWrap"]'));
  const target = contentWraps.find((cw) => {
    const h3 = cw.querySelector('h3');
    return h3 && h3.innerText.includes(screenKeyword);
  });
  if (!target) return false;

  const buttons = Array.from(target.querySelectorAll('button[class*="screenInfo_timeLink"]'));
  return buttons.some((b) => b.getAttribute('aria-disabled') === 'false');
}
"""


def has_bookable_session(page) -> bool:
    return page.evaluate(HAS_BOOKABLE_SESSION_JS, [MOVIE_KEYWORD, SCREEN_KEYWORD])


def parse_tab_label(label: str):
    """탭 숫자 라벨에서 (월 or None, 일)을 뽑는다. '16' -> (None, 16), '9.1' -> (9, 1), '02' -> (None, 2)"""
    m = re.search(r"(?:(\d{1,2})\s*\.\s*)?(\d{1,2})\s*$", label.strip())
    if not m:
        raise ValueError(f"날짜 탭 라벨을 해석할 수 없습니다: {label!r}")
    month = int(m.group(1)) if m.group(1) else None
    return month, int(m.group(2))


def resolve_tab_dates(tabs):
    """탭에 적힌 숫자를 기준으로 각 탭의 실제 날짜를 확정한다.

    CGV의 첫 탭("오늘")은 시스템 날짜보다 하루 뒤처져 있을 수 있으므로, 시스템 날짜 주변에서
    첫 탭의 일(day)과 일치하는 날짜를 찾아 기준점(anchor)으로 삼는다. 탭은 연속된 날짜이므로
    이후는 하루씩 더해가되, 각 탭에 적힌 숫자와 어긋나지 않는지 검증한다.
    """
    today = datetime.now(KST).date()
    first_month, first_day = parse_tab_label(tabs[0]["label"])

    anchor = None
    for delta in (0, -1, 1, -2, 2):
        cand = today + timedelta(days=delta)
        if cand.day == first_day and (first_month is None or cand.month == first_month):
            anchor = cand
            break
    if anchor is None:
        raise ValueError(
            f"첫 날짜 탭({tabs[0]['label']!r})을 시스템 날짜({today}) 근처에서 특정하지 못했습니다."
        )

    dates = []
    for i, tab in enumerate(tabs):
        d = anchor + timedelta(days=i)
        month, day = parse_tab_label(tab["label"])
        if d.day != day or (month is not None and d.month != month):
            raise ValueError(
                f"날짜 계산이 탭 표시와 어긋납니다: {i}번 탭 표시={tab['label']!r}, 계산={d}. "
                "사이트 구조가 바뀌었을 수 있어 잘못된 알림을 막기 위해 중단합니다."
            )
        dates.append(d)
    return dates


READ_TABS_JS = """
() => Array.from(document.querySelectorAll('button[class*="dayScroll_scrollItem"]')).map((b) => {
  const numEl = b.querySelector('[class*="dayScroll_number"]');
  return { label: (numEl ? numEl.innerText : b.innerText).trim(), disabled: b.disabled };
})
"""


def check_all_dates(page) -> dict:
    page.wait_for_selector(DAY_TAB_SELECTOR, timeout=30000)
    tabs = page.evaluate(READ_TABS_JS)
    if not tabs:
        return {}

    dates = resolve_tab_dates(tabs)
    print("날짜 탭 해석 결과:",
          ", ".join(f"{t['label']}->{d}{'(휴관)' if t['disabled'] else ''}"
                    for t, d in zip(tabs, dates)))

    result = {}
    for i, (tab_info, d) in enumerate(zip(tabs, dates)):
        if tab_info["disabled"]:
            continue
        tab = page.locator(DAY_TAB_SELECTOR).nth(i)
        tab.click()
        # 클릭한 탭이 실제로 선택 상태가 될 때까지 기다린 뒤 내용을 읽는다
        # (이전 날짜의 화면을 잘못 읽는 것을 막기 위함).
        try:
            page.wait_for_function(
                """(idx) => {
                    const btns = document.querySelectorAll('button[class*="dayScroll_scrollItem"]');
                    return btns[idx] && btns[idx].className.includes('itemActive');
                }""",
                arg=i,
                timeout=10000,
            )
        except Exception:
            print(f"경고: {d} 탭이 선택 상태로 바뀌는 것을 확인하지 못했습니다.")
        page.wait_for_timeout(1500)
        result[d.isoformat()] = has_bookable_session(page)
    return result


def notify(message: str) -> None:
    if not NTFY_TOPIC:
        print("NTFY_TOPIC이 설정되지 않아 알림을 보낼 수 없습니다.")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": "CGV 용산 IMAX 예매 알림".encode("utf-8")},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def load_known_dates():
    """이미 예매 가능하다고 확인/알림한 날짜 집합을 불러온다.
    state.json이 아예 없으면 None(진짜 첫 실행)을 반환한다.
    예전 버전({날짜: bool} 형식)과도 호환: True였던 날짜는 이미 알려진 날짜로 간주한다.
    """
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return set()

    if isinstance(data, list):
        return set(data)
    if isinstance(data, dict):
        return {d for d, has in data.items() if has}
    return set()


def save_known_dates(known: set) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(known), f, ensure_ascii=False, indent=2)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
        )
        try:
            page.goto(URL, wait_until="networkidle", timeout=60000)
            current = check_all_dates(page)
        except Exception:
            print(f"오류 발생. 현재 URL: {page.url}, 제목: {page.title()}")
            page.screenshot(path="debug.png", full_page=True)
            with open("debug.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            print("디버그용 debug.png / debug.html 저장함.")
            raise
        finally:
            browser.close()

    if not current:
        print("날짜 탭을 하나도 찾지 못했습니다. 사이트 구조가 바뀌었을 수 있습니다. "
              "상태를 저장하지 않고 종료합니다.")
        return

    known = load_known_dates()
    is_first_run = known is None
    if is_first_run:
        known = set()

    new_dates = sorted(d for d, has in current.items() if has and d not in known)

    print(f"이미 알려진 날짜: {sorted(known)}")
    print(f"현재 예매 가능 상태: {current}")

    if is_first_run:
        known = {d for d, has in current.items() if has}
        print("첫 실행: 기준 상태만 저장하고 알림은 보내지 않음. 기준선:", sorted(known))
    elif new_dates:
        msg = (
            f"CGV 용산아이파크몰 IMAX '{MOVIE_KEYWORD}' 새 예매 날짜 오픈!\n"
            f"{', '.join(new_dates)}\n{URL}"
        )
        print("새로 예매 가능해진 날짜 발견 -> 알림 전송:", new_dates)
        notify(msg)
        known = known | set(new_dates)
    else:
        print("새로 예매 가능해진 날짜 없음.")

    save_known_dates(known)


if __name__ == "__main__":
    main()
