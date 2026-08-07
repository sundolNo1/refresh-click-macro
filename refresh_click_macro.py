#!/usr/bin/env python3
"""주기적 새로고침 + 좌표 클릭 매크로.

지정한 주기마다 활성 창을 새로고침(키 입력)하고, 잠시 대기한 뒤
지정한 화면 좌표를 클릭하는 동작을 반복한다.

중단 수단: ESC 키 / Ctrl+C / 마우스를 화면 좌측 상단 모서리로(FAILSAFE).
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime

import pyautogui

# pyautogui 내부 자동 지연이 타이밍에 끼어들지 않게 한다.
pyautogui.PAUSE = 0
# 마우스를 화면 좌측 상단(0, 0)으로 옮기면 예외를 발생시켜 즉시 중단한다.
pyautogui.FAILSAFE = True

# ESC 등으로 중단 요청이 들어오면 set 되는 전역 이벤트.
_stop_event = threading.Event()


def _ts() -> str:
    """밀리초까지 포함한 현재 시각 문자열을 반환한다."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(message: str) -> None:
    """타임스탬프를 붙여 한 줄 로그를 출력한다."""
    print(f"[{_ts()}] {message}", flush=True)


def start_esc_listener() -> bool:
    """ESC 키 감시 리스너를 데몬 스레드로 시작한다.

    pynput 설치 여부에 따라 성공/실패를 bool 로 반환한다.
    설치되어 있지 않으면 프로그램을 죽이지 않고 False 를 반환한다.
    """
    try:
        from pynput import keyboard
    except Exception:
        return False

    def on_press(key: "keyboard.Key") -> None:
        if key == keyboard.Key.esc:
            _stop_event.set()

    listener = keyboard.Listener(on_press=on_press, daemon=True)
    listener.start()
    return True


def find_position(seconds: float = 20.0) -> None:
    """지정 시간 동안 현재 마우스 좌표를 한 줄 갱신하며 출력한다."""
    print(f"좌표 확인 모드 — {int(seconds)}초 동안 마우스를 클릭할 위치로 옮기세요.")
    print("(Ctrl+C 로 조기 종료)\n")
    end = time.perf_counter() + seconds
    last_x, last_y = 0, 0
    try:
        while time.perf_counter() < end:
            last_x, last_y = pyautogui.position()
            remaining = end - time.perf_counter()
            sys.stdout.write(
                f"\r현재 좌표:  x={last_x:<5}  y={last_y:<5}   남은 시간 {remaining:4.1f}s "
            )
            sys.stdout.flush()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    print("\n")
    print(f"이 좌표를 -x {last_x} -y {last_y} 에 넣어 실행하세요.")
    print(f"예) python refresh_click_macro.py -x {last_x} -y {last_y}")


def parse_key(key: str) -> list[str]:
    """'ctrl+r' 같은 조합키 문자열을 키 리스트로 파싱한다."""
    return [part.strip().lower() for part in key.split("+") if part.strip()]


def press_key(keys: list[str]) -> None:
    """단일 키 또는 조합키를 입력한다."""
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)


