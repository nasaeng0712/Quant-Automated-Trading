# QAT 운영 런북 (최소판)

대상: 로컬 단일 사용자가 QAT를 **Paper(과거 데이터 재생)** 모드로 운영·점검할 때의 절차. Live는 **BLOCKED**이며 이 문서의 어떤 절차도 Live를 켜지 않는다.

> 범위: 소프트웨어 / Paper / 연구 인프라 운영. 수익성은 UNKNOWN, Alpha는 NOT PROVEN이다. 2026 holdout과 기존 일봉 Lockbox는 열지 않았다.

## 1. 전제와 한계

- **단일 프로세스 전용.** 서버(`python -m qat.ui serve`)는 한 프로세스만 상태 디렉터리(`state/`, `QAT_STATE_DIR`)를 쓴다. 두 프로세스가 같은 디렉터리를 동시에 쓰는 구성은 **지원하지 않는다**(파일 잠금·분산 합의를 구현하지 않았다). 한 호스트에서 한 번에 하나만 실행한다.
- 상태 파일은 모두 임시 파일 + fsync + `os.replace`로 **원자적 교체**한다(`state/safety_latches.json`, `recovery_state.json`, `safety_state.json`, `results/*_registry.json`, 실행 결과 JSON). 감사 로그(`state/audit_log.jsonl`)는 줄 단위 추가(append) + fsync다.
- 손상된 상태 파일은 **조용히 초기화하지 않는다**: 안전 잠금 파일은 격리(`*.corrupt-<ms>`) 후 `STATE_CORRUPT` 잠금으로 대체되어 Kill Switch가 유지되고, 다른 상태 파일은 시작 점검 BLOCK이 된다.
- 설정: `config/settings.yaml`(비용·위험 placeholder, 연구 증거에 해시가 묶여 있어 수정 금지), `config/ops.yaml`(운영 설정; `null` = NOT_CONFIGURED = 규칙이 있으나 비활성이며 PASS로 표시하지 않는다).

## 2. 시작과 시작 점검

```bash
python -m qat.ui serve --port 8765
```

시작 시 서버는 자체 점검을 수행한다(UI: **운영 → 상태·준비도 → 시작 점검**). 결과 `PASS / WARN / BLOCK`.

| 점검 | BLOCK 조건 |
|---|---|
| ops_config / settings | 설정 파일 없음·형식 오류·범위 위반 |
| live_config | `ops.mode: live` 또는 `settings project.mode: live` (Live 미지원) |
| state_dir_writable | 상태 디렉터리 쓰기 불가 |
| latches | 안전 잠금 파일 손상(`STATE_CORRUPT`) |
| state:recovery_state.json / safety_state.json | 손상 |
| audit_chain | 감사 로그 해시 체인 검증 실패(잘림·변조·중복·순번 누락) |
| broker_requirements | `snapshot.policy: broker_connected`인데 Broker 어댑터 없음 |

**BLOCK이면 세션 시작·주문 제출·재생 진행이 모두 거절**된다(409 fail-closed). Kill Switch 켜기만은 항상 동작한다.

## 3. 재시작 평가

재시작 때 서버는 이전 실행의 안전 상태(`state/safety_state.json`)와 복구 상태를 읽어 판정한다(UI: **운영 → 복구**).

- `FIRST_START`: 이전 기록 없음.
- `SAFE_RECOVERY`: 이전 실행이 정상 종료(`clean_shutdown`)했고 무결성 문제·Breach·불일치·미체결·잠금이 없다. (Paper 재생 세션은 복원하지 않고 처음부터 시작한다.)
- `MANUAL_INTERVENTION_REQUIRED`: 사유가 하나라도 있다(안전 잠금 활성, Kill Switch ON, 미체결 주문이 있는 비정상 종료, 무결성 문제, Reservation Breach, 대조 불일치, 감사 로그 손상, 복구 상태 `RECOVERY_REQUIRED/FLATTEN_SUBMITTED`). 사유는 UI에 모두 표시된다.

정상 종료는 Ctrl+C(서버가 `close()`로 `clean_shutdown`을 기록). 강제 종료 후 미체결 주문이 있었다면 "order_state_unknown_after_restart"가 된다.

## 4. Kill Switch와 안전 잠금

- 켜기: UI 리스크 화면 또는 `POST /api/paper/kill-switch`. 켜면 영구 잠금이 기록되고 모든 신규 주문(감소 주문 포함)이 차단된다. **자동 청산은 일어나지 않는다.**
- 해제: UI에는 없다. 오프라인 검토 후에만:

