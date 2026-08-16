"""
CGV 용산아이파크몰 IMAX관 - 특정 영화 새 예매 날짜 감지 스크립트

실제 사이트 구조 (Playwright로 직접 접속해서 확인함, 2026-08-16):
- 페이지 상단에 "오늘 16 / 월 17 / 화 18 ..." 형태의 날짜 탭(button.dayScroll_scrollItem)이
  가로로 나열되어 있고, 하나를 클릭해야 그 날짜의 상영시간표가 화면에 그려지는 SPA 구조.
  영화 이름 옆에 날짜 텍스트가 붙어있는 구조가 아님 (기존 버전의 잘못된 가정).
- 날짜 탭 중 일부는 disabled 클래스(dayScroll_disabled)가 붙어 클릭 불가 (해당 극장 휴관일 등).
- 오디세이는 "IMAX관\nIMAX LASER 2D\n..." 형태로 상영관 섹션이 나타남.
- 날짜 탭에는 실제 날짜 값이 DOM에 없고 "오늘/월/화.." + 숫자만 있어서, 탭 순서(0번째=오늘,
  1번째=내일, ...)로 실제 날짜를 계산한다 (한국 시간 기준).

동작 방식:
1. Playwright로 페이지를 열고, 비활성화되지 않은 날짜 탭을 순서대로 클릭한다.
2. 각 날짜에서 MOVIE_KEYWORD가 등장하는 위치 주변에 SCREEN_KEYWORD(IMAX관)가 있는지 확인해서
   그 날짜에 오디세이 IMAX 상영이 있는지 여부를 기록한다.
3. 이전 실행 때 저장해둔 state.json(날짜별 상영 여부)과 비교해서, 새로 상영이 생긴 날짜가
   있으면 ntfy.sh로 푸시 알림을 보낸다. (첫 실행은 기준선만 저장하고 알림을 보내지 않는다.)
4. 현재 상태를 state.json에 다시 저장한다 (GitHub Actions가 커밋해줌).

주의:
- CGV가 화면 구조(클래스명 등)를 바꾸면 DAY_TAB_SELECTOR나 SCREEN_KEYWORD를 다시 맞춰야 한다.
"""

import json
import os
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


def has_movie_screen(page_text: str) -> bool:
    idx = page_text.find(MOVIE_KEYWORD)
    if idx == -1:
        return False
    window = page_text[idx : idx + 800]
    return SCREEN_KEYWORD in window


def check_all_dates(page) -> dict:
    page.wait_for_selector(DAY_TAB_SELECTOR, timeout=30000)
    count = page.locator(DAY_TAB_SELECTOR).count()
    today = datetime.now(KST).date()

    result = {}
    for i in range(count):
        tab = page.locator(DAY_TAB_SELECTOR).nth(i)
        if tab.get_attribute("disabled") is not None:
            continue
        tab.click()
        page.wait_for_timeout(1200)
        text = page.inner_text("body")
        d = today + timedelta(days=i)
        result[d.isoformat()] = has_movie_screen(text)
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


def load_prev_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        current = check_all_dates(page)
        browser.close()

    if not current:
        print("날짜 탭을 하나도 찾지 못했습니다. 사이트 구조가 바뀌었을 수 있습니다. "
              "상태를 저장하지 않고 종료합니다.")
        return

    prev = load_prev_state()
    is_first_run = len(prev) == 0

    new_dates = sorted(d for d, has in current.items() if has and not prev.get(d, False))

    print(f"이전 상태: {prev}")
    print(f"현재 상태: {current}")

    if is_first_run:
        print("첫 실행: 기준 상태만 저장하고 알림은 보내지 않음.")
    elif new_dates:
        msg = (
            f"CGV 용산아이파크몰 IMAX '{MOVIE_KEYWORD}' 새 예매 날짜 오픈!\n"
            f"{', '.join(new_dates)}\n{URL}"
        )
        print("새 날짜 발견 -> 알림 전송:", new_dates)
        notify(msg)
    else:
        print("새로운 날짜 없음.")

    save_state(current)


if __name__ == "__main__":
    main()