def run(
    x: int,
    y: int,
    interval: float,
    wait: float,
    keys: list[str],
    clicks: int,
    limit: int,
    delay: float,
) -> None:
    """새로고침 → 대기 → 클릭 사이클을 반복 실행한다.

    주기 기준점은 '새로고침 시점'이며, perf_counter 절대 시각 기반으로
    스케줄링해 타이밍 드리프트를 방지한다.
    """
    key_label = "+".join(keys)
    print("=" * 52)
    print(" 새로고침 + 좌표 클릭 매크로")
    print("=" * 52)
    print(f" 클릭 좌표   : ({x}, {y})")
    print(f" 새로고침 키 : {key_label}")
    print(f" 새로고침 주기: {interval}s")
    print(f" 클릭 대기   : {wait}s (새로고침 후)")
    print(f" 클릭 횟수   : {clicks}")
    print(f" 반복 한도   : {'무제한' if limit == 0 else limit}")
    print("-" * 52)
    print(" 중단: ESC / Ctrl+C / 마우스를 좌측 상단 모서리로")
    print("=" * 52)

    # 시작 전 카운트다운 — 대상 창을 활성화할 시간을 준다.
    for remaining in range(int(delay), 0, -1):
        if _stop_event.is_set():
            log("시작 전 중단되었습니다.")
            return
        sys.stdout.write(f"\r{remaining}초 후 시작합니다... 대상 창을 클릭해 활성화하세요. ")
        sys.stdout.flush()
        _stop_event.wait(timeout=1.0)
    print("\n")

    if wait >= interval:
        log(f"경고: --wait({wait}) 가 --interval({interval}) 이상입니다. 타이밍이 밀릴 수 있습니다.")

    count = 0
    next_run = time.perf_counter()
    try:
        while not _stop_event.is_set():
            if limit and count >= limit:
                break
            count += 1

            # --- 새로고침 (주기 기준점) ---
            press_key(keys)
            log(f"[{count}] 새로고침({key_label}) 전송")

            # --- 로딩 대기 (중단 즉시 반응) ---
            if _stop_event.wait(timeout=wait):
                break

            # --- 좌표 클릭 ---
            pyautogui.click(x=x, y=y, clicks=clicks)
            log(f"[{count}] 클릭 ({x}, {y}) x{clicks}")

            if limit and count >= limit:
                break

            # --- 다음 새로고침 시각까지 대기 (드리프트 방지) ---
            next_run += interval
            now = time.perf_counter()
            sleep_for = next_run - now
            if sleep_for < 0:
                # 이미 예정 시각이 지났으면 현재 시각으로 리셋해 따라잡는다.
                log(f"주기 초과({-sleep_for:.3f}s 지연) — 스케줄 리셋")
                next_run = now
                continue
            if _stop_event.wait(timeout=sleep_for):
                break
    except KeyboardInterrupt:
        print()
        log("Ctrl+C 감지 — 정상 종료합니다.")
        return
    except pyautogui.FailSafeException:
        print()
        log("FAILSAFE 감지(마우스 좌측 상단) — 정상 종료합니다.")
        return

    if _stop_event.is_set():
        log("ESC 감지 — 정상 종료합니다.")
    else:
        log(f"반복 한도({limit}회) 도달 — 종료합니다.")


def build_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 구성한다."""
    parser = argparse.ArgumentParser(
        prog="refresh_click_macro.py",
        description="주기적으로 새로고침한 뒤 지정 좌표를 클릭하는 매크로.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pos", action="store_true", help="좌표 확인 모드 (20초간 마우스 좌표 출력)")
    parser.add_argument("-x", type=int, help="클릭할 화면 x 좌표")
    parser.add_argument("-y", type=int, help="클릭할 화면 y 좌표")
    parser.add_argument("--interval", type=float, default=59.99, help="새로고침 주기(초)")
    parser.add_argument("--wait", type=float, default=2.0, help="새로고침 후 클릭까지 대기(초)")
    parser.add_argument("--key", type=str, default="f5", help="새로고침 키. 'ctrl+r' 처럼 +로 조합")
    parser.add_argument("--clicks", type=int, default=1, help="클릭 횟수")
    parser.add_argument("--limit", type=int, default=0, help="총 반복 횟수 (0이면 무제한)")
    parser.add_argument("--delay", type=float, default=5.0, help="시작 전 대기(초)")
    return parser


def main() -> None:
    """진입점: 인자를 파싱하고 모드에 맞게 실행한다."""
    parser = build_parser()
    args = parser.parse_args()

    if args.pos:
        find_position()
        return

    if args.x is None or args.y is None:
        parser.error("클릭할 좌표가 필요합니다. -x 와 -y 를 지정하거나, --pos 로 좌표를 먼저 확인하세요.")

    if start_esc_listener():
        log("ESC 중단 활성화됨.")
    else:
        log("안내: pynput 미설치 — ESC 중단 비활성. (Ctrl+C 또는 FAILSAFE 사용)")

    run(
        x=args.x,
        y=args.y,
        interval=args.interval,
        wait=args.wait,
        keys=parse_key(args.key),
        clicks=args.clicks,
        limit=args.limit,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