```bash
python -m qat.ui latches
```

```bash
python -m qat.ui clear-latch --id KILL_SWITCH-1 --approver "이름" --note "검토 내용"
```

## 5. 긴급 청산 (Emergency Flatten)

운영자가 **명시적으로** 실행할 때만 동작한다(UI: 운영 → 복구, 또는 `POST /api/recovery/flatten`). 자동 실행 경로는 없다.

1. 입력: 운영자 이름, 사유, 확인 문구 `FLATTEN`(그대로). 그 외 필드는 서버가 거절한다(수량·가격·방향·승인값은 서버가 원장으로 결정).
2. 서버가 장부를 점검한다. **회계 무결성 문제 / 대조 불일치 / 비정상 Breach / 비유한 값 / 평가가격 없음** 중 하나라도 있으면 아무것도 매도하지 않고 상태를 `RECOVERY_REQUIRED`로 바꾸며 "수동 개입 필요"를 반환한다.
3. 통과하면 미체결 주문을 먼저 취소(예약 해제)하고, 보유 롱 포지션을 **보유 수량 이하**로만 시장가 매도한다(노출을 늘리거나 반대 포지션을 만드는 주문은 거절). 이 경로는 일반 주문 파이프라인과 분리된 권한 객체(서버만 발급)를 쓰며, Kill Switch는 **그대로 유지**된다.
4. 체결은 다음 재생 봉 시가에 일어난다(재생 진행 필요). 모든 포지션이 0이 되면 `FLATTENED`.
5. 같은 세션에서 재요청하면 기록된 결과를 그대로 돌려준다(주문을 다시 만들지 않는다 — 멱등).

상태 기계: `NORMAL → (요청) → FLATTEN_SUBMITTED → FLATTENED`, 신뢰 불가 시 `→ RECOVERY_REQUIRED`. `RECOVERY_REQUIRED`와 `FLATTEN_SUBMITTED`는 영속되며 새 세션·새 주문을 막는다. 해제는 오프라인 확인뿐이다:

```bash
python -m qat.ui ack-recovery --approver "이름" --note "장부 확인 내용"
```

(이력과 감사 로그에 `recovery_acknowledged`로 기록된다.) `FLATTENED` 이후 새 세션을 시작하면 `NORMAL`로 돌아간다.

## 6. 감사 로그

- 위치 `state/audit_log.jsonl`. 한 줄 = 한 이벤트. 필드: `seq`, `prev_hash`, `hash`(= sha256(prev_hash + 정규화 JSON)), `event_id`, `event_type`, 시각, 세션, 주체, 출처, 주문/제안 ID, Gate 결과, 상태 전이 등.
- 비밀값(키·토큰·`serviceKey=` 등)은 저장 전에 `<REDACTED>`로 가려진다. data.go.kr 키는 어떤 로그·산출물에도 저장하지 않는다.
- 같은 `event_id`+같은 내용은 멱등(재기록 없음), 다른 내용은 충돌 거절.
- 읽기 전용 UI/API: `GET /api/audit?offset&limit&session_uid&event_type&order`. 경로·자유 질의는 받지 않는다. 검증이 실패하면 이벤트를 **내보내지 않고** 오류를 보여준다.
- 기록 실패 시 저장소가 FAULT가 되어 다음 거래 동작이 거절된다(주문 처리 도중에 예외를 던지지는 않는다).
- 손상 대응: 서버 중지 → 파일을 **삭제·수정하지 말고** 복사 보관 → 원인 조사 → 새 로그로 시작하려면 파일을 별도 보관 폴더로 옮긴 뒤(운영자 판단) 재시작. 보관 사실을 기록한다.

## 7. 운영 상태 요약과 준비도 평가

UI **운영 → 상태·준비도** (`GET /api/operations`). 구성요소: Data, Ledger, Reconciliation, Risk, Compliance, Integrity, Strategy Health, Audit, Broker, Startup, Recovery(+ 참고: Research, Paper, Live). 상태: `PASS / DEGRADED / BLOCKED / UNKNOWN / NOT_CONFIGURED`. NOT_CONFIGURED와 UNKNOWN은 PASS로 승격되지 않는다. Paper 재생에는 Broker 계좌가 없어 Reconciliation은 UNKNOWN이다.

