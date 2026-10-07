# QAT — Quant Automated Trading

QAT는 한국주식·미국주식·암호화폐를 대상으로 하는 개인용 퀀트 자동매매 연구 프로젝트다. 현재 저장소는 **Core v0.1 + Batch #2 (Historical Data · Backtest · Walk-Forward · Paper 재생 UI) + 실제 데이터 연구 기반 + QAT v1 최종 완료(운영·감사·복구)** 단계이며, 실거래는 의도적으로 차단되어 있다.

## 현재 상태 (2026-10-07)

| 항목 | 상태 |
|---|---|
| Core 거래 인프라 | 구현·검증 완료 (Batch #2.0 결함 수정 포함) |
| Historical Data Engine (로컬 CSV, Parquet은 pyarrow 설치 시) | 구현 |
| Backtest / Metrics / Cost Stress | 구현 (기존 Core 파이프라인 경유) |
| Walk-Forward / Lockbox / Manifest / 결과 저장 | 구현 |
| 로컬 UI (개요·포트폴리오·연구·주문·**운영·감사·복구**·리스크·설정) | 구현 — Paper는 **과거 데이터 재생** 방식 |
| **QAT v1 최종 완료** | **`QAT_V1_COMPLETE`** — 소프트웨어 / Paper / 연구 인프라에 한정. Stale snapshot 정책, 영속 해시체인 감사 로그, Strategy Health, R-07/08/09·MI-05/06, 운영자 전용 긴급 청산·복구 상태 기계·재시작 평가, Broker 어댑터 경계(계약 + Fake, SDK 없음), Paper 졸업/Live 준비도 평가기, 운영 상태 요약, 시작 점검, 원자적 상태 저장. **Live BLOCKED · 수익성 UNKNOWN · Alpha NOT PROVEN · 2026 holdout UNTOUCHED · 일봉 Lockbox UNTOUCHED.** 상세는 `artifacts/verification/final_completion/`, 설계 §19, 런북 |
| 자동화 테스트 | **669 passed** (Python 3.11 / 3.13 각각) — 기존 500(아래 구성) + QAT v1 최종 완료 169(Broker 경계 21·운영 코어 68·운영 서비스/E2E/장애 주입/보안 80) / 기존 500 = 기존 315 + 실제 데이터 기반(Batch #3A, #3A.2) 단위 56·통합 23·변이 harness 8 + Walk-Forward 프로토콜(Batch #3B) 15 + 연구 타당성 기반(Batch #3C) 11 + BTCUSDT 4h·비용 v2·trial series(Batch #3D) 25 + Protocol v2 사전등록(Batch #3E) 16 + v2 개발 실행·holdout 평가기(통합 작업) 23 + Protocol v2 종결(closure) 8 |
| 실제 시장 데이터 수집·검증 (Batch #3A) | 수행 — 원본 보존·정규화·검증·교차검증·결정론 identity. KR 공식 소스(data.go.kr)로 삼성전자 2020-01-02~2025-12-30 검증 PASS (**OD-04 CLOSED WITH SCOPE** — 검증 범위 2020-01-02~2025-12-30, 2020년 이전은 UNKNOWN이며 Yahoo/Naver로 메우지 않는다. 범위 밖 기간 요청은 명시적으로 거부) |
| Protocol v2 Official Development Run #1 | 실행·동결 완료(development evidence). 후보 상태: breakout **PASS**, ma_trend UNSTABLE, mean_reversion INSUFFICIENT_ACTIVITY → `DEVELOPMENT_SELECTED_CANDIDATE`(breakout, 후보 해시 `d038da459ad78a4a…`). **그러나** 최악 시나리오 stitched +49.0%는 정렬 passive +305.7%에 못 미쳐 frozen 규칙상 holdout 자격(holdout_eligible)은 False. 통계적 검정력 UNKNOWN. 2026 holdout은 열지 않음 |
| **Protocol v2 최종 상태** | **`CLOSED / NOT_HOLDOUT_ELIGIBLE`** — `DEVELOPMENT_SELECTED_CANDIDATE — NOT_HOLDOUT_ELIGIBLE`. development candidate breakout, development status PASS, **benchmark eligibility FAIL**, temporal holdout eligibility **FALSE**, statistical power UNKNOWN. 2026 holdout 승인 경로는 Protocol v2에서 비활성화(사용자 승인으로 frozen eligibility 우회 불가). 결과·trial series·candidate는 historical development evidence로 보존 |
| Protocol v2 사전등록·freeze (Batch #3E) | **`PROTOCOL_V2_FROZEN`** (해시 `c1dad1503b43e58a…`). 전체 4h 부모는 `VALIDATION_FAIL_INCOMPLETE` 그대로, 개발 데이터는 결정적 규칙으로 도출한 연속 구간 2021-09-29T08:00~2025-12-31T20:00(9,328행, 4 fold)만 사용(`ADMITTED_CONTINUOUS_SUBPERIOD`). 새 시간적 holdout 2026-01-01~2026-12-31은 값을 읽지 않았다. 전략 성과 실행 없음 |
| BTCUSDT 4h·비용 v2 기반 (Batch #3D) | Binance 공식 아카이브 4h 17,516행(2018-01-01~2025-12-31)·checksum 96/96 일치·결정론 identity 확인. **엄격 검증 `FAIL`**(누락 4h 봉 16개·짧은 봉 18개 — 정직하게 기록, 보간 없음). 현재 수수료만 VERIFIED_CURRENT, 과거 수수료·스프레드·슬리피지는 HISTORICAL_UNKNOWN. 판정 `RESEARCH_STOP_RECOMMENDED`(엄격 완전성 게이트 때문; 사용자 gap 정책 수락 시 `PROTOCOL_V2_FREEZE_READY`로 전환 가능). 2026 값·Lockbox 접근 없음, 전략 실행 없음 |
| 연구 타당성 기반 (Batch #3C) | 고유 trial 1,343개 등록, 비용 증거 분류 완료(VERIFIED 0), 다중검정 방법은 **미구현·UNKNOWN**, Protocol v2 판정 `PROTOCOL_V2_RECOMMENDED`(BTC/USDT 한정·조건부). 전략 실행·Lockbox 접근 없음 |
| 실제 시장 데이터 Walk-Forward (Batch #3B) | 동결 프로토콜 `wf-protocol-1`로 BTC/USDT·AAPL·005930 × 기존 전략 3종 개발/OOS 구간 1회 수행. **Lockbox 판정: `NOT_READY_FOR_LOCKBOX`** (9개 후보 모두 `INSUFFICIENT_ACTIVITY`). 수익성은 입증되지 않았다. Lockbox 미사용 |
| 실제 Net Alpha(비용 후 초과수익) | **UNKNOWN** |
| Paper 졸업 | **NOT_READY** — 평가기 구현(`GRADUATED/NOT_READY/BLOCKED/UNKNOWN`), Protocol v2 `NOT_HOLDOUT_ELIGIBLE`가 정직하게 반영됨. 기준 수치는 소유자 미정의 → 잠정 공학 수치 |
| Live Ready | **아니오** — Live Trading **BLOCKED** (평가기도 항상 BLOCKED, UI·설정 경로 없음) |

테스트 PASS와 UI 완성은 수익성·법적 적합성·Live 안전성을 뜻하지 않는다.

## 절대 원칙

1. 전략/AI는 Broker를 직접 호출하지 않는다. 거래 진입점은 `submit_trade_proposal()`뿐이다. Backtest와 UI도 같은 경로를 쓴다.
2. Risk / Compliance / Market Integrity는 우회할 수 없는 Hard Gate다. 모든 Gate가 PASS일 때만 진행한다(BLOCK > UNKNOWN > PASS).
3. 안전 판단이 불명확하면 Fail-Closed한다.
4. Gross가 아니라 거래비용을 반영한 Net PnL을 평가한다.
5. `Historical → OOS/Walk-Forward → Paper → Shadow → Small Live` 순서를 지킨다.
6. 실시간 손실만으로 모델을 자동 교체하지 않는다.

## 설치 (깨끗한 환경)

권장 Python은 3.11 (3.13에서도 검증). 외부 의존성은 `pytest`, `PyYAML`뿐이다.

```bash
py -3.11 -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
python -m pip install -r requirements.txt
```

```bash
python -m pip install -e .
```

`pip install -e .`는 `python -m qat.ui` / `python -m qat.research` 실행용이다(테스트는 `pyproject.toml`의 `pythonpath = ["src"]`로 설치 없이도 동작).

## 검증

```bash
python -m pytest -q
```

기대 결과: `669 passed`. 추가 정적 검사(선택): `python -m compileall -q src tests tools`, `pip install pyflakes` 후 `python -m pyflakes src tests tools`.

변이 검사(소스를 임시 변경 후 **바이트 단위로 원복**하며 원복 여부를 검증, 기대: 216종 전부 CAUGHT = 기존 159 + QAT v1 최종 완료 57; 실행 중에는 `src`·`tests`·`config`·`tools`를 수정하지 않는다):

```bash
python artifacts/verification/audit_batch2/mutate_leakage_and_defects.py
```

## UI 실행

```bash
python -m qat.ui serve --port 8765
```

브라우저에서 `http://127.0.0.1:8765` 를 연다. 서버는 기본적으로 127.0.0.1에만 바인딩된다.

- **개요**: 실행 모드, 총 평가액, 기간 Net 수익률, 실현/미실현손익, drawdown, 주요 차단 사유, 재생 제어
- **포트폴리오**: 통화별 현금/예약/가용, 포지션·평균원가·평가가격·노출·손익. FX가 없으면 합산값을 만들지 않는다
- **연구**: 데이터 선택·검증 결과, 전략·기간·비용 설정, Backtest / Walk-Forward / Cost Stress 실행, 저장된 실행 재조회·비교
- **주문·체결**: 주문 제안(서버 측 Gate 판정 타임라인), 상태 이력, 부분체결·취소·예약, 거절 사유, 감사 기록
- **리스크·준법**: Live BLOCKED, Kill Switch, 안전 잠금, Reservation Breach, 대조 상태, 데이터·FX 누락, 규칙별 구현/미설정/미구현
- **설정·실험**: placeholder 표시, 설정 파일 해시, 비용·위험 설정, 코드 식별 정보, UNKNOWN 목록
- **운영**: 구성요소별 상태 요약(Data·Ledger·Reconciliation·Risk·Compliance·Integrity·Strategy Health·Audit·Broker·Startup·Recovery, 참고: Research·Paper·Live), 시작 점검, Paper 졸업/Live 준비도, Snapshot 정책
- **운영 → 감사 로그**: 영속 해시체인 감사 로그(읽기 전용, 필터·페이지네이션, 무결성 검증 표시)
- **운영 → 복구**: 복구 상태, 재시작 평가, 운영자 전용 긴급 청산(운영자·사유·확인 문구 `FLATTEN`)

Paper 세션은 실시간 시세가 아니라 **로컬 데이터셋 재생**이다. 기준가격은 현재 재생 봉의 종가이며, 승인된 주문은 "다음 봉" 진행 시 다음 봉 시가에 체결을 시도한다. 클라이언트는 가격·승인 상태를 지정할 수 없다.

### 안전 잠금 (Kill Switch / Reservation Breach)

UI에서 Kill Switch를 켜거나 예약 초과가 발생하면 `state/safety_latches.json`에 잠금이 원자적으로 기록된다(손상된 파일은 격리 후 `STATE_CORRUPT` 잠금으로 대체 — 조용한 초기화 없음). 잠금이 있는 동안에는 새로고침·새 세션·서버 재시작 후에도 모든 신규 주문이 차단된다. **UI에서는 해제할 수 없다.** 오프라인 검토 후 다음 명령으로만 해제한다.

```bash
python -m qat.ui latches
```

```bash
python -m qat.ui clear-latch --id KILL_SWITCH-1 --approver "이름" --note "검토 내용"
```

복구 상태(`RECOVERY_REQUIRED` / `FLATTEN_SUBMITTED`)와 재시작 평가의 해제도 오프라인 운영자 작업이다(감사 기록됨).

```bash
python -m qat.ui ack-recovery --approver "이름" --note "장부 확인 내용"
```

운영 설정은 `config/ops.yaml`이며(`null` = NOT_CONFIGURED), 서버는 **단일 프로세스 전용**이다(같은 `state/`를 쓰는 두 프로세스는 지원하지 않음). 절차 전반은 [운영 런북](docs/QAT_운영_런북.md).

## 연구 CLI

```bash
python -m qat.research validate-data data/fixtures/SYN_KR1_1d.csv
```

```bash
python -m qat.research backtest --data data/fixtures/SYN_KR1_1d.csv --strategy ma_trend -p fast=10 -p slow=30
```

```bash
python -m qat.research walkforward --data data/fixtures/SYN_KR1_1d.csv --strategy ma_trend --train 250 --test 60 --lockbox 100
```

```bash
python -m qat.research lockbox --data data/fixtures/SYN_KR1_1d.csv --run <walkforward_run_id>
```

```bash
python -m qat.research stress --data data/fixtures/SYN_KR1_1d.csv --strategy breakout --mult 1 2 3
```

```bash
python -m qat.research list
```

전략: `ma_trend`(이동평균 추세), `breakout`(돌파), `mean_reversion`(평균회귀) — 연구 파이프라인 검증용 기준전략이며 수익 보장 전략이 아니다. 기대수익은 기본적으로 **과거 완료 사례의 평균(인과적)** 이고, 근거가 부족하면 0으로 두어 Net Alpha Gate가 판단한다. `-p alpha_mode='"fixture"' -p fixture_expected_return=0.01`은 검증용 고정값이며 결과에 FIXTURE로 표시된다.

결과는 `results/runs/<run_id>/`(`manifest.json`, `metrics.json`, `result.json`, `equity.csv`, `fills.csv`, `audit.jsonl`)에 저장되고 `list`/`show` 또는 UI에서 다시 연다. 저장 위치는 `QAT_RESULTS_DIR` 환경변수로 바꿀 수 있다.

## Walk-Forward 연구 프로토콜 (Batch #3B)

```bash
python -m qat.research.protocol show      # 동결된 프로토콜 정의(해시 포함)
python -m qat.research.protocol freeze    # artifacts/verification/walkforward_3b/protocol.json 기록(멱등)
python -m qat.research.protocol run       # 프로토콜 버전당 1회만 실행(재실행은 추가 OOS 열람이라 거부)
```

결과는 `artifacts/verification/walkforward_3b/summary.json`, 각 실행은 `results/runs/`에 저장된다. Lockbox는 열지 않는다(`python -m qat.research.protocol`은 Lockbox 평가·registry를 사용하지 않는다).

## 연구 타당성 기반 (Batch #3C)

```bash
python -m qat.research.trials reconstruct   # 저장된 Walk-Forward run에서 trial registry 재구성(멱등) 후 요약
python -m qat.research.validity             # artifacts/verification/validity_3c/ 증거 재생성(전략 실행·Lockbox 접근 없음)
```

Batch #3B 결과는 관찰된 개발 증거로 고정한다(재해석·재실행 금지). 모든 Walk-Forward run의 평가 후보는 결정론적 `trial_id`로 `results/trial_registry.json`에 등록된다. 실제 거래비용은 어느 항목도 VERIFIED가 아니다(PLACEHOLDER/UNKNOWN). 다중검정 보정은 구현하지 않았다.

## Protocol v2 개발 실행·holdout 평가기

```bash
python -m qat.research.protocol_v2_run verify-addendum   # 성과 실행 이전에 고정된 후보 선정 addendum 검증
python -m qat.research.protocol_v2_run verify-candidate  # 개발 선정 후보 identity 검증
python -m qat.research.holdout_eval status               # 2026 월 가용성(파일명만, 값 읽지 않음)
```

`protocol_v2_run official`은 한 번만 실행되며 재실행은 거부된다(이미 실행됨). `holdout_eval evaluate`는 2026 전체 연도·사용자 승인 마커·후보/프로토콜/addendum 해시 일치가 없으면 값을 읽기 전에 중단한다.

## Protocol v2 (Batch #3E)

```bash
python -m qat.realdata.intraday view      # 부모 4h에서 연속 research view 도출(시각·무결성만 사용)
python -m qat.research.protocol_v2 freeze # artifacts/verification/protocol_v2/ 에 사양·해시·freeze 마커 기록(재실행 거부)
python -m qat.research.protocol_v2 verify # 동결 이후 수정(파일·코드 상수) 탐지
python -m qat.research.protocol_v2 decide # 최종 판정(전략 실행 없음)
```

freeze 마커는 **기술적 권한일 뿐**이다. 2026 값을 읽으려면 별도의 holdout 평가 승인 마커도 필요하며 이는 사용자가 승인한 별도 one-shot Batch만 만들 수 있다.

## BTCUSDT 4h 기반 (Batch #3D)

```bash
python -m qat.realdata.intraday acquire   # 2018-01~2025-12 월별 4h klines + 공식 CHECKSUM (2026 요청은 코드가 거부)
python -m qat.realdata.intraday build     # 정규화·검증·identity 저장 (data/processed/real_4h/, 연구 admission 없음)
python -m qat.research.intraday_evidence  # artifacts/verification/intraday_3d/ 증거 재생성 (전략 실행 없음)
```

2026 데이터는 Protocol v2 freeze 마커가 생기기 전에는 가져오거나 읽을 수 없다(가드). 4h 데이터셋은 `real_data` identity가 없어 연구에 admission되지 않는다(fail-closed).

## 데이터 형식

CSV 헤더(대소문자 무관): `timestamp,open,high,low,close,volume`. 같은 폴더에 `<파일명>.meta.json` 사이드카가 필요하다.

```json
{"market": "KR", "symbol": "005930", "timeframe": "1d", "timezone": "+09:00",
 "timestamp_label": "open", "source": "LOCAL_FILE", "synthetic": false}
```

- `timestamp`: ISO-8601 (`2024-01-02`, `2024-01-02T09:00:00`, 오프셋 포함 가능). 오프셋이 없으면 `timezone`으로 해석한다.
- `timezone`: `UTC` 또는 고정 오프셋(`+09:00`). IANA 이름은 tz 데이터베이스가 있을 때만(Windows 기본 환경에는 없음 → BLOCKED 오류).
- `timestamp_label`: 봉을 시가시각(`open`)으로 표기했는지 종가시각(`close`)으로 표기했는지. 내부에서는 모두 UTC 시가시각으로 정규화한다.
- Crypto 종목은 `BASE/QUOTE` (예: `BTC/KRW`).
- 검증: 정렬·중복·결측·비유한 값·가격≤0·OHLC 관계·음수/0 거래량·공백(주말/미설명 평일/세션 경계). **보간·행 삭제를 하지 않으며**, ERROR가 하나라도 있으면 `FAIL`로 연구·재생이 차단된다. 거래소 휴장일 캘린더는 없다(미설명 평일 공백은 WARN).
- 실제 데이터는 `data/raw/`에 둔다(Git 제외). 원본은 읽기만 하며 SHA-256과 `data_version`이 기록된다.
- **실제 시장 데이터(Batch #3A)**: `python -m qat.realdata acquire crypto|crypto-crosscheck|us|kr-crosscheck` (공개 소스), `acquire kr-official`(공공데이터포털 serviceKey 필요), `verify-raw`, `evidence`, `key-status`. 원본(`data/raw/<provider>/...`)은 불변이며 `*.provenance.json` 사이드카(제공자·시장·종목·조회 시각·요청/응답 범위·SHA-256·크기·형식·endpoint(키는 `<REDACTED>`)·타임존·`synthetic=false`)가 붙는다. 정규화 결과는 `data/processed/real/`에, 증거는 `artifacts/verification/real_data/`에 저장된다. **serviceKey는 저장소·로그·문서·증거 어디에도 저장하지 않으며** 키 파일 경로는 `--key-file`로 지정한다(기본 `~/Desktop/key.txt`; 경로일 뿐 키 값이 아니다). data.go.kr 데이터는 공공누리 4유형(제3자 재배포 금지)이므로 `data/raw/`는 Git에 올리지 않는다.
- 실제 제공자(`yahoo-chart`, `binance-vision`, `data.go.kr` 등) 소스의 데이터셋은 identity(`extra.real_data`)가 없거나, 원본 재검증·재정규화·재검증 중 하나라도 실패하면 **연구·재생에서 거부**된다(fail-closed, UI/API는 HTTP 409).
- `data/fixtures/`의 `SYN_*` 파일은 결정론적 **합성 데이터**다(`python -m qat.data.synthetic --out data/fixtures`로 재생성). 시장 성과로 해석하지 않는다.

## 정책 (Control Tower 결정, Batch #2.1)

- **OD-01 노출 축소 주문**: 보유 포지션을 줄이는 SELL(가용 수량 이내)은 Net Alpha 임계값만 면제된다. 서버가 Ledger로 판정하며 클라이언트가 주장할 수 없다. 포지션 없는 SELL, BUY, 보유량 초과 SELL은 면제되지 않는다. Validation·비용·Risk·Compliance·Integrity·Settlement·Kill Switch는 그대로 적용된다.
- **OD-02 종목 universe**: 허용 목록이 없으면 자동 허용하지 않는다(UNKNOWN). UI/Manual/Paper는 `config/settings.yaml`의 `paper_universe`만 사용하고, Research/Backtest는 검증된 dataset의 declared symbol을 **해당 실행에만** 사용한다(Manifest `universe` 기록). 연구가 Paper 허용 목록을 넓히지 않는다.
- **OD-03 예약 버퍼**: 기본 2%는 provisional policy value(잠정 정책값)이며 경험적으로 검증되지 않았다. 자동 튜닝하지 않는다.
- **Risk-reducing exit hierarchy (CLOSED, Batch #2.2)**: 서버가 Ledger로 "노출 축소 SELL"(Long 보유 + SELL + 가용 수량 이내)을 판정하며 클라이언트 주장은 무시된다. **Kill Switch는 예외 없이 BLOCK**(자동 청산을 뜻하지 않음; 긴급 강제 청산·복구는 향후 별도 정책). Daily Loss·Drawdown·절대 노출 한도는 노출 축소 SELL을 막지 않고 BUY·비축소 SELL은 계속 막는다(Drawdown의 UNKNOWN 의미는 유지). Reservation Breach는 plain overrun + Ledger 일관성 + 대조 불일치 없음 + 평가 가능일 때만 축소 SELL을 허용한다. 주문금액 한도는 축소 주문도 우회할 수 없고(자동 분할 없음), Compliance·Integrity·Settlement·실행 비용은 그대로다. 설계 §7.1.

## 설정

`config/settings.yaml`의 비용·세율·FX·Risk 수치는 모두 **placeholder**다. 연구 실행은 `load_research_settings()`로 설정을 엄격히 읽는다(파일·비용 누락 시 오류; 무비용 정상 실행으로 대체하지 않음). `project.mode: live`나 `execution.live_enabled: true`로 Live가 열리지 않는다. `paper_universe`는 UI Manual/Paper 허용 종목 목록이다(저장소의 SYN* 3개는 오프라인 검증용 fixture이며 투자 universe 결정이 아님).

## 폴더 구조

```text
├─ README.md
├─ docs/                      # 통합 설계·운영 / 검증 이력
├─ config/settings.yaml       # placeholder 설정 (연구 증거에 해시 결합 — 수정 금지)
├─ config/ops.yaml            # 운영 설정(snapshot 정책, R-07~MI-06 임계값, Strategy Health, 졸업 기준)
├─ data/fixtures/             # 합성 fixture (+ .meta.json)
├─ data/raw/, data/processed/ # 로컬 실제 데이터 (Git 제외)
├─ results/                   # 연구 결과 (Git 제외)
├─ state/                     # 안전 잠금·복구/안전 상태·감사 로그 (Git 제외)
├─ artifacts/verification/    # 재현 스크립트·출력, UI 확인 캡처
├─ src/qat/
│  ├─ core/ cost/ risk/ compliance/ integrity/ execution/ portfolio/ paper/ live/ audit/
│  ├─ data/        # Historical Data Engine (loader, validation, synthetic)
│  ├─ realdata/    # 실제 데이터 기반: provenance, sources, fetch, normalize, validate, calendars, adjustment, crosscheck, admission, evidence
│  ├─ research/    # strategies, backtest, metrics, walkforward, manifest, store, CLI
│  ├─ ops/         # 운영: atomic, config, snapshot, market_rules, audit_store, strategy_health, recovery, startup, graduation, health
│  ├─ brokerage/   # Broker 어댑터 계약, Fake 어댑터, 경계(SDK·자격 증명 없음)
│  └─ ui/          # service, server, static SPA, CLI
├─ tools/                     # 비밀정보 검색, 캘린더 snapshot 생성, 변이 harness
└─ tests/
```

## 테스트 파일

- `tests/test_phase0.py`, `tests/test_invariants.py`, `tests/test_fix_batch_1_1.py`, `tests/test_verify_1_1_v.py`: 기존 Core 57개 (변경 없음)
- `tests/test_fix_batch_2_0.py`: 인계 검토 후보(가~사) 재현·회귀
- `tests/test_data_engine.py`: 데이터 로딩·검증·fixture 결정론
- `tests/test_research.py`: 수작업 계산 거래, 미래 데이터 누수, Fold 경계, Train/OOS 오염, 결정론, Manifest, 저장·재조회, 거래 0건, Breach 중단, Gate 경유
- `tests/test_ui.py`: UI 서비스·HTTP (원장 일치, 가격·승인 지정 불가, 중복·동시 요청, 안전 잠금 지속, 보안 헤더)
- `tests/test_exit_policy_batch2_2.py`: 위험 축소 청산 계층(Kill Switch·Daily Loss·Drawdown·절대 노출·Reservation Breach A/B/C·주문 한도·Compliance/Integrity/Settlement 유지·클라이언트 위조 불가)
- `tests/test_policy_batch2_1.py`: OD-01(면제 조건·flip 방지·위조 불가·다른 Gate 유지·189/619 재현 사례), OD-02(fail-closed·허용 목록·research 범위·누수 없음), OD-03(2% 유지·경계), Risk-reducing exit 현재 동작 고정
- `tests/test_protocol_v2_closure.py`: Protocol v2 종결 기록·보존 증거 변조 탐지, 사용자 승인의 frozen eligibility 우회 불가, 종결 프로토콜의 2026 잠금, 승인 마커 생성 경로 부재
- `tests/test_protocol_v2_run.py`: 후보 순위·분기 규칙, 시나리오 설정, 동결 절차(누수 없음·S0 제외·worst-case 선택·trial 등록·series 저장), 결정론 재실행, 사전 addendum, 실행 가드
- `tests/test_holdout_eval.py`: 합성 fixture로만 one-shot 2026 평가기(전체 연도·승인·후보 identity·exactly-once·불변 결과·실패 시 재시도 금지)
- `tests/test_protocol_v2.py`: 연속 구간 도출·부모 FAIL 유지·admission 의미, worst-case 비용 선택(S0 제외), 후보 규칙, fold 계획, 해시 동결·변조 탐지, 2026 가드, 성과 필드 부재
- `tests/test_intraday.py`: 4h 타임스탬프(ms/us)·연속성·checksum 변조·결정론 identity·2026 holdout 가드·비용 v2 분류·per-trial series·정렬 벤치마크·Lockbox 미접근
- `tests/test_validity.py`: trial 식별·등록·재구성, 비용 증거 분류 검증, 벤치마크 정렬 audit, 고정된 #3B 결과 불변, Lockbox 미접근
- `tests/test_protocol.py`: Walk-Forward 프로토콜(시장별 fold, common period, 분류 규칙, 동결·재실행 거부, Lockbox 미접근, 결정론, manifest 완결성, KR 2020-01-02 이전 거부)
- `tests/test_realdata_unit.py`: 실제 데이터 기반 단위 시험(원본 불변·변조 탐지, 파서, 정규화 결정론, 검증 V01~V18, 캘린더, 보정 의미, 교차검증, identity·admission 변조 거부)
- `tests/test_realdata_integration.py`: 보존된 실제 원본이 있을 때의 통합 시험(원본 재해시, 재정규화 재현, KR 공식 PASS, HTTP 409, 저장소 전체 credential 부재). 원본이 없으면 skip
- `tests/test_mutation_harness.py`: 변이 harness가 LF·CRLF·혼합 파일을 바이트 단위로 원복하는지
- `tests/test_audit_batch2.py`: 독립 감사 재현·회귀(D1~D10), 회계 불변식, 파이프라인 순서, 누수·오염 탐지기와 그 자체 검증(변이), Cost Stress 구성요소, 결과 불변성
- `tests/test_brokerage.py`: Broker 경계(멱등 제출, UNKNOWN은 종결 아님, 중복·불일치 체결, 취소 타임아웃, 재연결, stale/unknown-age 스냅샷)
- `tests/test_ops_core.py`: 신선도·stale snapshot 정책, R-07/08/09·MI-05/06, ops 설정 검증, 원자적 쓰기, 영속 감사(체인·변조·잘림·멱등·비밀 가림), Strategy Health
- `tests/test_ops_service.py`: 시작 점검, 손상 상태 처리, 재시작 평가, 긴급 청산·복구 상태 기계, 운영 상태 요약, 졸업/Live 준비도, 신규 HTTP 경로 보안, E2E·장애 주입

## 문서

- **[통합 설계·운영 문서](docs/QAT_통합_설계_운영.md)** — 설계, 정책, 확정 결정, 현재 구현 상태
- **[검증·변경 이력](docs/QAT_검증_이력.md)** — Batch별 재현·수정·테스트 이력, 이어받기 정보
- **[운영 런북](docs/QAT_운영_런북.md)** — 시작 점검, 재시작 평가, Kill Switch·긴급 청산·복구, 감사 로그, 준비도 평가(최소판)

문서와 코드가 충돌할 경우 실제 코드 + 자동화 테스트 결과를 우선 증거로 삼고 문서를 수정한다.
