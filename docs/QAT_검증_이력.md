# QAT 검증·변경 이력

> 이 문서는 기존 `VALIDATION_LOG.md`, `CLAUDE_BATCH_HANDOFF.md`, `GEMINI_AUDIT_PROMPT.md`를 통합한 기록이다. 구현 결과, 결함 재현, 회귀 테스트, AI 작업 역할, 다음 Batch를 이 문서에 누적한다.

---

## 1. 검증 원칙

QAT는 AI의 설명보다 **재현 가능한 실행 증거**를 우선한다.

```text
의심/감사 지적
→ 재현 테스트 작성
→ 실제 재현 여부 확인
→ 재현된 문제만 최소 수정
→ 회귀 테스트
→ 전체 테스트 재실행
```

판정 예:

- `PASS`: 요구조건을 검증 증거가 충족
- `FAIL`: 실제 결함 또는 요구조건 미충족
- `BLOCKED`: 환경/의존성 문제로 검증 불가
- `UNKNOWN`: 증거 부족
- `NOT REPRODUCED`: 제기된 문제를 지정 조건에서 재현하지 못함

테스트 통과는 **수익성, 법적 적합성, Live 안전성**을 자동으로 증명하지 않는다.

---

## 2. 2026-08-27 — Core Batch #1

### 범위

- Domain Model / Order State Machine
- Multi-Currency Ledger / Reconciliation
- Level-1 Paper Broker
- Risk Gate v0.1
- Cost Engine / Net Alpha Gate
- Compliance / Market Integrity
- Orchestrator / StrategyGateway
- Execution Router / Coordinator / Live Stub
- Audit Log

### 검증

- `tests/test_phase0.py`
- `tests/test_invariants.py`
- Offline / no network
- `pytest -q`
- `compileall`
- `pyflakes`
- Secret scan

### 결과

**35 tests PASS** — CPython 3.11.5 / 3.13.14.

확인된 핵심:

- Live Broker 실행 불가
- Strategy → Broker 직접 경로 없음
- Risk / Compliance BLOCK 우회 없음
- Partial Fill 회계
- Duplicate Fill idempotency
- 결정론적 Paper Mode
- Audit Log 기록
- Secret 미검출

판정: **Local PASS → Independent Audit 대상**.

---

## 3. 2026-08-27 — Fix Batch #1.1

독립 감사에서 나온 위험을 보완했다.

### FIX-01 — Multi-Currency Exposure

- `FXRateProvider`
- 기준통화 환산
- Missing FX → Risk `UNKNOWN` → 승인 안 함

### FIX-02 — Reservation

- APPROVED 시 BUY 현금 / SELL 수량 예약
- Partial Fill 비례 감소
- Cancel/Reject/Expire/Error 시 잔여 예약 해제

### FIX-03 — Cost Basis / Realized PnL

- BUY 취득비용을 Average Cost에 포함
- SELL은 Sell-side Cost만 추가 차감
- Slippage 이중 차감 금지

### FIX-04 — Terminal / Idempotency

- `processed_fill_ids`
- Duplicate Fill 무시
- Terminal Order Late Fill 차단

### FIX-05 — LIMIT Price Gate

- BUY: ref ≤ limit
- SELL: ref ≥ limit
- 조건 미충족은 `None`, 주문은 OPEN
- Slippage가 있어도 Limit보다 불리한 가격 금지

### 결과

**50 tests PASS** — CPython 3.11.5 / 3.13.14.

판정: **Local PASS → Re-audit 대상**.

---

## 4. 2026-08-27 — Verification #1.1-V

재감사에서 제기된 다섯 항목을 **수정 전에 먼저 재현**했다.

| 항목 | 결과 | 핵심 증거/판정 |
|---|---|---|
| V-01 LIMIT + Slippage 위반 | NOT REPRODUCED | Fill Price Clamp가 정상 동작 |
| V-02 Missing FX Fail-Closed 실패 | NOT REPRODUCED | Missing FX가 Risk `UNKNOWN`으로 연결되어 승인되지 않음 |
| V-03 Partial Fill + Cancel Reservation 오류 | NOT REPRODUCED | Partial 후 Cancel 시 예약금 정확히 해제 |
| V-04 Actual Cost > Reservation | **REPRODUCED** | 실제비용 10,201 > 예약 10,200에서 available cash 음수, 차단 없음 |
| V-05 Slippage Double Counting | NOT REPRODUCED | Slippage는 Fill Price에만 반영, 별도 현금비용으로 재차감되지 않음 |

### V-04 원인

예약 수수료 기준은 pre-slippage notional인데 Paper Broker의 실제 수수료는 slipped gross를 기준으로 계산해 작은 예약 부족이 발생할 수 있었다.

### V-04 수정

1. MARKET BUY 예약의 fee base를 slipped notional로 변경.
2. `PortfolioLedger.reservation_breaches` 추가.
3. 실제 Fill이 예약을 초과해도 Fill/비용은 그대로 기록.
4. Breach가 남아 있는 동안 `RiskGate`가 모든 신규 주문을 `reservation_breach`로 BLOCK.
5. Reconciliation 후에만 정상화할 수 있는 구조로 설정.

### 최종 결과

**57 tests PASS** — CPython 3.11.5 / 3.13.14, 연속 실행에서도 동일 결과. `compileall`, `pyflakes`, Secret scan도 당시 Clean.

판정: **Core v0.1 연구기반 기준 PASS**.

주의: 이 판정은 실제 시장 수익성이나 Live Ready 판정이 아니다.

---

## 5. 알려진 한계 (2026-08-27 기준 — 최신은 §11.8)

- Paper Broker는 Level-1이며 Order Book / Queue / Depth가 없음
- Fixed-BPS Slippage만 사용
- Latency 없음
- `fx_cost`의 실제 체결비용 모델 없음
- 실제 시장별 Cost 수치는 미보정
- Live cancel/fill race 미해결
- Historical Data / Backtest / Walk-Forward 미구현
- Strategy Health / Strategy Virtual Ledger 미구현
- 실제 Net Alpha는 UNKNOWN
- Live는 BLOCKED

---

## 6. AI 역할 분담

### ChatGPT — Architecture / Control Tower

- 요구사항과 경계 정의
- 다음 Batch 결정
- PASS/FAIL 기준 정의
- 구현 결과와 감사 결과 종합
- 새 복잡성이 실제로 필요한지 판단

### Claude — Main Implementation

- Repository 단위 구현
- 큰 Batch를 일관되게 처리
- 전체 테스트 실행
- 불필요한 설계 확장 금지
- 구현 중 설계 모순 발견 시 임의 우회보다 보고

### Codex — Code Review / Bug Reproduction / Test Strengthening

중요 Batch 이후 선택적으로 사용한다.

특히 다음을 공격한다.

- Look-ahead Leakage
- PnL / Cost 이중계산
- State / Reservation 오류
- Core Gate 우회
- 결정론 실패
- Edge Case 누락

### Gemini — Independent Red Team

- Architecture Bypass
- 잘못된 회계/PnL
- Risk/Compliance Fail-Open
- Duplicate / Conflicting Order
- 데이터 누수
- 문서와 코드 불일치
- 재현성 문제

AI가 제기한 문제는 바로 수정하지 않고 가능한 경우 **재현 우선** 원칙을 적용한다.

---

## 7. 독립 감사 체크리스트

중요 변경 후 다음을 재확인한다.

### Architecture

- Strategy가 Broker/Exchange를 직접 호출할 수 있는가?
- Risk / Compliance / Integrity를 건너뛰는 실행경로가 생겼는가?
- Live가 의도치 않게 활성화되었는가?

### Accounting

- BUY/Sell 비용이 정확히 한 번만 반영되는가?
- Duplicate Fill이 원장을 두 번 변경하는가?
- Partial Fill과 Cancel 후 Reservation이 일관적인가?
- Terminal Order가 Late Fill을 받는가?
- Missing FX가 추정값으로 조용히 통과하는가?

### Paper Execution

- LIMIT 가격조건이 실제로 지켜지는가?
- Slippage가 이중 차감되는가?
- 동일 입력에서 결과가 재현되는가?

### Research 단계 추가 체크

Batch #2 이후 반드시 추가한다.

- Future Data / Look-ahead Leakage
- Signal bar와 Execution bar 분리
- Train / OOS / Walk-Forward 오염
- Gross와 Net 성과 분리
- Cost Stress Test
- 실패한 전략/실험을 숨기거나 삭제하지 않는지
- Data/Code/Config/Seed Manifest 재현성

---

## 8. 다음 개발 Batch — Profit Research Foundation

현재 다음 핵심 작업은 **Historical Data + Backtest + Walk-Forward**다.

### Batch #2 목표

1. Historical Data Engine
   - OHLCV Loader
   - Timestamp / Duplicate / Gap / OHLC 검증
   - `data_version` / hash 기록
   - 우선 Local CSV/Parquet, 외부 Vendor API는 뒤로 미룸

2. Backtest Engine
   - 기존 Core Pipeline을 우회하지 않음
   - Proposal → Gates → Paper → Settlement → Ledger
   - Equity Curve / Trade Log

3. Look-ahead 방지
   - bar `t` 종료 후 Signal 생성
   - 가장 빠른 체결은 `t+1`
   - 현재 봉의 미래 High/Low 등을 이용한 체결 금지

4. Benchmark Strategy
   - Moving Average Trend
   - Breakout
   - Mean Reversion
   - 수익 보장 전략이 아니라 Research Pipeline 검증용 Control

5. Metrics
   - Starting / Ending Equity
   - Gross / Net PnL & Return
   - Trades / Win Rate
   - Avg Win / Loss
   - Expectancy
   - Profit Factor
   - Max Drawdown
   - Commission / Tax / Fee / Slippage
   - Turnover / Exposure Time

6. Walk-Forward
   - Train/Calibration → OOS Test → Roll
   - Fold 기간 저장
   - OOS를 보고 반복 튜닝하지 않도록 별도 Final Lockbox 고려

7. Reproducibility Manifest
   - run_id
   - code_commit
   - data_version/hash
   - strategy/parameters
   - cost/risk settings
   - initial capital
   - market/symbol/timeframe
   - train/test dates
   - random seed
   - git dirty 상태

### Batch #2에서 제외