- **Strategy Health**(HEALTHY/DEGRADED/UNHEALTHY/UNKNOWN): 운영 상태만(초기화, 데이터 신선도, 평가 heartbeat, 오류 연속, 거절 집중, 상태 이월). 거래 없음은 실패가 아니다. 성과를 보지 않으며 전략을 바꾸거나 끄지 않는다.
- **Paper 졸업 평가**(GRADUATED/NOT_READY/BLOCKED/UNKNOWN): Paper 시스템의 **운영 준비도**일 뿐 Alpha·수익성 증명이 아니다. 기준값(`config/ops.yaml paper_graduation`)은 소유자가 정의하지 않아 **잠정 공학 수치**다. Protocol v2가 `NOT_HOLDOUT_ELIGIBLE`이므로 검증된 전략 조건(G5)은 정직하게 미충족이며 결과는 NOT_READY다.
- **Live 준비도**: 항상 BLOCKED. 실제 거절 지점(라우터 LIVE, Live Broker stub)을 호출해 확인하며, 설정·UI·증거 입력으로 바꿀 수 없다.

## 8. 규칙 설정 (R-07/08/09, MI-05/06)

`config/ops.yaml market_rules`에서 임계값을 지정하면 활성화된다(`null` = NOT_CONFIGURED).

| 규칙 | 의미 | 필요 데이터 | 현재 데이터 지원 |
|---|---|---|---|
| R-07 유동성 부족 | 기준 봉 거래량 < 최소 → 위험 증가 주문 BLOCK | 봉 거래량 | 지원 |
| R-08 비정상 Spread | 호가 스프레드(bp) > 한도 → BLOCK | **호가(quote)** | **미지원 → 설정 시 UNKNOWN** |
| R-09 변동성 충격 | (고가-저가)/종가 > 한도 → BLOCK | 봉 OHLC | 지원 |
| MI-05 Cancel/Replace | 심볼별 취소 횟수 > 한도(창 내) → BLOCK | 서버 취소 이력 | 지원 |
| MI-06 유동성 참여율 | 주문 수량/봉 거래량 > 한도 → BLOCK | 봉 거래량 | 지원 |

R-07/08/09는 위험 증가 주문에만 적용되고 노출 감소 SELL은(장부가 신뢰될 때) 막지 않는다(동결된 청산 위계). Kill Switch는 항상 최상위다. 임계값은 placeholder이며 경험적으로 보정되지 않았다.

## 9. Stale Snapshot 정책

- `snapshot.policy: paper_replay`(현재 기본): Broker 계좌가 없다. 신선도는 해당 없음이고 대조는 UNKNOWN으로 유지한다(PASS로 만들지 않는다).
- `snapshot.policy: broker_connected`: Broker 스냅샷의 **소스 시각** 나이가 `max_age_seconds`를 넘으면 STALE → 위험 증가 주문 BLOCK, 시각이 없거나 미래이거나 형식이 잘못되면 UNKNOWN → BLOCK. STALE/UNKNOWN 스냅샷은 대조를 통과시키지 못하고 이전 FRESH 대조 결과를 덮어쓰지도 못한다. 장부가 신뢰되고 마지막 FRESH 대조가 일치했을 때만 노출 감소 주문이 허용된다. 재연결은 대조를 무효화하며 새 FRESH 스냅샷이 일치해야 풀린다.
- Broker 어댑터는 인터페이스+Fake 어댑터(계약 테스트용)만 있고 실제 SDK·자격 증명은 없다.

## 9-1. Broker 경계 실패 처리 (계약 수준)

타임아웃/연결 끊김/거부/부분체결/중복 응답·콜백/스냅샷 stale/불일치 체결/재연결을 다룬다. `UNKNOWN` 주문 상태는 **종결 상태가 아니다**(예약 유지, 회계 무결성 이슈 기록, 신규 노출 거부). `client_order_id`(= Order id)로 재시도해도 두 번째 주문이 생기지 않는다. 체결은 `broker_fill_id`로 중복 제거 후 검증(주문·방향·유한 양수·누적 수량)하고 Ledger에 반영한다. 어댑터는 Risk/Compliance 결정을 하지 않는다.

## 10. 일상 점검 체크리스트

1. 시작 점검 `PASS/WARN`(BLOCK 아님), 재시작 평가 확인.
2. 운영 화면 종합 상태와 가장 심각한 구성요소.
3. 감사 로그 무결성 PASS, 기록 수 증가.
4. 활성 안전 잠금 / 복구 상태.
5. Live가 BLOCKED로 표시되는지.

## 11. 하지 않는 것 (범위 밖)

실거래·실 Broker 연결·자격 증명, 2026 holdout 읽기·평가, 기존 일봉 Lockbox, 수익성/Alpha 증명, Protocol v2 재개, 다중 프로세스/다중 호스트 운영.