- Live Broker / API Key
- 자동 Live Deployment
- Deep Learning / RL
- 자동 Online Retraining
- GUI (2026-09-30: 연구 코어 선행 순서로 반영 후 Batch #2에서 UI 구현)
- News AI
- 대규모 Parameter Sweep

---

## 9. 앞으로의 검증 흐름

```text
ChatGPT 설계/Acceptance Criteria
→ Claude 구현
→ 전체 pytest
→ 필요 시 Codex 독립 코드 공격
→ 실제 데이터 실험
→ 필요 시 Gemini Red Team
→ 재현된 결함만 수정
→ Regression Test
→ 다음 Batch 결정
```

새 기능을 많이 만드는 것보다 **실제 Alpha가 존재하는지 반증 가능한 방식으로 검증하는 것**을 우선한다.

---

## 10. 이력 추가 규칙

앞으로 새 Batch마다 이 문서 맨 아래에 다음 형식으로 기록한다.

```text
날짜:
Batch/Commit:
목적:
변경 파일:
검증 명령:
테스트 결과:
발견 결함:
재현 여부:
수정 내용:
남은 UNKNOWN:
최종 판정:
다음 작업:
```

기능별로 별도 Markdown 로그를 새로 만들지 않는다.

---

## 11. 2026-09-30 — Batch #2 (Fix #2.0 + Profit Research Foundation + UI)

```text
날짜: 2026-09-30
Batch/Commit: Batch #2.0 결함 수정 + Batch #2 연구 파이프라인 + UI. Git 저장소 아님 → code_commit unavailable (소스 트리 SHA-256으로 식별)
목적: 인계 정적 검토 후보(가~사) 재현·수정, Historical Data → Backtest → OOS/Walk-Forward → Paper 재생 흐름과 로컬 UI 구현
```

### 11.1 기준선

- 입력: `Quant Automated Trading.zip` SHA-256 `0676EC98F7C674EE694C319AC390DC42886A0E2E01DE86E946ED8EE688CEB412` **일치**, 디렉터리 제외 120개 파일, 작업 폴더와 바이트 단위 동일(차이 0). `.pyc`/`egg-info`는 근거로 사용하지 않고 `__pycache__`를 삭제한 뒤 새 환경에서 실행.
- 환경: Windows 11 Pro, 새 venv **CPython 3.11.5**, pytest 8.4.2, PyYAML 6.0.3 (선언 범위 내). 보조 검증: **CPython 3.13.14** 별도 venv(동일 의존성). 정적 검사용 pyflakes는 venv에만 설치(프로젝트 의존성 아님).
- 기준 테스트: `python -m pytest -q` → **57 passed** (기록과 일치).
- 환경 사실: IANA tz DB 없음(zoneinfo `America/New_York` 실패), `pyarrow` 없음, Node.js 없음, Git 2.55 있음(저장소 아님).

### 11.2 인계 후보 재현 결과 (수정 전 코드)

재현 스크립트·출력: `artifacts/verification/batch2_0_prefix_repro.py` / `.txt`, 회귀 테스트: `tests/test_fix_batch_2_0.py`.

| ID | 후보 | 판정 | 수정 전 증거 | 조치 |
|---|---|---|---|---|
| 가-1 | 기본 설정 경로 `parents[2]`가 `src/config` 지칭 | **NOT REPRODUCED** (소스 배치) | 루트 `config/settings.yaml`로 해석, 존재 | 비편집 설치 시 경로가 달라질 수 있어 연구는 엄격 로딩 사용 |
| 가-2 | 설정 파일 누락 시 `{}` 대체 | REPRODUCED | 명시 경로 누락 → `{}` | 명시 경로 누락 = `FileNotFoundError`, `load_research_settings()` 엄격 검증 |
| 가-3 | 설정 비용이 Paper Broker 체결에 미연결 | REPRODUCED | 설정 Stack 체결 commission 0, 가격=기준가 | 시장별 CostModel을 Broker·예약에 연결(D-019) |
| 가-4 | `project.mode`/`markets` 미반영 | REPRODUCED | `mode: live` → Router PAPER, US 비활성인데 Compliance 통과(USD 현금 부족으로 우연히 Risk 차단) | mode → Router/Compliance, markets → `market_disabled` BLOCK. `live_enabled: true`는 경고만(해제 불가) |
| 나-1 | `realized_pnl_total()` 1.0 대체 | REPRODUCED | USD 10, 환율 없음 → 10.0 | 누락 시 `MissingFXRateError` |
| 나-2 | `equity()` 1.0 대체 | REPRODUCED | KRW 1000 + USD 10 → 1010 | 누락 시 오류 |
| 나-3 | 평가가격 누락 시 평균원가 대체 | REPRODUCED (`equity_in`) | 평가가격 없이 1210 | 연구·UI용 엄격 `valuation()` 추가, 기존 경로는 한계로 문서화 |
| 다-1 | 여유 현금이 있으면 예약 초과 미기록 | REPRODUCED | 예약 10,201 < 실제 20,402, breach 0 | 조각 대비 초과 시 기록(D-020) |
| 다-2 | 부분체결 초과 미기록 | REPRODUCED | 조각 4,080.4 < 실제 6,120.6, breach 0 | 동일 |
| 다-3 | 예약 이내 체결 | 결함 아님 확인 | breach 0, 잔여 예약 0 | — |
| 라-1~3 | 내부 전용 포지션/현금, 빈 snapshot을 일치로 판정 | REPRODUCED | 세 경우 모두 `ok=True` | 합집합 비교, `snapshot_empty`/`snapshot_incomplete` (D-021) |
| 마-1 | Fill-주문 불일치 반영 | REPRODUCED | 다른 종목 Fill `APPLIED` | `REJECTED_MISMATCH` |
| 마-2 | 정산 경로 과체결 | REPRODUCED | 주문 10에 11 반영 | 누적 체결 검사 |
| 마-3 | 실패한 SELL 전에 비용 집계 변경 | REPRODUCED | fees 1, taxes 2 남음 | 검증 후 변경 |
| 마-4 | 원장 실패 전에 예약 해제 | REPRODUCED | 예약 수량 10 → 0 | 검증 → 원장 → 예약 순서 (D-022) |
| 마-5 | 동시 제출 이중 예약 | **NOT REPRODUCED** (8 스레드, CPython 3.11.5) | 1건만 승인 | 방어적으로 Orchestrator lock 추가, UI 멱등 키 |
| 바-1 | NaN/Inf 통과 | REPRODUCED | NaN 수량·가격·환율 생성, **NaN 기준가 주문 승인**, inf 기대수익 승인 | 도메인·Validator·Broker·FX에서 차단 (D-023) |
| 바-2 | UUID·시각 비결정 | REPRODUCED (설계상) | 실행마다 ID 상이 | `id_fn`/`now_fn` 주입, Backtest 감사 로그까지 동일 (D-028) |
| 사-1 | 정산·PnL 감사 미연결 | REPRODUCED | `fill_settled` 없음 | Settlement 감사 기록 |
| 사-2 | 신규 진입 경로의 Gate 우회 | 테스트 추가 | — | Backtest 감사 단계 수 = 주문 수, UI 미허용 필드 400, Kill Switch |

### 11.3 신규 구현

- `src/qat/data/` — bars(계약), loader(CSV/Parquet), validation(수정 없는 검증·공백 분류), synthetic(결정론적 fixture). `data/fixtures/SYN_KR1_1d`, `SYN_US1_1d`, `SYN_CRYPTO1_1h` (+meta, 모두 PASS).
- `src/qat/research/` — strategies(3종, 인과적 History), backtest(Core 파이프라인 경유), metrics, walkforward(+lockbox, cost stress), manifest, store, CLI.
- `src/qat/ui/` — service, server, static(index.html/app.css/app.js), CLI(serve/latches/clear-latch).
- 기존 코어 변경: models, fx, validator, orchestrator, ledger, reconciliation, settlement, paper broker, compliance, risk, audit, config, app (모두 최소 수정, 기존 API 유지).
- 기타: `pyproject.toml`(package-data), `.gitignore`(state/, results/), `.claude/launch.json`(UI 미리보기), `results/.gitkeep`.

### 11.4 검증 명령과 결과

| 명령 | 환경 | 결과 |
|---|---|---|
| `python -m pytest -q` | 3.11.5 | **150 passed** (기존 57 변경 없음 + fix 38 + data 19 + research 22 + ui 14) |
| `python -m pytest -q` | 3.13.14 (새 venv) | **150 passed** |
| `python -m compileall -q src tests` | 3.11.5 | OK |
| `python -m pyflakes src tests` | 3.11.5 | clean |
| 비밀정보 패턴 검색(api key/secret/password/private key/token 형식) | — | 검출 없음 |
| `node --check app.js` | — | **BLOCKED** (Node 미설치). 대신 브라우저에서 전 화면 실행, 콘솔 오류 0 |
| 변이 검사: 전략에 1봉 미래 노출(`History(t+2)`) | 3.11.5 | spy 테스트가 검출(FAIL). 미래 가격 교란 테스트는 breakout만 검출 — MA/MR에는 약함(한계로 기록) |

수작업 대조 예(`test_hand_computed_round_trip_matches_ledger_and_metrics`): 수량 4,843, 매수 102.102(시가 102 × 1.001), 매도 105.894, 수수료 494.479986 / 512.844642, 세금 1,025.689284 → Net 16,331.642088 = 원장 실현손익, Gross 18,364.656 = 매도−매수 체결금액, 명시 비용 2,033.013912, 슬리피지 기록 1,007.344(재차감 없음).

### 11.5 UI 확인 증거

- 앱 내장 브라우저(Chromium 계열)에서 `python -m qat.ui serve` 실행 후 실제 클릭으로 확인: 세션 시작 → 주문 제안(**더블클릭 → 주문 1건**) → Net Alpha 거절(사유 표시) → 다음 봉 체결 → 연구 화면에서 Backtest 실행·저장·재조회 → Walk-Forward 실행·Fold 표 표시.
- UI 값 ↔ 원장 대조(실행 중 API): 평가액 = 현금 + 수량×평가가격, 체결가 슬리피지 7bp = KR ½스프레드 5 + 슬리피지 2, 수수료율 0.015%, 평균원가 = (체결금액+수수료)/수량, 미실현 = (평가가격−평균원가)×수량 — 모두 일치.
- 375×812 / 1366×900에서 6개 화면 모두 가로 넘침 0(표는 내부 스크롤), 데스크톱은 사이드바·2~4열 그리드. 모든 입력·버튼에 접근 가능한 이름, 내비게이션 키보드 포커스 가능.
- 확인 중 발견·수정한 UI 결함: MI-02/03(상시 검사)이 "미설정"으로 표시, 차단 사유 묶음 라벨 과다, 그리드 간격, 주문유형 선택지 잘림, 라디오 접근성 이름.
- 캡처: `artifacts/verification/ui_overview_mobile_375.jpg`, `ui_overview_desktop_1366_crop.jpg` (창이 숨겨진 상태의 캡처 한계로 데스크톱은 일부 영역).
- Kill Switch는 UI 클릭 대신 서비스·HTTP 테스트로 검증(확인 대화상자, 프로젝트 `state/` 오염 방지): 새로고침·새 세션·서비스 재시작 후에도 차단 유지, UI 해제 경로 없음, 해제에는 승인자·메모 필요.

### 11.6 연구 관찰 (합성 데이터 — 실제 성과 아님)

- empirical 기대수익에서 **청산 제안이 Net Alpha Gate에 반복 차단**(예: SYN_KR1 `ma_trend` 1회 진입 후 청산 거절 189회, 포지션 잔존). 현재 계약을 유지하고 사용자 결정 항목 OD-01로 올림.
- FIXTURE(1%) 모드에서는 26회 체결·13회 왕복, 원장 항등식(실현+미실현 = Net = 거래 현금흐름 합) 성립, 비용 배수 1/2/3에서 Net 수익률 단조 감소. 파이프라인·회계 검증이며 Alpha 증거가 아니다.

### 11.7 판정

| 항목 | 판정 |
|---|---|
| 코드·UI 구현 (오프라인, 합성 fixture 기준) | **PASS** |
| 실제 데이터 연구 | **BLOCKED** — 실제 시장 데이터 없음 (입력 경로는 준비됨) |
| 수익성(지속 가능한 Net Alpha) | **UNKNOWN** |
| Paper 졸업 | **NO** (기준 미정 — UNKNOWN) |
| Live Ready | **NO — Live BLOCKED 유지** |

### 11.8 남은 결함·UNKNOWN·BLOCK

- 사용자 결정: OD-01(청산 SELL의 Net Alpha 적용), OD-02(종목 검증 목록 미설정 시 PASS), OD-03(예약 버퍼 2%).
- 미구현/한계: 휴장일 캘린더, US 장중 세션 분류(tz DB 필요 — `tzdata` 추가는 의존성 결정 필요), 영속 감사 저장소(UI 세션), 실시간 시세·Market Recorder, L2/L3·지연·시장충격, MI-05/MI-06, R-07~09, Strategy Virtual Ledger/Health, Live cancel/fill race, 기존 `equity_in/exposure_in`의 평균원가 대체.
- 기존 UNKNOWN 유지: Broker/Exchange, Data Vendor, Risk Limit, Capital Allocation, Paper 졸업 기준, Initial Live Capital, Universe, ML, Net Alpha.
- 법령·거래소 규정·Broker 약관 확인은 수행하지 않았다(소프트웨어 PASS ≠ 법적 적합성).

### 11.9 이어받기

```text
설치: py -3.11 -m venv .venv → .venv\Scripts\activate → pip install -r requirements.txt → pip install -e .
검증: python -m pytest -q            (기대 150 passed)
UI:   python -m qat.ui serve --port 8765  → http://127.0.0.1:8765
연구: python -m qat.research backtest --data data/fixtures/SYN_KR1_1d.csv --strategy ma_trend
다음 우선순위:
  1) OD-01~03 사용자 결정 반영
  2) 실제 로컬 데이터(data/raw + meta.json) 투입 → validate-data → Walk-Forward(최초 1회 독립 OOS) → Lockbox 1회
  3) Batch #2 독립 감사(Codex/Gemini): 누수·회계·Gate 우회·UI 우회 공격
  4) 휴장일 캘린더 / 영속 감사 저장소 필요성 판단
```

---

## 12. 2026-09-30 — Batch #2 Independent Audit & Defect Correction

```text
날짜: 2026-09-30
Batch/Commit: Batch #2 독립 감사 (Git 저장소 아님 → code_commit unavailable)
목적: Batch #2 구현이 설계·안전 원칙·회계 불변조건·재현성 요구를 실제로 만족하는지 독립 검증, 재현된 결함만 최소 수정
변경 파일: 아래 §12.6
검증 명령: python -m pytest -q (3.11.5 / 3.13.14), compileall, pyflakes, 비밀정보 검색, 프로브 5종, 변이 17종, 실서버 UI/API 스모크
최종 판정: 재현된 결함 10건(D1~D10) 수정 + 테스트 실효성 결함 1건(T1) 보강. 설계 결정 OD-01~03은 변경하지 않음.
```

### 12.1 Baseline (수정 전)

| 항목 | 결과 |
|---|---|
| Python 3.11.5 (.venv) | **150 passed** |
| Python 3.13.14 (별도 venv) | **150 passed** |
| compileall / pyflakes / 비밀정보 검색 | PASS / clean / 검출 없음 |
| Git | 저장소 아님. `results/runs` 기존 결과 2건은 삭제·덮어쓰기 없이 유지 |

### 12.2 감사 범위와 방법

A 주문·Gateway·Broker / B Ledger·회계 / C FX / D Reconciliation / E Risk·Compliance·Integrity·UI 표시 / F 데이터 / G 누수 / H Walk-Forward·Lockbox / I Cost Stress / J UI·API 신뢰 경계.
방법: 코드 정독 → 가설별 프로브(`artifacts/verification/audit_batch2/probe1~5.py`, 수정 전 관측 결과 `prefix_findings.txt`) → 재현된 것만 수정 → 회귀 테스트 `tests/test_audit_batch2.py`(92개). 미구현 규칙(MI-05/06, R-07~09, L2/L3, 지연, Strategy Health, 휴장일)은 구현하지 않고 NOT IMPLEMENTED 상태와 UI 표시 일치만 확인.

### 12.3 CONFIRMED (수정함)

| ID | 결함 | 재현(수정 전) | 수정 | 회귀 테스트 |
|---|---|---|---|---|
| D1 | Reconciliation이 NaN 잔고·수량·tol을 일치로 판정 (`abs(diff) > tol`이 NaN에서 False) | broker cash/position NaN → `ok=True`, `tol=NaN` → `ok=True` | 비유한 값은 `non_finite` 불일치, 비정상 tol은 ValueError | `test_d1_*`, `test_reconciliation_case_matrix` |
| D2 | API 페이로드: `Infinity`가 `int()`에서 OverflowError(HTTP 500), `true`가 수량 1로 승인 | `start_bar/bars/train_bars = Infinity` → 500, `quantity=true` → accepted | `_to_float/_to_int` (bool·NaN·Inf·비정수 거부), 금액 상한 < 1e15, 배수 목록 검증 | `test_d2_*` (HTTP `Infinity`/`NaN` 리터럴 → 400 포함) |
| D3 | 전략 파라미터 무검증 (window 0 → ZeroDivisionError, 음수/1.5/NaN/문자열 처리 불명확) | `fast=0` ZeroDivisionError, `fast=-5`·`1.5`·`horizon=0` 조용히 실행 | `Strategy.__init__`에서 정수 ≥ 1 / 유한 실수 검증, 정수형 float 정규화 | `test_d3_*` |
| D4 | **Drawdown 한도 무력화 (fail-open)**: 기준 equity가 관측되지 않으면 한도가 조용히 PASS. (a) Backtest에서 FX가 없으면 한도 1%에 실제 DD 16.3%, 체결 27건·거절 0건 (b) UI Paper 세션은 equity를 RiskGate에 전달하지 않음 | 프로브 1·4 | RiskGate: 한도 설정 + 기준 없음 → `UNKNOWN(drawdown_baseline_unknown)`. UI 세션이 매 봉 `observe_equity` 호출 | `test_d4_*` |
| D5 | UI 세션의 일일 손실 기준이 리셋되지 않음 (세션 누적으로 동작, Backtest와 불일치, fail-closed 방향의 과차단) | 손실 후 다음 날 BUY도 `daily_loss_limit` BLOCK | 날짜 변경 시 `start_new_day()` (Backtest와 동일) | `test_d5_*` |
| D6 | 데이터 입력: 겹치는 봉이 PASS, KR/US 심볼의 `/` 허용, `source=SYNTHETIC`인데 `synthetic=false` 허용(합성이 실제로 표시될 수 있음), 잘못된 tz 오프셋·비UTF-8·깨진 sidecar가 DataError가 아닌 다른 예외 | 프로브 2 | `overlapping_bars` ERROR(일봉은 DST 1시간 허용), 시장-심볼·출처-합성 일관성 검사, 모든 입력 오류를 `DataError`로 | `test_d6_*` |
| D7 | 전략 인스턴스를 재사용하면 이전 실행의 봉이 새 실행에 섞임 (ma_trend에서 fresh와 체결 상이) | 프로브 3 | `run_backtest`가 시작 시 `strategy.reset()` | `test_d7_*` |
| D8 | Lockbox one-shot 우회: 기록 키가 정확한 구간이라 700–750을 잡으면 이미 본 650–750이 독립으로 표시 | 프로브 2 | 같은 data_version의 겹치는 구간 사용 이력은 전략과 무관하게 재사용으로 판정(승인 없이는 거부) | `test_d8_*` |
| D9 | UI 요청 캐시 500건 이후 proposal_id 충돌 → 이후 모든 제안이 `duplicate_proposal`로 차단, 캐시에서 밀린 요청 ID의 멱등성 상실 | 프로브 2 (502번째 요청) | 단조 카운터 + 본 적 있는 ID 집합(결과가 밀려도 재실행 안 함) | `test_d9_*` |
| D10 | HTTP 서버가 Host/Origin을 검증하지 않음 (DNS rebinding 페이지가 로컬 API를 same-origin처럼 호출 가능) | `Host: evil.example`로 GET/POST(kill-switch 포함) 200 | 루프백 바인딩이면 Host가 루프백일 때만, POST의 Origin도 루프백/미존재만 허용(403) | `test_d10_*` |
| T1 | **테스트 실효성 부족(false confidence)**: 학습창 +50봉 오염 변이가 생존(오염 테스트가 거래 0건이라 공허), 미래 교란 테스트는 breakout만 검출 | 변이 검사 | fixture 모드로 거래가 있는 구간에서 결정 필드만 비교하는 탐지기 + 탐지기 자체 검증(일부러 미래 노출/학습창 확대 시 탐지) + 비공허 가드 | `test_leakage_*`, `test_train_test_contamination_*`, `test_oos_fold_never_sees_*` |

감사 중 내 탐지기 초안이 정상 엔진을 "미래 의존"으로 오판(체결 필드 비교)한 것을 발견해 결정 필드 비교로 수정했다(제품 결함 아님).

### 12.4 NOT REPRODUCED / 결함 없음 확인

- 동시 정산 이중 반영: 6 스레드 × 300회(switch interval 1µs) → 0건. (CPython 3.11.5, 한정된 범위)
- 동시 동일 제안(요청 ID만 다름) 12개 → 1건만 승인(Integrity 중복 차단 + Orchestrator lock). 동시 제출 이중 예약 없음.
- 회계 불변식: 무작위 다중통화(KRW/USD, 부분체결·취소·수수료·세금·슬리피지·예약) 60회(프로브) / 25회(회귀) — 현금 흐름, equity = 현금 + 포지션, 실현 + 미실현 = Net, 수수료 합, 예약 잔액(현금·수량), 기준통화 합산 모두 위반 0.
- 1e308 초기자본 실행 저장 후 조회: 200 (NaN 직렬화 문제 없음).
- FX: 한도 설정 + FX 누락 → UNKNOWN(`fx_unavailable`), 한도 없는 동일 통화 거래는 승인(설계 일치), 합산값은 None (1.0 대체 없음).
- Fill 불일치·과체결·실패한 SELL 원자성·NaN 가격/수량: Batch #2.0 회귀로 이미 고정됨(재확인 통과).
- 파이프라인 순서(Validator → Net Alpha → Risk → Compliance → Integrity)와 첫 비-PASS에서 중단, 이후 Gate 미실행: 감사 기록으로 확인. Kill Switch는 BUY/SELL/LIMIT 모두 차단.
- Cost Stress: 수수료·매도세·½스프레드+슬리피지가 체결 단위(체결가 bp, 비용률)로 실제 배수 적용됨을 개별 확인. 변이(세율만 배수 제외)도 검출.
- 결과 저장: Manifest의 data sha256/data_version이 원본 파일과 일치, 재조회가 파일을 변경하지 않음. data_version은 경로 무관·내용 기반.

### 12.5 UNKNOWN / NOT IMPLEMENTED

- **Stale snapshot**: `reconcile()`는 snapshot 시각을 받지 않아 오래된 snapshot을 판별할 수 없다. 설계에 명세가 없어 구현하지 않음 → UNKNOWN (실제 Broker 연동 시 필요).
- 수정 전 코드에서 확인하지 못한 것: 서로 다른 프로세스가 같은 `results/`·`state/`를 동시에 쓰는 경쟁(파일 읽기-수정-쓰기 비원자) — 범위 밖, 미검증.
- 관찰(변경 없음): RiskGate의 포트폴리오 한도(일일 손실·Drawdown)와 Kill Switch는 **SELL(위험 축소)도 차단**한다. 설계상 "Risk 우선"과 일치하지만 OD-01과 같은 성격의 정책 문제이므로 결정 시 함께 검토 권장.
- 여전히 NOT IMPLEMENTED: MI-05, MI-06, R-07~R-09, L2/L3·지연, Strategy Health/Virtual Ledger, 휴장일 캘린더, 영속 UI 감사 로그. 문서·UI 모두 미구현으로 표시됨을 확인.

### 12.6 변경 파일

- 수정: `src/qat/portfolio/reconciliation.py`, `src/qat/risk/gate.py`, `src/qat/research/strategies.py`, `src/qat/research/backtest.py`, `src/qat/research/walkforward.py`, `src/qat/data/bars.py`, `src/qat/data/validation.py`, `src/qat/data/loader.py`, `src/qat/ui/service.py`, `src/qat/ui/server.py`
- 추가: `tests/test_audit_batch2.py`, `artifacts/verification/audit_batch2/*`
- 문서: README, 통합 설계·운영 문서, 이 문서

### 12.7 최종 검증

| 명령 | 환경 | 결과 |
|---|---|---|
| `python -m pytest -q` | 3.11.5 | **242 passed** (기존 150 + 감사 92) |
| `python -m pytest -q` | 3.13.14 | **242 passed** |
| `python -m compileall -q src tests` | 3.11.5 | OK |
| `python -m pyflakes src tests` | 3.11.5 | clean |
| 비밀정보 패턴 검색 | — | 검출 없음 |
| 변이 17종 (`mutate_leakage_and_defects.py`) | 3.11.5 | **17/17 검출** (미래 1봉 노출, 종료 슬라이스 +1, 미래 수익 사용, 학습창 확대, OOS 창 침범, Lockbox 침범, 동일봉 종가 체결, D1·D4·D5·D6·D7·D8·D9·D10 재도입, 세금 비스케일) |
| 실서버 UI/API 스모크 (앱 내장 브라우저) | — | 6개 화면 렌더 오류 0·가로 넘침 0, 세션 없음 409, `Infinity`/`true`/위조 필드 400, 멱등 재요청, UI 값 = 원장(평가액·미실현·슬리피지 7bp·수수료 0.015%), MI-02/03 "항상 적용" 표시, Live BLOCKED, Strategy Health NOT_IMPLEMENTED |

Kill Switch 조작은 프로젝트 `state/`에 잠금이 남으므로 실서버에서 누르지 않고 격리 디렉터리 기반 테스트로 검증했다(`state/`는 비어 있음).

### 12.8 미해결 사용자 결정 (변경하지 않음)

- **OD-01**: 청산 SELL에도 Net Alpha Gate 적용 유지. 현재 코드 실측(합성, empirical): KR ma_trend 진입 1회 후 청산 거절 189회·미청산 144주, KR breakout 40회, US breakout 48회, US mean_reversion 41회, CRYPTO breakout 118회·mean_reversion 619회 — 모두 포지션이 남고 종가 평가로 종료. 영향 분석만 수행.
- **OD-02**: 종목 허용 목록 미설정 시 전 종목 허용 유지. UI는 "미설정"으로 표시.
- **OD-03**: 예약 버퍼 2% 유지. 갭이 버퍼를 넘으면 Breach → 이후 주문 차단(Backtest·UI 모두 재확인).
- 위 세 가지 모두 감사 진행을 막지 않았다.

### 12.9 이어받기

```text
검증: python -m pytest -q            (기대 242 passed)
변이: python artifacts/verification/audit_batch2/mutate_leakage_and_defects.py   (소스를 임시 변경 후 원복; 실행 전 백업 권장)
다음 우선순위:
  1) OD-01~03 사용자 결정 (OD-01 결정 시 Risk의 SELL 차단 여부도 함께)
  2) 실제 로컬 데이터 투입 → validate-data → Walk-Forward(독립 OOS 1회) → Lockbox 1회
  3) stale snapshot 정책(Reconciliation) — 실제 Broker 연동 전 결정
```

---

## 13. 2026-09-30 — Batch #2.1 Policy Closure (OD-01 / OD-02 / OD-03)

```text
날짜: 2026-09-30
Batch/Commit: Batch #2.1 (Git 저장소 아님 → code_commit unavailable)
목적: Control Tower 결정 OD-01~03을 구현·테스트·문서에 반영 (신규 기능·실제 데이터 연구·Broker·Live 없음)
변경 파일: §13.6
검증 명령: python -m pytest -q (3.11.5 / 3.13.14), compileall, pyflakes, 비밀정보 검색, 변이 28종, 실서버 UI/API 스모크
테스트 결과: 242 → 277 passed (기존 242개 삭제·약화 없음)
최종 판정: OD-01 / OD-02 / OD-03 CLOSED (코드·테스트 PASS 이후). Risk-reducing exit hierarchy는 OPEN POLICY.
```

### 13.1 OD-01 — 노출 축소 주문의 Net Alpha 면제 (CLOSED)

- 구현: `NetAlphaGate(ledger=...)`의 `reduces_exposure()`가 **Ledger의 포지션**으로 판정. 파이프라인 순서 변경 없음. 면제 시 `net_alpha_exempt:exposure_reducing` + 계산된 `expected_net_alpha`/`cost_fraction`을 감사 기록.
- 조건: Long 보유 + SELL + `quantity ≤ 가용(미예약) 수량`. 부호 규칙을 일반화(Short 보유 + BUY + `quantity ≤ |보유|`), 현재 Short는 미지원.
- 면제 아님: BUY, 포지션 없는 SELL, 보유·가용 초과 SELL, 이미 예약된 수량. 101/100 SELL은 Net Alpha 적용 후 Risk `insufficient_position` 차단 → flip·신규 Short·exposure 증가 없음(포지션 불변·주문 미생성 테스트).
- 클라이언트 위조: `TradeProposal`에 exit/reduce 필드 없음(`TypeError`), UI는 `exit`/`reduce_only`/`reducing`/`net_alpha_exempt`/`is_exit`를 400으로 거부, `reason_code="exit"`는 무효.
- 유지되는 것: 제안 검증, 실행 비용(체결에서 수수료·세금 부과 확인), Risk(Kill Switch·주문금액 한도), Compliance(`trading_halt`), Integrity(`opposing_order`), Settlement/Ledger.
- 효과(합성 fixture, empirical, 정책 전 → 후 청산 거절): KR ma_trend 189 → 0 (왕복 8, 미청산 0), KR breakout 40 → 0, US breakout 48 → 0, US mean_reversion 41 → 0, CRYPTO breakout 118 → 0, CRYPTO mean_reversion 619 → 0 (왕복 3, 미청산 0). 남은 Net Alpha 거절은 전부 진입(ENTER_LONG) 제안.

### 13.2 OD-02 — 종목 universe Fail-Closed (CLOSED)

- 코어: `ComplianceGate.DEFAULT_UNRESTRICTED_UNIVERSE = False`. 허용 목록 없음 + 명시적 opt-in 없음 → `UNKNOWN(tradable_universe_not_configured)`(LIVE에서는 BLOCK으로 승격). 목록 있음 + 미포함 → `UNKNOWN(symbol_not_verified)`. 명시적 `unrestricted_universe=True`만 통과하며 `universe:unrestricted_explicit`로 감사에 남음.
- **판단 기록 — BLOCK 대신 UNKNOWN**: 결정문은 "BLOCK 또는 현재 설계에 맞는 UNKNOWN"을 허용했고, 미포함 종목의 UNKNOWN은 기존 테스트 T-018/T-018b가 고정한 계약이다(PAPER/SHADOW=UNKNOWN, LIVE=BLOCK). UNKNOWN도 Orchestrator가 진행시키지 않으므로 체결·예약·주문은 생기지 않는다.
- UI/Manual/Paper: `config/settings.yaml`의 `paper_universe`(이번에 추가, UI 전용)만 허용 근거. 없으면 전부 UNKNOWN. 저장소의 SYN* 3개는 오프라인 검증 fixture이며 투자 universe 결정이 아님. 설정 검증: 문자열 목록이 아니면 `SettingsError`.
- Research/Backtest: 검증을 통과한 dataset의 declared symbol을 해당 run 전용 Compliance 인스턴스의 허용 목록으로 사용(`universe:research_run:<data_version>`), allow-all 사용 안 함. Manifest `universe`(scope/market/symbols/source/data_version/validation/`manual_trading_universe_effect: none`)를 Backtest·Walk-Forward·Cost Stress 모두 기록.
- 분리 증거: 허용 목록에 없는 임의 심볼 `LEAK1`로 연구 실행은 정상(Manifest 기록)이지만 같은 데이터셋·서비스에서 Manual/Paper 제안은 `symbol_not_verified`; 세션의 `tradable_symbols`와 `settings.paper_universe` 불변, 주문 0건.
- **테스트 스캐폴딩 기록**: 레거시 단위 테스트 약 200개는 허용 목록 없는 bare stack을 사용하므로 `tests/conftest.py`의 autouse fixture가 **테스트에서만** 클래스 기본값을 True로 명시 opt-in한다(`fail_closed_universe` marker가 붙은 테스트는 프로덕션 기본값 유지). UI·연구 진입점은 항상 universe를 명시하므로 영향이 없다. 기존 테스트 중 수정이 필요했던 것은 `tests/test_ui.py::test_breach_latch_blocks_new_session` 1개(준비 단계에 임의 심볼 `GAP/KRW`의 허용 목록 1줄 추가, 단언은 그대로).

### 13.3 OD-03 — 예약 버퍼 2% 유지 (CLOSED)

- 값 변경 없음(`BacktestConfig` 0.02, UI 세션 0.02, UI 연구 기본 0.02, 입력창 기본값 0.02를 테스트로 고정). 튜닝·시장별 값 없음.
- 경계 회귀: 갭 1.5%·1.9% → Breach 없음, 2.5%·5% → Breach 기록 + 이후 주문 차단, 실제 체결은 유지.
- 문서·UI에 "2% is a provisional policy value, not empirically validated" 명시(설계 §5.1, README, UI 입력 힌트, 설정 화면 policies).

### 13.4 OPEN POLICY — Risk-reducing exit hierarchy

- 기존 명세 조사: 설계 문서·이력 전체에 청산/위험 축소 예외 규정 없음(Kill Switch 조건 목록과 5.3 "신규 주문 BLOCK"만 존재) → 새 override 체계를 만들지 않고 OPEN으로 유지.
- 현재 코드 동작(`test_open_policy_*`로 고정): Kill Switch BLOCK, Daily Loss BLOCK, Drawdown BLOCK(기준 미관측 시 UNKNOWN), Reservation Breach BLOCK, 주문금액 한도 BLOCK, 절대 노출 한도는 기존 노출만으로 초과면 SELL도 BLOCK, 비율 한도(R-02/03/04)는 SELL 미평가. 설계 §7.1에 표로 기록.
- 이는 "옳다"는 결정이 아니라 현재 상태의 기록이다. 결정 전 변경 금지.

### 13.5 검증

| 명령 | 환경 | 결과 |
|---|---|---|
| `python -m pytest -q` | 3.11.5 | **277 passed** (242 + 정책 35) |
| `python -m pytest -q` | 3.13.14 | **277 passed** (연속 10회 통과) |
| compileall / pyflakes / 비밀정보 검색 | 3.11.5 | OK / clean / 검출 없음 |
| 변이 28종 (`artifacts/verification/audit_batch2/mutate_leakage_and_defects.py`) | 3.11.5 | **28/28 검출** (감사 17 + OD-01 5 + OD-02 5 + OD-03 1). 첫 실행에서 "Ledger 없는 NetAlphaGate의 면제" 1종이 생존 → 테스트 추가 후 검출 |
| 실서버 UI/API 스모크 | 내장 브라우저 | 6개 화면 오류 0·가로 넘침 0; 포지션 없는 SELL·약한 BUY·초과 SELL은 Net Alpha 차단, `exit:true` 400, 보유분 SELL 면제(수수료·세금 부과, 포지션 0), Compliance 카드에 허용 목록 표시, 예약 버퍼 입력에 잠정값 안내, 설정 화면 policies 표시 |

**검증 중 발견해 수정한 결함(정책과 별개)**: UI 서버가 본문을 읽지 않고 403/415/413으로 응답하면 드물게(120회 중 2회) OS가 연결을 리셋해 클라이언트가 상태 코드 대신 `ConnectionAbortedError`를 받았다. 3.13 전체 실행 중 `test_http_post_guards`가 7회 중 1회 실패하며 드러났고(18회 반복으로 원인 특정), 모든 POST 경로에서 한도(1 MiB) 내 본문을 판정 전에 소비하도록 수정했다. 수정 후 `test_http_post_guards` 60회·전체 3.13 10회·스트레스 120회 모두 통과, 회귀 테스트 `test_rejected_post_requests_always_get_their_status_not_a_connection_reset` 추가(403/415/413/400 × 60회).

### 13.6 변경 파일

- 수정: `src/qat/cost/engine.py`, `src/qat/app.py`, `src/qat/compliance/gate.py`, `src/qat/research/backtest.py`, `src/qat/research/manifest.py`, `src/qat/research/store.py`, `src/qat/research/walkforward.py`, `src/qat/config.py`, `src/qat/ui/service.py`, `src/qat/ui/server.py`, `src/qat/ui/static/app.js`, `config/settings.yaml`, `pyproject.toml`(pytest marker), `tests/conftest.py`(universe opt-in fixture), `tests/test_ui.py`(1줄 준비 단계)
- 추가: `tests/test_policy_batch2_1.py`, 변이 스크립트 갱신
- 문서: README, 통합 설계·운영 문서(§5.1, §6.1–6.2, §7.1, §8.1, §13.1, D-034~036, §17), 이 문서

### 13.7 남은 UNKNOWN / OPEN

- **OPEN POLICY — Risk-reducing exit hierarchy** (사용자 결정 필요).
- 실제 투자 universe·허용 목록, 실제 Net Alpha, Paper 졸업 기준, 예약 버퍼의 경험적 값(실제 데이터로 갭 분포 측정 후), stale snapshot 정책, 휴장일 캘린더 등 기존 UNKNOWN 유지.
- 이번 작업에서 실제 시장 데이터 연구·Broker 연결·Live·신규 전략은 수행하지 않았다. Live는 BLOCKED.

### 13.8 이어받기

```text
검증: python -m pytest -q            (기대 277 passed)
변이: python artifacts/verification/audit_batch2/mutate_leakage_and_defects.py   (기대 28/28 CAUGHT; 소스를 임시 변경 후 원복)
다음 우선순위:
  1) OPEN POLICY — Risk-reducing exit hierarchy 결정
  2) 실제 로컬 데이터 투입 → validate-data → Walk-Forward(독립 OOS 1회) → Lockbox 1회
  3) 실제 데이터로 갭 분포 측정 후 예약 버퍼 재결정, stale snapshot 정책
```

---

## 14. 2026-09-30 — Batch #2.2 Risk-Reducing Exit Hierarchy Closure

```text
날짜: 2026-09-30
Batch/Commit: Batch #2.2 (Git 저장소 아님 → code_commit unavailable)
목적: OPEN POLICY "Risk-reducing exit hierarchy"를 Control Tower 결정대로 구현·검증·문서화 (신규 전략·실제 데이터·Broker·Live 없음)
변경 파일: §14.7
검증 명령: python -m pytest -q (3.11.5 / 3.13.14), compileall, pyflakes, 비밀정보 검색, 변이 47종, 실서버 UI/API 스모크
테스트 결과: 277 → 315 passed
최종 판정: Risk-reducing exit hierarchy CLOSED (전체 regression PASS 이후). 긴급 강제 청산/복구는 향후 별도 정책.
```

### 14.1 판정 방식 (공용 분류)

- `qat.core.exposure.reduces_exposure(ledger, proposal)` 하나로 판정하고 Net Alpha 면제(OD-01)와 Risk 한도 완화가 **같은 함수**를 재사용한다(Gate마다 중복 구현 없음). 입력은 Ledger의 포지션과 제안의 side·quantity뿐이다.
- 조건: Long 포지션 > 0 + SELL + 수량 ≤ 가용(미예약) 수량 (부호 규칙은 Short 대비 일반화, Short 미지원). 클라이언트 `exit`/`reduce_only`/`risk_reducing` 등은 읽지 않는다: `TradeProposal`에 필드 없음(TypeError), UI는 미허용 필드 400, 객체에 임의 속성을 심어도(`object.__setattr__`) 무시됨을 테스트로 확인.
- 완화는 `Ledger.integrity_problems()`가 비어 있을 때만 적용된다(`relax = reducing and no integrity problems`).

### 14.2 규칙별 최종 동작

| 규칙 | 노출 축소 SELL | BUY / 비축소 SELL |
|---|---|---|
| Kill Switch | **BLOCK (예외 없음)**. 자동 청산을 뜻하지 않음, flatten 기능 없음 | BLOCK |
| Daily Loss | 이 규칙으로 막지 않음 (`exit_allowed:daily_loss_limit` 감사 기록). FX 누락·비유한 값은 UNKNOWN 유지 | BLOCK |
| Drawdown | 이 규칙으로 막지 않음. 기준 equity 없음/FX 없음/비유한 값은 **UNKNOWN 유지** | BLOCK |
| 절대 노출 한도 | 이미 초과여도 막지 않음 (150k→120k, 150k→80k, 전량 모두 통과) | BLOCK (150k→151k) |
| Reservation Breach | 조건부 허용(§14.3) | BLOCK |
| 주문금액/크기 한도 | **우회 불가**(한도 초과 주문 BLOCK, 자동 분할 없음, 한도 이하 부분 SELL은 통과; Daily Loss breach 중에도 동일) | BLOCK |
| Compliance / Market Integrity / Settlement / 실행 비용 | 그대로 적용 (Daily Loss breach + 거래정지 → `compliance` BLOCK, 미체결 BUY 존재 → `integrity` BLOCK) | 그대로 |

### 14.3 Reservation Breach — 재무 위반 vs 회계 무결성 실패

- 재무 위반(plain overrun, `kind=reservation_overrun`)만으로는 축소 SELL을 가두지 않는다. 허용 조건: ① 실제 축소 ② plain overrun(다른 종류는 `not_an_overrun` BLOCK) ③ Ledger 내부 일관성 ④ 최근 Reconciliation 기록에 불일치 없음 ⑤ 권위 있는 평가 가능 ⑥ 다른 안전 Gate BLOCK 없음.
- 무결성 신호: 비유한 cash/PnL/수수료/포지션/Breach 값, 음수 포지션(Short), 음수 예약 현금, 예약 수량 > 보유 수량, 미해결 정산 불일치(`fill_order_mismatch`). 실패 시 `accounting_untrusted`(BLOCK), `reconciliation_mismatch`(BLOCK), FX 없음 `fx_unavailable`(UNKNOWN), `RiskGate(require_reconciliation=True)`에서 대조 이력 없음 `reconciliation_not_performed`(UNKNOWN).
- Case A(overrun만, 정상): 축소 SELL 통과·BUY BLOCK, Breach 기록은 유지. 실제 갭으로 만든 Breach에서 전량 청산 후에도 Breach가 남아 신규 위험 주문은 계속 BLOCK. Case B(대조 불일치): BLOCK → 깨끗한 실제 대조 후 회복. Case C(비유한·모순 회계 7종, 정산 불일치): BLOCK.
- 회복 경로: 정산 불일치는 `resolve_integrity_issues(깨끗한 대조, 승인자, 메모)`로만 해제(승인자 없음/대조 불일치 시 `LedgerError`). `reconcile()`은 결과를 Ledger에 기록한다.
- Paper 재생에는 대조할 Broker 계좌가 없어 `require_reconciliation=False`다. Broker가 연결되는 모드는 반드시 True로 설정해야 한다(문서화).

### 14.4 부수 발견·수정

- 일일 손실/Drawdown 계산 값이 NaN이면 `>=` 비교가 False라 조용히 통과하던 경로를 UNKNOWN(`daily_loss_not_finite`, `drawdown_not_finite`)으로 막음(완화가 "잘못된 accounting을 PASS로 바꾸지 않는다"는 원칙과 같은 계열).

### 14.5 기존 테스트 처리 (투명성 기록)

- 정책이 바뀌어 **기대값이 바뀐 기존 특성화 테스트 4개**(`tests/test_policy_batch2_1.py`의 `test_open_policy_*`: Daily Loss, Drawdown, plain Reservation Breach, 절대 노출 한도)는 "OPEN POLICY의 현재 동작을 고정(결정 아님)"하던 것이므로 새 결정에 맞게 제자리에서 갱신했다(이름에 `# updated by #2.2`). Kill Switch·Drawdown UNKNOWN 기준선·주문금액 한도 테스트와 나머지 273개는 변경 없음. 문서 문구 검사 테스트는 "OPEN POLICY" → "CLOSED"로 갱신.
- 변이 스크립트: Batch #2.1의 OD-01 변이 5종은 분류 코드가 `core/exposure.py`로 이동해 **같은 변이 내용, 대상 경로만** 조정했다(검출력 동일). 기존 28종은 모두 유지.

### 14.6 검증

| 명령 | 환경 | 결과 |
|---|---|---|
| `python -m pytest -q` | 3.11.5 | **315 passed (연속 4회 통과)** |
| `python -m pytest -q` | 3.13.14 | **315 passed (연속 4회 통과)** |
| compileall / pyflakes / 비밀정보 검색 | 3.11.5 | OK / clean / 검출 없음 |
| 변이 (`artifacts/verification/audit_batch2/mutate_leakage_and_defects.py`) | 3.11.5 | **47/47 검출** (기존 28종 유지 + OD-01 재배치 포함 + Batch #2.2 신규 19종). 첫 전체 실행에서 `NaN daily loss passes silently` 1종이 생존 → NaN 일일 손실 전용 테스트 추가 후 검출 |
| 실서버 UI/API 스모크 | 내장 브라우저 | 6개 화면 오류 0·가로 넘침 0, 위조 필드(`exit`/`reduce_only`/`risk_reducing`) 400, 초과 SELL 차단, 축소 SELL 통과(5개 Gate PASS)·포지션 0, Risk 화면에 노출 축소 SELL 정책 표·회계 무결성 상태·대조 상태 표시 |

### 14.7 변경 파일

- 추가: `src/qat/core/exposure.py`, `tests/test_exit_policy_batch2_2.py`(38개)
- 수정: `src/qat/risk/gate.py`, `src/qat/cost/engine.py`(분류 위임), `src/qat/portfolio/ledger.py`, `src/qat/portfolio/reconciliation.py`, `src/qat/execution/settlement.py`, `src/qat/ui/service.py`, `src/qat/ui/static/app.js`, `tests/test_policy_batch2_1.py`(4개 갱신·주석), 변이 스크립트
- 문서: README, 통합 설계·운영 문서(§6.2, §7.1, §13.1, §17, D-037~039), 이 문서

### 14.8 남은 OPEN / UNKNOWN

- 긴급 강제 청산(Emergency flatten)·복구(recovery)는 **향후 별도 정책** — 이번에 만들지 않음. Kill Switch는 자동 청산을 뜻하지 않는다.
- 기존 UNKNOWN 유지: 실제 투자 universe, 실제 Net Alpha, 예약 버퍼의 경험적 값(실제 데이터 후), stale snapshot 정책, 휴장일 캘린더, Paper 졸업 기준, Broker 연결 시 `require_reconciliation=True` 운용 세부.
- 실제 시장 데이터 단계는 다음 Batch에서 별도로 시작한다. Live는 BLOCKED.

### 14.9 이어받기

```text
검증: python -m pytest -q            (기대 315 passed)
변이: python artifacts/verification/audit_batch2/mutate_leakage_and_defects.py   (기대 전부 CAUGHT; 소스를 임시 변경 후 원복)
다음: 실제 로컬 데이터 투입 → validate-data → Walk-Forward(독립 OOS 1회) → Lockbox 1회 (별도 Batch)
```

---

## 15. 2026-10-06 — Batch #3A 재구축 + KR 공식 데이터 검증 (OD-04)

> 이전에 보고된 #3A 수치(테스트 수, 변이 수, 교차검증 결과 등)는 **사실로 취급하지 않고** 가설로만 재검증했다. 아래 수치는 모두 이번에 측정한 값이다. 기준선: Batch #2.2 — 315 passed, 변이 47종, Live BLOCKED.

### 15.1 구현 요약

- 새 패키지 `src/qat/realdata/`(provenance, sources, fetch, acquire, normalize, validate, calendars, adjustment, crosscheck, datasets, admission, evidence, secrets, http), `tools/`(secret_scan, generate_calendar_snapshots, mutation_harness), 캘린더 snapshot `data/calendars/XKRX.json`·`XNYS.json`.
- 기존 모듈 수정: `data/bars.py`(`DataRejected`), `research/backtest.py`·`walkforward.py`(admission), `ui/service.py`(DataRejected→409, 데이터셋 admission 표시), `ui/static/app.js`(미승인 데이터셋 비활성). 기존 315개 테스트는 수정 없이 통과.
- 상세 설계: 통합 설계·운영 문서 §15.5.

### 15.2 실측 결과

| 데이터셋 | 행 | 기간 | 검증 | 보정 의미 |
|---|---|---|---|---|
| crypto-binance (BTC/USDT, Binance public archive + CHECKSUM) | 2,922 | 2018-01-01~2025-12-31 | PASS | UNADJUSTED |
| crypto-coinbase (교차검증) | 1,703 | 2021-05-04~2025-12-31 | PASS | UNADJUSTED |
| us-yahoo (AAPL) | 2,011 | 2018-01-02~2025-12-31 | PASS | SPLIT_ADJUSTED (2020-08-31 4:1 기준 관찰; 제공자 문서는 미확보, 종가는 배당 미보정) |
| us-nasdaq (AAPL) | 2,011 | 2018-01-02~2025-12-31 | PASS | SPLIT_ADJUSTED |
| **kr-official (data.go.kr, 005930)** | **1,473** | **2020-01-02~2025-12-30** | **PASS (V18 WARN)** | **UNADJUSTED(공식 필드 정의 기준)** |
| kr-naver (005930) | 1,963 | 2018-01-02~2025-12-30 | FAIL (V04, V05) | SPLIT_ADJUSTED |
| kr-yahoo (005930) | 1,961 | 2018-01-02~2025-12-30 | FAIL (V02, V05, V12, V13) | SPLIT_ADJUSTED |

교차검증(필드 단위 판정 수):

| 비교 | 판정 | MATCH / EXPLAINED / CONFLICT / UNKNOWN |
|---|---|---|
| Binance ↔ Coinbase | EXPLAINED_DIFFERENCE | 2,438 / 6,077 / 0 / 0 (거래소별 호가 차이 5% 이내, 거래량은 비교하지 않음) |
| KR 공식 ↔ Naver (공식 기간 내) | MATCH | 7,365 / 0 / 0 / 0 |
| KR 공식 ↔ Yahoo | CONFLICT | 7,250 / 0 / 100 / 5 |
| US Yahoo ↔ Nasdaq | CONFLICT | 9,419 / 0 / 59 / 577 (충돌 59 = 거래량 52 + 시가 4 + 저가 2 + 종가 1, 거래량 차이는 최대 약 2.0%; 원인 미확정 차이는 UNKNOWN으로 둠) |

과거 가설 재현(현재 데이터로): Yahoo의 2022-01-03·2022-05-09 누락 REPRODUCED(공식 데이터에 존재), Yahoo null 행 REPRODUCED, Yahoo OHLC 이상(2024-10-14) REPRODUCED, Yahoo 0거래량·stale REPRODUCED, Naver 0가격 행(정지일) REPRODUCED. KR Yahoo↔공식 차이를 날짜 단위로 분류(45일): Yahoo 누락 2, null 행 1, 거래량 0 stale 행 13, 부분 거래량(공식의 5% 미만) 23, 종가만 상이 5, 기타 1.

결정론: 정규화를 두 번 재수행해 해시·`data_version`·identity·manifest가 동일. 원본 재해시 실패 0건(`python -m qat.realdata verify-raw`).

### 15.3 OD-04 결정 — CLOSED (범위 한정)

13개 기준 모두 충족(`artifacts/verification/real_data/od04_decision.json`). 판정상 주의:

- "요청 기간 충족"은 사용자 문구대로 **"전체 기간 충족 또는 부족분 설명"**으로 해석했다. 요청 2018-01-01~2025-12-31 중 2018-01-02~2019-12-30(예상 490세션)은 서비스가 0건을 반환하며(보존된 프로브 9건, 1990-01-01~2020-01-02 포함) 데이터셋에 없다. 이 해석에 동의하지 않으면 OD-04는 OPEN으로 되돌려야 한다.
- 보정 의미는 공식 가이드에 명시 문장이 없어 **공식 필드 정의(장중 체결 값) + 기준가(`vs`) 일관성(1,472행 불일치 0)**에 근거한다. 이 기간에는 기업행동(분할)이 없어 분할 후 연속성 관찰은 NOT_IN_RANGE. KRX가 기업행동 이후 과거 행을 소급 수정하는지는 UNKNOWN.
- 구 endpoint 코드 30(`SERVICE_KEY_IS_NOT_REGISTERED_ERROR`)은 키 문제가 아니라 경로 문제였다(V2 경로 인증 성공).

### 15.4 검증

| 명령 | 결과 |
|---|---|
| `python -m pytest -q` (3.11) | **395 passed** (315 + 단위 53 + 통합 19 + harness 8) |
| `python -m pytest -q` (3.13) | **395 passed** |
| compileall / pyflakes | OK / clean |
| `tools/secret_scan.py` (587개 파일) | credential 검출 **없음** |
| 변이 (`mutate_leakage_and_defects.py`) | **73/73 CAUGHT** (기존 47 + 데이터 계층 26), 소스 트리 해시 전/후 동일(바이트 단위 원복). 신규 26종 첫 실행에서 6종 생존(admission synthetic 무시, V14 원본 제공자, 재정규화 비교 생략, backtest·walk-forward admission 생략, endpoint 키 비마스킹) → 단위 시험 5개 추가 후 전부 검출 |
| UI/API 스모크 | 6개 화면 렌더링·가로 넘침 없음, 데이터셋 목록에 실제 데이터셋 3종 admitted 표시, 변조 데이터셋 HTTP 409(통합 시험) |
| Lockbox | 보호 파일 18개 해시 전/후 동일, `results/lockbox_registry.json` 없음, Walk-Forward·Lockbox 사용 없음 |

변이 harness는 이전에 `Path.write_text`로 CRLF/LF가 섞일 수 있었다. 이제 바이트로 읽고 쓰며 파일의 줄바꿈 방식에 맞춰 패턴을 적용하고, 원복 후 파일·트리 SHA-256을 확인한다(`tests/test_mutation_harness.py`).

### 15.5 남은 UNKNOWN

- 공식 KR 소스의 2020-01-02보다 이른 날짜 이력, KRX의 소급 보정 여부, 서비스 시작일(2021-11-16)과 실제 2020-01-02 시작의 불일치 이유.
- XKRX/XNYS 캘린더는 커뮤니티 데이터(공식 아님). 미국 데이터의 제공자 보정 문서는 미확보(관찰 기반 판정).
- 005930 외 종목·KOSDAQ·US의 공식 소스, 실제 Net Alpha, 예약 버퍼 경험값, 실제 universe 등 기존 UNKNOWN 유지.
- Batch #3B(실제 데이터 Walk-Forward 등)는 시작하지 않았다.

### 15.6 이어받기

```text
검증: python -m pytest -q                 (기대 395 passed)
원본: python -m qat.realdata verify-raw   (실제 원본 재해시, 실패 0)
증거: python -m qat.realdata evidence     (artifacts/verification/real_data/ 재생성, 결정론적)
변이: python artifacts/verification/audit_batch2/mutate_leakage_and_defects.py   (기대 73종 전부 CAUGHT)
KR 공식 재수집: python -m qat.realdata acquire kr-official --key-file <키 파일>   (키는 저장·출력하지 않음)
```

---

## 16. 2026-10-06 — Batch #3A.2 KR Research Scope Closure

정책·admission 의미·문서 계약 정리. 새 dependency·대규모 변경 없음.

### 16.1 결정
- **OD-04: CLOSED WITH SCOPE.** KR 공식 소스(data.go.kr 금융위원회 주식시세정보 V2) 확보, 005930 검증 PASS, 검증 범위 **2020-01-02~2025-12-30**. 2020-01-02보다 이른 날짜은 **UNKNOWN**(공식 데이터 없음). 보간·backfill·합성·비공식 이어붙이기 금지. Yahoo/Naver는 공식 fallback이 아니며 기존 FAIL 증거는 유지한다.
- 교차검증 해석 유지: 공식↔Naver(공식 기간) MATCH, 공식↔Yahoo CONFLICT, Yahoo·Naver 단독 FAIL. Naver MATCH를 Naver 2018~2019의 공식급 승격 근거로 쓰지 않는다.
- 보정 의미: Fact = 가이드에 보정 명시 문장 없음 / Evidence = 공식 필드 정의 + 기준가 일관성(1,472행, 불일치 0) / Interpretation = UNADJUSTED와 일치(`UNADJUSTED_EVIDENCE_SUPPORTED`) / 제공자 명시 보증 = **없음**(`UNAVAILABLE`).

### 16.2 현행 코드 확인과 최소 변경
확인: 이전에는 연구 요청에 날짜 기간 개념이 없어(bar index만) 조용한 절단은 없었지만 범위 밖 기간을 요청해 거부시킬 경로도 없었다. 변경:
- `realdata/admission.py`: admission 결과에 `covered_start/covered_end`, `require_coverage()` 추가(범위 밖·더 좁은 기간·검증 기록 없는 데이터셋 모두 `DataRejected`).
- `ui/service.py`: 선택 필드 `requested_start`/`requested_end` → `require_coverage` → 409(`requested period exceeds verified dataset coverage`).
- `realdata/adjustment.py`·`datasets.py`: 기록에 `assurance`, `provider_explicit_statement` 추가(`final` 값·정규화 바이트·`data_version` 불변). 저장된 identity 파일은 `python -m qat.realdata evidence`로 재생성.
- `realdata/evidence.py`: `od04_decision.json`의 판정을 `CLOSED WITH SCOPE`로, `scope` 블록 추가.

### 16.3 검증
| 항목 | 결과 |
|---|---|
| 신규 테스트 | 단위 3 + 통합 4 (T1 검증 구간 허용, T2 2019 포함 요청 거부·조용한 절단 없음, T3 공식 dataset은 data.go.kr 원본만·409 후 대체 소스 없음, T4 제공자 명시 보증 UNAVAILABLE, T5 정규화 해시·raw-set 해시·data_version·행수 불변) |
| 신규 변이 | 6종 전부 CAUGHT (범위 밖 요청 허용, 좁은 기간 조용한 전체 실행, service 가드 생략, 검증 기록 없는 데이터셋 보증, 보정 보증 표기 승격, 제공자 명시 보증 위조) |
| 전체 회귀 | 아래 최종 보고 참조 (3.11/3.13 pytest, compileall, pyflakes, secret scan, 변이 전체) |

### 16.4 남은 UNKNOWN
2020-01-02보다 이른 날짜 공식 KR OHLCV·표현·보정 의미, KRX 소급 보정 여부, 서비스 시작일 불일치 이유, 연구 기간 선택 방식(Batch #3B 설계), 005930 외 종목.

---

## 17. 2026-10-06 — Batch #3B 실제 데이터 Walk-Forward 연구 프로토콜

> 목적은 수익률 개선이 아니라 leakage 없는 재현 가능한 프로토콜 동결과 Lockbox 이전 후보 선별이다. **Lockbox는 열지 않았다.** 설계·결과 상세는 통합 설계·운영 문서 §15.6.

### 17.1 순서와 규율
1. 현행 계약 확인(기존 `run_walkforward`: rolling fold, train grid 선택, Lockbox 제외, 비용 placeholder). 새 전략·탐색 공간 없음.
2. 배관 smoke(성능 수치 미출력, fold 수·실패·실행 시간만)로 윈도가 동작함을 확인한 뒤 프로토콜(윈도, 임계값, 상태 규칙)을 **결과를 보기 전에** 동결(`protocol.json`, SHA-256 `40a7b682bf1b77355f598d52a0e63de1bf4f429c318964a25ad06a6431e2d94d`).
3. 동결된 프로토콜로 9개 후보(3시장 × 3전략) primary + 비용 stress(2×·3×) + common period(KR 제외) + 결정론 재실행을 1회 수행. 이후 프로토콜 변경이나 재실행은 코드가 거부한다.

### 17.2 코드 변경
- 추가: `src/qat/research/protocol.py`, `tests/test_protocol.py`(15개).
- 최소 수정: `WalkForwardConfig.start_bar`(기본 0, common period용; Lockbox 안쪽 불가), `run_walkforward(extra_manifest=...)`(manifest에 protocol·데이터 identity 기록), `require_coverage(allow_subperiod=...)`(UI/API는 계속 부분 기간 거부), 변이 script에 프로토콜 변이 11종.
- 표기 정정: "2020-01-02 이전"처럼 모호한 표현을 "2020-01-02보다 이른 날짜"로 고쳤다(2020-01-02 자체는 verified coverage에 포함).

### 17.3 결과(Fact)
통합 설계·운영 문서 §15.6의 표 참조. 요약: 9개 후보 전부 `INSUFFICIENT_ACTIVITY`, Lockbox 후보 0개 → **`NOT_READY_FOR_LOCKBOX`**. 결정론 재실행 9/9 일치. 모든 엔진 호출이 개발 구간 안(`trade_end ≤ n − lockbox_bars`).

### 17.4 검증
| 항목 | 결과 |
|---|---|
| `python -m pytest -q` 3.11 / 3.13 | **417 passed** / **417 passed** |
| 변이 | **90/90 CAUGHT**(기존 79 + 프로토콜 11), 소스 트리 바이트 단위 원복 |
| compileall / pyflakes / secret scan | OK / clean / 검출 없음 |
| Lockbox | `results/lockbox_registry.json` 없음, 기존 보호 파일 해시 불변(`results/oos_registry.json`은 OOS 평가 기록이라 갱신됨), Lockbox 평가 호출 없음 |

### 17.5 남은 결정·UNKNOWN
- 후보가 없으므로 Lockbox는 열 수 없다. 다음 선택지는 사용자 결정: (a) 현 결과를 그대로 종결, (b) 더 많은 거래가 나오는 설계(예: 더 높은 빈도의 검증된 데이터, 더 긴 기간) — 새 프로토콜 버전과 새 Lockbox 계획 필요, (c) 임계값 재검토(사후 조정이므로 데이터 snooping 위험을 명시해야 함).
- 실제 비용 모델(현재 placeholder), 다중 검정 영향, 임계값 적정성은 UNKNOWN. 수익성은 입증되지 않았다.

---

## 18. 2026-10-06 — Batch #3C 연구 타당성 기반

> 전략 성과 개선이 아니라 다음 Protocol 전에 research validity를 강화하는 Batch. Batch #3B 결과는 변경·재해석하지 않았고 **전략 실행·Lockbox 접근은 없었다**. 설계·표·근거는 통합 설계·운영 문서 §15.7.

### 18.1 결과(Fact)
- Batch #3B 고정: 프로토콜 코드 해시가 동결 해시와 일치, 결과 파일 해시 기록. #3B pre-Lockbox OOS는 observed development evidence.
- 임계값(30건 등)은 저장소에 문헌 근거가 없는 internal pre-registered heuristic.
- Trial registry: 저장된 run 1,343개 고유 trial(train 선택 1,106, OOS 평가 237), 고유 설정 126, 후보 절차 9.
- 비용: VERIFIED 0 / EVIDENCE_SUPPORTED 1 / PLACEHOLDER 13 / UNKNOWN 1. Spread·slippage는 데이터로 측정 불가(unavailable).
- 벤치마크: 1봉 진입 불일치와 노출·비용 비대칭 발견(v1 동결, 판정 무영향).
- 다중검정: 방법을 평가했으나 구현하지 않음(White RC는 v2 잠정 선택, 가정 미확인; DSR·PBO는 현 구조에 부적합). 적용 가능성 UNKNOWN.
- Protocol v2 판정: `PROTOCOL_V2_RECOMMENDED`(BTC/USDT 한정, 선결 조건 미충족 시 `RESEARCH_STOP_RECOMMENDED`).

### 18.2 코드 변경
- 추가: `src/qat/research/trials.py`, `cost_evidence.py`, `validity.py`, `tests/test_validity.py`(11개), 증거 `artifacts/verification/validity_3c/`.
- 최소 수정: `run_walkforward`가 평가 후보를 trial로 수집해 manifest(`trial_context`)·`results/trial_registry.json`에 등록(fold 행은 수정하지 않음 — 오염 탐지기가 calibration 행을 비교하기 때문). 변이 script에 12종 추가. 기존 프로토콜·임계값·결과는 변경 없음.

### 18.3 검증
| 항목 | 결과 |
|---|---|
| `python -m pytest -q` 3.11 / 3.13 | **428 passed** / **428 passed** |
| 변이 | **102/102 CAUGHT**(기존 90 + 12), 소스 트리 바이트 단위 원복 |
| compileall / pyflakes / secret scan | OK / clean / 검출 없음 |
| Lockbox | `results/lockbox_registry.json` 없음, `evaluate_lockbox` 미호출, 벤치마크 audit는 개발 구간 `bars[:development_end]`만 사용(시험으로 확인) |

### 18.4 남은 결정·UNKNOWN
사용자 결정: 계속 여부, 거래소·수수료 등급·세무 관할, 2026년 데이터를 새 untouched holdout으로 쓸지. UNKNOWN: 실제 비용(스프레드·슬리피지 포함), 유효 독립 trial 수, 다중검정 방법의 가정과 적용 가능성, 임계값 적정성, White(2000)·DSR 수식 본문 확인. 수익성·알파·벤치마크 우위·견고성은 주장하지 않는다(경제적 우월성 UNKNOWN).

---

## 19. 2026-10-06 — Batch #3D BTCUSDT 4h·비용 v2 기반

> Protocol v2를 실행하지 않았다. 4h 수익성 백테스트·파라미터 선택·2026 값 접근·Lockbox 접근 없음. 상세는 통합 설계·운영 문서 §15.8, 증거는 `artifacts/verification/intraday_3d/`.

### 19.1 결과(Fact)
- 4h 데이터: 2018-01~2025-12 월별 zip 96개 + 공식 CHECKSUM 96개, 모두 일치. 17,516행(기대 17,532). 무결성 PASS, **엄격 완전성 FAIL**: 누락 16봉(8구간), 짧은 봉 18개. 정책(누락=FAIL)을 사후 완화하지 않음.
- 결정론: 독립 빌드 2회 동일, 저장 파일과 일치. `data_version` `CRYPTO-BTCUSDT-4h-binance-vision-f3ab848bf53c-4d2aff`.
- 비용 v2: VERIFIED_CURRENT(현재 maker/taker 0.100%), HISTORICAL_UNKNOWN(과거 수수료·스프레드·슬리피지), PLACEHOLDER(FX·세금), SCENARIO(S0~S3). Batch #3C 분류 불변.
- 미시구조: aggTrades/trades 하루 샘플 기술 통계만(스프레드 아님), 호가 데이터는 아카이브에 없음, bookTicker/depth 필드명만 확인.
- 2026 holdout: 메타데이터(2026-01~09 파일 9개)만, 값 접근 NO. Lockbox 불변.
- 다중검정: White RC = CANDIDATE_METHOD — APPLICABILITY UNVERIFIED(#3C의 '선택(잠정)' 표현 정정), DSR·PBO 미구현.
- 판정: `RESEARCH_STOP_RECOMMENDED`(엄격 완전성 게이트; gap 정책 수락 시 전환 가능).

### 19.2 코드 변경
- 추가: `src/qat/realdata/intraday.py`, `microstructure.py`, `src/qat/research/trial_series.py`, `intraday_evidence.py`, `tests/test_intraday.py`(25개), 증거 디렉터리, 변이 17종.
- 수정: `cost_evidence.py`(v2 계약 추가, #3C 분류 불변), `walkforward.py`(선택적 `series_store`; 기본 동작·기존 시험 불변), `validity.py`(White RC 상태 문구 정정).

### 19.3 검증
| 항목 | 결과 |
|---|---|
| `python -m pytest -q` 3.11 / 3.13 | **453 passed** / **453 passed** |
| 변이 | **119/119 CAUGHT**(기존 102 + 17), 소스 트리 바이트 단위 원복 |
| compileall / pyflakes / secret scan | OK / clean / 검출 없음 |
| Lockbox | registry 없음, `evaluate_lockbox` 미호출, 보호 파일 해시 불변(oos/trial registry 제외) |

### 19.4 남은 결정·UNKNOWN
사용자 결정: gap 정책 수락 여부(수락 시 FREEZE_READY), v2 개발 종료일(2024-12-31 vs 2025-12-31 — 일봉 Lockbox 연도 문제), 2026 holdout 사용 승인, 시나리오 비용 범위. UNKNOWN: 누락/짧은 봉의 공식 원인, 과거 수수료·스프레드·슬리피지, 6배 신호 빈도 가정, 30건 기준의 적절성, White RC 적용 가능성. 수익성·알파·견고성은 주장하지 않는다.

---

## 20. 2026-10-07 — Batch #3E Protocol v2 사전등록·freeze

> 전략 성과를 실행·출력하지 않았다. 사양·근거는 통합 설계·운영 문서 §15.9, 동결 파일은 `artifacts/verification/protocol_v2/`.

### 20.1 결과(Fact)
- 판정 `PROTOCOL_V2_FROZEN`, protocol hash `c1dad1503b43e58af6abb5cd711d99f8f3a51baa179d95067903c6239023c8bc`.
- 부모 4h(2018-01~2025-12): strict `FAIL`(누락 16봉·짧은 봉 18개) **유지**, `VALIDATION_FAIL_INCOMPLETE`, 데이터 불변. 보간·채움·병합·합성·정책 완화 없음.
- 연구 view: 결정적 `trailing-continuous-segment-v1`로 도출한 `2021-09-29T08:00:00`~`2025-12-31T20:00:00`, 9,328행, `ADMITTED_CONTINUOUS_SUBPERIOD`, 모든 검사 PASS, 부모의 바이트 동일 슬라이스(수정 0·합성 0). continuous_start는 시각·무결성만으로 도출(수익률·거래 수·성과 미사용).
- Fold: 4개(train 4,380 / OOS 1,095 / step 1,095봉), OOS 2.0년, 최소 3 충족.
- 개발 종료 2025-12-31, 새 holdout 2026-01-01~2026-12-31(값 접근 NO, 메타데이터만). 기존 일봉 Lockbox 불변·v2 holdout 아님.
- 비용: 현재 0.100% `VERIFIED_CURRENT`, 과거 수수료 `HISTORICAL_UNKNOWN`(연구용 `ASSUMED_FOR_RESEARCH`), 스프레드·슬리피지 `HISTORICAL_UNKNOWN`, S0~S3 `SCENARIO ASSUMPTION`, S0 진단 전용, 선택·후보는 S1/S2/S3 worst-case, taker 가정.
- 파라미터: 기존 grid 봉 수 그대로 = NEW INTRADAY HORIZON(옵션 A).
- 설계 기록: gap 결정 = POST-DATA-INTEGRITY OBSERVATION / PRE-PERFORMANCE; 정책은 gap 허용이 아니라 결정적 연속 구간 규칙에 의한 제외.

### 20.2 코드 변경
- 추가: `src/qat/research/protocol_v2.py`, `tests/test_protocol_v2.py`(16개), 동결 산출물 디렉터리, 변이 15종.
- 수정: `realdata/intraday.py`(연속 view 도출·admission 의미·승인 마커), `research/trial_series.py`(`cost_scenario` 필드), `tests/test_intraday.py`(가드 시험을 두 마커 규칙으로 갱신 — 약화 아님).
- 가드 강화: 2026 값은 freeze 마커 **및** 별도 평가 승인 마커가 있어야만 읽을 수 있다.

### 20.3 검증
| 항목 | 결과 |
|---|---|
| `python -m pytest -q` 3.11 / 3.13 | **469 passed** / **469 passed** |
| 변이 | **134/134 CAUGHT**(기존 119 + 15), 소스 트리 바이트 단위 원복 |
| compileall / pyflakes / secret scan | OK / clean / 검출 없음 |
| Lockbox / 2026 | registry 없음·`evaluate_lockbox` 미호출, 2026 값 접근 NO, 승인 마커 없음 |

### 20.4 남은 UNKNOWN
누락·짧은 봉의 공식 원인, 과거 수수료·스프레드·슬리피지, 임계값(30건 등)의 적절성, White RC 적용 가능성, 4 fold(OOS 2년)로 충분한 통계적 검정력이 있는지(미검증), 마지막 OOS 이후 사용되지 않는 꼬리 구간. 평가는 별도 사용자 승인 Batch에서 2026 전체 연도 이후에만 가능하다. 수익성·알파·견고성은 주장하지 않는다.

---

## 21. 2026-10-07 — Protocol v2 Official Development Run #1 (통합 실행)

> 하나의 자율 작업으로 수행: freeze 검증 → 사전 addendum → 공식 개발 실행 → 결정론 검증 → 후보 판정/동결 → one-shot 평가기 구현·시험. 2026 holdout 값은 읽지 않았다. 상세는 통합 설계·운영 문서 §15.10.

### 21.1 Fact
- frozen 해시 `c1dad1503b43e58af6abb5cd711d99f8f3a51baa179d95067903c6239023c8bc` 검증 통과, 성과 실행 이전에 `candidate_selection_addendum_v1`(`0f73787de09722bbf84a59ef7ba0bc3f15881749d40baf908a28aaa2af567797`) 기록 후 실행.
- 상태: ma_trend UNSTABLE, breakout PASS, mean_reversion INSUFFICIENT_ACTIVITY → 분기 B, 후보 breakout 동결(`d038da459ad78a4a9895ea5cf1f6237c9802aa6a155d286ffb9f798d10fb9d9e`). 정렬 passive 초과는 모든 시나리오에서 아님 → holdout 자격 False.
- 평가 216회·고유 trial 216·series 216 전부 저장, 검증 재실행 동일(중복 제거, 새 trial 아님).
- 2026: 파일명 메타데이터(01~09월)만, 값 접근 NO. 기존 일봉 Lockbox 불변.

### 21.2 코드 변경
- 추가: `protocol_v2_run.py`(addendum·순위·절차·후보 동결), `holdout_eval.py`(one-shot 평가기), `tests/test_protocol_v2_run.py`·`tests/test_holdout_eval.py`, 동결 산출물(addendum 2개, 후보, 결과, 비용 settings), 변이 17종.
- 수정: `realdata/admission.py`(연속 view admission), `realdata/intraday.py`(canonical_lines 분리·holdout_phase), `protocol_v2.py`(`min_folds` 선택 인자, 기본 불변·frozen 상수 불변), `trial_series.py`(`build_record`).

### 21.3 검증
| 항목 | 결과 |
|---|---|
| `python -m pytest -q` 3.11 / 3.13 | **492 passed** / **492 passed** |
| 변이 | **154/154 CAUGHT**(기존 134 + 17), 소스 바이트 단위 원복 |
| compileall / pyflakes / secret scan | OK / clean / 검출 없음 |
| Lockbox / 2026 | registry 없음, `evaluate_lockbox` 미호출, 승인 마커 없음, 2026 값 접근 NO |

### 21.4 UNKNOWN
통계적 검정력(4 fold·청산 38건), 다중검정 보정, 임계값의 적절성, 비용(과거 수수료·spread·slippage), 2026 holdout 결과(미평가), holdout에서 30건 임계값 충족 여부.

---

## 22. 2026-10-07 — Protocol v2 종결 (CLOSED / NOT_HOLDOUT_ELIGIBLE)

> 상태 표현 정정: §21의 'WAITING FOR FULL 2026 HOLDOUT + USER APPROVAL'은 부정확했다. breakout은 development PASS이고 유일한 `DEVELOPMENT_SELECTED_CANDIDATE`지만 frozen benchmark criterion을 충족하지 못해 `holdout_eligible=False`다. 최종 상태: **`DEVELOPMENT_SELECTED_CANDIDATE — NOT_HOLDOUT_ELIGIBLE`, Protocol v2 CLOSED**.

### 22.1 Fact
- development candidate breakout / development status PASS / benchmark eligibility FAIL / temporal holdout eligibility FALSE / statistical power UNKNOWN / Protocol v2 final state CLOSED / NOT_HOLDOUT_ELIGIBLE. 종결 기록 해시 `c03aa605358656e9d4362341f3ec6a4978277cb866822165fd7f5cfc91aeaf4a`.
- 2026 holdout 승인 경로 비활성화: 종결 마커가 있으면 `holdout_unlocked()` 항상 False, 평가기는 자격 없는 후보·종결 프로토콜 후보를 거부, 승인의 인정 필드 제거 — 사용자 승인으로 frozen eligibility를 우회할 수 없다.
- Official Development Run #1·검증 재실행·trial id·trial series·후보 artifact·addendum은 historical development evidence로 보존(해시 검증). 성과 수치·임계값·grid·벤치마크·시나리오·fold 불변.
- 2026 값, 기존 일봉 Lockbox 접근 없음. 승인 마커·holdout 디렉터리 없음.

### 22.2 코드 변경
- 추가: `protocol_v2_closure.py`, `tests/test_protocol_v2_closure.py`(8개), 종결 기록 파일, 변이 6종(구 'acknowledgement 불필요' 변이는 우회 경로 삭제로 대체).
- 수정: `realdata/intraday.py`(종결 마커), `research/holdout_eval.py`(`ProtocolClosed`, 자격 필수, 인정 필드 삭제, `closure_path`), `tests/test_holdout_eval.py`(평가기 infrastructure 시험을 '자격 있는 미래 후보 복사본 + 종결 없음'으로 갱신 — 약화 아님, 종결 시험이 별도로 보호).

### 22.3 검증
| 항목 | 결과 |
|---|---|
| `python -m pytest -q` 3.11 / 3.13 | **500 passed** / **500 passed** |
| 변이 | **159/159 CAUGHT**, 소스 바이트 단위 원복 |
| compileall / pyflakes / secret scan | OK / clean / 검출 없음 |

### 22.4 남은 UNKNOWN
통계적 검정력, 다중검정 영향, 비용(과거 수수료·spread·slippage), 임계값의 적절성. 새로운 연구는 Protocol v3로만 가능하다. 수익성·알파·견고성은 주장하지 않는다.

---

## 23. QAT v1 Final Completion (통합 실행, 2026-10-07)

목표: 남은 **구현 가능** 범위를 한 번에 완료하고 최종 회귀를 한 번만 수행한다. 완료의 정의는 **소프트웨어 / Paper / 연구 인프라**에 한정된다 — Live·수익성·Alpha와 무관.

### 23.1 상태 구분 (섞지 않는다)
| 항목 | 상태 |
|---|---|
| 소프트웨어 / Paper / 연구 인프라 | **COMPLETE** (구현 가능한 gap 전부 처리·시험) |
| Live trading | **BLOCKED** (구조적으로 항상 BLOCKED, UI·설정 경로 없음) |
| 실제 Broker adapter | NOT_CONFIGURED / BLOCKED (외부 의존) |
| 수익성 | **UNKNOWN** |
| Alpha | **NOT PROVEN** |
| Protocol v2 | **CLOSED / NOT_HOLDOUT_ELIGIBLE** (개발 결과만; 재개·성능 개선 시도 없음) |
| 2026 holdout | **UNTOUCHED** (값 미열람, 승인 마커 미생성) |
| 일봉 Lockbox | **UNTOUCHED** (`results/lockbox_registry.json` 없음) |
| Paper 졸업 | **NOT_READY** (평가기 구현; 기준 수치는 잠정 공학 수치) |

### 23.2 Gap 처리
상세: `artifacts/verification/final_completion/gap_inventory.json`(ID·이전 상태·관련 코드/문서·구현 가능 여부·외부 의존·처리 여부). 요약: 구현 가능 21건 처리(R-08은 규칙 구현 + 호가 데이터 의존으로 UNKNOWN, Broker 경계는 계약+Fake까지, Paper 졸업은 잠정 수치) / 외부·미결 8건은 BLOCKED·UNKNOWN으로 기록.

### 23.3 구현 요약
- `config/ops.yaml` 신설(settings.yaml은 변경하지 않음: 해시가 protocol-v1 증거에 묶여 있음).
- `src/qat/ops/`: `atomic`(원자적 쓰기·손상 감지), `config`, `snapshot`(신선도·stale 정책), `market_rules`(R-07/08/09, MI-05/06), `audit_store`(해시체인 JSONL + 파이프라인 sink), `strategy_health`, `recovery`(복구 상태·재시작 평가·긴급 청산), `startup`, `graduation`(Paper 졸업·Live 준비도), `health`.
- `src/qat/brokerage/`: `contract`, `fake`, `boundary`(SDK·자격 증명 없음).
- `core/recovery.py`(권한 토큰 등록부), `core/orchestrator.py`(`submit_recovery_order`, `_place_order` 분리), `risk/gate.py`(snapshot·시장 규칙), `integrity/engine.py`(MI-05/06), `portfolio/ledger.py`(snapshot 메타), `app.py`(감사 sink, 어댑터 경유 취소/정산), `research/store.py`(원자적 쓰기, 손상 registry 오류).
- `ui/service.py`·`server.py`·`__main__.py`·`static/*`: 운영·감사·복구 화면, 신규 API, `ack-recovery`, 원자적 안전 잠금(손상 → 격리 + `STATE_CORRUPT`), 시작 점검 게이트.

### 23.4 시험
- 기존 500개 유지(삭제·약화 없음) + 신규 169개(`test_brokerage` 21, `test_ops_core` 68, `test_ops_service` 80) = **669**.
- **사양 변경에 따른 단언 3곳 수정**(약화가 아님): Strategy Health와 R-07/R-08/R-09, MI-05/MI-06이 구현되었으므로 "NOT_IMPLEMENTED / implemented False" 단언을 "구현됨 + NOT_CONFIGURED(임계값 null) + Strategy Health 상태값"으로 바꿨다(`tests/test_ui.py` 2곳, `tests/test_audit_batch2.py` 1곳). `tests/test_research.py`의 연구 manifest `strategy_health: NOT_IMPLEMENTED` 단언은 연구 실행에는 운영 상태가 없으므로 그대로다.
- E2E Paper 시나리오·장애 주입·보안 점검 매핑: `final_completion/e2e_scenarios.json`.
- 변이: 기존 159 + 신규 57(stale snapshot 우회, 감사 영속 우회, 복구 노출 증가, 무결성 점검 없는 flatten, UNKNOWN 주문 성공 처리, 졸업 false PASS, Live 해제 우회 등) = **216/216 CAUGHT**, 감사 소스 바이트 단위 원복. 첫 실행에서 졸업 G2/G3 변이 1건이 SURVIVED → 각 게이트를 단독으로 검증하는 사례를 추가해 CAUGHT.

### 23.5 작업 중 발견·수정한 결함
1. 복구 권한이 callable 검증이라 위조 가능 → 128비트 토큰 등록부로 교체(시험+변이).
2. `refuse()`의 감사 payload 키가 `_log(stage)` 인자와 충돌(거절 경로 TypeError) → 키 이름 변경.
3. `PaperStack.cancel_order/settle`이 Paper broker만 조회 → 주문을 가진 broker를 조회(Broker 경계 필요).
4. 확인되지 않은 취소가 `SettlementService.cancel`을 통해 예약을 풀 수 있었음 → 경계가 예외를 던져 예약 유지.
5. 도구 사고로 `ui/__main__.py` 문자열 리터럴이 깨짐 → 첫 사용 전에 수정.
6. 내비 링크의 접근성 이름 누락 → `aria-label` 추가.

### 23.6 최종 회귀 (1회, 변경 동결 후)
| 항목 | 결과 |
|---|---|
| `pytest` Python 3.11 / 3.13 | **669 passed** / **669 passed** |
| `compileall` / `pyflakes` | OK / clean |
| secret scan | 1399 파일, 유출 **없음** |
| 변이 전체 | **216/216 CAUGHT**, 원복 YES |
| 실서버 UI 스모크 (375px / 1280px / 625px) | 가로 넘침 0, 콘솔 오류 0, 이름 없는 컨트롤 0, UI == API, Live BLOCKED·감사·복구·상태 표시, 긴급 청산(UI 폼)·Kill Switch·강제 종료 후 재시작 평가(MANUAL_INTERVENTION_REQUIRED) 확인(임시 상태 디렉터리, 합성 fixture) |
| 2026 값 / Lockbox / settings.yaml / protocol v2 산출물 | 접근·변경 없음 |

증거: `artifacts/verification/final_completion/`(gap_inventory, completion_matrix, regression, ui_smoke, e2e_scenarios, 로그).

### 23.7 남은 외부 의존 / UNKNOWN
실제 Broker·자격 증명·SDK, Live와 Live 취소/체결 경합 대조, 호가 데이터(R-08)·실시간 Market Recorder·현실적 Paper Broker L2/L3, Shadow, 휴장일 캘린더, 소유자 결정(첫 Broker·vendor·실제 Risk 한도·자본·universe·ML·초기 Live 자본·졸업 기준), 실제 Net Alpha·통계적 검정력·보정된 비용. 새로운 연구는 Protocol v3로만 가능하며 이번 작업의 범위가 아니다.

### 23.8 이어받기 정보
운영 절차는 `docs/QAT_운영_런북.md`, 설계는 통합 문서 §19. 상태 파일은 단일 프로세스 전용. 이 작업 이후 새 Batch를 시작하지 않았다.
